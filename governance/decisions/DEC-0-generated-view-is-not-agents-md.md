---
id: DEC-0
title: No generated file is named AGENTS.md
status: accepted
kind: negative
created: 2026-09-17
superseded_by: null
controls:
  - path: controls/fitness/view_naming.py
    type: fitness_fn
    enforcement: block
    pragma: supported
---

## Rule
No file under `governance/views/` may be named `AGENTS.md`, and `governance/views/RULES.md` must exist.

## Context

`AGENTS.md` is the conventional name for a *hand-written* agent-instructions file, and
this repo has one at the root. Naming the generated view the same thing, with the rule
"agents read only the generated one" balanced on top, is a trap. It fails in both
directions and both failures are quiet:

- Someone edits the generated view believing it is the contract. The next `make views`
  silently discards the edit.
- Someone reads the hand-written contract believing it is the enforced rule set, and
  acts on a rule that no control backs.

The second is worse, because it is indistinguishable from the system working.

**This is a seeded rule, not a discovered one.** The harness's thesis is that rules
earn a control on their third sighting. This one is promoted on sight because it is a
structural naming invariant the design cannot be built without resolving. Recorded as
an exception rather than pretending the rule of three was followed. It is also the only
decision this repo starts with — everything after it should be discovered through
review.

## Consequences

The generated view is `governance/views/RULES.md`. The hand-written contract is
`AGENTS.md` at the repo root, and it never restates a `DEC-N` rule — it points at the
view. The two can never be confused because they can never share a name.

`controls/fitness/view_naming.py` goes **red** if any file under `governance/views/` is
named `AGENTS.md`, or if `RULES.md` is absent. It goes **green** on a tree whose only
generated view is `RULES.md`.

The absence check matters as much as the naming check: an empty `views/` directory looks
exactly like a repo with no rules, and an agent reading nothing cannot tell "no rules
apply" from "the generator never ran."

## Rejected alternatives

- **Name the generated view `AGENTS.md` and drop the hand-written file.** Rejected. A
  working memory of background, architecture and current decisions is worth more than
  purity while a project is young. *Positive recast:* keep both files, give them names
  that cannot collide, and hold the discipline that only one of them states rules.

- **Keep both names and disambiguate by path** (`AGENTS.md` versus
  `governance/views/AGENTS.md`). Rejected. Path disambiguation works for tools and fails
  for humans and agents, who refer to files by name in prose, commit messages, and each
  other's context windows. *Positive recast:* make the names themselves distinct.

- **A comment header in the generated file warning not to edit it.** Rejected as
  insufficient alone — it is advisory, and does nothing for a reader who opens the wrong
  file expecting rules. The header is still emitted; it is just not the control.
