"""Builders for a throwaway ledger, used by the `ledger` fixture in conftest.py.

Writing decisions and controls by hand in every test would bury the thing each test
is actually asserting. These helpers keep the setup to one line per decision.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import build_views as bv


@dataclass
class ControlSpec:
    """One entry for a decision's `controls:` frontmatter."""

    path: str
    type: str = 'fitness_fn'
    enforcement: str = 'block'
    pragma: str = 'supported'
    sha256: str | None = None

    def to_yaml(self) -> str:
        lines = [
            f'  - path: {self.path}',
            f'    type: {self.type}',
            f'    enforcement: {self.enforcement}',
            f'    pragma: {self.pragma}',
        ]
        if self.sha256 is not None:
            lines.append(f'    sha256: {self.sha256}')
        return '\n'.join(lines)


@dataclass
class Ledger:
    """A fake repo root with decisions, controls and generated views."""

    root: Path
    decisions_dir: Path
    views_dir: Path
    written: list[str] = field(default_factory=list)

    def decision(
        self,
        id_: str,
        *,
        title: str | None = None,
        status: str = 'accepted',
        kind: str = 'constraint',
        superseded_by: str | None = None,
        controls: Sequence[ControlSpec] = (),
        rule: str = 'Do the thing.',
        body: str = '',
    ) -> Path:
        # Built line by line rather than with textwrap.dedent: dedent computes the
        # common leading whitespace *after* interpolation, and the injected control
        # YAML is shallower than the template, so it would strip the wrong amount.
        control_yaml = '\n' + '\n'.join(spec.to_yaml() for spec in controls) if controls else ' []'
        frontmatter = '\n'.join(
            [
                '---',
                f'id: {id_}',
                f'title: {title or f"Title for {id_}"}',
                f'status: {status}',
                f'kind: {kind}',
                'created: 2026-07-30',
                f'superseded_by: {superseded_by or "null"}',
                f'controls:{control_yaml}',
                '---',
                '',
                '## Rule',
                rule,
                '',
            ]
        )
        path = self.decisions_dir / f'{id_}-fixture.md'
        path.write_text(frontmatter + body, encoding='utf-8')
        return path

    def control(self, relpath: str, content: str) -> Path:
        """Write a control file at a repo-relative path."""
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    def view(self, content: str, name: str = 'RULES.md') -> Path:
        self.views_dir.mkdir(parents=True, exist_ok=True)
        path = self.views_dir / name
        path.write_text(content, encoding='utf-8')
        return path

    def load(self) -> list[bv.Decision]:
        return bv.load_decisions(self.decisions_dir)

    def build(self) -> dict[Path, str]:
        return bv.build(self.decisions_dir)

    def generate(self) -> None:
        """Run the generator for real, writing into the temp tree."""
        bv.write(self.build())

    def rules_text(self) -> str:
        return (self.views_dir / 'RULES.md').read_text(encoding='utf-8')

    def registry_text(self) -> str:
        return (self.root / 'governance' / 'registry.json').read_text(encoding='utf-8')
