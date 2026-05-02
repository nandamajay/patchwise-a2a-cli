# A2A_CLI

A local CLI scaffold for running a two-agent engineering loop:

- `builder`: implements changes.
- `aryabhatta`: adversarial reviewer with evidence-backed findings.

This repository currently provides:

- Design docs (`DESIGN.md`, `docs/RUNBOOK.md`)
- Prompt templates (`templates/prompts/`)
- Initial CLI commands:
  - `a2a init`
  - `a2a status`
  - `a2a prepare`
  - `a2a run`
  - `a2a loop`
  - `a2a review`
  - `a2a config`
  - `a2a report`

## Quick Start

From repository root:

```bash
python -m a2a_cli.main init
python -m a2a_cli.main prepare --repo /path/to/target-repo --branch a2a/my-task
python -m a2a_cli.main run --task "Describe task"
python -m a2a_cli.main loop --task "Autonomous task" --builder-cmd "<cmd>" --reviewer-cmd "<cmd>"
python -m a2a_cli.main review --session <session-id> --advance
python -m a2a_cli.main config set reviewer_command "./scripts/reviewer.sh"
python -m a2a_cli.main config get --json
python -m a2a_cli.main report --latest --format markdown
python -m a2a_cli.main run --resume <session-id>
python -m a2a_cli.main status
```

Optional alias:

```bash
alias a2a='python -m a2a_cli.main'
a2a init
a2a prepare --repo /path/to/target-repo --branch a2a/my-task
a2a run --task "Describe task"
a2a loop --task "Autonomous task" --builder-cmd "<cmd>" --reviewer-cmd "<cmd>"
a2a review --session <session-id> --advance
a2a config get
a2a report --latest --format json
a2a status
```

## What `a2a init` does

- Creates:
  - `.a2a/sessions`
  - `.a2a/logs`
  - `.a2a/reports`
  - `.a2a/templates/prompts`
- Writes default:
  - `.a2a/config.json`
  - `.a2a/state.json`
- Copies prompt templates for:
  - `builder`
  - `aryabhatta`
  - `checklist`

## What `a2a prepare` does

- Validates target git repo and HEAD availability.
- Creates worktrees under `.a2a/worktrees/`:
  - `builder`
  - `aryabhatta` (or custom reviewer name)
- Writes `.a2a/prepare.json` with repo/branch/worktree metadata.

## What `a2a run` does (current version)

- `a2a run --task "..."`
  - starts a new session
  - writes round templates and findings JSON skeleton
  - marks session active
- `a2a run --resume <session-id>`
  - validates current round findings schema
  - computes open findings
  - advances to next round or marks session `LGTM`/`STOPPED`
- Optional automation:
  - `--auto --builder-cmd "<cmd>" --reviewer-cmd "<cmd>"`
  - runs builder and reviewer commands for round 1, then validates

## What `a2a review` does (current version)

- validates findings for current (or selected) round
- prints concise findings summary with severity/location/status
- optional:
  - `--run-agent --reviewer-cmd "<cmd>"` to execute reviewer step first
  - `--advance` to move session state after successful validation

## What `a2a loop` does

- Fully autonomous orchestration:
  - runs builder command
  - runs reviewer command
  - validates findings and advances rounds
  - repeats until `LGTM` or `STOPPED`
- Works with:
  - `--task` (new session), or
  - `--session` (resume existing)
- Optional:
  - `--max-iterations` to bound rounds in one invocation

## What `a2a config` does

- `a2a config get [--key KEY] [--json]`
- `a2a config set KEY VALUE`
- `a2a config reset [--keep-reviewer-name]`
- stores values in `.a2a/config.json`

Common keys:

- `reviewer_name`
- `strict_evidence`
- `default_max_rounds`
- `builder_command`
- `reviewer_command`

## What `a2a report` does

- renders consolidated session report from stored session metadata
- defaults to active session (or latest if no active)
- supports:
  - `--format markdown|json`
  - `--output <path>`
  - `--latest` / `--session <id>`
  - `--all` for aggregate multi-session view
  - `--all --status <state>` (repeatable filter)
  - `--all --since <ISO-datetime>` (time filter)

## Agent Command Environment

When builder/reviewer commands are executed, these env vars are provided:

- `A2A_SESSION_ID`
- `A2A_TASK`
- `A2A_ROUND`
- `A2A_ROLE`
- `A2A_REPO_PATH`
- `A2A_BRANCH`
- `A2A_REPORT_DIR`
- `A2A_BUILDER_FILE`
- `A2A_REVIEW_FILE`
- `A2A_FINDINGS_FILE`
- `A2A_WATCH_PATH`

## Builder Change Artifacts

When `--watch-path` is provided to `run` or `loop`, each builder round writes:

- `round-XX-changed_files.txt`
- `round-XX-builder.diff`

under `.a2a/reports/<session-id>/`.

## Notes

- This is scaffold-only for now. The full orchestration loop from `DESIGN.md`
  will be implemented next.
- `a2a status` inspects `.a2a` and reports active session metadata if present.
