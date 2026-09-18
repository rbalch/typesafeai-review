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
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from typesafe_sdk import Noul
from typesafe_sdk import constants as typesafe_constants

from typesafe_review import pipeline
from typesafe_review.ask import AskFailed, RequestItem, ask_all
from typesafe_review.calibrate import run as run_calibrate
from typesafe_review.case import CaseError, write_state_files
from typesafe_review.checks import CheckError
from typesafe_review.compose import ComposeInvariantError
from typesafe_review.env import EnvError, load_env
from typesafe_review.prsource import PRSourceError, PullRequest, extract_brief, extract_red_sha, fetch_pr, parse_pr_ref
from typesafe_review.render import RenderError
from typesafe_review.slicing import SlicingError, slice_diff
from typesafe_review.state import (
    StateError,
    build_change_state,
    build_hunk_states,
    load_acceptance_tests,
    load_conventions,
)
from typesafe_review.target import TargetError, resolve_target
from typesafe_review.taskfile import Task, TaskFileError, load_task, parse_task
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
    CaseError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='ts-review',
        description='AI-powered code review: deterministic checks + Jev per-hunk questions.',
    )
    parser.add_argument(
        '--worktree',
        type=Path,
        default=None,
        help='path to the repo/worktree to review (default: toplevel of cwd, RA-03)',
    )
    parser.add_argument(
        '--task',
        default=None,
        help='planner task file for this change, or a PR number/#number/URL to pull the brief from (RA-02)',
    )
    parser.add_argument('--red-sha', dest='red_sha', default=None, help='commit SHA of the failing acceptance tests')
    parser.add_argument(
        '--base',
        default=None,
        help='base ref to diff against (default: develop, falling back to main, master)',
    )
    parser.add_argument(
        '--out',
        type=Path,
        default=None,
        help='directory to write ts-review.md/.json to (default: --worktree, or the source repo root in a ref mode)',
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument('--range', default=None, help='review A..B or A...B: merge-base(A, B) vs B (RA-03)')
    mode_group.add_argument('--commit', default=None, help='review a single commit S: S^ vs S (RA-03)')
    mode_group.add_argument('--ref', default=None, help='review a branch or ref R: merge-base(<base>, R) vs R (RA-03)')
    mode_group.add_argument(
        '--pr', type=int, default=None, help='review a PR by number, merged or not (RA-03; implies --task)'
    )
    parser.add_argument('--dump-state', type=Path, default=None, help='write state + questions as JSON and exit')
    parser.add_argument('--record', type=Path, default=None, help='record API responses to this directory')
    parser.add_argument('--replay', type=Path, default=None, help='replay recorded API responses from this directory')
    parser.add_argument('--calibrate', type=Path, default=None, help='run calibration against a labelled fixtures dir')
    parser.add_argument(
        '--case',
        type=Path,
        default=None,
        help='write a real-run calibration case to this directory (RA-04; implies recording responses there)',
    )
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


def _default_worktree() -> Path | None:
    """The toplevel of the cwd's git repo, or `None` if cwd is not inside one
    (RA-03 item 2: `--worktree` is optional in every mode; this is what it defaults
    to). Never raises: a missing `git` binary or a non-repo cwd both just mean "no
    default", for the caller to report."""
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


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


def _dump_state(worktree: Path, base: str, task_arg: str | None, red_sha: str | None, dump_dir: Path) -> int:
    """`--dump-state`: slice, build every state shape, write it to `dump_dir` as
    JSON (through `case.write_state_files`, so it is redacted the same way a
    `--case`'s own `state/` files are -- RA-04 fix round 1), print a token
    estimate per file to stderr, and exit — no API call, nothing written in
    `worktree`.

    `--dump-state` only ever reads `task_arg` as a file path (RA-02's PR source is out
    of scope here: this mode inspects state shapes offline, it never calls `gh`).
    """
    task = None
    if task_arg is not None:
        try:
            task = load_task(Path(task_arg))
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
        written = write_state_files(dump_dir, hunk_states, change_state)
    except CaseError as e:
        print(str(e), file=sys.stderr)
        return EXIT_TOOL_FAILURE

    for filename, size in written:
        print(f'{filename}: ~{size // 4} tokens', file=sys.stderr)

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


def _confirmed_commit_exists(worktree: Path, sha: str) -> bool:
    result = subprocess.run(
        ['git', '-C', str(worktree), 'cat-file', '-e', f'{sha}^{{commit}}'],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _resolve_task_and_red_sha(
    worktree: Path, task_arg: str | None, red_sha_arg: str | None, pr: PullRequest | None = None
) -> tuple[Task | None, str, str | None, str]:
    """`(task, task_source, red_sha, red_sha_source)` for `pipeline.run` (RA-02).

    `task_arg` a PR ref -> fetch the PR, parse its brief, and (absent `--red-sha`)
    take a red-sha out of the body if `git cat-file` confirms the commit exists in
    `worktree`. `task_arg` anything else -> a file path, exactly as before RA-02.
    `pr`, if given and its `number` matches `task_arg`'s, is the PR `target.py`
    already fetched for `--pr N` -- reused here instead of a second `gh pr view`
    call (fix round 1, item 3: `--pr N` makes exactly one `gh` call). Raises
    `PRSourceError` / `TaskFileError` for the caller to turn into a stderr line and
    exit 1; never guesses past those.
    """
    red_sha = red_sha_arg
    red_sha_source = 'flag' if red_sha_arg is not None else 'none'

    if task_arg is None:
        return None, 'none', red_sha, red_sha_source

    pr_number = parse_pr_ref(task_arg)
    if pr_number is None:
        task = load_task(Path(task_arg))
        return task, 'file', red_sha, red_sha_source

    if pr is None or pr.number != pr_number:
        pr = fetch_pr(pr_number, worktree)
    brief = extract_brief(pr.body)
    task = parse_task(brief, f'PR #{pr_number}')

    if red_sha is None:
        candidate = extract_red_sha(pr.body)
        if candidate is not None:
            if _confirmed_commit_exists(worktree, candidate):
                red_sha = candidate
                red_sha_source = 'pr'
            else:
                print(
                    f'red sha {candidate} from PR #{pr_number} body not found in {worktree}; continuing without one',
                    file=sys.stderr,
                )

    return task, 'pr', red_sha, red_sha_source


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.case is not None and args.record is not None:
        # RA-04 item 1: `--case` always records to `<case>/responses`; an explicit
        # `--record` elsewhere would leave two, silently disagreeing homes for the
        # same run's responses.
        parser.error('argument --case: not allowed with argument --record (--case always records to <case>/responses)')

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

    # `--pr` implies `--task N` unless `--task` was already given (RA-03 scope item 1).
    if args.pr is not None and args.task is None:
        args.task = str(args.pr)

    if args.worktree is None:
        worktree = _default_worktree()
        if worktree is None:
            print(
                'no --worktree given and `git rev-parse --show-toplevel` failed '
                '(run ts-review from inside a git repo, or pass --worktree)',
                file=sys.stderr,
            )
            return EXIT_TOOL_FAILURE
    else:
        worktree = args.worktree
        root_error = _worktree_root_error(worktree)
        if root_error is not None:
            print(root_error, file=sys.stderr)
            return EXIT_TOOL_FAILURE

    mode = (
        'range'
        if args.range is not None
        else 'commit'
        if args.commit is not None
        else 'ref'
        if args.ref is not None
        else 'pr'
        if args.pr is not None
        else 'worktree'
    )

    # `--base` only feeds the no-mode default and `--ref` (merge-base(<base>, ref));
    # `--range`/`--commit`/`--pr` compute their own range entirely from the ref/PR
    # given, so resolving today's develop/main/master fallback for them would just
    # be a spurious failure mode when none of those branches exist.
    base: str | None = None
    if mode in ('worktree', 'ref'):
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
        # actually reviewed. It only ever supports today's default target (RA-03
        # does not extend it to a ref mode).
        if mode != 'worktree' or base is None:
            print('--dump-state does not support --range/--commit/--ref/--pr', file=sys.stderr)
            return EXIT_TOOL_FAILURE
        return _dump_state(worktree, base, args.task, args.red_sha, args.dump_state)

    try:
        target = resolve_target(worktree, args, base)
    except TargetError as error:
        print(str(error), file=sys.stderr)
        return EXIT_TOOL_FAILURE

    print(f'target: {target.source} base={target.base_sha} head={target.head_sha}', file=sys.stderr)

    try:
        task, task_source, red_sha, red_sha_source = _resolve_task_and_red_sha(
            worktree, args.task, args.red_sha, target.pr
        )
    except (TaskFileError, PRSourceError) as error:
        print(str(error), file=sys.stderr)
        return EXIT_TOOL_FAILURE

    out_dir = args.out if args.out is not None else worktree

    def _run_pipeline(pipeline_worktree: Path) -> int:
        try:
            return pipeline.run(
                args,
                pipeline_worktree,
                target.base_sha,
                task,
                red_sha,
                task_source,
                red_sha_source,
                target.head_sha,
                target.source,
                out_dir,
            )
        except _PIPELINE_ERRORS as error:
            print(str(error), file=sys.stderr)
            return EXIT_TOOL_FAILURE
        except subprocess.SubprocessError as error:
            print(f'git error: {error}', file=sys.stderr)
            return EXIT_TOOL_FAILURE
        except OSError as error:
            print(f'io error: {error}', file=sys.stderr)
            return EXIT_TOOL_FAILURE

    if target.source == 'worktree':
        # Today's contract: run straight against the user's own worktree. Never the
        # user's checkout in any other mode (RA-03 item 3).
        return _run_pipeline(worktree)

    # Any ref mode: never touch `worktree` (the source repo). Review a temporary
    # detached worktree checked out at `target.head_sha` instead, and always remove
    # it -- including when the pipeline raises -- matching `checks._red_proof`'s own
    # cleanup shape (best-effort remove, then rmtree, then prune).
    tmp_dir = Path(tempfile.mkdtemp(prefix='ts-review-target-'))
    try:
        add_result = subprocess.run(
            ['git', '-C', str(worktree), 'worktree', 'add', '--detach', str(tmp_dir), target.head_sha],
            capture_output=True,
            text=True,
            check=False,
        )
        if add_result.returncode != 0:
            print(
                f'git worktree add --detach {tmp_dir} {target.head_sha} failed: {add_result.stderr.strip()}',
                file=sys.stderr,
            )
            return EXIT_TOOL_FAILURE
        return _run_pipeline(tmp_dir)
    finally:
        subprocess.run(
            ['git', '-C', str(worktree), 'worktree', 'remove', '--force', str(tmp_dir)],
            capture_output=True,
            check=False,
        )
        shutil.rmtree(tmp_dir, ignore_errors=True)
        subprocess.run(['git', '-C', str(worktree), 'worktree', 'prune'], capture_output=True, check=False)


if __name__ == '__main__':
    sys.exit(main())
