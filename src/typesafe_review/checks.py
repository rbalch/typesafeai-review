"""Deterministic subprocess checks: red proof, green at HEAD, make check.

No model involved. Spec §4.1: same three rows as `reviewer.md`'s required-checks
table, each reporting `pass | fail | not_run | not_applicable` with the captured
output tail as notes, plus the deterministic findings they imply.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

CheckStatus = Literal['pass', 'fail', 'not_run', 'not_applicable']

#: Default per-subprocess timeout, in seconds. Tests inject a smaller value.
DEFAULT_TIMEOUT_SECONDS = 600

_REDACT_RE = re.compile(r'(?i)(token|secret|key)\s*[=:]\s*\S+')
#: `scheme://user:pass@host/...` -- a URL's own userinfo, the shape a `git remote
#: get-url origin` with embedded credentials takes (RA-04 fix round 1). Requires a
#: `user:pass` pair, not just a bare `user@host` (RA-04 fix round 2): an
#: `ssh://git@host/org/repo.git` origin has no password component at all, and
#: redacting its bare `git` username would make a committed fixture's `meta.json`
#: factually wrong about what the remote actually is. A bare-token-as-username
#: `https://` origin (no `:pass`) is still handled upstream, by
#: `pipeline._strip_url_userinfo` stripping the whole `http(s)` userinfo outright
#: before `meta` is ever built -- this pattern is the backstop for what that
#: doesn't cover (ssh URLs and scp-like `git@host:path` are left alone there too).
_URL_CREDENTIAL_RE = re.compile(r'://[^/@:\s]+:[^/@\s]+@')

#: `Authorization: Bearer <token>` headers -- F-8 sighting 3's corpus (DEC-3): the
#: token itself rarely contains the literal word "token", so `_REDACT_RE` never saw
#: it. Matches the header name and the whole token in one go.
_BEARER_RE = re.compile(r'(?i)authorization\s*:\s*bearer\s+\S+')

#: Well-known bare credential prefixes that carry no `key=`/`token=` label of their
#: own -- exactly how F-8 sighting 2's `gh` "Bad credentials: ghp_..." message and a
#: leaked GitHub/OpenAI/AWS key read in the wild. `\b` keeps this from matching
#: inside a longer identifier.
_BARE_SECRET_RE = re.compile(r'\b(?:ghp_[A-Za-z0-9]{36}|sk-[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})\b')

#: A PEM-encoded key's header line, e.g. `-----BEGIN RSA PRIVATE KEY-----`.
_PEM_RE = re.compile(r'-----BEGIN [A-Z0-9 ]+-----')


class CheckError(Exception):
    """A subprocess or filesystem operation needed for a check failed to run."""


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    notes: str = ''


@dataclass(frozen=True)
class CheckFinding:
    id: str
    path: str | None
    notes: str = ''


@dataclass
class CheckReport:
    results: list[CheckResult] = field(default_factory=list)
    findings: list[CheckFinding] = field(default_factory=list)


def redact(text: str) -> str:
    """Replace anything that looks like `token=`/`secret=`/`key=<value>` with
    `<redacted>`, any URL's embedded `user:pass@` with `<redacted>@` (RA-04 fix
    round 1: a `git remote get-url origin` can carry a credential this way), an
    `Authorization: Bearer <token>` header, a bare well-known credential prefix
    (`ghp_`/`sk-`/`AKIA`, DEC-3), and a PEM key header line. Public so any module
    that surfaces captured subprocess or `gh` output, or a git remote URL, in an
    error message or a written file (`prsource.py`'s `fetch_pr`, RA-02 fix round
    1; `case.py`'s `meta.json`, RA-04) can reuse the same patterns instead of
    hand-rolling a second one. Measured against a fixed must-scrub/must-keep
    corpus by `controls/fitness/redaction_corpus.py` (DEC-3) -- widen the corpus,
    not just this function, when a new leak shape shows up."""
    text = _REDACT_RE.sub('<redacted>', text)
    text = _URL_CREDENTIAL_RE.sub('://<redacted>@', text)
    text = _BEARER_RE.sub('<redacted>', text)
    text = _BARE_SECRET_RE.sub('<redacted>', text)
    return _PEM_RE.sub('<redacted>', text)


def _tail_notes(output: str) -> str:
    lines = output.splitlines()
    tail = lines[-30:]
    return redact('\n'.join(tail))


def _run(cmd: list[str], cwd: Path, timeout: float) -> tuple[int | None, str]:
    """Run `cmd` in `cwd`. Returns (returncode, combined output).

    `returncode` is `None` on timeout. Never raises for a timeout; raises
    `CheckError` only when the subprocess could not be started at all (missing
    executable, permission denied, ...) -- a failure mode distinct from the
    command running and failing.
    """
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raw = e.stdout
        if raw is None:
            output = ''
        elif isinstance(raw, bytes):
            output = raw.decode('utf-8', errors='replace')
        else:
            output = raw
        return None, output
    except OSError as e:
        raise CheckError(f'could not run {cmd[0]!r}: {e}') from e
    return proc.returncode, proc.stdout.decode('utf-8', errors='replace')


def _timeout_notes(timeout: float) -> str:
    return f'timed out after {int(timeout)}s'


def _git(worktree: Path, args: list[str], timeout: float) -> tuple[int | None, str]:
    return _run(['git', *args], cwd=worktree, timeout=timeout)


def _ls_tree_paths(worktree: Path, sha: str, subdir: str, timeout: float) -> list[str]:
    """Files under `subdir` that existed at `sha`, using -z to survive git's quoting.

    Raises `CheckError` if the listing itself could not be produced: a caller must
    never treat "we could not tell" as "there is nothing to worry about".
    """
    code, out = _git(worktree, ['ls-tree', '-r', '--name-only', '-z', sha, '--', subdir], timeout)
    if code != 0:
        raise CheckError(f'git ls-tree failed (exit {code}): {_tail_notes(out)}')
    # `out` was decoded as text; the NUL separators survive utf-8 decoding untouched.
    return [p for p in out.split('\0') if p]


# pytest's own exit codes (https://docs.pytest.org/en/stable/reference/exit-codes.html).
# Only 1 (tests failed) and 2 (collection error / interrupted, the ImportError-before
# -implementation case) are evidence the acceptance tests were actually red. Everything
# else -- including 0, "all passed" -- means the red proof was never established.
_PYTEST_EXIT_REASONS = {
    0: 'tests passed',
    1: 'tests failed',
    2: 'collection error or interrupted run',
    3: 'pytest internal error',
    4: 'pytest usage error',
    5: 'no tests collected',
}
_PYTEST_RED_EXIT_CODES = {1, 2}


def _pytest_exit_notes(code: int, out: str) -> str:
    reason = _PYTEST_EXIT_REASONS.get(code, 'unrecognised exit code')
    header = f'pytest exit {code}: {reason}'
    tail = _tail_notes(out)
    if tail:
        return _tail_notes(f'{header}\n{tail}')
    return header


def _red_proof(worktree: Path, red_sha: str | None, timeout: float) -> tuple[CheckResult, list[CheckFinding]]:
    name = 'red proof'
    if red_sha is None:
        return (
            CheckResult(name, 'not_run', 'no red sha provided'),
            [CheckFinding('red_proof_missing', None, 'no red sha provided')],
        )

    tmp_dir = Path(tempfile.mkdtemp(prefix='ts-review-red-'))
    try:
        try:
            code, out = _git(worktree, ['worktree', 'add', '--detach', str(tmp_dir), red_sha], timeout)
        except CheckError as e:
            return (
                CheckResult(name, 'fail', _tail_notes(str(e))),
                [CheckFinding('red_proof_missing', None, _tail_notes(str(e)))],
            )
        if code != 0:
            notes = _tail_notes(out)
            return (
                CheckResult(name, 'fail', notes),
                [CheckFinding('red_proof_missing', None, notes)],
            )

        try:
            pcode, pout = _run(['uv', 'run', 'pytest', '-q'], cwd=tmp_dir, timeout=timeout)
        except CheckError as e:
            return (
                CheckResult(name, 'fail', _tail_notes(str(e))),
                [CheckFinding('red_proof_missing', None, _tail_notes(str(e)))],
            )

        if pcode is None:
            notes = _timeout_notes(timeout)
            return (
                CheckResult(name, 'fail', notes),
                [CheckFinding('red_proof_missing', None, notes)],
            )

        if pcode not in _PYTEST_RED_EXIT_CODES:
            # Anything other than "tests failed" or "collection error/interrupted"
            # means the acceptance tests were never actually proven red -- including
            # exit 0 (passed), 5 (no tests collected), 4 (usage error), and 3
            # (internal error).
            notes = _pytest_exit_notes(pcode, pout)
            return (
                CheckResult(name, 'fail', notes),
                [CheckFinding('red_proof_missing', None, notes)],
            )
        return CheckResult(name, 'pass', _tail_notes(pout)), []
    finally:
        try:
            _git(worktree, ['worktree', 'remove', '--force', str(tmp_dir)], timeout)
        except CheckError:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)
        try:
            _git(worktree, ['worktree', 'prune'], timeout)
        except CheckError:
            pass


def _acceptance_tests_edited(worktree: Path, red_sha: str, timeout: float) -> list[CheckFinding]:
    """Test files edited between `red_sha` and `HEAD`.

    Raises `CheckError` on any git failure along the way: a diff we could not
    compute is not evidence that nothing changed.
    """
    test_paths = _ls_tree_paths(worktree, red_sha, 'tests/', timeout)
    if not test_paths:
        return []

    code, out = _git(worktree, ['diff', '--name-only', '-z', red_sha, 'HEAD', '--', *test_paths], timeout)
    if code != 0:
        raise CheckError(f'git diff --name-only failed (exit {code}): {_tail_notes(out)}')

    changed = [p for p in out.split('\0') if p]
    findings: list[CheckFinding] = []
    for path in changed:
        code, diff_out = _git(worktree, ['diff', red_sha, 'HEAD', '--', path], timeout)
        if code not in (0, 1):
            raise CheckError(f'git diff failed (exit {code}) on {path!r}: {_tail_notes(diff_out)}')
        findings.append(CheckFinding('acceptance_tests_edited', path, _tail_notes(diff_out)))
    return findings


def _green_at_head(worktree: Path, red_sha: str | None, timeout: float) -> tuple[CheckResult, list[CheckFinding]]:
    name = 'green at HEAD'
    try:
        code, out = _run(['uv', 'run', 'pytest', '-q'], cwd=worktree, timeout=timeout)
    except CheckError as e:
        notes = _tail_notes(str(e))
        return CheckResult(name, 'fail', notes), [CheckFinding('gate_failed', None, notes)]

    if code is None:
        notes = _timeout_notes(timeout)
        return CheckResult(name, 'fail', notes), [CheckFinding('gate_failed', None, notes)]

    notes = _tail_notes(out)
    if code != 0:
        return CheckResult(name, 'fail', notes), [CheckFinding('gate_failed', None, notes)]

    findings: list[CheckFinding] = []
    if red_sha is not None:
        try:
            findings.extend(_acceptance_tests_edited(worktree, red_sha, timeout))
        except CheckError as e:
            fail_notes = _tail_notes(f'could not verify acceptance tests: {e}')
            return CheckResult(name, 'fail', fail_notes), [CheckFinding('gate_failed', None, fail_notes)]
    return CheckResult(name, 'pass', notes), findings


_MAKE_CHECK_TARGET_RE = re.compile(r'(?m)^check\s*:')


def _make_check(worktree: Path, timeout: float) -> tuple[CheckResult, list[CheckFinding]]:
    name = 'make check'
    makefile = worktree / 'Makefile'
    if not makefile.exists():
        return CheckResult(name, 'not_applicable', 'no Makefile'), []

    try:
        contents = makefile.read_text(errors='replace')
    except OSError as e:
        notes = _tail_notes(f'could not read Makefile: {e}')
        return CheckResult(name, 'fail', notes), [CheckFinding('gate_failed', None, notes)]

    if not _MAKE_CHECK_TARGET_RE.search(contents):
        return CheckResult(name, 'not_applicable', 'Makefile has no check target'), []

    try:
        code, out = _run(['make', 'check'], cwd=worktree, timeout=timeout)
    except CheckError as e:
        notes = _tail_notes(str(e))
        return CheckResult(name, 'fail', notes), [CheckFinding('gate_failed', None, notes)]

    if code is None:
        notes = _timeout_notes(timeout)
        return CheckResult(name, 'fail', notes), [CheckFinding('gate_failed', None, notes)]

    notes = _tail_notes(out)
    if code != 0:
        return CheckResult(name, 'fail', notes), [CheckFinding('gate_failed', None, notes)]
    return CheckResult(name, 'pass', notes), []


def run_checks(
    worktree: Path,
    red_sha: str | None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> CheckReport:
    """Run the three deterministic checks against `worktree`. No model involved."""
    try:
        _git(worktree, ['worktree', 'prune'], timeout)
    except CheckError:
        pass

    report = CheckReport()

    red_result, red_findings = _red_proof(worktree, red_sha, timeout)
    report.results.append(red_result)
    report.findings.extend(red_findings)

    green_result, green_findings = _green_at_head(worktree, red_sha, timeout)
    report.results.append(green_result)
    report.findings.extend(green_findings)

    make_result, make_findings = _make_check(worktree, timeout)
    report.results.append(make_result)
    report.findings.extend(make_findings)

    return report
