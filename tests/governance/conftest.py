"""Pytest fixtures for the governance harness tests.

The `Ledger` helper itself lives in `ledger.py` so the test modules can import its
types; this file only wires it up as a fixture.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import build_views as bv
import check_governance as cg
import pytest

from .ledger import Ledger


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Ledger]:
    """A throwaway ledger in a temp directory, for testing the harness against itself.

    Every path constant in `build_views` and `check_governance` is repointed at the
    temp tree, so a test can author a broken decision without touching the real one.
    Repointing rather than writing fixtures into the repo also keeps check 4 -- which
    walks the whole tree looking for pragmas -- from tripping over test scaffolding if
    a test crashes mid-run.
    """
    decisions_dir = tmp_path / 'governance' / 'decisions'
    views_dir = tmp_path / 'governance' / 'views'
    decisions_dir.mkdir(parents=True)
    views_dir.mkdir(parents=True)

    patches: dict[Any, dict[str, Any]] = {
        bv: {
            'REPO_ROOT': tmp_path,
            'DECISIONS_DIR': decisions_dir,
            'VIEWS_DIR': views_dir,
            'RULES_PATH': views_dir / 'RULES.md',
            'REGISTRY_PATH': tmp_path / 'governance' / 'registry.json',
        },
        cg: {
            'REPO_ROOT': tmp_path,
            'VIEWS_DIR': views_dir,
        },
    }
    for module, attributes in patches.items():
        for name, value in attributes.items():
            monkeypatch.setattr(module, name, value)

    yield Ledger(root=tmp_path, decisions_dir=decisions_dir, views_dir=views_dir)
