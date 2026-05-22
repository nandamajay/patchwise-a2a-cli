#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run A2A validation suite.

Usage:
  scripts/validation/run_suite.sh [--with-llm] [--with-lore] [--watch-path <path>]

Options:
  --with-llm         Run one LLM-native autonomous smoke round.
  --with-lore        Run lore-network ingestion smoke check.
  --watch-path PATH  Patch watch path for integration smokes.
  -h, --help         Show this help.
EOF
}

WITH_LLM=0
WITH_LORE=0
WATCH_PATH="/local/mnt/workspace/upstream_patches/xo_sd_LPI/linux-next/patches/xo_sd_v3"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-llm) WITH_LLM=1; shift ;;
    --with-lore) WITH_LORE=1; shift ;;
    --watch-path) WATCH_PATH="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

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
    "$ROOT/.runtime/qgenie-cli/config.toml"
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
    done
  fi
  if [[ "${#MODEL_CANDIDATES[@]}" -eq 0 ]]; then
    resolved_default="$(resolve_qgenie_default_model)"
    add_unique_model "anthropic::claude-4-6-sonnet"
    add_unique_model "anthropic::claude-4-5-sonnet"
    add_unique_model "$resolved_default"
    add_unique_model "azure::gpt-5.3-codex"
  fi
}

if [[ ! -d .a2a ]]; then
  echo "Missing .a2a workspace. Run: python -m a2a_cli.main init" >&2
  exit 1
fi
if [[ ! -f .a2a/prepare.json ]]; then
  echo "Missing .a2a/prepare.json. Run: python -m a2a_cli.main prepare ..." >&2
  exit 1
fi

echo "[suite] static compile checks"
python -m py_compile a2a_cli/main.py a2a_cli/config.py a2a_cli/prior_review.py

echo "[suite] unit tests (pytest)"
if "$PYTHON_BIN" - <<'PY'
import importlib.util
import sys

sys.exit(0 if importlib.util.find_spec("pytest") else 1)
PY
then
  PYTHONPATH=. "$PYTHON_BIN" -m pytest -q
else
  echo "pytest not installed; falling back to unittest discover." >&2
  "$PYTHON_BIN" -m unittest discover -s tests -p 'test_*.py' -v
fi

echo "[suite] deterministic autonomous smoke"
SMOKE_OUT="$(
  python -m a2a_cli.main loop \
    --task "suite-deterministic-smoke" \
    --watch-path "$WATCH_PATH" \
    --max-rounds 1 \
    --max-iterations 1 \
    --builder-cmd "python $ROOT/scripts/agents/builder_agent.py" \
    --reviewer-cmd "python $ROOT/scripts/agents/reviewer_aryabhatta.py" \
    2>&1
)"
echo "$SMOKE_OUT"
SID="$(printf '%s\n' "$SMOKE_OUT" | sed -n 's/^Started session: //p' | tail -n1)"
if [[ -z "$SID" ]]; then
  echo "Could not parse smoke session id." >&2
  exit 1
fi

python - "$SID" <<'PY'
import json
import sys
from pathlib import Path

sid = sys.argv[1]
root = Path(".").resolve()
session = json.loads((root / ".a2a" / "sessions" / f"{sid}.json").read_text(encoding="utf-8"))
rounds = session.get("rounds", [])
if not rounds:
    raise SystemExit("No rounds validated in deterministic smoke.")
r1 = rounds[0]
for key in ["builder_patch_gauge", "builder_confidence", "reviewer_confidence"]:
    if key not in r1:
        raise SystemExit(f"Missing score key: {key}")
gate_path = root / ".a2a" / "reports" / sid / "round-01-gate.json"
if not gate_path.exists():
    raise SystemExit("Missing round-01-gate.json")
gate = json.loads(gate_path.read_text(encoding="utf-8"))
if "passed" not in gate:
    raise SystemExit("Gate payload missing 'passed'.")
print("[suite] deterministic smoke assertions: OK")
PY

if [[ "$WITH_LLM" == "1" ]]; then
  echo "[suite] llm schema smoke"
  if ! command -v qgenie >/dev/null 2>&1; then
    echo "qgenie not found; skipping --with-llm check." >&2
  else
    QGENIE_LLM_CMD=(qgenie agent exec)
    PROMPT="$(mktemp)"
    OUT="$(mktemp)"
    LOG="$(mktemp)"
    trap 'rm -f "$PROMPT" "$OUT" "$LOG"' EXIT
    cat >"$PROMPT" <<'EOF'
Return a minimal valid JSON object matching schema.
EOF
    rc=1
    if qgenie codex-exec --help >/dev/null 2>&1; then
      build_model_candidates
      if [[ "${#MODEL_CANDIDATES[@]}" -eq 0 ]]; then
        MODEL_CANDIDATES=("azure::gpt-5.3-codex")
      fi
      per_model_timeout=$((90 / ${#MODEL_CANDIDATES[@]}))
      if [[ "$per_model_timeout" -lt 30 ]]; then
        per_model_timeout=30
      fi
      for model in "${MODEL_CANDIDATES[@]}"; do
        echo "[suite] llm schema model attempt: $model" >&2
        : >"$OUT"
        : >"$LOG"
        set +e
        timeout "$per_model_timeout" qgenie codex-exec \
          -m "$model" \
          --cd "$WATCH_PATH" \
          --skip-git-repo-check \
          --full-auto \
          --output-schema "$ROOT/schemas/reviewer_findings.schema.json" \
          --output-last-message "$OUT" \
          - < "$PROMPT" >"$LOG" 2>&1
        rc=$?
        set -e
        if [[ $rc -eq 0 ]]; then
          break
        fi
      done
    else
      set +e
      timeout 90 "${QGENIE_LLM_CMD[@]}" \
        --cd "$WATCH_PATH" \
        --skip-git-repo-check \
        --full-auto \
        --output-schema "$ROOT/schemas/reviewer_findings.schema.json" \
        --output-last-message "$OUT" \
        - < "$PROMPT" >"$LOG" 2>&1
      rc=$?
      set -e
    fi
    if [[ $rc -ne 0 ]]; then
      cat "$LOG" >&2
      echo "LLM schema smoke failed." >&2
      exit 1
    fi
    if grep -qi "invalid_json_schema" "$LOG"; then
      cat "$LOG" >&2
      echo "LLM schema smoke failed due to invalid schema." >&2
      exit 1
    fi
    python - "$OUT" <<'PY'
import json
import sys
from pathlib import Path
text = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
if not text:
    raise SystemExit("LLM smoke output empty")
payload = json.loads(text)
if not isinstance(payload, dict) or "findings" not in payload:
    raise SystemExit("LLM smoke output not findings JSON")
print("[suite] llm schema assertions: OK")
PY
  fi
fi

if [[ "$WITH_LORE" == "1" ]]; then
  echo "[suite] lore ingestion smoke"
  python - "$WATCH_PATH" <<'PY'
import tempfile
from pathlib import Path
from a2a_cli.prior_review import ingest_prior_review_context

watch = Path(__import__("sys").argv[1]).resolve()
with tempfile.TemporaryDirectory() as td:
    report_dir = Path(td) / "report"
    context = ingest_prior_review_context(
        watch,
        report_dir,
        search_if_missing=True,
        max_comments=50,
    )
    if not context:
        raise SystemExit("No prior-review context returned.")
    if int(context.get("comments_total", 0)) <= 0:
        raise SystemExit("Lore smoke: comments_total is zero.")
print("[suite] lore ingestion assertions: OK")
PY
fi

echo "[suite] all checks passed"
