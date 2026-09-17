---
name: ledger-ops
description: Mechanics of the ledger governance harness in this repo — author a decision, supersede a rule, add a control and its pragma, rebuild the generated view, and run the integrity check. Use whenever a rule needs to be created, changed, retired, or debugged, or when check-governance fails.
---

# Ledger operations

The harness has three layers, tethered both ways so nothing drifts silently.

```
DECISION (the why)          CONTROL (the teeth)        VIEW (what agents read)
governance/decisions/  ──►  controls/fitness/*.py ──►  governance/views/RULES.md
DEC-N-<slug>.md             fails CI when violated     GENERATED, live rules only
      │                            ▲
      └── frontmatter: controls ───┴── pragma: "governance: enforces DEC-N"
```

Decision frontmatter names its control paths. Each control file carries a pragma
naming its decision. `check_governance.py` refuses to pass unless both directions
line up.

## The rule that matters most

**Read rules from `governance/views/RULES.md`. Never from
`governance/decisions/`.** The decisions directory retains superseded records by
design. A superseded rule in context steers toward the exact pattern we abandoned —
and a `superseded` label does not help, because the mere presence of the abandoned
text does the damage.

## Authoring a decision

`governance/decisions/DEC-N-<slug>.md`, next free integer for N:

```markdown
---
id: DEC-7
title: Bind listeners to loopback only
status: accepted            # draft | accepted | superseded
kind: negative              # constraint | negative | preserved
created: 2026-01-15
superseded_by: null
controls:
  - path: controls/fitness/loopback_only.py
    type: fitness_fn         # lint | fitness_fn | test | policy | config
    enforcement: block       # block | warn
    pragma: supported        # supported | external
---

## Rule
State it so a machine could check it. One behavior per decision.

## Context
The force that produced the rule.

## Consequences
What passes, what fails, how the control proves it — red on X, green on Y.

## Rejected alternatives
- **The thing we did not do** — rejected. *Positive recast:* what we do instead.
```

Field notes:

- `kind: negative` is "must NOT do X"; `kind: preserved` is "must CONTINUE to do X".
  Negative space is first-class and gets the same anchoring as any constraint.
- `enforcement: warn` is the escape hatch for a rule you cannot automate yet. The
  view marks it advisory. Prefer `block`. A rule with no control at all should be
  rare and justified in the body.
- `pragma: external` is for artifacts that cannot hold a comment — strict JSON, a
  pinned config. The integrity check matches those by path plus a recorded content
  hash instead of a comment. Powerful and noisy: the hash covers the whole file, so
  any unrelated edit turns it red. Use it sparingly, and never re-record a hash
  without reading the diff — that reflex is what turns a control into ceremony.
- **One behavior, one decision.** Never state a rule in two decisions. Reference
  the ID.

## The pragma

One line in each `pragma: supported` control, in that language's comment syntax:

```python
# governance: enforces DEC-7
```

Several decisions, one control: `# governance: enforces DEC-7, DEC-9`.

`governance:` is a reserved namespace — never reuse it for anything else. The
pragma survives file moves because paths are resolved fresh, and it makes
circumvention detectable: gut the control's logic and the dangling pragma still
flags.

## Superseding a rule

The only legal way to change a rule. Never edit code to evade a control, and never
quietly loosen one.

1. Author `DEC-N+1` with the new rule.
2. On the old decision set `status: superseded` and `superseded_by: DEC-N+1`.
3. Add or modify the control(s) and their pragmas so CI enforces the new rule.
4. `make views`
5. `make governance`
6. Commit all of it in one change. A human reviews that diff.

**The supersession diff is the guardrail.** Rule change is meant to be a visible,
reviewable act. Enforcement is automated; changing what is enforced is not.

## Commands

```bash
make views       # regenerate views/RULES.md + registry.json from decisions/
make governance  # the integrity + drift check
make check       # controls → views --check → governance → tests
```

Never hand-edit `governance/views/**` or `governance/registry.json`. They are
generated from the decision files, and CI rebuilds them and fails on any
difference — which is how "forgot to rebuild" and "edited a generated file" are
both caught.

## Debugging a check-governance failure

The nine checks, and what each failure actually means:

| Failure | Meaning |
|---|---|
| Accepted decision with no control listed | The rule has no teeth. Add a control or mark `enforcement: warn` with justification. |
| Listed control path missing on disk | Control deleted or moved without updating frontmatter. |
| Control missing its pragma | Someone stripped the tether. Often the first sign of evasion. |
| Pragma points at a missing/superseded/draft decision | Rot or circumvention. Fix the pragma or finish the supersession. |
| `pragma: external` hash mismatch | A comment-less control changed. Re-review the decision, then update the hash. |
| `views/` differs from a fresh build | Stale generated file, or someone hand-edited it. Run `make views`. |
| Superseded/draft text leaked into `views/` | Generator bug. **Fix immediately** — this is the failure the whole design exists to prevent. |
| Duplicate `id`, or `superseded_by` dangling | Bad bookkeeping in a supersession. |
| Control suite fails | Either real code violation, or a control that fires on correct code. Check which before "fixing" the code. |

A control that fires on correct code is the most damaging failure mode in this
system — it teaches everyone to route around the harness. Treat it as urgent and
consider whether the rule was Bin 3 taste all along.

## Do not write rules speculatively

If asked to add rules in bulk, refuse and say why. The value is *discovered*
through review, not predicted. Rules imagined in advance are usually taste dressed
as controls: brittle, firing on fine code, pure ceremony. Route the request through
the `finding-triage` skill instead.

---

Decisions are the canon, controls are the teeth, the view is the only thing agents
read. Change a rule by supersession, never by edit.
