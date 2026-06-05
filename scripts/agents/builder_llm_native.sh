#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [[ -z "${CODEX_HOME:-}" ]]; then
  export CODEX_HOME="$REPO_ROOT/.runtime/codex-home"
fi
if [[ -z "${TMPDIR:-}" ]]; then
  export TMPDIR="$REPO_ROOT/.runtime/tmp"
fi
mkdir -p "$CODEX_HOME" "$TMPDIR"

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
LLM_TIMEOUT_PER_MODEL_SEC="${A2A_LLM_TIMEOUT_PER_MODEL_SEC:-$LLM_TIMEOUT_SEC}"
STABLE_MODE="${A2A_STABLE_MODE:-1}"

run_fallback() {
  if [[ ( "$ALLOW_FALLBACK" == "1" || "$STABLE_MODE" == "1" ) && -n "$FALLBACK_CMD" ]]; then
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
  echo "[builder-llm] qgenie subcommand unavailable (agent/codex-exec)" >&2
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
      add_unique_model "azure::gpt-5.3-codex"
      add_unique_model "anthropic::claude-4-6-sonnet"
    fi
  fi
}

run_qgenie_builder() {
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
      echo "[builder-llm] attempting model=$model ($((idx + 1))/$total_models)" >&2
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
      echo "[builder-llm] model=$model failed (rc=$rc)" >&2
    done
    return "$rc"
  fi

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
}

if [[ "$QGENIE_SUBCMD" == "codex-exec" ]]; then
  build_model_candidates
fi

PROMPT_FILE="$(mktemp)"
OUT_FILE="$(mktemp)"
trap 'rm -f "$PROMPT_FILE" "$OUT_FILE"' EXIT

PROMPT_TEMPLATE="$REPO_ROOT/templates/prompts/builder.md"
if [[ ! -f "$PROMPT_TEMPLATE" ]]; then
  echo "[builder-llm] missing prompt template: $PROMPT_TEMPLATE" >&2
  exit 1
fi

cat "$PROMPT_TEMPLATE" >"$PROMPT_FILE"
cat >>"$PROMPT_FILE" <<EOF

Runtime context:
- Working directory: ${WORKDIR}
- Patch watch path: ${A2A_WATCH_PATH:-<unset>}
- Focus issues: ${A2A_FOCUS_ISSUES:-<none>}
- Prior review context: ${A2A_PRIOR_COMMENTS_FILE:-<none>}
- Previous round findings JSON: ${PREV_FINDINGS:-<none>}
- Current round: ${ROUND}
- Subsystem: ${A2A_KB_SUBSYSTEM:-unknown}
- Knowledge base context:
${A2A_KB_CHANAKYA_CONTEXT:-<none>}
- Extra scrutiny required: ${A2A_EXTRA_SCRUTINY:-0}

Execution requirements:
1) If previous findings exist, fix all OPEN findings first.
2) Preserve patch semantics and maintain coherent patch ordering/bisect safety.
3) Keep changes minimal and focused.
4) Return markdown only with required section headings.
5) If no changes were needed, state why with evidence.
6) Always include a `## Residual Risks` section with explicit yes/no risk statements and evidence.
7) Never ignore fallible __must_check runtime-PM APIs in changed code; fix or justify maintainer-requested commit subject/message wording updates.
8) If focus issues are provided, address each explicitly with concrete patch_file:line evidence.
9) If no *.patch files exist under the review path, operate on repository diffs in this worktree and do not reference unrelated patch files outside this path.
EOF

set +e
run_qgenie_builder
RC=$?
set -e

if [[ $RC -ne 0 ]]; then
  if [[ -s "$OUT_FILE" ]]; then
    echo "[builder-llm] qgenie returned rc=$RC but produced output; continuing" >&2
    RC=0
  fi
fi

if [[ $RC -ne 0 ]]; then
  if run_fallback; then
    exit 0
  fi
  echo "[builder-llm] qgenie $QGENIE_SUBCMD failed (rc=$RC)" >&2
  exit $RC
fi

set +e
python - "$OUT_FILE" "$A2A_BUILDER_FILE" <<'PY'
import sys
from pathlib import Path

src = Path(sys.argv[1])
out = Path(sys.argv[2])
text = src.read_text(encoding="utf-8", errors="replace").strip()

if not text:
    text = "# Builder Output\n\n## Changes\n- no file changes reported by LLM\n"

low = text.lower()
bad_markers = [
    "starting review workflow",
    "loaded skill instructions",
    "patchwise skill selected",
    "loading required skill instructions",
    "i will load the patch-review skill",
    "progress update",
    "starting review",
    "using patchwise skill workflow",
]
if any(marker in low for marker in bad_markers):
    raise RuntimeError(
        "builder output looks like workflow/meta chatter, not implementation report"
    )

required_sections = [
    "## changes",
    "## rationale",
    "## verification commands",
    "## response to reviewer findings",
    "## residual risks",
]
missing = [sec for sec in required_sections if sec not in low]
if missing:
    raise RuntimeError(
        "builder output missing required sections: " + ", ".join(missing)
    )

out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")

print(f"[builder-llm] wrote builder report: {out}")
PY
PARSE_RC=$?
set -e

if [[ $PARSE_RC -ne 0 ]]; then
  if run_fallback; then
    exit 0
  fi
  echo "[builder-llm] failed to normalize builder output (rc=$PARSE_RC)" >&2
  exit $PARSE_RC
fi
