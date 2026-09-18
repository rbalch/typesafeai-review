# typesafeai-review — agent contract

A code reviewer built as AI-powered software, not an LLM agent. Code runs the checks,
slices the diff, asks Jev (TypeSafe's System One model) narrow typed questions per
hunk, and composes verdict, score and findings in code. The model returns numbers; the
report is written from templates. Spec: `docs/specs/typesafe-reviewer.md`.

This repo runs a ledger governance harness: architectural rules live as decisions,
each decision is backed by an executable control, and CI fails on drift. Read
`docs/governance-harness.md` once for why.

## Read this first

**Binding rules live in `governance/views/RULES.md`.** It is generated. Read it before
writing code. Every rule in it is enforced by CI; violating one fails the build.

**Never read `governance/decisions/` for rules.** It retains superseded records on
purpose. History is for humans; the view is for you. The generated view is `RULES.md`,
not `AGENTS.md`, so it can never be confused with this file (DEC-0).

**This file never restates an enforced rule.** The `Always` / `Never` lists below are
the shape of the design. The enforced wording lives in the view, and the view wins.

## The contract

- **Never edit code to evade a control.** Supersede it: author `DEC-N+1`, mark the old
  one `status: superseded` with `superseded_by`, retarget the control and pragma, run
  `make views`, commit together. Changing a rule is a visible act.
- **New rules ship with controls**, or are marked `enforcement: warn` with a reason.
- **One behavior, one decision.** Never restate a rule in two places.
- **Do not author rules speculatively.** Run `finding-triage` first. Only the
  articulable, recurring middle earns a control; the rule of three applies.

## How work gets done here

`planner` turns a conversation into a spec and task files. `orchestrate` turns task
files into reviewed, squashed PRs against `develop`, and feeds every finding into the
ledger.

```
/orchestrate tasks/typesafe-reviewer   ORCHESTRATOR (you, on develop, never in a worktree)
  builder (worktree)      acceptance tests RED → GREEN
  boundary-reviewer       live rules + architectural seams
  reviewer                red-then-green proof, correctness, tests, shape
  findings → you judge → builder → re-review → APPROVE, score ≥ 4/5
  squash → push → PR to develop → triage every finding into docs/ledger-findings.md
```

- **Acceptance tests first**, committed alone, watched failing. A wrong criterion is a
  planning finding, never a test to rewrite.
- **Branches.** One PR per task, one squashed commit, to `develop`. `develop` → `main`
  is a human's PR. A task whose dependency is unmerged waits.
- **Task status is derived**, never stored: `make tasks PLAN=tasks/typesafe-reviewer`.
  Task files are untracked; the PR carries each brief verbatim.
- **Triage is the point.** Bin 1 lintable / Bin 2 systemic / Bin 3 taste. Third
  sighting of a Bin 2 finding → `control-author`. Skipping triage discards the data.
- **Skip the loop** for a one-line fix or a doc edit. `make check`, commit, still
  triage what you disliked.

### Things that will bite you

- `make check` fails fast; re-run the whole gate after a fix.
- Touched a decision? `make views`, or the next task's gate goes red.
- Reviewers start in the root checkout. Every reviewer brief opens with the worktree path.
- Blocked by a rule is a valid outcome. Say so and stop; no thresholds, no `# noqa`.

## Background

- Origin and reference: `/home/ryan/code/new-project/`, a Claude Code skill that scaffolds Python projects with
  a planner → tasks → orchestrate loop. That loop dispatches two LLM reviewer subagents
  per task. This package is an experiment to replace one of them
  (`assets/.claude/agents/reviewer.md` there) with a deterministic, calibrated tool.
  Read that file, `boundary-reviewer.md`, and `assets/.claude/skills/orchestrate/SKILL.md`
  there when you need the contract this tool must honour.
- Ships as a standalone dev dependency (`uv run ts-review`), never vendored into the
  target codebase. Scaffolded projects will add it to their `dev` group once it works.
- Contract with the orchestrator: writes `ts-review.md` / `ts-review.json`, same shape
  as the LLM reviewer's `review.md` / `review.json` (distinct names so both can run on
  one worktree), plus `question_id` and `probability` per finding. Exit codes: 0 APPROVE,
  2 CHANGES_REQUESTED, 3 NEEDS_HUMAN, 1 tool failure.
- The `new-project` flow is untouched until this works on real diffs. Test targets are
  the user's other repos, passed via `--worktree`.

## TypeSafe facts you need

- Docs index: https://docs.typesafe.ai/llms.txt . Read pages as `<url>.md`.
- Endpoint `POST https://api.typesafe.ai/v1/systemone`; body `{state, model, questions}`.
- Python: `uv add typesafe-sdk` (3.10+). `TypeSafeClient` / `AsyncTypeSafeClient`,
  `client.system_one(state, questions)`. Question objects `Noul`, `Choice`, `Score`;
  answers under `result.nouls / .choices / .scores` keyed by question id.
- Noul → `noul` 0..1 (no separate confidence). Choice → `choice, probabilities,
  confidence`. Score → `score` (expected value over levels, 2–10 levels),
  `probabilities, confidence`. Confidence zones from the docs: >0.9 act, 0.5–0.9 review,
  <0.5 escalate. Calibrate; do not trust defaults.
- `instructions` and `criteria` accept JSON structure (`{question, inspect, focus}`,
  Noul `criteria={true:…, false:…}`). Point at state paths with backticks.
- Send every question for one state in one request; they run in parallel.
- No documented cap on state size or question count. Measure `usage.input_tokens`.
- SDK env: `TYPESAFE_API_KEY`, `TYPESAFE_BASE_URL`, `TYPESAFE_DEFAULT_MODEL`
  (default `jev-latest`), `TYPESAFE_LOG_LEVEL`. `TYPESAFE_BASE_URL` is
  `https://api.typesafe.ai` — the SDK appends `/v1/systemone` itself; `env.py` strips
  that suffix if a file carries the full endpoint (ledger H-4). The repo `.env` sets
  them; `env.load_env` also finds `--env-file`, `<worktree>/.env`, and
  `~/.config/typesafe-review/env` (`uv run ts-review --doctor` proves which one it
  found). Never print the key.

## Always

- Deterministic work in code. The model only answers atomic typed questions.
- Severity, verdict and score come from tables in `questions.py` / `compose.py`. Never
  from the model.
- Fail closed. A path that reports success it did not verify is a blocker, in the
  reviewer's own code as much as in the code it reviews.
- Tests use recorded responses. No live API calls in the test suite.
- `uv run` for every command. Ruff + ty clean before a commit.

## Never

- Generate review prose with an LLM (v1). Fix text is a template per question.
- Read `governance/decisions/` in a target repo for rules; that is `boundary-reviewer`'s
  job and it reads the generated view.
- Edit the worktree under review. Only `ts-review.md` and `ts-review.json` are written
  there.
- Commit `.env`, fixtures containing secrets, or the API key in any form.

## Layout (planned, see spec §8)

```
src/typesafe_review/   cli · checks · slicing · state · questions · ask · compose · render
tests/                 recorded responses as fixtures; tests/governance/ is the harness's own
fixtures/              labelled diffs for calibration (spec §9)
docs/specs/            the spec
tasks/typesafe-reviewer/   T-01..T-11, untracked (tasks/README.md)
governance/ controls/  the ledger harness; agents read governance/views/RULES.md only
```

## Commands

```bash
make check       # the single gate: controls → views --check → governance → tests
make tasks PLAN=tasks/typesafe-reviewer   # task status from PR state
make views       # regenerate governance/views/RULES.md + registry.json
uv run ts-review --worktree <path>        # once T-01 lands
```

## Working context (keep this current)

- Owner: Ryan (ryan@balch.io). Style in `~/.claude/CLAUDE.md`.
- Scaffolded with `new-project` on 2026-09-17, no GCP. The `new-project` skills and
  agents live under `.claude/` here; the reviewer agent among them is the one this tool
  replaces, so it stays as the reference and the control group.
- Distribution name is `typesafe-review`, package `typesafe_review`, directory
  `typesafeai-review`. The spec and task files use the first two.
- **Current work:** all 11 tasks built. PRs #11 (T-09) → #12 (T-10) → #13 (T-11) are
  stacked and await merge in that order. Next: fix `.env` `TYPESAFE_BASE_URL`, real-repo
  manual QA (T-10), grow `fixtures/` past 8 cases before any threshold change.
