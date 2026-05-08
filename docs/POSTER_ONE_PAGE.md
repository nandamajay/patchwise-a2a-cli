# PatchWise A2A_CLI - One-Page Poster

## What It Is
A local Agent2Agent orchestrator for Linux patch workflows:

- `chanakya` (builder): proposes and fixes patch changes.
- `aryabhatta` (reviewer): performs adversarial evidence-based review.
- CLI orchestrator: runs rounds, validates gates, blocks unsafe LGTM, writes reports.

## Why It Exists
- Remove manual "AI babysitting" during multi-round patch review.
- Make reviews reproducible, evidence-backed, and resumable.
- Keep all artifacts local in project workspace (`.a2a/`).

## End-to-End Pipeline
```mermaid
flowchart LR
  A[Input Source<br/>Local patch | Lore URL | Lore msgid | Session resume]
  B[Session Init<br/>a2a loop]
  C[Round N: Builder]
  D[Validation Gate<br/>schema + optional checkpatch/static checks]
  E[Round N: Reviewer]
  F[Findings Engine<br/>open/closed/new/resolved]
  G{LGTM Gate}
  H[LGTM<br/>Generate report + optional auto-respin]
  I[Next Round<br/>inject prior findings/comments]
  J[STOPPED<br/>max rounds / manual stop]

  A --> B --> C --> D --> E --> F --> G
  G -- pass --> H
  G -- fail --> I --> C
  I --> J
```

## Core Decision Rules
- LGTM is blocked when any `open findings > 0`.
- LGTM is blocked on reviewer self-inconsistency (risk/uncertainty noted but unresolved).
- Lore mode supports prior-comment ingestion and mapping via `source_comment_id`.
- Full subsystem review policy can require both prior-thread mapping and independent subsystem scan.

## Inputs and Execution
- Local series: `a2a loop --task <name> --watch-path /abs/path --max-rounds 3`
- Lore URL: `a2a loop --task <name> --lore-url "https://lore.kernel.org/all/<msgid>/" --max-rounds 3`
- Lore msgid: `a2a loop --task <name> --lore-msgid "<msgid>" --max-rounds 3`
- Resume: `a2a loop --session <session-id>`
- Smart launcher: `./run.sh <lore-url|msgid|session|path>`

## Validation Stack
- Mandatory: findings schema + round-state validation.
- Optional per config: `checkpatch.pl`, static analysis gates, strict reviewer consistency.
- Post-LGTM option: auto-generate next version (`vN+1`) for lore patchsets.

## Artifacts (Per Session)
- Session: `.a2a/sessions/<sid>.json`
- Logs: `.a2a/logs/<sid>/`
- Reports: `.a2a/reports/<sid>/`
- HTML report: `.a2a/reports/<sid>/session-report.html`
- Round files: `round-XX-builder.md`, `round-XX-aryabhatta.md`, `round-XX-findings.json`, `round-XX-summary.{json,md}`, `round-XX-builder.diff`

## Worktree + Concurrency Safety
- Uses prepared builder/reviewer worktrees.
- Worktree locking prevents concurrent loop contention using `.a2a/locks/worktrees/`.

## Email/Remote Control (Optional)
- Email bridge can run commands and send session updates.
- Optional auto-detect mode can start a review when an allowlisted sender includes a lore link or patch attachment.

## Outcome
- Faster patch iteration with traceable evidence.
- Cleaner reviewer/builder separation of concerns.
- Deterministic reports for maintainer-facing respins and audit trail.
