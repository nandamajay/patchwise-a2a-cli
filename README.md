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

#### C. Review directly from GitHub PR

```bash
a2a loop \
  --task github-pr-review \
  --github-pr "https://github.com/<owner>/<repo>/pull/<number>" \
  --fetch-out-dir /abs/path/for/fetched-sources \
  --max-rounds 3
```

Notes:
- `--github-pr` also accepts short form `<owner>/<repo>#<number>`.
- Prior-review context is ingested from GitHub PR discussion/review comments.

#### D. Review directly from Gerrit change

```bash
a2a loop \
  --task gerrit-review \
  --gerrit-change "https://review.example.com/c/project/+/12345" \
  --fetch-out-dir /abs/path/for/fetched-sources \
  --max-rounds 3
```

Notes:
- You can pass `--gerrit-change 12345 --gerrit-base-url https://review.example.com`.
- Prior-review context is ingested from Gerrit change messages and inline comments.

#### E. Resume an existing session

```bash
a2a loop --session <session-id>
# run exactly 5 more rounds in same session budget
a2a loop --session <session-id> --extend-rounds 5
```

#### F. Manual/stepwise mode

```bash
# start
a2a run --task "my task" --watch-path /abs/path/to/patch_or_series

# validate/advance current round
a2a review --session <session-id> --advance
```

### 4) If max rounds are reached

At max rounds, interactive runs prompt:

- `Additional rounds to run [N | y=1 | number]:`

You can enter:
- `y` to add 1 more round
- a number (for example `5`) to add that many rounds in one shot

No restart from round 1.

### 5) Generate reports

```bash
a2a report --session <session-id> --format markdown
a2a report --latest --format json
a2a report --session <session-id> --format html --output /abs/path/report.html
```

`a2a loop` now auto-generates an HTML session report at:

- `.a2a/reports/<session-id>/session-report.html`

When multiple loops target the same prepared worktrees, `a2a loop` now serializes them with a worktree lock under `.a2a/locks/worktrees/` to avoid concurrent write contention.

### 6) Analyze downstream-to-upstream driver gaps

```bash
a2a gap-analyze \
  --downstream-root /path/to/downstream/kernel \
  --upstream-root /path/to/upstream/kernel \
  --subsystem audio \
  --driver-name wsa884x \
  --output-dir /path/to/output/reports
```

Generated artifacts include:
- API gap and missing upstream interfaces
- deprecated downstream APIs
- vendor hook inventory
- DT/Kconfig/Makefile/architecture differences
- dependency graph
- upstreaming roadmap, patch sequence, difficulty and risk
- architecture document, implementation plan, MVP scope, and first executable milestone

### 7) Optional submission gate

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
- prior-comment status summary
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
  - `a2a watch --msgid <id> --auto-followup --task "<task>"` (starts/continues loop on new replies)
  - `a2a watch --msgid <id> --notify-email nandam@qti.qualcomm.com` (emails reply-miss observations)
  - Watch notifications have a hard guard against mailing-list/lore-style recipients; blocked targets are skipped.
  - For unattended follow-up, the watcher process must stay running continuously (for example via `tmux`, `screen`, `systemd`, or CI runner).
  - If watcher is not running, no automatic reply-triggered review occurs; run `a2a loop` manually.

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
- `post_respin_checkpatch`, `post_respin_upstream_compat`
- `prior_review_gate`, `prior_review_search`, `prior_review_max_comments`
- `reviewer_consistency_guard`
- `full_subsystem_review_required`
- `lore_fetch_dir`
- `email_bridge`
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

## Email Bridge (Phone/Remote Control)

Run one cycle (safe smoke):

```bash
a2a email-bridge --once
```

Run daemon poller:

```bash
a2a email-bridge --poll-sec 60
# or wrapper
python scripts/email_a2a_bridge.py --poll-sec 60
```

Supported email commands:

- `A2A HELP`
- `A2A STATUS`
- `A2A STATUS SESSION=sess-...`
- `A2A RUN LORE URL=<lore-url> TASK=<task> MAX_ROUNDS=3`
- `A2A RUN FILE WATCH_PATH=/abs/path/to/patch_or_series TASK=<task>`
- `A2A RUN ATTACHMENT TASK=<task>` (attach `.patch`/`.diff`)
- `A2A RESUME SESSION=sess-...`
- `A2A EXTEND SESSION=sess-... TOKEN=<token> AUTO_RUN=yes`

Optional implicit mode (no explicit `A2A ...` command):

- Enable `email_bridge.auto_detect_requests=true`.
- Then bridge auto-starts a review when an allowlisted email contains:
  - a `lore.kernel.org` URL, or
  - `.patch`/`.diff` attachments.

Recommended config block in `.a2a/config.json`:

```json
{
  "email_bridge": {
    "poll_sec": 60,
    "imap_host": "imap.example.com",
    "imap_port": 993,
    "imap_user": "user@example.com",
    "imap_password_env": "A2A_EMAIL_IMAP_PASSWORD",
    "mailbox": "INBOX",
    "allowed_senders": ["user@example.com"],
    "notify_to": ["user@example.com"],
    "auto_detect_requests": false
  }
}
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

Interactive loop wizard:

```bash
python scripts/a2a_loop_wizard.py
```

Wizard actions:
- Start a new loop (file-based patch or lore-based)
- Resume an existing session
- Extend a stopped session by one round and resume
- Show loop command options help

Quick smart launcher (auto-detect lore URL/msgid/GitHub PR/Gerrit URL/session/path):

```bash
./run.sh "https://lore.kernel.org/all/<msgid>/"
# or
./run.sh "https://github.com/<owner>/<repo>/pull/<number>"
# or
./run.sh "https://review.example.com/c/project/+/12345"
# or
./run.sh "<message-id>"
# or
./run.sh sess-20260504-123829-203732
# or
./run.sh /abs/path/to/patch_or_series
```

Launch interactive mode:

```bash
./run.sh --wizard
```
