"""Test-session isolation for RA-01's implicit env loading.

`cli.main` now calls `env.load_env` unconditionally, before anything else reads the
environment -- including a walk from `cwd` up to the filesystem root and
`~/.config/typesafe-review/env`. Every test here runs inside a worktree nested under
this repo's own checkout, which carries a real `.env` for local `ts-review` runs;
without isolation, an ordinary test that invokes `cli.main` with no `--record`/
`--replay` would pick up a real `TYPESAFE_API_KEY` from that outer `.env` and could
make a live network call -- exactly what `AGENTS.md` rules out for this suite. This
autouse fixture keeps every test's `HOME` and cwd inside a throwaway `tmp_path`, and
strips any `TYPESAFE_*` variable the invoking shell had already set, so no test can
reach a real credential by accident.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith('TYPESAFE_'):
            monkeypatch.delenv(key, raising=False)
    home = tmp_path_factory.mktemp('home')
    cwd = tmp_path_factory.mktemp('cwd')
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(cwd)
