#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCHEMA="$REPO_ROOT/schemas/reviewer_findings.schema.json"

: "${A2A_FINDINGS_FILE:?A2A_FINDINGS_FILE is required}"
: "${A2A_REVIEW_FILE:?A2A_REVIEW_FILE is required}"

WORKDIR="${A2A_WATCH_PATH:-$PWD}"
if [[ -f "$WORKDIR" ]]; then
  WORKDIR="$(dirname "$WORKDIR")"
fi

ALLOW_FALLBACK="${A2A_ALLOW_FALLBACK:-0}"
FALLBACK_CMD="${A2A_FALLBACK_REVIEWER_CMD:-}"
LLM_TIMEOUT_SEC="${A2A_LLM_TIMEOUT_SEC:-900}"

run_fallback() {
  if [[ "$ALLOW_FALLBACK" == "1" && -n "$FALLBACK_CMD" ]]; then
    echo "[aryabhatta-llm] LLM call failed, using fallback reviewer command"
    bash -lc "$FALLBACK_CMD"
    return 0
  fi
  return 1
}

if ! command -v qgenie >/dev/null 2>&1; then
  if run_fallback; then
    exit 0
  fi
  echo "[aryabhatta-llm] qgenie binary not found" >&2
  exit 1
fi

PROMPT_FILE="$(mktemp)"
OUT_FILE="$(mktemp)"
trap 'rm -f "$PROMPT_FILE" "$OUT_FILE"' EXIT

cat >"$PROMPT_FILE" <<EOF
You are aryabhatta, the adversarial patch reviewer.

Task:
- Review patch files under: ${A2A_WATCH_PATH:-<unset>}
- Use prior review context from: ${A2A_PRIOR_COMMENTS_FILE:-<none>}
- Round: ${A2A_ROUND:-?}
- Subsystem: ${A2A_KB_SUBSYSTEM:-unknown}
- Knowledge base evidence context:
${A2A_KB_ARYABHATTA_CONTEXT:-<none>}
- Extra scrutiny required: ${A2A_EXTRA_SCRUTINY:-0}

Strict requirements:
1) Return ONLY JSON matching the provided schema.
2) findings must include severity/title/location/evidence/required_action/status.
3) For every prior comment, include source_comment_id and set status=closed only with concrete evidence.
4) Use location as patch_file_name:line_number.
5) If an issue is not addressed, keep it open.

Focus on correctness, bisect safety, PM-runtime/refcount behavior, and whether prior-thread feedback is addressed.
EOF

set +e
if command -v timeout >/dev/null 2>&1; then
  timeout "$LLM_TIMEOUT_SEC" qgenie agent exec \
    --cd "$WORKDIR" \
    --skip-git-repo-check \
    --full-auto \
    --output-schema "$SCHEMA" \
    --output-last-message "$OUT_FILE" \
    - < "$PROMPT_FILE"
else
  qgenie agent exec \
    --cd "$WORKDIR" \
    --skip-git-repo-check \
    --full-auto \
    --output-schema "$SCHEMA" \
    --output-last-message "$OUT_FILE" \
    - < "$PROMPT_FILE"
fi
RC=$?
set -e

if [[ $RC -ne 0 ]]; then
  if run_fallback; then
    exit 0
  fi
  echo "[aryabhatta-llm] qgenie agent exec failed (rc=$RC)" >&2
  exit $RC
fi

python - "$OUT_FILE" "$A2A_FINDINGS_FILE" "$A2A_REVIEW_FILE" "${A2A_ROUND:-?}" <<'PY'
import json
import re
import sys
from pathlib import Path

src = Path(sys.argv[1])
out_findings = Path(sys.argv[2])
out_review = Path(sys.argv[3])
round_no = str(sys.argv[4])
text = src.read_text(encoding="utf-8", errors="replace").strip()

payload = None
try:
    payload = json.loads(text)
except json.JSONDecodeError:
    m = re.search(r"```json\s*(\{.*\})\s*```", text, re.S)
    if m:
        payload = json.loads(m.group(1))
    else:
        m2 = re.search(r"(\{.*\})", text, re.S)
        if m2:
            payload = json.loads(m2.group(1))
        else:
            raise

if not isinstance(payload, dict):
    raise RuntimeError("LLM output is not a JSON object")

findings = payload.get("findings", [])
if not isinstance(findings, list):
    raise RuntimeError("LLM output missing findings list")

out_findings.parent.mkdir(parents=True, exist_ok=True)
out_findings.write_text(json.dumps({"findings": findings}, indent=2) + "\n", encoding="utf-8")

open_count = 0
lines = [
    f"# Round {round_no}: Aryabhatta Review",
    "",
    "## Findings",
    "",
]
if not findings:
    lines.append("- no findings")
else:
    for f in findings:
        status = str(f.get("status", "open")).lower()
        if status != "closed":
            open_count += 1
        lines.append(
            "- [{sev}] {title} ({loc}) status={st}".format(
                sev=f.get("severity", "?"),
                title=f.get("title", ""),
                loc=f.get("location", ""),
                st=status,
            )
        )

lines.extend(["", "## Verdict", "", f"- {'LGTM' if open_count == 0 else 'pending'}", ""])
out_review.parent.mkdir(parents=True, exist_ok=True)
out_review.write_text("\n".join(lines), encoding="utf-8")

print(f"[aryabhatta-llm] findings_total={len(findings)} open={open_count} closed={len(findings)-open_count}")
print(f"[aryabhatta-llm] findings_file={out_findings}")
print(f"[aryabhatta-llm] review_file={out_review}")
PY
