# A2A_CLI Runbook (Operator Guide)

## 1. Scope

This runbook defines the practical operating steps for running two-agent
review loops:

- `builder` (implementation)
- `aryabhatta` (adversarial validation)

## 2. Preconditions

- Target repository exists locally.
- You can open two terminal sessions (or tmux panes).
- You have an agent runner available for each role.

## 3. Workspace Strategy

- Keep one clean base repo checkout.
- Use two sibling worktrees:
  - `<repo>/.worktrees/builder`
  - `<repo>/.worktrees/aryabhatta`

Recommended:

- Builder worktree: read-write
- Aryabhatta worktree: read-only policy

## 4. Session Boot

1. Define task:
   - Example: "Fix PM runtime error-path handling in LPASS pinctrl patches."
2. Create branch:
   - `a2a/<date>-<short-task>`
3. Start builder session with builder prompt template.
4. Start aryabhatta session with reviewer prompt template.
5. Register session id in `.a2a/sessions/<id>.json`.

CLI equivalent:

```bash
a2a init
a2a config set reviewer_name aryabhatta
a2a prepare --repo /path/to/repo --branch a2a/<task>
a2a run --task "<task text>"
a2a review --session <session-id> --advance
a2a loop --task "<task text>" --builder-cmd "<cmd>" --reviewer-cmd "<cmd>"
a2a report --session <session-id> --format markdown
a2a report --all --format markdown
a2a report --all --status lgtm --since 2026-05-02T00:00:00+00:00
```

## 5. Round Protocol

Each round must follow this order:

1. Builder proposes changes
   - includes files touched and rationale.
2. Aryabhatta reviews
   - emits findings with severity + evidence.
3. Builder responds to each finding
   - "fixed" or "rejected with evidence."
4. Aryabhatta re-validates
   - closes finding or reopens with stronger evidence.

No `LGTM` until all findings are closed.

## 6. Evidence Rules

Each finding must include:

- `path:line`
- command output summary (if relevant)
- why this is a defect/risk
- expected fix

Each fix response must include:

- exact changed location(s)
- verification command(s)
- outcome summary

## 7. Stop Conditions

Stop as `LGTM` only when:

- no open findings remain
- required checks have passed
- reviewer confirms evidence sufficiency

Stop as `FAILED` when:

- max rounds reached
- timeout budget exceeded
- blocking issue unresolved

## 8. Final Artifacts

Write these files under `.a2a/reports/<session-id>/`:

- `summary.md`
- `findings.json`
- `rounds.log`
- `final_verdict.txt`

## 9. Fast Manual Mode (Before Full CLI Exists)

Until `a2a` command is implemented, run this manually:

1. Open terminal A (builder).
2. Open terminal B (aryabhatta).
3. Share findings via markdown file in `.a2a/sessions/<id>/`.
4. Iterate until all findings are closed.
5. Export final summary.

Current scaffold supports manual round progression:

```bash
a2a review --session <session-id> --advance
```

This validates findings JSON for the current round and either:
- creates next-round templates, or
- finalizes session as `LGTM`/`STOPPED`.

Optional reviewer command execution in the same step:

```bash
a2a review --session <session-id> --run-agent --reviewer-cmd "<cmd>" --advance
```

Agent commands receive context through env vars, including:
`A2A_SESSION_ID`, `A2A_ROUND`, `A2A_ROLE`, `A2A_FINDINGS_FILE`,
`A2A_BUILDER_FILE`, `A2A_REVIEW_FILE`, and `A2A_REPORT_DIR`.

Full autonomous mode:

```bash
a2a loop --task "<task text>" --builder-cmd "<builder_cmd>" --reviewer-cmd "<reviewer_cmd>"
```

This continues automatically until the session reaches `LGTM` or `STOPPED`.
