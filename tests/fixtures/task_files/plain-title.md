---
id: RA-CORPUS-01
plan: run-anywhere
title: Plain title with no special characters
depends_on: []
files:
  - see `src/foo.py` for details
rules: []
---

## Goal

Corpus fixture (RA-02b round 3): a task file with the plain shape -- no backtick, no
`: ` in the title -- to prove the round-trip test's baseline case, tracked so CI
exercises it even when `tasks/` is absent.

## Acceptance

- one bullet is enough for this fixture
