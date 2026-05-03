#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

: "${A2A_BUILDER_FILE:?A2A_BUILDER_FILE is required}"
: "${A2A_REPORT_DIR:?A2A_REPORT_DIR is required}"

WORKDIR="${A2A_WATCH_PATH:-$PWD}"
if [[ -f "$WORKDIR" ]]; then
  WORKDIR="$(dirname "$WORKDIR")"
fi

ROUND="${A2A_ROUND:-1}"
PREV_ROUND=$((ROUND - 1))
PREV_FINDINGS=""
if [[ "$PREV_ROUND" -ge 1 ]]; then
  PREV_FINDINGS="${A2A_REPORT_DIR}/round-$(printf '%02d' "$PREV_ROUND")-findings.json"
fi

STRICT="${A2A_LLM_STRICT:-1}"
ALLOW_FALLBACK="${A2A_ALLOW_FALLBACK:-0}"
FALLBACK_CMD="${A2A_FALLBACK_BUILDER_CMD:-}"
LLM_TIMEOUT_SEC="${A2A_LLM_TIMEOUT_SEC:-900}"

run_fallback() {
  if [[ "$ALLOW_FALLBACK" == "1" && -n "$FALLBACK_CMD" ]]; then
    echo "[builder-llm] LLM call failed, using fallback builder command"
    bash -lc "$FALLBACK_CMD"
    return 0
  fi
  return 1
}

if ! command -v qgenie >/dev/null 2>&1; then
  if run_fallback; then
    exit 0
  fi
  echo "[builder-llm] qgenie binary not found" >&2
  exit 1
fi

PROMPT_FILE="$(mktemp)"
OUT_FILE="$(mktemp)"
trap 'rm -f "$PROMPT_FILE" "$OUT_FILE"' EXIT

cat >"$PROMPT_FILE" <<EOF
You are builder, the implementation agent.

Working directory:
- ${WORKDIR}

Task:
- Apply fixes directly to patch files under ${A2A_WATCH_PATH:-<unset>}.
- Prior review context: ${A2A_PRIOR_COMMENTS_FILE:-<none>}.
- Previous round findings JSON: ${PREV_FINDINGS:-<none>}.
- Current round: ${ROUND}.
- Subsystem: ${A2A_KB_SUBSYSTEM:-unknown}.
- Knowledge base context:
${A2A_KB_CHANAKYA_CONTEXT:-<none>}
- Extra scrutiny required: ${A2A_EXTRA_SCRUTINY:-0}

Required behavior:
1) If previous findings exist, fix all OPEN findings first.
2) Preserve patch semantics and maintain coherent patch ordering/bisect safety.
3) Keep changes minimal and focused.
4) After edits, return a concise markdown report with sections:
   - Changes
   - Rationale
   - Verification Commands
   - Response To Reviewer Findings
5) If no changes were needed, state why with evidence.

Do not return JSON; return markdown only.
EOF

set +e
if command -v timeout >/dev/null 2>&1; then
  timeout "$LLM_TIMEOUT_SEC" qgenie agent exec \
    --cd "$WORKDIR" \
    --skip-git-repo-check \
    --full-auto \
    --output-last-message "$OUT_FILE" \
    - < "$PROMPT_FILE"
else
  qgenie agent exec \
    --cd "$WORKDIR" \
    --skip-git-repo-check \
    --full-auto \
    --output-last-message "$OUT_FILE" \
    - < "$PROMPT_FILE"
fi
RC=$?
set -e

if [[ $RC -ne 0 ]]; then
  if run_fallback; then
    exit 0
  fi
  echo "[builder-llm] qgenie agent exec failed (rc=$RC)" >&2
  exit $RC
fi

python - "$OUT_FILE" "$A2A_BUILDER_FILE" <<'PY'
import sys
from pathlib import Path

src = Path(sys.argv[1])
out = Path(sys.argv[2])
text = src.read_text(encoding="utf-8", errors="replace").strip()

if not text:
    text = "# Builder Output\n\n## Changes\n- no file changes reported by LLM\n"

out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")

print(f"[builder-llm] wrote builder report: {out}")
PY
