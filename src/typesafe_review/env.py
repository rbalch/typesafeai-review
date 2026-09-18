"""Load `.env`-style files for `TYPESAFE_*` keys, from known locations (RA-01).

`load_env` is `cli.main`'s first call, before anything else reads the environment.
Search order, first hit per key wins, every file in scope is parsed (never short-
circuited on the first file that defines *any* key): `--env-file` (a caller-supplied
path; missing is an `EnvError`) -> `<worktree>/.env` -> every `.env` walking from
`cwd` up to the filesystem root -> `~/.config/typesafe-review/env`. A key already
present in the process environment is never overridden by any file. Only keys
starting with `TYPESAFE_` are loaded.

After loading, `TYPESAFE_BASE_URL` is normalised whatever its source (a file here, or
already set in the process environment): a trailing `/v1/systemone` (with or without
a trailing slash) is stripped, with one stderr line saying so -- the SDK appends that
path itself (ledger H-4: the first live run 404'd because `.env` carried the full
endpoint).

`env.py` declares `EnvError`; DEC-1 then applies to every `open`/`read_text` call
below -- the only one is `_read_env_file`, wrapped in its own `try`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from typesafe_sdk import constants as typesafe_constants

_KEY_PREFIX = 'TYPESAFE_'
_ENDPOINT_SUFFIX = '/v1/systemone'
_HOME_CONFIG_RELATIVE = Path('.config') / 'typesafe-review' / 'env'


class EnvError(Exception):
    """A `--env-file` path was given but does not exist, or could not be read."""


def _parse_env_text(text: str) -> dict[str, str]:
    """Parse `KEY=VALUE` lines: `export KEY=VALUE`, single/double-quoted values,
    `#` comments, blank lines. Returns only keys starting with `TYPESAFE_`."""
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[len('export ') :].strip()
        if '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key.startswith(_KEY_PREFIX):
            result[key] = value
    return result


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text()
    except OSError as e:
        raise EnvError(f'{path}: cannot read env file ({e})') from e
    return _parse_env_text(text)


def _cwd_env_files(cwd: Path) -> list[Path]:
    """Every `.env` from `cwd` up to the filesystem root, closest first."""
    files: list[Path] = []
    current = cwd.resolve()
    while True:
        candidate = current / '.env'
        if candidate.is_file():
            files.append(candidate)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return files


def _normalize_base_url() -> None:
    value = os.environ.get(typesafe_constants.BASE_URL_ENV)
    if not value:
        return
    stripped = value.rstrip('/')
    if not stripped.endswith(_ENDPOINT_SUFFIX):
        return
    normalized = stripped[: -len(_ENDPOINT_SUFFIX)]
    if not normalized:
        return
    os.environ[typesafe_constants.BASE_URL_ENV] = normalized
    print(
        f'env: stripped {_ENDPOINT_SUFFIX} from {typesafe_constants.BASE_URL_ENV} (the SDK appends it itself)',
        file=sys.stderr,
    )


def load_env(
    worktree: Path | None,
    env_file: Path | None,
    *,
    home: Path | None = None,
    cwd: Path | None = None,
    trace: list[str] | None = None,
) -> list[Path]:
    """Load every `TYPESAFE_*` key found across the search order into `os.environ`,
    never overriding a key already set there. Returns the files that contributed at
    least one key. Raises `EnvError` if `env_file` is given but does not exist.

    `home`/`cwd` are injectable for tests; real defaults (`Path.home()`/`Path.cwd()`)
    apply when omitted. If `trace` is given, one line per search location (in search
    order) is appended to it -- `searched: <path>` or `skipped: <label> (not given)`
    -- regardless of whether that location contributed a key; `--doctor` uses this on
    failure to name everywhere it looked.
    """
    resolved_home = home if home is not None else Path.home()
    resolved_cwd = cwd if cwd is not None else Path.cwd()

    contributing: list[Path] = []
    seen: set[Path] = set()

    def _apply(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        parsed = _read_env_file(path)
        contributed = False
        for key, value in parsed.items():
            if key in os.environ:
                continue
            os.environ[key] = value
            contributed = True
        if contributed:
            contributing.append(path)

    # 1. --env-file
    if env_file is not None:
        if trace is not None:
            trace.append(f'searched: {env_file}')
        if not env_file.is_file():
            raise EnvError(f'{env_file}: --env-file not found')
        _apply(env_file)
    elif trace is not None:
        trace.append('skipped: --env-file (not given)')

    # 2. <worktree>/.env
    if worktree is not None:
        worktree_env = worktree / '.env'
        if trace is not None:
            trace.append(f'searched: {worktree_env}')
        if worktree_env.is_file():
            _apply(worktree_env)
    elif trace is not None:
        trace.append('skipped: worktree/.env (not given)')

    # 3. every .env walking from cwd up to the filesystem root
    if trace is not None:
        trace.append(f'searched: .env walk from {resolved_cwd}')
    for candidate in _cwd_env_files(resolved_cwd):
        _apply(candidate)

    # 4. ~/.config/typesafe-review/env
    home_file = resolved_home / _HOME_CONFIG_RELATIVE
    if trace is not None:
        trace.append(f'searched: {home_file}')
    if home_file.is_file():
        _apply(home_file)

    _normalize_base_url()

    return contributing
