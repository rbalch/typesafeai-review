"""`ts-review` entry point.

Parses every flag from spec §3, resolves the base ref, and either serves `--dump-state`
(T-01), `--calibrate` (T-11, `calibrate.run`), or hands off to `pipeline.run` (T-10)
for the real steps 0-7.

`main` is the one place every module's own exception type -- and a bare
`subprocess.SubprocessError` (an unwrapped git call) or `OSError` (unwrapped file I/O,
e.g. `pipeline.py`'s own stale-output cleanup or the recorder's cache file) -- gets
turned into a stderr message and exit 1 (spec §6.7). Nothing upstream of here ever
prints a raw traceback.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from typesafe_sdk import Noul
from typesafe_sdk import constants as typesafe_constants

from typesafe_review import pipeline
from typesafe_review.ask import AskFailed, RequestItem, ask_all
from typesafe_review.calibrate import run as run_calibrate
from typesafe_review.checks import CheckError
from typesafe_review.compose import ComposeInvariantError
from typesafe_review.env import EnvError, load_env
from typesafe_review.render import RenderError
from typesafe_review.slicing import SlicingError, slice_diff
from typesafe_review.state import (
    StateError,
    build_change_state,
    build_hunk_states,
    load_acceptance_tests,
    load_conventions,
)
from typesafe_review.taskfile import TaskFileError, load_task
from typesafe_review.verdict import EXIT_APPROVE, EXIT_TOOL_FAILURE

BASE_CANDIDATES = ('develop', 'main', 'master')

#: Exceptions each module's own contract names (spec §6.7 exit-code table); a
#: traceback from any of these escaping `main` is a blocker, not a review outcome.
_PIPELINE_ERRORS = (
    AskFailed,
    TaskFileError,
    CheckError,
    SlicingError,
    StateError,
    ComposeInvariantError,
    RenderError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='ts-review',
        description='AI-powered code review: deterministic checks + Jev per-hunk questions.',
    )
    parser.add_argument('--worktree', type=Path, default=None, help='path to the builder worktree to review')
    parser.add_argument('--task', type=Path, default=None, help='planner task file for this change')
    parser.add_argument('--red-sha', dest='red_sha', default=None, help='commit SHA of the failing acceptance tests')
    parser.add_argument(
        '--base',
        default=None,
        help='base ref to diff against (default: develop, falling back to main, master)',
    )
    parser.add_argument('--dump-state', type=Path, default=None, help='write state + questions as JSON and exit')
    parser.add_argument('--record', type=Path, default=None, help='record API responses to this directory')
    parser.add_argument('--replay', type=Path, default=None, help='replay recorded API responses from this directory')
    parser.add_argument('--calibrate', type=Path, default=None, help='run calibration against a labelled fixtures dir')
    parser.add_argument(
        '--env-file', dest='env_file', type=Path, default=None, help='load TYPESAFE_* keys from this file first'
    )
    parser.add_argument(
        '--doctor', action='store_true', help='prove the key, base URL and model work with one live call'
    )
    return parser


def _worktree_root_error(path: Path) -> str | None:
    """Return an error message if `path` is not a git worktree root, else None."""
    if not path.is_dir():
        return f'not a git worktree: {path}'
    result = subprocess.run(
        ['git', '-C', str(path), 'rev-parse', '--show-toplevel'],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        return f'not a git worktree: {path}'
    toplevel = Path(result.stdout.strip()).resolve()
    if toplevel != path.resolve():
        return f'not a worktree root: {path} (root is {toplevel})'
    return None


def _resolve_base(worktree: Path, base: str | None) -> str | None:
    candidates = (base,) if base else BASE_CANDIDATES
    for ref in candidates:
        result = subprocess.run(
            ['git', '-C', str(worktree), 'rev-parse', '--verify', ref],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return ref
    return None


def _dump_state(worktree: Path, base: str, task_path: Path | None, red_sha: str | None, dump_dir: Path) -> int:
    """`--dump-state`: slice, build every state shape, write it to `dump_dir` as JSON,
    print a token estimate per file to stderr, and exit — no API call, nothing written
    in `worktree`.
    """
    task = None
    if task_path is not None:
        try:
            task = load_task(task_path)
        except TaskFileError as e:
            print(str(e), file=sys.stderr)
            return EXIT_TOOL_FAILURE

    try:
        change = slice_diff(worktree, base)
    except SlicingError as e:
        print(str(e), file=sys.stderr)
        return EXIT_TOOL_FAILURE

    try:
        conventions = load_conventions(worktree)
        acceptance_tests = load_acceptance_tests(worktree, red_sha)
        hunk_states = build_hunk_states(task, change, conventions)
        change_state = build_change_state(task, change, acceptance_tests)
    except StateError as e:
        print(str(e), file=sys.stderr)
        return EXIT_TOOL_FAILURE

    try:
        dump_dir.mkdir(parents=True, exist_ok=True)

        for i, hunk_state in enumerate(hunk_states, start=1):
            encoded = json.dumps(hunk_state, indent=2)
            path = dump_dir / f'hunk-{i:02d}.json'
            path.write_text(encoded)
            print(f'{path.name}: ~{len(encoded) // 4} tokens', file=sys.stderr)

        change_encoded = json.dumps(change_state, indent=2)
        change_path = dump_dir / 'change.json'
        change_path.write_text(change_encoded)
        print(f'{change_path.name}: ~{len(change_encoded) // 4} tokens', file=sys.stderr)
    except OSError as e:
        print(f'{dump_dir}: cannot write state dump ({e.strerror or e})', file=sys.stderr)
        return EXIT_TOOL_FAILURE

    return EXIT_APPROVE


#: The exact state/questions `--doctor` sends: a `check` field that must equal
#: `"ping"`, so a passing answer means the model actually looked at the state rather
#: than defaulting to yes. Public (not `_`-prefixed) so a test can rebuild the same
#: `ask.request_key` a `--record` pass would have used, to plant a forged low-noul
#: replay fixture (RA-01 fix round 1).
DOCTOR_STATE: dict[str, str] = {'check': 'ping'}
DOCTOR_QUESTIONS: dict[str, Noul] = {
    'check': Noul(
        instructions='Does `check` equal the string "ping"?',
        criteria={'true': '`check` is exactly the string "ping"', 'false': '`check` is anything else'},
    )
}
DOCTOR_NOUL_THRESHOLD = 0.5


def _doctor(args: argparse.Namespace, trace: list[str]) -> int:
    """`--doctor`: print the resolved model, base URL and whether the key is
    present, send one `Noul` (`DOCTOR_QUESTIONS`) through the same `Recorder` path
    the pipeline uses (so `--replay` works in a test), and verify the answer --
    print `ok` only once the `check` question actually came back a `Noul` answer
    with `noul >= DOCTOR_NOUL_THRESHOLD` (a forged or missing answer must not print
    `ok`: "fail closed... a path that reports success it did not verify is a
    blocker," AGENTS.md). Any SDK error, missing key, or an answer below threshold
    prints its cause plus `trace` -- every search location `load_env` visited, in
    order -- and exits 1 (RA-01 item 4)."""
    model = pipeline.resolve_model()
    base_url = os.environ.get(typesafe_constants.BASE_URL_ENV, typesafe_constants.DEFAULT_BASE_URL)
    key_present = bool(os.environ.get(typesafe_constants.API_KEY_ENV))

    print(f'model: {model}', file=sys.stderr)
    print(f'base url: {base_url}', file=sys.stderr)
    print(f'key: {"present" if key_present else "missing"}', file=sys.stderr)

    if not key_present:
        print(f'doctor: {typesafe_constants.API_KEY_ENV} is not set', file=sys.stderr)
        for line in trace:
            print(line, file=sys.stderr)
        return EXIT_TOOL_FAILURE

    request: RequestItem = ('doctor', DOCTOR_STATE, DOCTOR_QUESTIONS)
    recorder = pipeline.resolve_recorder(args)
    try:
        result = asyncio.run(ask_all([request], model=model, recorder=recorder))
    except AskFailed as error:
        print(f'doctor: {error}', file=sys.stderr)
        for line in trace:
            print(line, file=sys.stderr)
        return EXIT_TOOL_FAILURE

    answer = result.answers['doctor'].nouls.get('check')
    noul = answer.noul if answer is not None else None

    if noul is None or noul < DOCTOR_NOUL_THRESHOLD:
        shown = 'missing' if noul is None else f'{noul}'
        print(f'doctor: check question answered {shown}, expected >= {DOCTOR_NOUL_THRESHOLD}', file=sys.stderr)
        for line in trace:
            print(line, file=sys.stderr)
        return EXIT_TOOL_FAILURE

    print(f'noul: {noul}', file=sys.stderr)
    print('ok')
    return EXIT_APPROVE


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    trace: list[str] = []
    try:
        contributing = load_env(args.worktree, args.env_file, trace=trace)
    except EnvError as error:
        print(str(error), file=sys.stderr)
        return EXIT_TOOL_FAILURE
    for path in contributing:
        print(f'env: {path}', file=sys.stderr)

    if args.doctor:
        # `--doctor` never needs `--worktree`, just like `--calibrate` below: it
        # proves the API credentials work, it does not review anything.
        return _doctor(args, trace)

    if args.calibrate is not None:
        # `--calibrate` never needs `--worktree`: it replays a labelled fixtures
        # directory, not the worktree under review (T-01 already waives the
        # requirement for this flag).
        return run_calibrate(args.calibrate)

    if args.worktree is None:
        parser.error('the following arguments are required: --worktree')

    worktree: Path = args.worktree
    root_error = _worktree_root_error(worktree)
    if root_error is not None:
        print(root_error, file=sys.stderr)
        return EXIT_TOOL_FAILURE

    base = _resolve_base(worktree, args.base)
    if base is None:
        tried = args.base if args.base else ', '.join(BASE_CANDIDATES)
        print(f'no base ref found (tried: {tried})', file=sys.stderr)
        return EXIT_TOOL_FAILURE

    print(f'base: {base}', file=sys.stderr)

    if args.dump_state is not None:
        # Dump mode is read-only inspection of `worktree`; step 0 (stale-output
        # cleanup) must never run for it, or `--dump-state` would delete a
        # pre-existing `review.md` / `review.json` in a target that was never
        # actually reviewed.
        return _dump_state(worktree, base, args.task, args.red_sha, args.dump_state)

    try:
        return pipeline.run(args, worktree, base)
    except _PIPELINE_ERRORS as error:
        print(str(error), file=sys.stderr)
        return EXIT_TOOL_FAILURE
    except subprocess.SubprocessError as error:
        print(f'git error: {error}', file=sys.stderr)
        return EXIT_TOOL_FAILURE
    except OSError as error:
        print(f'io error: {error}', file=sys.stderr)
        return EXIT_TOOL_FAILURE


if __name__ == '__main__':
    sys.exit(main())
