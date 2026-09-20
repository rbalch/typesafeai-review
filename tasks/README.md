# Tasks

One directory per plan, one file per task. The `planner` skill writes them; the
`orchestrate` skill consumes them. A task file is a self-contained brief: an agent given
only that file, `AGENTS.md`, and `governance/views/RULES.md` has everything it needs.

**`tasks/<slug>/` is untracked.** Only this README is in git. Task files are working
notes on the machine running the plan; agents in worktrees read them by absolute path
from the root checkout. The permanent record of a brief is its PR, which carries the
task file verbatim in a collapsed block. Status is never stored either: `make tasks`
derives it from PR state.

```
tasks/
├── <plan-slug>/
│   ├── T-01-<slug>.md
│   ├── T-02-<slug>.md
│   └── ...
└── <another-plan>/
    ├── CT-01-<slug>.md          # each plan owns its own id prefix
    └── ...
```

## Ids

A task id is `<PREFIX>-NN`: an uppercase prefix, a dash, a zero-padded number, optionally
followed by a single lowercase letter (`T-01b`) to slot a follow-up task in after an
existing one without renumbering. **Each
plan owns one prefix and no two plans share one.** The first plan in a repo uses `T`;
later plans pick a short prefix from their slug (`critic-tooling` → `CT`). The prefix is
the only thing that ties a PR titled `CT-01: …` back to its plan, so `make tasks`
refuses a plan whose files mix prefixes or whose prefix another plan already uses.

The file name is `<id>-<slug>.md`, so a directory sorts in id order and a dependency
always has a lower number than its dependents.

## File format

```markdown
---
id: T-02
plan: <plan-slug>                  # tasks/<plan-slug>/, matches docs/specs/<plan-slug>.md
title: Add the repository layer for orders
depends_on: [T-01]                 # ids that must be merged first; [] if none (inline or block list)
files:                             # what this task expects to create or edit
  - src/typesafe_review/orders/repository.py
  - tests/orders/test_repository.py
rules: [DEC-0]                     # DEC ids from RULES.md that plausibly apply; [] if none
---

## Goal

One paragraph. What exists when this task is done that does not exist now, and why the
plan needs it.

## Scope

1. Numbered, concrete, checkable items.
2. Each one is something a reviewer can confirm is present or absent.

## Non-scope

- What a builder will be tempted to do and must not. Later tasks, adjacent refactors,
  "while I'm here" cleanups.

## Acceptance

Runnable checks, each with the command and the expected result. These are the contract
the builder writes tests against **before** implementing. If an acceptance criterion
cannot be expressed as a test, say so and name what a human checks instead.

- `uv run pytest tests/orders/test_repository.py -q` → exit 0, covers: create, get by id,
  get missing raises `OrderNotFound`
- `make check` → exit 0

## Context

Facts a fresh agent cannot derive from the tree: prior decisions from the spec, the
shape of neighbouring code it should match, external constraints, what was tried and
rejected. Link the spec section rather than restating it at length.

## Manual QA

What the human looks at after merge, if anything, and what "correct" looks like.
`None.` is a valid answer.
```

## Rules

- **`depends_on` is a merge dependency**, not a "nice to have first". A task lists a
  dependency only when it cannot be built or tested without that task's code present.
  Dependent tasks wait; they never build speculatively on an unmerged branch.
- **`files` is the parallelism signal.** Two tasks with no dependency and disjoint
  `files` can run in separate sessions at the same time. Overlapping `files` means run
  them in order, even with no logical dependency. The list is an expectation, not a
  fence; a builder that needs to touch something else reports it.
- **Acceptance criteria are the test contract.** The builder turns them into tests
  first and watches them fail. If a criterion turns out to encode a wrong assumption,
  that is a planning error to report, not a test to quietly rewrite.
- **Status is derived, never stored.** `make tasks PLAN=tasks/<slug>` reads PR state:
  a merged PR titled `<id>: …` is `done`, an open one `in_review`, all dependencies
  done `ready`, otherwise `blocked`. `make tasks` with no `PLAN` reports every plan.
  Nobody edits a task file to change its status. Builders never edit task files at all.
- **`make tasks` is the format check.** It exits with a message, not a traceback, when
  the plan directory is missing, has no task files, a file lacks `id`/`title`/
  `depends_on`, an id is malformed, prefixes are mixed or shared, or `depends_on` names
  an id the plan does not have. The planner runs it once after writing the files.
- **Small enough for one review loop.** If a task needs more than roughly one day of
  human-equivalent work, or touches more than one architectural layer, the planner splits
  it.
