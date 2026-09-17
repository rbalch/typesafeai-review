---
id: T-02
plan: <plan-slug>                  # tasks/<plan-slug>/, matches docs/specs/<plan-slug>.md
title: Add the repository layer for orders
depends_on: [T-01]                 # ids that must be merged first; [] if none (inline or block list)
files:                             # what this task expects to create or edit
  - src/{package_name}/orders/repository.py
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
