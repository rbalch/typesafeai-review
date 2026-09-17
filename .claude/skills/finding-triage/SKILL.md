---
name: finding-triage
description: Sort a human's dislike of agent output into already-lintable, articulable-as-a-control, or genuine taste — then apply the rule of three before promoting anything to a CI-enforced control. Use whenever the user rejects a pattern, says "don't do that", or asks for a new rule. Also logs the observation for the ledger.
---

# Triaging a finding

This skill is the heart of the harness. The ledger is only worth keeping if the
**middle bin is real**. Every dislike gets sorted, and honest sorting matters more
than control coverage.

## The three bins

**Bin 1 — already lintable.** An existing linter, formatter, or type-checker covers
it. The harness adds nothing. Say so, configure the linter, move on.

**Bin 2 — articulable as a fitness function.** A concrete, checkable, systemic
pattern. **This is the value.** A lot of what feels like taste is actually a
repeatable rule that had not been named yet — "don't catch-and-swallow
exceptions", "one config format", "DB access only through repositories" all *feel*
subjective but are rule-shaped once articulated.

**Bin 3 — genuine subjective taste.** "Is this readable", "is this elegant". No
control will ever catch it. **Say so plainly and leave it to human judgment.** Do
not manufacture a brittle control just to have one.

Line-level style is Bin 1. What belongs in Bin 2 is *systemic and cross-file* — the
things a linter cannot express and that agents violate confidently and often:

- layer A never imports layer B
- no module reaches into another module's internals
- dependencies flow one direction
- every handler passes through auth
- secrets never cross a named boundary

## The rule of three

A dislike does **not** become a control on first sighting.

- **Soft layer** — a note, an `AGENTS.md` nudge, a one-off correction. Cheap,
  instant, no CI. Every dislike starts here.
- **Hard layer** — a decision plus an executable control. Expensive, permanent,
  CI-enforced.

**Keep it soft until the third sighting.** A third sighting proves it is recurring
*and* articulable — not a one-off, not pure taste. Anything that never recurs stays
soft or dies, which is correct: it was never worth a fitness function.

Resist pressure to promote early, including your own eagerness to be useful. A
premature control that fires on correct code does more damage than the nit it was
meant to catch.

## Procedure

1. **Restate the dislike** as a checkable claim. If you cannot state it so a
   machine could check it, that is strong evidence for Bin 3.
2. **Assign the bin.** Say which and why, in one sentence.
3. **Bin 1** → point at the linter rule that covers it. Done.
4. **Bin 3** → say plainly that no control will catch this and it stays a human
   call. Done. This is a *good* outcome, not a failure.
5. **Bin 2** → check `docs/ledger-findings.md` for prior sightings.
   - Fewer than three → log the sighting, apply the soft correction, stop.
   - Third sighting → propose a decision plus control. Hand off to the
     `control-author` agent, or follow the `ledger-ops` skill.
6. **Log it either way** (see below). The log *is* the experiment; an unlogged
   finding is a lost data point.

## Logging

Append to `docs/ledger-findings.md`:

```markdown
### F-<n> — <one-line description>
- **Date:** YYYY-MM-DD
- **Bin:** 2
- **Claim:** <the dislike, stated so a machine could check it>
- **Sightings:** 2 (F-3 was the first)
- **Action:** soft — noted, no control yet
- **Notes:** <anything surprising about the harness itself>
```

## What the log is for

The falsifiable test of this whole harness: **if Bin 2 stays fat and review burden
measurably shrinks, the system earns its keep. If nearly everything collapses into
Bin 1 or Bin 3, it is complicated linters plus a wiki and should be dropped.**

So do not evaluate the harness by "does CI go red." Evaluate it by "did the middle
bin turn out to be real, and did review load go down." That means the bin
distribution has to be recorded honestly, including when it is unflattering.

Also worth logging as `**Notes:**` whenever they happen, because they are findings
about the harness rather than about the code:

- pressure to promote a one-off before three sightings
- a supersession that felt like friction rather than a guardrail
- a control that fired on correct code
- a case where `check-governance` caught something real, versus ceremony on green

If after weeks of real use nothing has graduated to the hard layer, that is a
finding, not a failure of diligence. Write it down and say it out loud.

---

Sort honestly, keep it soft until the third sighting, log every one. Refusing to
write a brittle rule is worth more than coverage.
