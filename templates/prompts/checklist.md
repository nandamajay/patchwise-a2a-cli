# A2A Review Checklist

## Correctness

- Behavior matches requested change.
- Error paths unwind resources correctly.
- No obvious regressions introduced.

## Evidence

- Findings include `path:line`.
- Verification commands are reported clearly.
- Claims are traceable to code or command output.
- If prior-review context exists, each historical comment has an explicit
  `source_comment_id` mapping and closure status.

## Quality

- Diff scope is minimal and coherent.
- Naming and structure are maintainable.
- Documentation/changelog updates are consistent.

## Decision

- `LGTM` only if all findings are closed.

## A2A Enforcement Checks

- No workflow/meta chatter in builder/reviewer outputs.
- Reviewer locations must be `patch_file:line`.
- Every open finding has actionable `required_action`.
- Resolving old findings does not suppress newly discovered risks.
- `LGTM` only if `open=0`, `new=0`, and reviewer verdict is explicit `LGTM`.
