#!/usr/bin/env python3
"""Fitness control: a module-level function body copied verbatim into a sibling
module, instead of being shared.

governance: enforces DEC-2

See DEC-2. `docs/ledger-findings.md` F-16 reached its third sighting when
`_send_questions` and `_resolve_model` (bodies logically identical, only local
variable names differing) each existed twice across `pipeline.py`/`calibrate.py`.
This is the mechanical version of that dislike: two module-level function
definitions under `src/typesafe_review/`, in two different files, whose bodies are
identical once cosmetic differences are normalised away -- the function's own name,
its docstring, its argument/return type annotations, and the *names* it binds
locally (parameters, assignment targets, comprehension variables, `except ... as`,
`with ... as`, walrus targets). Two functions that differ only in whether the loop
variable is called `question_id` or `qid` are the same function wearing a disguise.

What is deliberately **not** normalised, because collapsing it would blur two
functions that really do different things into a false match: global/free names --
anything read but never locally assigned, such as a call to `cast(...)` or an
attribute chain like `typesafe_constants.DEFAULT_MODEL_ENV` -- keep their literal
spelling. A function that calls a different global helper, or reads a different
constant, is not a duplicate; it only looks similar until you check what it
actually touches.

Scope, and why it stops where it stops: only module-level `def`s (not methods, not
functions nested inside another function) are compared. Two classes that each
implement a small `Protocol` method with the same short body (a common, correct
shape -- see DEC-1's own note on `Record`/`Replay`) would otherwise false-positive
on every interface with more than one implementation; excluding methods sidesteps
that without a heuristic that would misfire. A `MIN_NODES` floor (walked AST node
count of the normalised body) excludes trivial one-liners (`return None`, `pass`)
that coincide by chance and carry no real logic to share.
"""

from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / 'src' / 'typesafe_review'

# Below this many AST nodes in the normalised body, a match is more likely a
# coincidence (a one-line `pass`/`return None`/`return self.x` shape) than a real
# duplicate worth sharing. Chosen below the smallest real F-16 sighting (a one-line
# `os.environ.get(...)` body normalises to ~20 nodes) with headroom to spare.
MIN_NODES = 12

_FuncNode = ast.FunctionDef | ast.AsyncFunctionDef


def _locally_bound_names(func: _FuncNode) -> set[str]:
    """Every name this function binds itself: parameters, assignment targets,
    comprehension variables, `except ... as name`, `with ... as name`, walrus
    targets. Anything else is a free/global reference and keeps its real name."""
    names: set[str] = set()
    args = func.args
    for arglist in (args.posonlyargs, args.args, args.kwonlyargs):
        for arg in arglist:
            names.add(arg.arg)
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)

    for node in ast.walk(func):
        if node is func:
            continue
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.comprehension):
            for sub in ast.walk(node.target):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.withitem) and isinstance(node.optional_vars, ast.Name):
            names.add(node.optional_vars.id)
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


class _Canonicalise(ast.NodeTransformer):
    """Rewrite every locally-bound name to a position-stable placeholder (`_v0`,
    `_v1`, ...), assigned in the order each name is first encountered. Leaves free
    names, attribute access, and literals untouched."""

    def __init__(self, local_names: set[str]) -> None:
        self._local_names = local_names
        self._canonical: dict[str, str] = {}

    def _rename(self, name: str) -> str:
        if name not in self._canonical:
            self._canonical[name] = f'_v{len(self._canonical)}'
        return self._canonical[name]

    def visit_Name(self, node: ast.Name) -> ast.Name:
        if node.id in self._local_names:
            node.id = self._rename(node.id)
        return node

    def visit_arg(self, node: ast.arg) -> ast.arg:
        if node.arg in self._local_names:
            node.arg = self._rename(node.arg)
        node.annotation = None
        return node

    def generic_visit(self, node: ast.AST) -> ast.AST:
        if isinstance(node, ast.ExceptHandler) and node.name and node.name in self._local_names:
            node.name = self._rename(node.name)
        return super().generic_visit(node)


def _has_docstring(body: list[ast.stmt]) -> bool:
    return (
        bool(body)
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    )


def _normalised_signature(func: _FuncNode) -> tuple[str, int]:
    """`(structural dump, node count)` for `func` with its own name, decorators,
    return annotation, docstring, and locally-bound names all normalised away."""
    local_names = _locally_bound_names(func)
    clone = ast.parse(ast.unparse(func)).body[0]
    assert isinstance(clone, (ast.FunctionDef, ast.AsyncFunctionDef))
    clone.name = 'f'
    clone.decorator_list = []
    clone.returns = None
    body = clone.body[1:] if _has_docstring(clone.body) else clone.body
    clone.body = body
    _Canonicalise(local_names).visit(clone)
    node_count = sum(1 for _ in ast.walk(clone))
    return ast.dump(clone, annotate_fields=False), node_count


def _module_level_functions(tree: ast.Module) -> list[_FuncNode]:
    return [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _base_name(name: str) -> str:
    return name.removeprefix('_')


def find_duplicates() -> list[str]:
    if not SRC_DIR.is_dir():
        return []

    # signature -> list of (rel_path, lineno, func_name)
    by_signature: dict[str, list[tuple[str, int, str]]] = defaultdict(list)

    for path in sorted(SRC_DIR.rglob('*.py')):
        try:
            source = path.read_text()
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError):
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        for func in _module_level_functions(tree):
            signature, node_count = _normalised_signature(func)
            if node_count < MIN_NODES:
                continue
            by_signature[signature].append((rel, func.lineno, func.name))

    violations: list[str] = []
    for locations in by_signature.values():
        modules = {rel for rel, _lineno, _name in locations}
        if len(modules) < 2:
            continue
        names = {_base_name(name) for _, _, name in locations}
        shape = 'the same name' if len(names) == 1 else 'different names'
        where = ', '.join(f'{rel}:{lineno} ({name})' for rel, lineno, name in locations)
        violations.append(
            f'{where}: identical function body under {shape} in different modules (DEC-2). '
            f'Move it to one module and import it at the other call sites instead of copying it.'
        )
    return violations


def main() -> int:
    violations = find_duplicates()
    if violations:
        for v in violations:
            print(f'FAIL [DEC-2] {v}', file=sys.stderr)
        print(
            '    -> A helper duplicated across modules drifts silently once one copy is fixed and '
            'the other is not. Expose one and import it everywhere else.',
            file=sys.stderr,
        )
        return 1

    print('ok [DEC-2] no module-level helper body is duplicated across src/typesafe_review/ modules.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
