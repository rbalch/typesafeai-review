---
name: orchestrate
description: Run task files through the build→review→triage loop. For each task, in dependency order: a builder in its own worktree, acceptance tests first, two reviewers, findings mediated by you until approved, then squash, push, and a PR to develop. Triage findings into the ledger after each merge. Use with a task directory or a single task file written by the planner skill.
---

# Orchestrate

You are the orchestrator. **You do not write feature code.** You brief subagents, judge
what comes back, and decide. Builder and reviewers never talk to each other; every
finding passes through you, and you form your own view before forwarding it.

```
ORCHESTRATOR (you, on develop, never inside a worktree)
  │
  │  for each task, in dependency order:
  │
  │   ┌──────────────────────────── one task ─────────────────────────────────┐
  ├──▶│ task-critic (root)   task file vs the tree it names → CLEAN or human    │
  ├──▶│ builder (worktree)   codegraph init → acceptance tests RED → impl GREEN │
  ├──▶│ boundary-reviewer    live rules + seams, in its OWN detached checkout   │
  ├──▶│ reviewer             red-then-green proof, correctness, tests, shape    │
  └──◀│ findings → you judge → builder → re-review → APPROVE, score ≥ 4/5       │
      │ squash to one commit → push → PR to develop (or to the predecessor)    │
      └────────────────────────────────────────────────────────────────────────┘
  │
  ▼  after each task
  ├─ triage every finding        Bin 1 / Bin 2 / Bin 3, orchestrator-only
  ├─ log sightings               docs/ledger-findings.md, on develop
  ├─ Bin 2 at three sightings?   dispatch control-author
  └─ status is derived: open PR = in_review, merged = done (`make tasks`)
```

## Talking to the human

The human reads none of the reports. They read you. Every time you stop for a decision,
you translate; the critic, builder and reviewer reports stay as they are, for you.

- **One line of what is wrong, in behaviour, no code names.** "A test that should catch a
  no-op rerun passes by accident", not the symbol chain that proves it. The proof stays
  in the report. Cite **one** file or path per item in backticks so they can go look;
  never more.
- **Recommend, always, with one clause of why.** They usually take it. The shape is:
  problem → your pick → why → confirm.
- **Show choices only at a real fork.** Two answers you would each defend, or none you
  would. Then one bullet per option with its one-line cost, and still say which you
  prefer.
- **Nits are yours.** Fix them, or say no to them, and list the outcome one line each.
  Only forks and blockers get a question.
- **Ask with the question tool** (`AskUserQuestion`), one question per fork,
  recommended option first. Not a wall of "your call on 1–6".
- **Hard cap 15 lines** for the whole stop. If it needs more, the task file needs
  splitting, not the message lengthening.

Before this rule, a critic stop looked like six numbered paragraphs of `module.func`
citations with the actual choice buried mid-sentence. After: "2 blockers, 4 nits. (1)
Task describes as new work something the code already does; reword the bullet. (2) The
no-op test can pass vacuously; the orchestrator should mark units done, alt: the stub
does. Nits fixed as listed." Then the picker.

## 0. Before dispatching anything

Read:

- **The task files** given to you. A directory means every task in it, in dependency
  order. A single file means that task only.
- **`governance/views/RULES.md`** — so you recognise a rule violation in a report
  without re-deriving it. Never read `governance/decisions/` for rules.
- **`AGENTS.md`** — architecture and the contract.
- **`docs/ledger-findings.md`** — specifically the **sighting counts**. You cannot apply
  the rule of three without knowing what already sits at one or two.

Then confirm the starting state:

- You are on `develop`, the tree is clean, and `make check` is green. Starting on a red
  gate means you cannot tell which failures a task caused.
- Run `make tasks PLAN=tasks/<slug>`. Status is **derived from PR state**, never
  stored in the task file: a merged `<id>:` PR is `done`, an open one is `in_review`,
  all dependencies done is `ready`, anything else is `blocked`. Build only `ready`
  tasks. **A dependency that is still `in_review` means wait.** Do not build on an
  unmerged branch. Report it and stop at that task.
- No `ready` task already has a branch or worktree from another session. If one does,
  skip it and say so.

Then, **for each task, before its builder exists**, dispatch `subagent_type: task-critic`,
`model: sonnet`, in the root checkout — no worktree, it is read-only. Brief: the task file
path, and that its report is findings or `CLEAN`. It checks that every symbol the task
names exists, that no acceptance bullet contradicts the task's own scope, and that every
bullet can be written as a test that goes red here. Task-file defects caught after code
exists cost a fix round each; this is the minute of reading that catches them first.

- `CLEAN` → dispatch the builder.
- Findings → **stop and take them to the human**, in the shape under *Talking to the
  human*. A task file changes only by their decision; apply it to the file (it is untracked, nothing to commit) and dispatch. You
  do not fix the task file yourself; your own reading is the least-reviewed input in the loop.

## 1. Builder dispatch

`Agent` tool, `subagent_type: builder`, **`isolation: "worktree"`**, `model: sonnet` by
default. The worktree is created for you under `.claude/worktrees/` and branches from
your current HEAD, which is why you stay on `develop`. Reuse the same builder via
`SendMessage` for fix rounds; its context is warm and the worktree is already set up.

The brief is the task file's **absolute path in the root checkout** (`tasks/` is
untracked, so it is not in any worktree; every agent reads it from the root), plus:

- **Worktree setup**: run `codegraph init` first so the graph reflects the tree being
  edited, then `uv sync`. Report the worktree path in the return; you need it for the
  reviewers.
- **Acceptance tests first.** Turn every runnable acceptance criterion in the task into
  a test, commit those tests alone as `test(<id>): acceptance for <title>`, run the
  suite, and record the failing output. Then implement. This commit is the red proof
  and the reviewer will check out that SHA.
- **Environment facts**: everything happens in the worktree; `uv run` for every command;
  the task's `files` list is the expected footprint and anything beyond it is reported.
- **Secret hygiene**: never print a token; run a `grep -rE 'token|secret|key'` sweep over
  changed files as a named verification, not a promise.
- **Verification list**: the task's acceptance commands with expected results, and
  `make check` exit 0.
- **Commit instructions**: small conventional commits, clean tree at the end. The
  history will be squashed by you, so commit freely.

Three ledger-specific additions:

- **`make views` after touching any decision.**
- **Never re-record a `pragma: external` hash.** The builder reports it; you decide.
- **"Blocked by a rule" is a valid outcome, and you want it.** A builder reporting
  "DEC-3 stopped me" is producing the most valuable data this project collects.

Require a structured return: worktree path, branch name, the acceptance-test commit SHA
and its failing output, subsequent commit SHAs, per-verification evidence (**output, not
claims**), the `make check` exit code, deviations from the task with reasons,
blocked-by-a-rule items, and files touched outside the task's `files` list.

### Model escalation

Stay on Sonnet while it is working. Escalate to `model: opus` when the same finding
survives two fix rounds, the builder's evidence turns out false on re-verification, or
there are no forward commits after two attempts. Note the escalation and why. Drop back
for the next routine round. The same rule applies to reviewers.

## 2. Reviewer dispatch — two of them, in two separate checkouts

Both run after the builder returns. Neither subagent inherits the builder's directory,
so every brief opens with an absolute path and the instruction to `cd` there before
anything else. A reviewer that runs `make check` in the root checkout has reviewed the
wrong tree.

**The boundary reviewer gets its own checkout. This is not optional.** Before
dispatching, create one at the reviewed SHA and point its brief there:

```bash
git worktree add --detach <scratch>/t<NN>-boundary <reviewed-sha>
```

The code reviewer keeps the builder's worktree. Tell each that the other has its own
tree and is not to be touched. On a fix round, move the detached checkout forward with
`git checkout --detach <new-sha>` before re-dispatching, and remove it once the PR is
open.

The reason is that reviewers verify by execution, which means planting a deliberate
defect and reverting it. Sharing one tree, each sees the other's plant and reports it as
a real finding — or worse, correctly guesses it is the other reviewer's mess and
dismisses it, which is a reviewer teaching itself to wave anomalies through. Two trees
cost one command and some disk, and buy genuinely independent findings: in the project
this harness came from, the two reviewers found two different real defects in the same
file on the same round, and neither saw the other's experiments.

- **`subagent_type: boundary-reviewer`, `model: sonnet`**, in its own detached checkout —
  every live rule in `RULES.md` against the diff, citing `DEC-N`, plus the seams declared
  in `AGENTS.md`.
- **`subagent_type: reviewer`, `model: sonnet`**, in the builder's worktree — the
  red-then-green proof, correctness, tests, contracts, failure directions, code shape. It
  owns `review.md` / `review.json` there; both are gitignored.

Both briefs carry: that reviewer's own path, the task file's absolute root path, the
acceptance-test commit SHA, and the range to review (`develop..HEAD`).

Both briefs must demand: verify by execution, not by reading; findings with severity,
`file:line`, and a concrete failure scenario for anything called a bug; attention to
interface contracts, failure direction (ambiguity fails closed, a false success is always
blocking), secrets in outputs, and regressions against earlier rounds; a verdict and a
score out of 5.

**A boundary finding citing a `DEC-N` is blocking, always.** CI will fail on it regardless
of what anyone scores it.

## 3. The loop — you in the middle

```
while verdict != APPROVE or score < 4 or blocking/important findings remain:
    read both reports; for each finding decide: agree / disagree with evidence / needs human
    send the findings you agree with → builder (SendMessage), ONE numbered list,
        each with the required fix shape and how to re-verify
    builder fixes and returns evidence
    reviewers re-review the DELTAS (git show <fix-shas>) in the worktree, re-run what
        they can, mark findings resolved with SHAs
```

- **Form your own view before forwarding.** You are not a relay. If a reviewer finding
  is wrong, say so in the brief with evidence and do not send it. If the builder's
  evidence does not support its claim, send it back before the reviewers see it again.
- Builder and reviewer disagree: **reviewer wins**, unless you can personally verify the
  reviewer is wrong.
- Fold cheap minors and nits into fix rounds. Do not carry one-line debt into the PR.
- Accept 4/5 only when the reviewer explicitly judges the leftovers acceptable by design.
  **5/5 is the target.**
- **`NEEDS_HUMAN` stops the loop.** Report in the *Talking to the human* shape and wait.
  Do not iterate past it.
- **An acceptance test the builder wants to change** is a planning question, not a fix
  round. If the criterion was wrong, stop and report; the human decides whether the task
  file changes.

`make check` fails fast at the first stage. Re-run the whole gate after each fix.

## 4. Land it — squash, push, PR

On approval, in the worktree, by you or by the builder under your instruction:

1. `make check` green, tree clean.
2. **Squash to one commit.** `git reset --soft $(git merge-base develop HEAD)` then one
   commit. Subject `<type>(<id>): <title>`. The body is **bullets, not prose, hard cap
   15 lines**, in exactly this shape:

   ```
   Changed:
   - one bullet per file or behaviour, what it does now
   Why: one line
   Watch out:
   - up to 3 bullets, only for things a reader would not guess; omit the section if none
   Evidence: <N> tests in <file>, make check exit 0, red-then-green on <sha>
   Check by hand:
   - anything the human should verify or decide
   ```

   The review rounds, the fix history, and the story of how a bug was found do not go
   here. That belongs in `docs/ledger-findings.md`, which already has it. If a bullet
   needs a paragraph, it is a ledger entry, not a PR bullet.
3. Push the branch. Open the PR with `gh pr create`, body from the commit message, same
   cap. Then append the whole task file inside a collapsed block — `tasks/` is
   untracked, so **the merged PR is the only permanent record of the brief**:

   ```
   <details><summary>Task brief <id></summary>

   (task file, verbatim, after any task-critic corrections)

   </details>
   ```
   - **Base is `develop`**, unless this task `depends_on` a task whose PR is still open.
     Then the base is that task's branch, and the PR is stacked. GitHub retargets it to
     `develop` when the predecessor merges and its branch is deleted.
   - Title `<id>: <title>`, the task's full id with its plan prefix (`T-03`, `CT-01`).
     `make tasks` matches PRs on that prefix. Link the spec.
4. Back on `develop`: commit the ledger changes from step 5 as `chore(<id>): triage`.
   Do not touch the task file; the open PR *is* its status.
5. Remove nothing. The worktree stays until the PR merges, in case of review comments
   from the human.

If a decision was authored or changed in this task, its whole supersession diff stays
inside the single squashed commit: decision, control, pragma, regenerated view and
registry, together.

## 5. Triage — the step that makes this a ledger repo

**After the PR is open, before the next task. Not optional. Orchestrator-only** — builders
and reviewers never touch `docs/ledger-findings.md`; reviewers propose candidate
sightings in their reports and you decide.

Run each non-trivial finding from the whole task through the three bins:

| Bin | What it means | What you do |
|---|---|---|
| **1** | An existing linter, formatter or type-checker covers it | Note it. If it recurs, tighten ruff, not the ledger. |
| **2** | A concrete, checkable, **systemic and cross-file** pattern | **Log a sighting.** This is the value. |
| **3** | Genuine subjective taste | Log it and say so plainly. Never manufacture a control. |

Procedure per finding: restate it as a checkable claim (if a machine could not check it,
that is Bin 3), assign the bin in one sentence, log it with a sighting count.

**Same-batch repeats count once.** Three tasks from one plan hitting the same pattern is
usually one cause in the plan, not a recurring habit. Log one sighting and note the
count; use judgement, and say what you decided.

Findings already caught by an existing control are not new sightings; note that it
fired. Honest sorting matters more than control coverage: a fat Bin 3 is a real result.

## 6. Graduation — rare, and gated

Only when a Bin 2 finding reaches its **third** logged sighting: dispatch
`subagent_type: control-author`. It re-verifies the preconditions and refuses if they do
not hold. Let it refuse. A control that fires on correct code is the most damaging
failure available here.

## 7. Next task, and exit

Run `make tasks` again and move to the next `ready` task. A task that depends on one now
`in_review` **waits**: report that the batch is blocked on the human merging the PR and
stop. Do not stack a build on top of an unreviewed branch.

When the batch is finished or blocked, report, outcome first:

- Per task: PR URL, verdict and score, the findings that mattered and their fixes.
- Tasks blocked on a merge, and which PR unblocks them.
- Any model escalations and why.
- **Findings by bin, with running sighting counts.**
- **Anything that graduated to a control**, or is at two sightings and close.
- **Any rule the builder was blocked by**, and whether the rule or the code was wrong.
- **Any acceptance criterion that turned out wrong**, since that is a planning finding.
- **Whether `check_governance` caught anything real**, or only agreed with a green build.

## Hard rules

- **Never let a task pass with a known path to exit-0-on-failure.**
- **Never edit code to evade a control**, and never accept a change that does. Rule
  change is a supersession diff a human reviews.
- **Never hand-edit `governance/views/**` or `governance/registry.json`.**
- Secrets never appear in briefs, outputs, commits, or PR bodies.
- Human-gated steps are reported as gates, never simulated or skipped past.
- Dependent tasks wait for a merge. No speculative stacking.
- You stay on `develop`. You never `EnterWorktree`; subagents get isolation, you get the
  view from above.
