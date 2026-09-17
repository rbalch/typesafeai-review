# typesafeai-review

A code reviewer built as AI-powered software, not an LLM agent. Code runs the checks,
slices the diff, asks Jev (TypeSafe's System One model) narrow typed questions per hunk,
and composes verdict, score and findings in code. Ships as a standalone dev dependency
(`uv run review --worktree <path>`). Spec: `docs/specs/typesafe-reviewer.md`.

## Development

All work happens inside the devcontainer. `uv` manages dependencies and the Python
toolchain itself — the image ships no system Python.

### Container credentials

`~/.ssh` and `~/.config/gh` in the container are **external Docker volumes**, `dev-ssh`
and `dev-gh`, shared by every project on the machine rather than scoped to this one.

They are not bind mounts of your host dotfiles on purpose. A host ssh config is often a
symlink to a path that does not exist inside the container, and a bind mount would hand
the container every key you own rather than the one you meant to give it.

Run this on the host after the first `make build`:

```bash
make init KEY=~/.ssh/your-github-key
```

It creates both volumes, installs the key as `id_github`, proves it against GitHub, runs
`gh auth login` if needed, and builds the code graph. Every step is skipped if it is
already done, so re-running is safe and `KEY` is only read the first time. On a machine
that has already been set up, `make init` just confirms everything and indexes the new
repo.

Being external, the volumes survive `docker compose down -v`. The one wrinkle: a volume
is seeded from the image **only while it is empty**, so once credentials are in place,
changing the starter ssh config in `dev.Dockerfile` will not reach them. Edit the file
inside the container, or remove the volume and set it up again.

### Updating tools

Every CLI installs under `$HOME`, so updating one needs no rebuild and no sudo:

```bash
npm update -g @colbymchenry/codegraph
claude update
uv self update
```

Those updates live in the container layer, not a volume — a rebuild resets them to the
versions pinned in `dev.Dockerfile`.

### Commands

```bash
make check       # the single gate: controls → views --check → governance → tests
make views       # regenerate governance/views/RULES.md + registry.json
make governance  # integrity + drift check
make controls    # every controls/fitness/*.py, plus ruff and ty
make test        # pytest
```

## How work gets done

```
/planner                 plan with the agent → docs/specs/, tasks/<slug>/, docs/adr/ if earned
/orchestrate tasks/<slug>  each task: worktree → acceptance tests first → build → two reviews
                         → one squashed commit → PR to develop → findings triaged into the ledger
```

Branches: `main` and `develop`. Agents open PRs to `develop`, one per task. `develop` to
`main` is yours. The task file format is in `tasks/README.md`; the whole loop is in
`AGENTS.md`.

## Governance

This repo runs a ledger governance harness. Architectural rules live as decisions under
`governance/decisions/`, each backed by an executable control under `controls/`, and CI
fails on any drift between them.

- **Agents read `governance/views/RULES.md`** — generated, live rules only. Never read
  `governance/decisions/` for rules; it retains superseded records on purpose.
- **`AGENTS.md`** is the hand-written contract: architecture, working context, and how
  work gets done here.
- **`docs/governance-harness.md`** explains why the harness exists and how to tell
  whether it is earning its keep.
- **`docs/ledger-findings.md`** is the experiment log.

Change a rule by supersession, never by edit. See the `ledger-ops` skill.
