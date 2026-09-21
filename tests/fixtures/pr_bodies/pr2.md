Changed:
- src/typesafe_review/taskfile.py: load_task(path) -> frozen Task(id, title,
  acceptance, criteria_text); yaml.safe_load frontmatter; bullets under
  `## Acceptance` with indented continuations joined by a space
- taskfile.py: every missing or malformed input, including an unreadable path,
  raises TaskFileError naming the path and what was missing; never an empty Task
- tests/fixtures/tasks/T-02-example.md: the planner README example as first fixture
- tests/test_taskfile.py: 9 tests
Why: the per-hunk and change-wide state both need the task block (spec §4.2, §4.3)
Watch out:
- criteria_text is the raw section body, untrimmed, up to the next `## ` line
- pyyaml is still in the dev group on this branch; T-01 (PR #1) moves it to runtime
Evidence: 9 tests in tests/test_taskfile.py, make check exit 0, red-then-green on 774c03d
Check by hand:
- nothing


Spec: docs/specs/typesafe-reviewer.md §3.1, §4.2, §4.3

<details><summary>Task brief T-02</summary>

```markdown
---
id: T-02
plan: typesafe-reviewer
title: Parse planner task files into the `task` state block
depends_on: []
files:
  - src/typesafe_review/taskfile.py
  - tests/test_taskfile.py
  - tests/fixtures/tasks/
rules: []
---

## Goal

A function turns a planner task file into the `task` dict the spec's state shapes need:
`{id, title, acceptance: [str], criteria_text: str}`. Missing or malformed input is an
explicit error, never an empty task.

## Scope

1. `taskfile.py`: `load_task(path: Path) -> Task` where `Task` is a frozen dataclass
   with `id`, `title`, `acceptance` (list of criterion strings), `criteria_text` (the
   raw `## Acceptance` section body: every character after the `## Acceptance`
   heading line up to, not including, the next line starting with `## `, or end of
   file; no trimming).
2. Frontmatter parsed with `yaml.safe_load` between the first two `---` lines. `id` and
   `title` required.
3. Acceptance criteria are the top-level bullets under `## Acceptance` up to the next
   `## ` heading. Continuation lines (indented) join their bullet with a space.
4. Errors: no frontmatter, missing `id`/`title`, no `## Acceptance` section, or zero
   bullets → `TaskFileError` with the path and what was missing.

## Non-scope

- Reading `status`, `depends_on`, `files`, `rules`. The reviewer does not need them.
- Validating that criteria are runnable. That is the planner's job.

## Acceptance

- `uv run pytest tests/test_taskfile.py -q` → exit 0, covers: the README example task
  parses to two criteria; a multi-line bullet joins; missing `## Acceptance` raises
  `TaskFileError`; empty bullet list raises; no frontmatter raises; missing `id` or
  `title` raises; `criteria_text` is byte-identical to the section body as defined in
  scope 1.
- `make check` → exit 0.

## Context

- `pyyaml` is a runtime dependency after T-01; if T-01 is not merged yet it is still
  importable from the dev group.
- Format: `tasks/README.md` in `/home/ryan/code/new-project/assets/`. Copy its example
  into `tests/fixtures/tasks/T-02-example.md` as the first fixture.
- Spec §4.2 (`task` block per hunk) and §4.3 (`criteria_text` for change-wide state).

## Manual QA

None.

```

</details>

🤖 Generated with [Claude Code](https://claude.com/claude-code)

