# A2A_CLI

A local CLI scaffold for running a two-agent engineering loop:

- `chanakya` (builder display name): implements changes.
- `aryabhatta` (reviewer display name): adversarial reviewer with evidence-backed findings.

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
python -m a2a_cli.main loop --task "Autonomous task"
python -m a2a_cli.main respin --input-path /path/to/patch_or_series --task "Create v2"
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
a2a loop --task "Autonomous task"
a2a respin --input-path /path/to/patch_or_series --task "Create v2"
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
  - runs validation gate (checkpatch/custom command) before reviewer
  - runs reviewer command
  - validates findings and advances rounds
  - repeats until `LGTM` or `STOPPED`
- Works with:
  - `--task` (new session), or
  - `--session` (resume existing)
- Optional:
  - `--max-iterations` to bound rounds in one invocation
- Per round console output now includes:
  - findings received (total/open/closed)
  - delta vs previous round (new/resolved findings)
  - prior-comment totals (received/open/closed/fixed_by_a2a)
  - top open findings (severity/title/location/id)
  - summary artifact paths

## What `a2a respin` does

- Creates a new patch revision path from an existing file or directory:
  - directory input default: `<name>_v2` (or increments trailing `_vN`/`-vN`)
  - file input default: `v2-<filename>` (or increments `vN-`/trailing `_vN`/`-vN`)
- Runs autonomous builder+reviewer loop on the new path (`--watch-path` is auto-set).
- Keeps original source patch path unchanged unless you explicitly use `--out-path` that overlaps it.
- Supports `--force` to overwrite an existing output path.

## What `a2a config` does

- `a2a config get [--key KEY] [--json]`
- `a2a config set KEY VALUE`
- `a2a config reset [--keep-reviewer-name]`
- stores values in `.a2a/config.json`

Common keys:

- `builder_display_name` (default `chanakya`; output/report label only)
- `reviewer_display_name` (default `aryabhatta`; output/report label only)
- `reviewer_name`
- `strict_evidence`
- `llm_native_default` (default true; auto-uses LLM-native builder/reviewer wrappers)
- `llm_native_strict` (default true; fail if LLM run fails)
- `llm_native_fallback` (default false; allow deterministic fallback on LLM failure)
- `validation_gate_enabled` (default true; run validation gate in autonomous loop)
- `validation_gate_strict` (default false; stop loop on gate failures when true)
- `validation_gate_checkpatch` (default true; run kernel `checkpatch.pl` when available)
- `validation_gate_timeout_sec` (default 300; per-gate command timeout)
- `validation_gate_max_checkpatch_files` (default 50)
- `validation_gate_command` (optional custom shell command for additional validation)
- `prior_review_gate` (auto-open findings for unresolved historical comments)
- `prior_review_search` (search lore by author/subject if vN>1 and no link found)
- `prior_review_max_comments`
- `default_max_rounds`
- `builder_command`
- `reviewer_command`

`reviewer_name` is still the internal reviewer identity used for worktree keys and reviewer artifact filenames.

## What `a2a report` does

- renders consolidated session report from stored session metadata
- defaults to active session (or latest if no active)
- includes per-round scores:
  - `builder_patch_gauge` (patch change magnitude signal)
  - `builder_confidence`
  - `reviewer_confidence`
  - confidence values are heuristic operational signals (range 1-95), not calibrated probabilities
- includes per-round validation gate outcomes:
  - `gate_passed`
  - `gate_failures`
- includes session-level gate totals:
  - `gate_failures_total`
  - `gate_failed_rounds`
- includes prior-comment closure summary table:
  - initial vs current status per `source_comment_id`
  - whether it was fixed during A2A rounds (`fixed_by_a2a`)
  - first closed round and latest evidence location
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
- `A2A_PRIOR_COMMENTS_FILE`
- `A2A_PRIOR_MATRIX_FILE`
- `A2A_PRIOR_COMMENTS_TOTAL`
- `A2A_LLM_STRICT`
- `A2A_ALLOW_FALLBACK`
- `A2A_FALLBACK_BUILDER_CMD`
- `A2A_FALLBACK_REVIEWER_CMD`
- `A2A_LLM_TIMEOUT_SEC`

## Builder Change Artifacts

When `--watch-path` is provided to `run` or `loop`, each builder round writes:

- `round-XX-changed_files.txt`
- `round-XX-builder.diff`
- `round-XX-summary.json`
- `round-XX-summary.md`

under `.a2a/reports/<session-id>/`.

## Validation Suite

Run full local validation:

```bash
scripts/validation/run_suite.sh
```

Optional extended checks:

```bash
scripts/validation/run_suite.sh --with-llm --with-lore
```

This validates:

- static compile checks
- unit tests
- deterministic autonomous smoke (builder/reviewer + validation gate artifacts)
- optional LLM-native smoke (schema/runtime sanity)
- optional lore ingestion smoke (network-dependent)

## Prior-Review Ingestion Gate

When `--watch-path` points to patch files:

- A2A scans cover letter/patch messages for prior-version links (`v1: ...`, `Link: ...`, lore URLs).
- If patch version is `v2+` and links are missing, A2A searches lore by author + subject.
- A2A ingests reviewer comments into:
  - `.a2a/reports/<session-id>/prior_comments.json`
  - `.a2a/reports/<session-id>/prior_comment_matrix.md`
- During validation, unresolved prior comments are injected as open findings (using `source_comment_id`),
  so `LGTM` is blocked until they are explicitly closed with evidence.

## Notes

- This is scaffold-only for now. The full orchestration loop from `DESIGN.md`
  will be implemented next.
- `a2a status` inspects `.a2a` and reports active session metadata if present.
- During `a2a loop`, a validation gate runs before reviewer step (configurable):
  - built-in kernel patch checks via `scripts/checkpatch.pl` when detectable
  - optional custom command via `validation_gate_command`
  - strict mode can stop the loop on failed validations

## LLM Native Default

`a2a run/loop/review` now use LLM-native agents by default when command flags/config are unset:

- `scripts/agents/builder_llm_native.sh`
- `scripts/agents/reviewer_llm_native.sh`

These wrappers call `qgenie agent exec` directly.
You can still override with `--builder-cmd` / `--reviewer-cmd` or config keys.

## Real Agent Scripts

Built-in runnable agents are available at:

- `scripts/agents/builder_agent.py`
- `scripts/agents/reviewer_aryabhatta.py`
- `scripts/agents/builder_llm_native.sh` (default path)
- `scripts/agents/reviewer_llm_native.sh` (default path)

These scripts are revision-agnostic:

- they do not rely on hardcoded `v1/v2/v3` filenames
- they detect relevant patches using diff content signatures

Example with default LLM-native mode:

```bash
python -m a2a_cli.main loop \
  --task "real-xo-sd-v3-validation" \
  --max-rounds 3 \
  --watch-path /local/mnt/workspace/upstream_patches/xo_sd_LPI/linux-next/patches/xo_sd_v3
```

Example creating `v2` from a single posted base patch:

```bash
python -m a2a_cli.main respin \
  --input-path /local/mnt/workspace/upstream_patches/wcd_aux/linux-next/0001-ASoC-codecs-wcd937x-enable-AUX-PA-and-add-AUX-relate.patch \
  --task "wcd937x-aux-v2-respin" \
  --max-rounds 3
```

Example forcing deterministic scripts (manual override):

```bash
python -m a2a_cli.main loop \
  --task "real-xo-sd-v3-validation" \
  --max-rounds 3 \
  --builder-cmd "python /local/mnt/workspace/A2A_CLI/scripts/agents/builder_agent.py" \
  --reviewer-cmd "python /local/mnt/workspace/A2A_CLI/scripts/agents/reviewer_aryabhatta.py" \
  --watch-path /local/mnt/workspace/upstream_patches/xo_sd_LPI/linux-next/patches/xo_sd_v3
```

## Live `screen` Sessions (Builder + Reviewer + Logs)

You can launch parallel live screens with:

```bash
scripts/launch_live_screens.sh \
  --task "xo-sd-live-round" \
  --watch-path /local/mnt/workspace/upstream_patches/xo_sd_LPI/linux-next/patches/xo_sd_v3 \
  --max-rounds 3
```

Or reuse an existing session:

```bash
scripts/launch_live_screens.sh --session <session-id>
```

Preview without launching:

```bash
scripts/launch_live_screens.sh --session <session-id> --dry-run
```

Then attach:

```bash
screen -r a2a-builder
screen -r a2a-reviewer
screen -r a2a-logs
```

What each screen shows:

- `a2a-builder`: live builder agent run (`qgenie` output, edits/commands, final builder report write status).
- `a2a-reviewer`: waits for builder report, then live reviewer run (`qgenie` output, schema-constrained findings summary, review/findings file paths).
- `a2a-logs`: combined `tail -F` of round builder/reviewer log files under `.a2a/logs/<session-id>/`.

After both agents finish:

```bash
python -m a2a_cli.main review --session <session-id> --advance
```
