#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCHEMA="$REPO_ROOT/schemas/reviewer_findings.schema.json"

if [[ -z "${CODEX_HOME:-}" ]]; then
  export CODEX_HOME="$REPO_ROOT/.runtime/codex-home"
fi
if [[ -z "${TMPDIR:-}" ]]; then
  export TMPDIR="$REPO_ROOT/.runtime/tmp"
fi
mkdir -p "$CODEX_HOME" "$TMPDIR"

: "${A2A_FINDINGS_FILE:?A2A_FINDINGS_FILE is required}"
: "${A2A_REVIEW_FILE:?A2A_REVIEW_FILE is required}"

WORKDIR="${A2A_WATCH_PATH:-$PWD}"
if [[ -f "$WORKDIR" ]]; then
  WORKDIR="$(dirname "$WORKDIR")"
fi

ALLOW_FALLBACK="${A2A_ALLOW_FALLBACK:-0}"
FALLBACK_CMD="${A2A_FALLBACK_REVIEWER_CMD:-}"
LLM_TIMEOUT_SEC="${A2A_LLM_TIMEOUT_SEC:-900}"
LLM_TIMEOUT_PER_MODEL_SEC="${A2A_LLM_TIMEOUT_PER_MODEL_SEC:-$LLM_TIMEOUT_SEC}"
STABLE_MODE="${A2A_STABLE_MODE:-1}"

run_fallback() {
  if [[ ( "$ALLOW_FALLBACK" == "1" || "$STABLE_MODE" == "1" ) && -n "$FALLBACK_CMD" ]]; then
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

QGENIE_SUBCMD=""
if qgenie agent exec --help >/dev/null 2>&1; then
  QGENIE_SUBCMD="agent-exec"
elif qgenie codex-exec --help >/dev/null 2>&1; then
  QGENIE_SUBCMD="codex-exec"
fi

if [[ -z "$QGENIE_SUBCMD" ]]; then
  if run_fallback; then
    exit 0
  fi
  echo "[aryabhatta-llm] qgenie subcommand unavailable (agent/codex-exec)" >&2
  exit 1
fi

if ! [[ "$LLM_TIMEOUT_SEC" =~ ^[0-9]+$ ]] || [[ "$LLM_TIMEOUT_SEC" -le 0 ]]; then
  LLM_TIMEOUT_SEC=900
fi
if ! [[ "$LLM_TIMEOUT_PER_MODEL_SEC" =~ ^[0-9]+$ ]] || [[ "$LLM_TIMEOUT_PER_MODEL_SEC" -le 0 ]]; then
  LLM_TIMEOUT_PER_MODEL_SEC="$LLM_TIMEOUT_SEC"
fi

resolve_qgenie_default_model() {
  if [[ -n "${A2A_LLM_MODEL:-}" ]]; then
    printf '%s\n' "${A2A_LLM_MODEL}"
    return
  fi
  if [[ -n "${QGENIE_MODEL:-}" ]]; then
    printf '%s\n' "${QGENIE_MODEL}"
    return
  fi

  local cfg=""
  local model=""
  local cfg_candidates=()
  if [[ -n "${QGENIE_CLI_HOME:-}" ]]; then
    cfg_candidates+=("${QGENIE_CLI_HOME}/config.toml")
  fi
  cfg_candidates+=(
    "$HOME/.config/qgenie-cli/config.toml"
    "$REPO_ROOT/.runtime/qgenie-cli/config.toml"
  )

  for cfg in "${cfg_candidates[@]}"; do
    [[ -f "$cfg" ]] || continue
    model="$(sed -n 's/^[[:space:]]*default_model[[:space:]]*=[[:space:]]*"\([^"]*\)"[[:space:]]*$/\1/p' "$cfg" | head -n 1)"
    if [[ -n "$model" ]]; then
      printf '%s\n' "$model"
      return
    fi
  done

  printf '%s\n' "azure::gpt-5.3-codex"
}

MODEL_CANDIDATES=()
add_unique_model() {
  local candidate="$1"
  local existing=""
  if [[ -z "$candidate" ]]; then
    return
  fi
  for existing in "${MODEL_CANDIDATES[@]}"; do
    if [[ "$existing" == "$candidate" ]]; then
      return
    fi
  done
  MODEL_CANDIDATES+=("$candidate")
}

build_model_candidates() {
  local token=""
  local resolved_default=""
  if [[ -n "${A2A_LLM_MODEL_PRIORITY:-}" ]]; then
    for token in ${A2A_LLM_MODEL_PRIORITY//,/ }; do
      add_unique_model "$token"
      if [[ "$STABLE_MODE" == "1" ]]; then
        break
      fi
    done
  fi
  if [[ "${#MODEL_CANDIDATES[@]}" -eq 0 ]]; then
    resolved_default="$(resolve_qgenie_default_model)"
    add_unique_model "$resolved_default"
    if [[ "$STABLE_MODE" != "1" ]]; then
      add_unique_model "anthropic::claude-4-6-sonnet"
      add_unique_model "anthropic::claude-4-5-sonnet"
      add_unique_model "azure::gpt-5.3-codex"
    fi
  fi
}

run_qgenie_reviewer() {
  local rc=1
  local model=""
  local total_models=1
  local per_model_timeout="$LLM_TIMEOUT_PER_MODEL_SEC"
  local idx=0

  if [[ "$QGENIE_SUBCMD" == "codex-exec" ]]; then
    total_models="${#MODEL_CANDIDATES[@]}"
    if [[ "$total_models" -eq 0 ]]; then
      MODEL_CANDIDATES=("azure::gpt-5.3-codex")
      total_models=1
    fi
    for idx in "${!MODEL_CANDIDATES[@]}"; do
      model="${MODEL_CANDIDATES[$idx]}"
      : > "$OUT_FILE"
      echo "[aryabhatta-llm] attempting model=$model ($((idx + 1))/$total_models)" >&2
      if command -v timeout >/dev/null 2>&1; then
        timeout "$per_model_timeout" qgenie codex-exec \
          -m "$model" \
          --cd "$WORKDIR" \
          --skip-git-repo-check \
          --full-auto \
          --output-last-message "$OUT_FILE" \
          - < "$PROMPT_FILE"
      else
        qgenie codex-exec \
          -m "$model" \
          --cd "$WORKDIR" \
          --skip-git-repo-check \
          --full-auto \
          --output-last-message "$OUT_FILE" \
          - < "$PROMPT_FILE"
      fi
      rc=$?
      if [[ $rc -eq 0 ]]; then
        return 0
      fi
      echo "[aryabhatta-llm] model=$model failed (rc=$rc)" >&2
    done
    return "$rc"
  fi

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
}

if [[ "$QGENIE_SUBCMD" == "codex-exec" ]]; then
  build_model_candidates
fi

PROMPT_FILE="$(mktemp)"
OUT_FILE="$(mktemp)"
trap 'rm -f "$PROMPT_FILE" "$OUT_FILE"' EXIT

PROMPT_TEMPLATE="$REPO_ROOT/templates/prompts/aryabhatta.md"
if [[ ! -f "$PROMPT_TEMPLATE" ]]; then
  echo "[aryabhatta-llm] missing prompt template: $PROMPT_TEMPLATE" >&2
  exit 1
fi

cat "$PROMPT_TEMPLATE" >"$PROMPT_FILE"
cat >>"$PROMPT_FILE" <<EOF

Runtime context:
- Review repository/worktree under: ${A2A_WATCH_PATH:-<unset>}
- Focus issues: ${A2A_FOCUS_ISSUES:-<none>}
- Prior review context: ${A2A_PRIOR_COMMENTS_FILE:-<none>}
- Round: ${A2A_ROUND:-?}
- Subsystem: ${A2A_KB_SUBSYSTEM:-unknown}
- Knowledge base evidence context:
${A2A_KB_ARYABHATTA_CONTEXT:-<none>}
- Extra scrutiny required: ${A2A_EXTRA_SCRUTINY:-0}
- Require independent subsystem scan: ${A2A_REQUIRE_INDEPENDENT_SCAN:-0}

Strict requirements:
1) Return ONLY JSON matching the provided schema.
2) Findings must include severity/title/location/evidence/required_action/status/source_comment_id.
3) For every prior comment, include source_comment_id and set status=closed only with concrete evidence.
4) Use location as patch_file_name:line_number.
5) If an issue is not addressed, keep it open.
6) Any open finding must include concrete evidence text suitable for upstream evidence enrichment.
7) Never suppress observed concerns:
   - In-scope unresolved concern -> emit open finding.
   - Pre-existing/out-of-scope concern -> emit low-severity advisory finding with explicit follow-up.
8) If your own reasoning mentions uncertainty/risk, do not return an empty findings list unless resolved with concrete evidence.
9) If prior comments exist and independent scan is required, include at least one non-prior finding with source_comment_id like subsys-scan:<topic>.
10) Enforce logical patch split and bisect-safe ordering across the full series, not only per-file syntax.
11) If patchset artifacts exist (*.patches/series, *.cover, *.mbx), verify subject counts/order consistency and emit open finding(s) on mismatch.
12) Cover letter "Changes since vN" must describe technical delta; tool/meta-only changelog text is a finding.
13) Flag unchecked __must_check runtime-PM calls in touched code (e.g. devm_pm_runtime_enable) and track maintainer-requested subject/message wording fixes.
14) If focus issues are provided, include at least one explicit finding/advisory row with source_comment_id prefix focus-issue: and concrete patch_file:line evidence.
15) Keep source_comment_id stable for unchanged concerns across rounds; do not invent new subsys-scan ids for unchanged closed advisories.
16) If no *.patch files exist under the review path, evaluate repository diffs in this worktree; do not read unrelated patch files outside this path.
EOF

set +e
run_qgenie_reviewer
RC=$?
set -e

if [[ $RC -ne 0 ]]; then
  if [[ -s "$OUT_FILE" ]]; then
    echo "[aryabhatta-llm] qgenie returned rc=$RC but produced output; continuing" >&2
    RC=0
  fi
fi

if [[ $RC -ne 0 ]]; then
  if run_fallback; then
    exit 0
  fi
  echo "[aryabhatta-llm] qgenie $QGENIE_SUBCMD failed (rc=$RC)" >&2
  exit $RC
fi

set +e
python - "$OUT_FILE" "$A2A_FINDINGS_FILE" "$A2A_REVIEW_FILE" "${A2A_ROUND:-?}" "${A2A_WATCH_PATH:-}" "${A2A_REQUIRE_INDEPENDENT_SCAN:-0}" "${A2A_PRIOR_COMMENTS_TOTAL:-0}" <<'PY'
import json
import os
import re
import sys
from pathlib import Path

src = Path(sys.argv[1])
out_findings = Path(sys.argv[2])
out_review = Path(sys.argv[3])
round_no = str(sys.argv[4])
watch_path = str(sys.argv[5] if len(sys.argv) > 5 else "").strip()
require_independent = str(sys.argv[6] if len(sys.argv) > 6 else "0").strip() == "1"
try:
    prior_comments_total = int(str(sys.argv[7] if len(sys.argv) > 7 else "0").strip() or "0")
except ValueError:
    prior_comments_total = 0
focus_issues: list[str] = []
focus_raw = str(os.environ.get("A2A_FOCUS_ISSUES_JSON", "")).strip()
if focus_raw:
    try:
        parsed = json.loads(focus_raw)
        if isinstance(parsed, list):
            focus_issues = [str(x).strip() for x in parsed if str(x).strip()]
    except Exception:
        focus_issues = []
text = src.read_text(encoding="utf-8", errors="replace").strip()


def _parse_payload_from_text(raw: str) -> dict:
    # 1) Fast path: full output is JSON.
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # 2) Try each fenced ```json ...``` block and keep the last valid findings payload.
    fenced_candidates = re.findall(r"```json\s*(\{.*?\})\s*```", raw, flags=re.S | re.I)
    for chunk in reversed(fenced_candidates):
        try:
            parsed = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("findings"), list):
            return parsed

    # 3) Recover from mixed prose/duplicate JSON by scanning for valid JSON objects.
    decoder = json.JSONDecoder()
    last_valid = None
    idx = 0
    length = len(raw)
    while idx < length:
        brace = raw.find("{", idx)
        if brace < 0:
            break
        try:
            parsed, end = decoder.raw_decode(raw, brace)
        except json.JSONDecodeError:
            idx = brace + 1
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("findings"), list):
            last_valid = parsed
        idx = max(end, brace + 1)

    if isinstance(last_valid, dict):
        return last_valid

    raise RuntimeError("cannot parse reviewer JSON payload from LLM output")


payload = _parse_payload_from_text(text)

if not isinstance(payload, dict):
    raise RuntimeError("LLM output is not a JSON object")

findings = payload.get("findings", [])
if not isinstance(findings, list):
    raise RuntimeError("LLM output missing findings list")

def is_meta_finding(row: dict) -> bool:
    title = str(row.get("title", "")).strip().lower()
    source_id = str(row.get("source_comment_id", "")).strip().lower()
    location = str(row.get("location", "")).strip().lower()
    evidence = row.get("evidence", [])
    if isinstance(evidence, list):
        evidence_text = " ".join(str(x) for x in evidence).lower()
    else:
        evidence_text = str(evidence).lower()
    markers = [
        "patchwise skill",
        "loaded skill instructions",
        "starting review workflow",
        "loading required skill",
        "progress update",
        "starting review",
        "using patchwise skill workflow",
    ]
    if any(m in title for m in markers):
        return True
    if source_id in {"workflow", "workflow-2", "n/a"}:
        return True
    if location.startswith("n/a:") or location == "n/a":
        return True
    if any(m in evidence_text for m in markers):
        return True
    return False

meta_rows = [row for row in findings if isinstance(row, dict) and is_meta_finding(row)]
if meta_rows:
    raise RuntimeError(
        f"reviewer output contains workflow/meta chatter findings ({len(meta_rows)}), refusing round"
    )

if findings:
    # Ensure findings point to patch-like locations, not generic placeholders.
    watch_name = Path(watch_path).name if watch_path else ""
    bad_locations = []
    for idx, row in enumerate(findings, start=1):
        location = str(row.get("location", "")).strip()
        if ":" not in location:
            bad_locations.append((idx, location, "missing path:line"))
            continue
        path_part = location.split(":", 1)[0].strip()
        if not path_part:
            bad_locations.append((idx, location, "empty path"))
            continue
        if watch_name and watch_name.endswith(".patch"):
            if path_part != watch_name and path_part not in {"<patch>", "<current_patch>"}:
                bad_locations.append((idx, location, f"expected {watch_name}"))
    if bad_locations:
        details = "; ".join(f"#{i} '{loc}' ({reason})" for i, loc, reason in bad_locations[:5])
        raise RuntimeError("reviewer output has invalid finding locations: " + details)

def is_prior_or_meta(source_id: str) -> bool:
    norm = source_id.strip().lower()
    return norm.startswith("prior-msg:") or norm.startswith("prior-meta:") or norm.startswith("meta")

has_independent = False
for row in findings:
    if not isinstance(row, dict):
        continue
    source_id = str(row.get("source_comment_id", "")).strip()
    if not source_id:
        continue
    if is_prior_or_meta(source_id):
        continue
    has_independent = True
    break

if require_independent and prior_comments_total > 0 and not has_independent:
    print(
        "[aryabhatta-llm] warning: missing independent subsystem-scan finding while prior comments exist; "
        "LGTM may be blocked by dual-track guard.",
        file=sys.stderr,
    )

if focus_issues:
    has_focus_row = False
    for row in findings:
        if not isinstance(row, dict):
            continue
        source_id = str(row.get("source_comment_id", "")).strip().lower()
        if source_id.startswith("focus-issue:"):
            has_focus_row = True
            break
    if not has_focus_row:
        raise RuntimeError(
            "reviewer output missing explicit focus-issue finding/advisory row "
            "(source_comment_id must start with focus-issue:)"
        )

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
PARSE_RC=$?
set -e

if [[ $PARSE_RC -ne 0 ]]; then
  if run_fallback; then
    exit 0
  fi
  echo "[aryabhatta-llm] failed to normalize reviewer output (rc=$PARSE_RC)" >&2
  exit $PARSE_RC
fi
