#!/usr/bin/env python3
"""The linchpin. Fail CI on any drift between decisions, controls, and the view.

Nine checks:

    Traceability  1. every live decision lists at least one control
                  2. every listed control path exists on disk
                  3. every `pragma: supported` control carries its pragma line
                  4. every pragma in the tree names an existing, accepted decision
                  5. every `pragma: external` control matches its recorded sha256
    Hygiene       6. build_views.py --check passes
                  7. no superseded or draft rule text appears under views/
                  8. ids are unique and every superseded_by resolves
    Teeth         9. the control suite actually runs and passes

Every failure names the decision, the file, and what to do about it. A check that
says only "FAILED" trains people to ignore it.

Two scoping decisions, both resolved the same way — **the ledger
enforces the present; history is a record, not a constraint**:

- Checks 1, 2, 3 and 5 apply to *live* decisions only (accepted and not superseded).
  Applying check 3 to superseded decisions would make a correct supersession
  impossible: retargeting a control's pragma from DEC-1 to DEC-3 necessarily leaves
  DEC-1 without its pragma, which is the desired end state, not a violation.
- Check 4 scans the whole tree but skips markdown. Markdown is prose, cannot be an
  executable control, and legitimately contains illustrative pragmas — `AGENTS.md`,
  `docs/governance-harness.md` and the `ledger-ops` skill all quote one.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import build_views as bv
from build_views import REPO_ROOT, Decision, DecisionError, relative

VIEWS_DIR = bv.VIEWS_DIR
PRAGMA_RE = re.compile(r'governance:\s*enforces\s+(?P<ids>DEC-\d+(?:\s*,\s*DEC-\d+)*)')
DEC_REF_RE = re.compile(r'DEC-\d+')

SKIP_DIRS = frozenset(
    {
        '.git',
        '.venv',
        '.ruff_cache',
        '.pytest_cache',
        '.mypy_cache',
        '__pycache__',
        'node_modules',
        'dist',
        'build',
    }
)
SKIP_SUFFIXES = frozenset({'.md'})

# Set while the control suite is running, so a test that shells out to this script
# cannot re-enter check 9 and recurse forever.
SUITE_GUARD_ENV = 'GOVERNANCE_SUITE_RUNNING'

# pytest's exit code for "nothing was collected", which needs its own diagnosis.
PYTEST_NO_TESTS_COLLECTED = 5


@dataclass(frozen=True)
class Failure:
    """One violation, reported with enough detail to act on without reading the source."""

    check: int
    decision: str | None
    file: str | None
    problem: str
    remedy: str

    def render(self) -> str:
        where = ' '.join(part for part in (self.decision, self.file) if part)
        head = f'FAIL [check {self.check}] {where}'.rstrip()
        return f'{head}\n    {self.problem}\n    -> {self.remedy}'


def live(decisions: list[Decision]) -> list[Decision]:
    return [decision for decision in decisions if decision.is_live]


def scannable_files() -> list[Path]:
    """Every file a pragma could meaningfully live in. Skips prose and vendored trees."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if path.suffix.lower() in SKIP_SUFFIXES:
                continue
            found.append(path)
    return found


def read_text(path: Path) -> str | None:
    """Return a file's text, or None if it is binary or unreadable."""
    try:
        return path.read_text(encoding='utf-8')
    except (UnicodeDecodeError, OSError):
        return None


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- Traceability -------------------------------------------------------------


def check_1_decisions_have_controls(decisions: list[Decision]) -> list[Failure]:
    """A live rule with no control has no teeth."""
    return [
        Failure(
            check=1,
            decision=decision.id,
            file=None,
            problem='accepted and not superseded, but lists no controls.',
            remedy=(
                'Add a control to the `controls:` frontmatter and write it, or add one with '
                '`enforcement: warn` and justify in the body why it cannot yet be automated.'
            ),
        )
        for decision in live(decisions)
        if not decision.controls
    ]


def check_2_control_paths_exist(decisions: list[Decision]) -> list[Failure]:
    failures = []
    for decision in live(decisions):
        for control in decision.controls:
            if (REPO_ROOT / control.path).exists():
                continue
            failures.append(
                Failure(
                    check=2,
                    decision=decision.id,
                    file=control.path,
                    problem='listed as a control but does not exist on disk.',
                    remedy=(
                        f'Write the control at that path, or correct the `path` in '
                        f'{decision.id}. A control deleted or moved without updating its '
                        f'decision is how a rule silently loses its teeth.'
                    ),
                )
            )
    return failures


def check_3_pragmas_present(decisions: list[Decision]) -> list[Failure]:
    """A control that lost its pragma is often the first sign of evasion."""
    failures = []
    for decision in live(decisions):
        for control in decision.controls:
            if control.pragma != 'supported':
                continue
            path = REPO_ROOT / control.path
            if not path.exists():
                continue  # already reported by check 2
            content = read_text(path)
            if content is None:
                failures.append(
                    Failure(
                        check=3,
                        decision=decision.id,
                        file=control.path,
                        problem='is not readable as text, so its pragma cannot be verified.',
                        remedy=f'Set `pragma: external` on this control in {decision.id} and record a `sha256`.',
                    )
                )
                continue
            if any(decision.id in DEC_REF_RE.findall(match.group('ids')) for match in PRAGMA_RE.finditer(content)):
                continue
            failures.append(
                Failure(
                    check=3,
                    decision=decision.id,
                    file=control.path,
                    problem=f'is `pragma: supported` but contains no `governance: enforces {decision.id}` line.',
                    remedy=(
                        f'Add a comment line reading `governance: enforces {decision.id}` to the control. '
                        f'The pragma is the control-to-decision tether; without it the rule is untraceable.'
                    ),
                )
            )
    return failures


def check_4_pragmas_resolve(decisions: list[Decision]) -> list[Failure]:
    """A pragma naming a missing, draft or superseded decision is rot or evasion."""
    by_id = {decision.id: decision for decision in decisions}
    failures = []
    for path in scannable_files():
        content = read_text(path)
        if content is None:
            continue
        for match in PRAGMA_RE.finditer(content):
            for referenced in DEC_REF_RE.findall(match.group('ids')):
                decision = by_id.get(referenced)
                if decision is not None and decision.is_live:
                    continue
                if decision is None:
                    problem = f'carries `governance: enforces {referenced}`, but no such decision exists.'
                    remedy = (
                        f'Fix the typo, author {referenced}, or delete the pragma. A pragma pointing at '
                        f'nothing means nobody can tell what this file enforces.'
                    )
                else:
                    state = 'superseded' if decision.superseded_by else decision.status
                    problem = f'carries `governance: enforces {referenced}`, but {referenced} is {state}.'
                    remedy = (
                        f'Finish the supersession: retarget this pragma at the replacement decision, '
                        f'or accept {referenced} if it was meant to be live.'
                    )
                failures.append(
                    Failure(check=4, decision=referenced, file=relative(path), problem=problem, remedy=remedy)
                )
    return failures


def check_5_external_hashes(decisions: list[Decision]) -> list[Failure]:
    """Drift detection for control artifacts that cannot hold a comment."""
    failures = []
    for decision in live(decisions):
        for control in decision.controls:
            if control.pragma != 'external':
                continue
            path = REPO_ROOT / control.path
            if not path.exists():
                continue  # already reported by check 2
            actual = sha256_of(path)
            if control.sha256 is None:
                failures.append(
                    Failure(
                        check=5,
                        decision=decision.id,
                        file=control.path,
                        problem='is `pragma: external` but no `sha256` is recorded for it.',
                        remedy=f'Record `sha256: {actual}` on this control in {decision.id}.',
                    )
                )
            elif control.sha256 != actual:
                failures.append(
                    Failure(
                        check=5,
                        decision=decision.id,
                        file=control.path,
                        problem=f'content hash changed.\n    recorded {control.sha256}\n    actual   {actual}',
                        remedy=(
                            f'Re-read the file and decide whether {decision.id} still says what you want. '
                            f'If it does, record `sha256: {actual}`. Do not update the hash without reading '
                            f'the diff — re-review is the entire point of hashing this file.'
                        ),
                    )
                )
    return failures


# --- Hygiene ------------------------------------------------------------------


def check_6_views_current() -> list[Failure]:
    try:
        artifacts = bv.build()
    except DecisionError as exc:
        return [
            Failure(
                check=6,
                decision=None,
                file=None,
                problem=f'a decision file could not be parsed: {exc}',
                remedy='Fix the decision file. Nothing can be generated until it parses.',
            )
        ]
    if bv.check(artifacts) == 0:
        return []
    return [
        Failure(
            check=6,
            decision=None,
            file='governance/views/, governance/registry.json',
            problem='generated files differ from a fresh build (diff printed above).',
            remedy='Run `make views` and commit the result. Never hand-edit a generated file.',
        )
    ]


def check_7_no_dead_rules_in_views(decisions: list[Decision]) -> list[Failure]:
    """The failure this whole design exists to prevent. Treat any hit as urgent."""
    if not VIEWS_DIR.exists():
        return []
    contents = {path: read_text(path) or '' for path in sorted(VIEWS_DIR.rglob('*')) if path.is_file()}
    failures = []
    for decision in decisions:
        if decision.is_live:
            continue
        state = 'superseded' if decision.superseded_by else decision.status
        for path, raw in contents.items():
            flat = bv.flatten(raw)
            leaks = []
            if f'[{decision.id}]' in raw:
                leaks.append(f'tagged as {decision.id}')
            if decision.rule and decision.rule in flat:
                leaks.append('its rule text appears verbatim')
            if not leaks:
                continue
            failures.append(
                Failure(
                    check=7,
                    decision=decision.id,
                    file=relative(path),
                    problem=f'{decision.id} is {state}, but {" and ".join(leaks)} in this generated view.',
                    remedy=(
                        'This is a generator bug and the most damaging failure in the system — a dead rule '
                        'reaching an agent steers it toward the exact pattern we abandoned. Fix '
                        'build_views.py before doing anything else.'
                    ),
                )
            )
    return failures


def check_8_ids_and_supersessions(decisions: list[Decision]) -> list[Failure]:
    failures = []
    seen: dict[str, Decision] = {}
    for decision in decisions:
        if decision.id in seen:
            failures.append(
                Failure(
                    check=8,
                    decision=decision.id,
                    file=relative(decision.source),
                    problem=f'duplicate id, already used by {relative(seen[decision.id].source)}.',
                    remedy='Renumber one of them to the next free integer. Ids are the anchor for every pragma.',
                )
            )
        else:
            seen[decision.id] = decision

    for decision in decisions:
        target_id = decision.superseded_by
        if target_id is None:
            continue
        if decision.status != 'superseded':
            failures.append(
                Failure(
                    check=8,
                    decision=decision.id,
                    file=relative(decision.source),
                    problem=f'has `superseded_by: {target_id}` but `status: {decision.status}`.',
                    remedy='Set `status: superseded`. A half-finished supersession leaves two live rules.',
                )
            )
        target = seen.get(target_id)
        if target is None:
            failures.append(
                Failure(
                    check=8,
                    decision=decision.id,
                    file=relative(decision.source),
                    problem=f'has `superseded_by: {target_id}`, but no such decision exists.',
                    remedy=f'Author {target_id}, or correct the reference.',
                )
            )
        elif not target.is_live:
            state = 'superseded' if target.superseded_by else target.status
            failures.append(
                Failure(
                    check=8,
                    decision=decision.id,
                    file=relative(decision.source),
                    problem=f'is superseded by {target_id}, which is itself {state}.',
                    remedy=f'Point at the decision that is actually live, or accept {target_id}.',
                )
            )
    return failures


# --- Teeth --------------------------------------------------------------------


def suite_commands() -> list[list[str]]:
    controls = sorted((REPO_ROOT / 'controls' / 'fitness').glob('*.py'))
    commands = [[sys.executable, str(path)] for path in controls]
    commands.append(['ruff', 'check', '.'])
    commands.append(['ruff', 'format', '--check', '.'])
    commands.append(['ty', 'check'])
    commands.append(['pytest', '-q'])
    return commands


def check_9_suite_runs() -> list[Failure]:
    """A traceable rule that never executes is theatre."""
    if os.environ.get(SUITE_GUARD_ENV):
        print(f'  check 9 skipped: the control suite is already running upstream ({SUITE_GUARD_ENV} is set).')
        return []
    env = {**os.environ, SUITE_GUARD_ENV: '1'}
    failures = []
    for command in suite_commands():
        printable = ' '.join(command)
        print(f'  running {printable}')
        result = subprocess.run(command, cwd=REPO_ROOT, env=env, check=False)
        if result.returncode == 0:
            continue
        if command[0] == 'pytest' and result.returncode == PYTEST_NO_TESTS_COLLECTED:
            remedy = (
                'Write tests under tests/. This check exists to prove the rules actually execute, '
                'and a suite with nothing in it proves nothing.'
            )
            problem = f'`{printable}` collected no tests at all.'
        else:
            remedy = (
                'Either the code genuinely violates a rule, or a control fires on correct code. '
                'Work out which before touching the code — a control that fires on correct code is '
                'the most damaging failure mode here, because it teaches everyone to route around '
                'the harness.'
            )
            problem = f'`{printable}` exited {result.returncode}.'
        failures.append(
            Failure(
                check=9,
                decision=None,
                file=command[-1] if command[0] == sys.executable else None,
                problem=problem,
                remedy=remedy,
            )
        )
    return failures


# --- Driver -------------------------------------------------------------------


def run(skip_suite: bool = False) -> list[Failure]:
    try:
        decisions = bv.load_decisions()
    except DecisionError as exc:
        return [
            Failure(
                check=0,
                decision=None,
                file=None,
                problem=f'a decision file could not be parsed: {exc}',
                remedy='Fix the decision file. No other check can run until every decision parses.',
            )
        ]

    failures: list[Failure] = []
    failures += check_1_decisions_have_controls(decisions)
    failures += check_2_control_paths_exist(decisions)
    failures += check_3_pragmas_present(decisions)
    failures += check_4_pragmas_resolve(decisions)
    failures += check_5_external_hashes(decisions)
    failures += check_6_views_current()
    failures += check_7_no_dead_rules_in_views(decisions)
    failures += check_8_ids_and_supersessions(decisions)
    if not skip_suite:
        failures += check_9_suite_runs()
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        '--no-suite',
        action='store_true',
        help="skip check 9. For use by the harness's own tests, which the suite itself runs.",
    )
    args = parser.parse_args(argv)

    failures = run(skip_suite=args.no_suite)
    if not failures:
        print('governance: all checks passed.')
        return 0

    print(file=sys.stderr)
    for failure in failures:
        print(failure.render(), file=sys.stderr)
        print(file=sys.stderr)
    checks = sorted({failure.check for failure in failures})
    plural = '' if len(failures) == 1 else 's'
    print(f'governance: {len(failures)} failure{plural} across check(s) {checks}.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
