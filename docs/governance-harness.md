# The governance harness — what it is, and how you know if it is working

Read this once. The day-to-day mechanics live in the `ledger-ops` skill; the triage
procedure lives in `finding-triage`. This file is the framing that makes both of them
make sense.

## What it is not

**It is not a code-taste oracle.** Do not try to encode subjective quality —
"readability", "elegance", "is this good code" — as a control. Every attempt produces
either a dumb proxy (line count, cyclomatic complexity) that fires on fine code and
misses bad code, or a false-confidence machine. Taste stays where it belongs: a human
looks at the diff and decides whether they like it.

## What it is — exactly two things

**1. An architectural-invariant enforcer.** Not line-level style; linters already do
that. Systemic, cross-file rules a linter cannot express:

- the API layer never imports the DB layer directly
- no module reaches into another module's internals
- dependencies flow one direction
- every route handler passes through auth
- secrets never cross a named boundary

These are objective, structural, and exactly what coding agents violate confidently and
often. That is the `fitness_fn` control type.

**2. A ratchet for review findings.** This is the real reason to build it. Normally,
every time a human reviews agent output and says "no, don't do that," the correction
evaporates — the next session repeats the mistake and you review it again, forever. The
harness gives you one move you do not otherwise have: **turn a recurring correction into
a permanent control, once, so you never review for it again.**

A lot of what feels like taste is a concrete repeatable pattern that had not been named
yet. "Don't catch-and-swallow exceptions", "one config format", "DB access only through
repositories" all *feel* subjective and are rule-shaped once articulated. The harness
exists to catch the articulable ones.

## The three layers

```
DECISION (the why)          CONTROL (the teeth)        VIEW (what agents read)
governance/decisions/  ──►  controls/fitness/*.py ──►  governance/views/RULES.md
DEC-N-<slug>.md             fails CI when violated     GENERATED, live rules only
      │                            ▲
      └── frontmatter: controls ───┴── pragma: "governance: enforces DEC-N"
```

Decision frontmatter names its control paths. Each control carries a pragma naming its
decision. `check_governance.py` refuses to pass unless both directions line up.

**Agents read only the generated view.** `governance/decisions/` retains superseded
records on purpose — history is for humans. A superseded rule in an agent's context
steers it toward the exact pattern you abandoned, and the `superseded` label does not
help, because the presence of the text does the damage.

## The rule of three

A dislike does not become a control on first sighting.

- **Soft layer** — a note, an `AGENTS.md` nudge, a one-off correction. Cheap, instant,
  no CI. Every dislike starts here.
- **Hard layer** — a decision plus an executable control. Expensive, permanent,
  CI-enforced.

Keep it soft until the third sighting. A third sighting proves it is recurring *and*
articulable. Anything that never recurs stays soft or dies, which is correct — it was
never worth a fitness function.

## The falsifiable test

Every dislike sorts into one of three bins: **1** already lintable, **2** articulable as
a fitness function, **3** genuine taste.

**If Bin 2 stays fat and review burden measurably shrinks, the harness earns its keep.
If nearly everything collapses into Bin 1 or Bin 3, it is complicated linters plus a
wiki, and it should be dropped.**

So do not evaluate this by "does CI go red." Evaluate it by "did the middle bin turn out
to be real, and did my review load go down." That means the bin distribution has to be
recorded honestly in `docs/ledger-findings.md`, including when it is unflattering. If
after weeks of real use nothing has graduated to the hard layer, that is a finding, not
a failure of diligence.

## The failure modes worth naming

- **A control that fires on correct code** is the worst thing that can happen here. It
  teaches everyone to route around the harness and discredits every other rule. Worse
  than the nit it was meant to catch.
- **Silent evasion** — a threshold raised, a pragma deleted, a file moved out of a scan
  path, a `# noqa` added. CI stays green and nobody finds out for weeks. Changing a rule
  is meant to be a visible supersession diff a human reviews.
- **Ceremony** — a control nobody reads the output of, or a hash re-recorded reflexively
  without reading the diff. When that happens, supersede the decision rather than
  keeping the ritual.
