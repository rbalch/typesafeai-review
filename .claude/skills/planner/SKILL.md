---
name: planner
description: Plan a piece of work with the human, then write the spec, the task files, and an ADR if the plan chose between real alternatives. Runs in the main session because planning is a conversation. Use at the start of any feature, milestone, or change larger than a one-line fix — before any code is written.
---

# Planner

You are planning, not building. Nothing under `src/`, `tests/`, `controls/` or
`governance/` changes during this skill. Your output is three kinds of document, and
you write none of them until the human says the plan is agreed.

```
conversation ──▶ agreed plan ──▶ docs/specs/<slug>.md
                                 tasks/<slug>/<PREFIX>-NN-<slug>.md   (one per task)
                                 docs/adr/NNNN-<slug>.md       (only if alternatives were rejected)
```

## 0. Read first

- `AGENTS.md` — the project, its architectural shape, the Always/Never lists, and the
  working context. The plan has to fit the shape; if it cannot, that is the first thing
  to raise.
- `governance/views/RULES.md` — the live rules. A plan that will collide with a `DEC-N`
  is either wrong or a supersession, and the human decides which before any task exists.
  Never read `governance/decisions/` for rules.
- `docs/specs/` and `docs/adr/` — prior plans and decisions. Do not re-decide something
  already decided; cite it.
- `make tasks` — anything not yet `done`. A new plan that overlaps unfinished work needs
  to say so. `tasks/<slug>/` is untracked (only `tasks/README.md` is in git); the files
  live on this machine and the merged PRs hold the shipped briefs.

## 1. The conversation

Work with the human until the plan is agreed. Your job is to make the plan precise
enough to break into tasks, and to surface what they have not said.

- **Ask the questions that change the work.** Scope boundaries, what is explicitly out,
  the acceptance the human will actually check, which existing code this must match,
  what happens on failure. Do not ask what you can read from the tree.
- **Push back once, with a reason, then defer.** If a choice looks wrong say so plainly
  in a sentence or two. If the human reaffirms, that is the decision.
- **Name the alternatives when there are real ones.** If the plan picks between two or
  more viable approaches, say which, why, and what was given up. That is what becomes
  the ADR.
- **Check the plan against the rules and the shape.** A plan that crosses a seam in
  `AGENTS.md` or a rule in `RULES.md` is raised here, never discovered by a builder.

Stop iterating when the human says the plan is agreed. Do not write files before that.
Do not ask "shall I write it up?" as a substitute for finishing the conversation.

## 2. The spec — `docs/specs/<slug>.md`

The plan in prose, for a human to read and for tasks to link back to. Short. Sections:

- **Goal** — what exists when this is done, one paragraph.
- **Approach** — how, at the level of components and seams. A diagram if the shape is
  non-obvious.
- **Out of scope** — explicit.
- **Decisions** — choices made in the conversation, each in one line, with the ADR id
  if one was written.
- **Tasks** — the ordered list of task ids and titles, with the dependency edges.
- **Open questions** — anything deferred, and who owns it.

## 3. The tasks — `tasks/<slug>/<PREFIX>-NN-<slug>.md`

Follow the format in `tasks/README.md` exactly. First pick the plan's id prefix: `T` if
no other plan exists, otherwise a short uppercase prefix from the slug that no directory
under `tasks/` already uses (`critic-tooling` → `CT`). Every id in the plan carries that
prefix, and the PR titles will too. Per task:

- **Self-contained.** A builder gets the task file, `AGENTS.md` and `RULES.md`, nothing
  else. Anything it needs beyond those is in the task's Context section, or linked to a
  spec section by heading.
- **Acceptance as runnable checks**, each with the command and expected result. These
  become the tests the builder writes first, so they must describe behaviour at the
  boundary of the task, not internals. "Returns the order or raises `OrderNotFound`" is
  acceptance. "Uses a dataclass" is not.
- **`depends_on` only for merge dependencies.** Ask: can this be built and tested with
  the dependency's code absent? If yes, it is not a dependency. Dependent tasks wait for
  the predecessor to merge; nothing builds on an unmerged branch.
- **`files` honest and complete.** This is the only signal the human has for which
  tasks can run in separate sessions at once.
- **`rules`** — the `DEC-N` ids that plausibly apply, so the builder reads those first.
- **Sized for one review loop.** Split anything that spans architectural layers or that
  a reviewer could not hold in one pass.

Order the ids so a dependency always has a lower number than its dependents.

After writing the files run `make tasks PLAN=tasks/<slug>`. It validates the frontmatter,
the prefix and the dependency edges, and shows every task `ready` or `blocked`. A
message instead of a listing is a planning error to fix before hand-off.

## 4. The ADR — only when earned

Write one only if the conversation chose between real alternatives that a future reader
would plausibly re-litigate. Use the `creating-adrs` skill and its MADR format, into
`docs/adr/`. A plan that had one obvious approach produces no ADR, and that is the
common case.

An ADR records a design choice. It is **not** a governance decision: it has no control,
it is not enforced, and it never appears in `RULES.md`. If the conversation concluded
that something should be *enforced*, that is a finding for `finding-triage`, subject to
the rule of three like anything else. Do not seed a `DEC-N` from a plan.

## 5. Hand-off

Report the spec path, the task ids with their dependency edges, which tasks can run in
parallel with which, and the ADR path if any. Then the two ways to run it:

```
/orchestrate tasks/<slug>            # every task, in dependency order
/orchestrate tasks/<slug>/T-03-*.md  # one task, in a second session, if it is independent
```

---

Plan in conversation, write on agreement, one task per review loop, acceptance at the
boundary, an ADR only when something was actually decided.
