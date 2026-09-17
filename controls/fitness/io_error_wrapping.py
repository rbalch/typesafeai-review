#!/usr/bin/env python3
"""Fitness control: a module that declares its own error type does not let a raw
filesystem or subprocess call escape it unguarded.

governance: enforces DEC-1

See DEC-1. A module under `src/` that defines `class <X>Error(...)` or
`class <X>Failed(...)` is claiming "failures from this module surface as <X>Error
(or <X>Failed), never as something else." `open`, `subprocess.run`, and the
`pathlib.Path` I/O methods (`read_text`, `read_bytes`, `write_text`, `write_bytes`,
`mkdir`) are exactly the calls that raise `OSError` / `subprocess.SubprocessError`
on their own, bypassing that contract, if nothing catches them.

Scope, and why it stops where it stops: this control only looks at standalone
functions (module-level `def`s and closures nested inside them), not methods on a
class. A method reached through dynamic dispatch (a `Protocol` with several
implementations, for instance) can be made safe by the *caller* wrapping the call
through the interface, and a static, type-free AST pass cannot see that without
resolving which concrete class answers each call -- that is a real limit of what
this control can prove, not a loophole opened to make a particular file pass. See
DEC-1's Context for the module this shows up in.

A "guard" here means: the call is lexically inside a `try` somewhere between it and
its enclosing function. This control does not additionally require the `except`
clause to *raise* `<X>Error` -- some of this codebase's modules convert the failure
into a returned result instead (`checks.py`), which is just as safe and arguably
better. What DEC-1 forbids is the call having no `try` around it at all.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / 'src'

_PATH_IO_METHODS = {'read_text', 'read_bytes', 'write_text', 'write_bytes', 'mkdir'}


def _base_name(base: ast.expr) -> str | None:
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return None


def _defines_own_error(tree: ast.Module) -> str | None:
    """The name of the first top-level `<X>Error`/`<X>Failed` exception class this
    module declares, or None if it declares none."""
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not (node.name.endswith('Error') or node.name.endswith('Failed')):
            continue
        for base in node.bases:
            name = _base_name(base)
            if name and (name in ('Exception', 'BaseException') or name.endswith(('Error', 'Failed', 'Exception'))):
                return node.name
    return None


def _io_call_label(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name) and func.id == 'open':
        return 'open(...)'
    if isinstance(func, ast.Attribute):
        if func.attr == 'run' and isinstance(func.value, ast.Name) and func.value.id == 'subprocess':
            return 'subprocess.run(...)'
        if func.attr in _PATH_IO_METHODS:
            return f'.{func.attr}(...)'
    return None


def _parent_map(tree: ast.Module) -> dict[int, ast.AST]:
    """Map `id(child)` to its parent node, since plain `ast` nodes carry none."""
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


_FuncNode = ast.FunctionDef | ast.AsyncFunctionDef


def _enclosing_function(node: ast.AST, parents: dict[int, ast.AST]) -> _FuncNode | None:
    current: ast.AST | None = parents.get(id(node))
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = parents.get(id(current))
    return None


def _is_method(func_node: _FuncNode, parents: dict[int, ast.AST]) -> bool:
    return isinstance(parents.get(id(func_node)), ast.ClassDef)


def _guarded_by_try(node: ast.AST, stop_at: ast.AST | None, parents: dict[int, ast.AST]) -> bool:
    current: ast.AST | None = parents.get(id(node))
    while current is not None and current is not stop_at:
        if isinstance(current, ast.Try):
            return True
        current = parents.get(id(current))
    return False


def check_file(path: Path) -> list[str]:
    try:
        source = path.read_text()
    except OSError:
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    error_name = _defines_own_error(tree)
    if error_name is None:
        return []

    parents = _parent_map(tree)
    rel = path.relative_to(REPO_ROOT).as_posix()

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        label = _io_call_label(node)
        if label is None:
            continue

        func_node = _enclosing_function(node, parents)
        if func_node is not None and _is_method(func_node, parents):
            continue  # out of scope: dynamic dispatch, see module docstring

        if not _guarded_by_try(node, func_node, parents):
            where = f'`{func_node.name}`' if func_node is not None else 'module scope'
            message = (
                f'{rel}:{node.lineno}: {label} in {where} is not inside a try, so a raw '
                f"OSError/SubprocessError can escape past {rel}'s own {error_name} (DEC-1)"
            )
            violations.append(message)
    return violations


def main() -> int:
    if not SRC_DIR.is_dir():
        print(f'ok [DEC-1] no {SRC_DIR.relative_to(REPO_ROOT)} directory to scan')
        return 0

    violations: list[str] = []
    for path in sorted(SRC_DIR.rglob('*.py')):
        violations.extend(check_file(path))

    if violations:
        for v in violations:
            print(f'FAIL [DEC-1] {v}', file=sys.stderr)
        print(
            '    -> Wrap the call in a try/except that handles OSError (or the relevant '
            'subprocess exception) before it can propagate raw -- either raise the '
            "module's own error type or return a failure value, as the rest of the module does.",
            file=sys.stderr,
        )
        return 1

    print('ok [DEC-1] every raw I/O call in a module with its own error type is guarded by a try.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
