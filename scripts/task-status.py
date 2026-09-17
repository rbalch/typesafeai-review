#!/usr/bin/env python3
"""Print the live status of every task in a plan directory.

Status is derived, not stored: a task is ``done`` when a PR titled ``<id>: ...``
has merged, ``in_review`` when one is open, ``blocked`` when a dependency is not
done, and ``ready`` otherwise. Usage::

    make tasks PLAN=tasks/<plan-slug>   # one plan
    make tasks                          # every plan under tasks/

Task ids are ``<PREFIX>-NN``. Each plan owns one uppercase prefix (``T``, ``CT``,
...) and no two plans share one, because a PR title carries the id alone and
nothing else says which plan it belongs to. See tasks/README.md.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

FRONT = re.compile(r'^---\n(.*?)\n---', re.DOTALL)
ID = re.compile(r'^([A-Z]+)-(\d+)$')
PR_TITLE = re.compile(r'([A-Z]+-\d+):')
REQUIRED = ('id', 'title', 'depends_on')
ICON = {'done': '✅', 'in_review': '👀', 'ready': '🟢', 'blocked': '⛔'}


@dataclass
class Task:
    id: str
    title: str
    depends_on: list[str] = field(default_factory=list)

    @property
    def prefix(self) -> str:
        return self.id.split('-', 1)[0]


def frontmatter(path: Path) -> dict[str, str]:
    """Parse the ``---`` block into flat strings. Block lists (``- x`` lines) are
    joined into ``[x, y]`` so they read the same as inline lists."""
    m = FRONT.match(path.read_text())
    if not m:
        raise SystemExit(f'{path}: no frontmatter')
    out: dict[str, list[str]] = {}
    key = ''
    for line in m.group(1).splitlines():
        line = line.split('#', 1)[0].rstrip()
        if not line.strip():
            continue
        if line[0].isspace():  # continuation: a block-list item under the current key
            if key:
                out[key].append(line.strip().lstrip('- ').strip())
            continue
        key, v = line.split(':', 1) if ':' in line else (line.strip(), '')
        key = key.strip()
        out[key] = [v.strip()] if v.strip() else []
    return {k: v[0] if len(v) == 1 else '[' + ', '.join(v) + ']' for k, v in out.items()}


def parse_list(raw: str) -> list[str]:
    return [d.strip() for d in raw.strip('[]').split(',') if d.strip()]


def task_files(plan: Path) -> list[Path]:
    return [p for p in sorted(plan.glob('*.md')) if p.name != 'README.md']


def plan_dirs(tasks_root: Path) -> list[Path]:
    return sorted(p for p in tasks_root.iterdir() if p.is_dir() and task_files(p))


def load_plan(plan: Path, tasks_root: Path = Path('tasks')) -> list[Task]:
    """Read one plan directory. Every way a plan can be malformed exits with a
    message naming the file and the rule, never a traceback."""
    if not plan.is_dir():
        known = ', '.join(f'tasks/{p.name}' for p in plan_dirs(tasks_root)) or 'none'
        raise SystemExit(f'{plan}: not a plan directory. Plans found: {known}')
    files = task_files(plan)
    if not files:
        raise SystemExit(f'{plan}: no task files (expected <PREFIX>-NN-<slug>.md)')

    tasks: list[Task] = []
    for path in files:
        fm = frontmatter(path)
        missing = [k for k in REQUIRED if k not in fm]
        if missing:
            raise SystemExit(f'{path}: frontmatter is missing {", ".join(missing)}')
        if not ID.match(fm['id']):
            raise SystemExit(f'{path}: id {fm["id"]!r} must look like T-01 (<PREFIX>-NN)')
        tasks.append(Task(id=fm['id'], title=fm['title'], depends_on=parse_list(fm['depends_on'])))

    prefixes = {t.prefix for t in tasks}
    if len(prefixes) > 1:
        raise SystemExit(f'{plan}: one id prefix per plan, found {sorted(prefixes)}')
    prefix = prefixes.pop()
    for other in plan_dirs(tasks_root):
        if other.resolve() == plan.resolve():
            continue
        other_prefixes = {frontmatter(p).get('id', '').split('-', 1)[0] for p in task_files(other)}
        if prefix in other_prefixes:
            raise SystemExit(f'{plan}: id prefix {prefix} is already used by tasks/{other.name}; pick another')

    ids = {t.id for t in tasks}
    for t in tasks:
        unknown = [d for d in t.depends_on if d not in ids]
        if unknown:
            raise SystemExit(f'{plan}/{t.id}: depends_on names unknown task(s) {unknown}')
    return tasks


def pr_index() -> dict[str, dict]:
    """Map task id -> PR record. Merged beats open beats closed if a task has several."""
    raw = subprocess.run(
        ['gh', 'pr', 'list', '--state', 'all', '--limit', '200', '--json', 'number,title,state,url'],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    rank = {'MERGED': 2, 'OPEN': 1, 'CLOSED': 0}
    index: dict[str, dict] = {}
    for pr in json.loads(raw):
        m = PR_TITLE.match(pr['title'])
        if not m:
            continue
        tid = m.group(1)
        if tid not in index or rank[pr['state']] > rank[index[tid]['state']]:
            index[tid] = pr
    return index


def derive_status(tasks: list[Task], prs: dict[str, dict]) -> dict[str, str]:
    status: dict[str, str] = {}
    for t in tasks:  # sorted by id, deps always point backwards
        pr = prs.get(t.id)
        if pr and pr['state'] == 'MERGED':
            status[t.id] = 'done'
        elif pr and pr['state'] == 'OPEN':
            status[t.id] = 'in_review'
        elif all(status.get(d) == 'done' for d in t.depends_on):
            status[t.id] = 'ready'
        else:
            status[t.id] = 'blocked'
    return status


def report(plan: Path, tasks: list[Task], prs: dict[str, dict]) -> None:
    status = derive_status(tasks, prs)
    width = max(len(t.title) for t in tasks)
    print(f'# {plan}')
    for t in tasks:
        s = status[t.id]
        pr = prs.get(t.id)
        tail = f'PR #{pr["number"]}' if pr else ''
        if s == 'blocked':
            waiting = [d for d in t.depends_on if status.get(d) != 'done']
            tail = f'waits on {", ".join(waiting)}'
        print(f'{ICON[s]} {t.id}  {s:<10} {t.title:<{width}}  {tail}')

    counts = {s: sum(1 for v in status.values() if v == s) for s in ICON}
    print('\n' + '  '.join(f'{k}={v}' for k, v in counts.items() if v))
    ready = [t.id for t in tasks if status[t.id] == 'ready']
    if ready:
        print(f'next: {", ".join(ready)}')


def main(argv: list[str]) -> None:
    root = Path('tasks')
    plans = [Path(argv[1])] if len(argv) > 1 and argv[1] else plan_dirs(root)
    if not plans:
        raise SystemExit('no plans under tasks/; the planner skill writes them')
    loaded = [(p, load_plan(p, root)) for p in plans]  # validate every plan before calling gh
    prs = pr_index()
    for i, (plan, tasks) in enumerate(loaded):
        if i:
            print()
        report(plan, tasks, prs)


if __name__ == '__main__':
    main(sys.argv)
