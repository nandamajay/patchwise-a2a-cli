# A2A_CLI

A2A_CLI is a local two-agent patch review orchestrator.

- `chanakya` acts as builder/fixer
- `aryabhatta` acts as adversarial reviewer

It runs iterative rounds, validates findings, blocks inconsistent LGTM decisions, and stores all round artifacts under `.a2a/`.

## Requirements

- Python 3.10+
- `git`
- Optional but recommended:
  - `qgenie` (used by default LLM-native builder/reviewer wrappers)
  - `b4` (required for `--lore-url` / `--lore-msgid` workflows)
  - Linux kernel tools if you want gate checks (`checkpatch.pl`, sparse/cocci/smatch)

## Install

From repo root:

```bash
python -m pip install -e .
```

Then use either form:

```bash
a2a --help
# or
python -m a2a_cli.main --help
```

## How To Run

### 1) Initialize workspace

```bash
a2a init
```

This creates `.a2a/` state, config, sessions, logs, reports, and prompt templates.

### 2) Prepare worktrees

```bash
a2a prepare --repo /path/to/kernel-tree --branch a2a/my-task
```

### 3) Choose one workflow

#### A. Review a local patch file/series

```bash
a2a loop \
  --task xo-sd-v3-autonomous \
  --watch-path /abs/path/to/patch_or_series \
  --max-rounds 3
```

#### B. Review directly from lore message URL

```bash
a2a loop \
  --task lore-review-wcd937x \
  --lore-url "https://lore.kernel.org/all/<message-id>/" \
  --lore-out-dir /abs/path/for/lore-fetch-cache \
  --max-rounds 3
```

Notes:
- Lore flow fetches patches using `b4` into a generated local directory (defaults under `/tmp/a2a_lore_series/` unless configured otherwise).
- You can pass a cover-letter link (`[PATCH vN 0/M]`) or first patch mail link as `--lore-url`; both work as series roots.
- `--lore-out-dir` overrides fetch location for that run.
- You can set a persistent default fetch directory in config using key `lore_fetch_dir`.
- `--auto-respin` is enabled by default for lore input; after LGTM it generates a next-version patch path.

#### C. Resume an existing session

```bash
a2a loop --session <session-id>
```

#### D. Manual/stepwise mode

```bash
# start
a2a run --task "my task" --watch-path /abs/path/to/patch_or_series

# validate/advance current round
a2a review --session <session-id> --advance
```

### 4) If max rounds are reached

At max rounds, interactive runs prompt:

- `Proceed with one more round? [y/N]`

If accepted, the same session is extended by one round (no restart from round 1).

### 5) Generate reports

```bash
a2a report --session <session-id> --format markdown
a2a report --latest --format json
```

### 6) Optional submission gate

```bash
a2a submit --session <session-id>
```

This runs the HITL submission gate and dry-run email flow per config.

## Capabilities

### Core orchestration

- Autonomous multi-round loop (`a2a loop`) with builder -> gate -> reviewer sequencing
- Session lifecycle management (`in_progress`, `lgtm`, `stopped`)
- Resume support for interrupted sessions
- Round extension prompt when max rounds is reached

### Findings quality and LGTM safety

- Strict findings schema validation
- LGTM blocked when:
  - open findings exist
  - new findings exist this round
  - reviewer verdict is not explicit LGTM
- Reviewer self-consistency guard:
  - blocks LGTM if reviewer reasoning shows unresolved risk/uncertainty

### Prior-thread (lore) intelligence

- Ingests prior comments from lore threads into `prior_comments.json`
- Auto-maps prior comments via `source_comment_id`
- Injects unresolved prior comments as synthetic open findings (when applicable)
- Classifies prior comments:
  - `actionable_review`
  - `maintainer_apply_notice`
  - `meta`
- Handles maintainer "Applied/Merged" notices as `external_resolved` (upstream-resolved), surfaced explicitly in comment tables and summaries

### Dual-track lore enforcement

When prior comments exist and policy is enabled (`full_subsystem_review_required=true`):

- Reviewer must do both:
  - prior-thread mapping
  - independent subsystem scan
- LGTM is blocked if only prior-thread mapping exists without independent scan evidence

### Rich CLI visibility

Per round, terminal output includes:

- scorecard (builder/reviewer confidence, patch gauge)
- prior-comment status and table
- advertised key findings (high severity, medium-open, evidence-backed, new findings, hardware-risk keywords)
- hardware risk banner when matching keywords are present
- top open findings and reasons
- elapsed time per round

### Validation and analysis gates

- Validation gate before reviewer step
- Optional `checkpatch.pl` integration (auto when kernel tree is detected)
- Static-analysis gate artifact support and blocking behavior by config

### Patch revision and respin

- `a2a respin` supports input path or session-based respin
- Auto-increments patch subject version tokens (`vN`)
- Lore sessions can auto-generate next patch version path after LGTM

### Knowledge base and thread watch

- KB operations:
  - `a2a kb --list [--subsystem X]`
  - `a2a kb --clear`
- Lore reply watcher:
  - `a2a watch --msgid <id>`

## Key Artifact Paths

For session `<sid>`:

- logs: `.a2a/logs/<sid>/`
- reports: `.a2a/reports/<sid>/`
- session metadata: `.a2a/sessions/<sid>.json`

Common per-round files:

- `round-XX-builder.md`
- `round-XX-aryabhatta.md`
- `round-XX-findings.json`
- `round-XX-summary.json`
- `round-XX-summary.md`
- `round-XX-suggested-replies.md`
- `round-XX-builder.diff`
- `round-XX-changed_files.txt`

Prior-thread files:

- `prior_comments.json`
- `prior_comment_matrix.md`

## Common Config Knobs

Use:

```bash
a2a config get --json
a2a config set <key> <value>
a2a config set lore_fetch_dir /abs/path/for/lore-fetch-cache
```

Frequently tuned keys:

- `builder_command`, `reviewer_command`
- `llm_native_default`, `llm_native_strict`, `llm_native_fallback`, `llm_native_timeout_sec`
- `validation_gate_enabled`, `validation_gate_strict`, `validation_gate_checkpatch`
- `prior_review_gate`, `prior_review_search`, `prior_review_max_comments`
- `reviewer_consistency_guard`
- `full_subsystem_review_required`
- `lore_fetch_dir`
- `default_max_rounds`

## Useful Commands

```bash
# show active session/status
a2a status

# list all sessions in report form
a2a report --all --format markdown

# filter reports
a2a report --all --status lgtm --since 2026-05-01T00:00:00+00:00

# maintainers data
a2a maintainers --list
```

## Validation / Smoke

```bash
python -m pytest -q
scripts/validation/run_suite.sh
```

Optional live screen helper:

```bash
scripts/launch_live_screens.sh --session <session-id>
# or
scripts/launch_live_screens.sh --task "my task" --watch-path /abs/path/to/patches
```
