# A2A_CLI Design Blueprint (v0)

## 1. Purpose

Build a local CLI system that runs a two-agent engineering loop:

- `builder`: implements requested changes.
- `aryabhatta`: adversarial validator, debates with evidence, approves only on resolved findings.

The CLI should make this repeatable, auditable, and easy to run in terminal-only environments.

## 2. Goals

- Provide a strict two-agent review loop with clear roles.
- Enforce evidence-backed validation (`file:line`, command output summaries).
- Support iterative "findings -> fixes -> re-review" cycles until `LGTM`.
- Keep all artifacts local (no mandatory cloud dependency in v1 design).
- Be simple to operate with one command sequence.

## 3. Non-Goals (Initial Phase)

- Not building a GUI.
- Not implementing distributed orchestration across many hosts yet.
- Not replacing CI; this complements CI with structured agent debate.

## 4. High-Level Architecture

## 4.1 Components

- `a2a` CLI entrypoint
- `session manager`
- `workspace manager` (main repo + worktrees)
- `prompt manager` (role/system prompts)
- `orchestrator` (loop control)
- `evidence collector` (captures outputs, diffs, references)
- `policy engine` (rules: reviewer read-only, severity gates)
- `artifact writer` (logs, reports, transcripts)

## 4.2 Data Flow

1. User initializes project and target repo.
2. CLI prepares two workspaces:
   - editable `builder` worktree
   - read-only `aryabhatta` worktree
3. CLI launches or connects to two agent sessions.
4. Orchestrator runs cycles:
   - builder proposal
   - reviewer findings
   - builder fixes + evidence
   - reviewer re-check
5. Cycle ends on `LGTM` or max-round/time budget.
6. Final report emitted with all findings and resolution trace.

## 5. Repository Layout (Planned)

```text
A2A_CLI/
  DESIGN.md
  README.md
  docs/
    RUNBOOK.md
  a2a_cli/
    __init__.py
    main.py
    config.py
    types.py
    orchestrator/
      loop.py
      states.py
    adapters/
      codex_adapter.py
      shell_adapter.py
    policies/
      reviewer_policy.py
      evidence_policy.py
    reports/
      writer.py
  templates/
    prompts/
      builder.md
      aryabhatta.md
      checklist.md
  .a2a/
    sessions/
    logs/
    reports/
```

## 6. Role Contract

## 6.1 Builder

- May edit code.
- Must provide:
  - changed files
  - reason for each change
  - verification commands executed

## 6.2 Aryabhatta (Reviewer)

- Read-only by policy.
- Must challenge claims.
- Must output findings with:
  - severity (`critical/high/medium/low`)
  - location (`path:line`)
  - evidence (command or code reference)
  - required action
- May only return `LGTM` when zero unresolved findings remain.

## 7. Orchestration State Machine

States:

- `INIT`
- `PROMPT_BUILDER`
- `REVIEW_BY_ARYABHATTA`
- `FIX_BY_BUILDER`
- `VERIFY_REVIEWER`
- `LGTM`
- `STOPPED` (budget exceeded / manual stop)

Transitions:

- `INIT -> PROMPT_BUILDER`
- `PROMPT_BUILDER -> REVIEW_BY_ARYABHATTA`
- `REVIEW_BY_ARYABHATTA -> FIX_BY_BUILDER` (if findings)
- `FIX_BY_BUILDER -> VERIFY_REVIEWER`
- `VERIFY_REVIEWER -> LGTM` (if no findings)
- `VERIFY_REVIEWER -> FIX_BY_BUILDER` (if findings persist)
- Any state -> `STOPPED` on limits or operator halt

## 8. CLI Design

## 8.1 Commands (Planned)

- `a2a init`
  - bootstrap `.a2a/`, templates, default config
- `a2a prepare --repo <path> --branch <name>`
  - create/attach worktrees for `builder` and `aryabhatta`
- `a2a run --task "<text>" [--max-rounds N] [--timeout-min M]`
  - execute full loop
- `a2a review --round <n>`
  - run only reviewer step
- `a2a report [--latest|--id <session>]`
  - render markdown/json summary
- `a2a status`
  - show active session and unresolved findings

## 8.2 Key Flags

- `--strict` fail if reviewer evidence is missing
- `--readonly-reviewer` enforce no write operations for reviewer
- `--checks "checkpatch,sparse,tests"`
- `--budget-minutes`

## 9. Evidence Model

Each finding record:

```json
{
  "id": "ARYA-0007",
  "severity": "high",
  "title": "Error path leaks runtime PM ref",
  "location": "drivers/foo/bar.c:214",
  "evidence": [
    "command: scripts/checkpatch.pl ...",
    "code: missing pm_runtime_put_noidle() in err path"
  ],
  "required_action": "Add unwind in err path and prove with targeted test",
  "status": "open"
}
```

Resolution record must include:

- commit/diff reference
- validation command(s)
- reviewer re-check result

## 10. Operator Run Steps (Detailed)

1. Initialize A2A project
   - `a2a init`
2. Prepare target repo
   - `a2a prepare --repo /path/to/repo --branch a2a/<task>`
3. Start run
   - `a2a run --task "..." --max-rounds 6 --strict`
4. Observe rounds
   - each round emits:
     - builder proposal
     - aryabhatta findings table
     - fix plan
5. On loop finish
   - inspect `.a2a/reports/<session>/summary.md`
6. If not `LGTM`
   - resume with `a2a run --resume <session-id>`
7. If `LGTM`
   - export final patch/report

## 11. Guardrails

- Reviewer cannot execute write operations in its workspace.
- Findings without evidence are invalid in strict mode.
- Builder claims without verification output are flagged.
- Orchestrator keeps immutable transcript per round.

## 12. Failure Handling

- Agent timeout:
  - mark round failed, retry with backoff.
- Command failure:
  - attach stderr to evidence log and continue if non-blocking.
- Contradictory claims:
  - force reviewer to cite evidence and builder to answer each item.
- Round limit exceeded:
  - stop with unresolved findings report.

## 13. Security/Isolation

- Use separate worktrees per role.
- Reviewer session defaults to read-only filesystem policy where possible.
- Never run destructive git commands automatically.
- Keep secrets out of logs by redaction rules.

## 14. Milestone Plan

Phase 1:

- repo scaffolding
- config + templates
- manual round orchestration commands
- markdown reporting

Phase 2:

- automated round loop
- evidence schema + validators
- strict mode policy engine

Phase 3:

- adapter abstraction for multiple agent backends
- richer checks integration
- replay/resume and comparative metrics

## 15. Acceptance Criteria for v1

- Can run at least one full builder-reviewer loop locally.
- Reviewer output includes evidence and severity.
- Final report clearly shows all findings closed before `LGTM`.
- Session reproducible from saved logs and prompts.
