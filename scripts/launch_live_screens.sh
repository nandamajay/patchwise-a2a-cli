#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Launch live A2A builder/reviewer in separate screen sessions plus a log follower.

Usage:
  scripts/launch_live_screens.sh --session <session-id> [options]
  scripts/launch_live_screens.sh --task "<task>" --watch-path <path> [options]

Options:
  --session <id>          Existing A2A session id.
  --task <text>           Create a new session with this task.
  --watch-path <path>     Watch path (required with --task).
  --max-rounds <n>        Max rounds for new session (default: 3).
  --round <n>             Round number to run (default: session current_round).
  --builder-screen <name> Builder screen name (default: a2a-builder).
  --reviewer-screen <name> Reviewer screen name (default: a2a-reviewer).
  --logs-screen <name>    Logs screen name (default: a2a-logs).
  --kill-existing         Kill existing screens with same names before launch.
  --skip-logs             Do not create the log follower screen.
  --dry-run               Print resolved settings and commands without launching.
  -h, --help              Show this help.

Notes:
  - Run this from A2A_CLI repo root (must contain .a2a).
  - Reviewer waits for builder report file before starting.
EOF
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

SESSION_ID=""
TASK=""
WATCH_PATH=""
MAX_ROUNDS="3"
ROUND_OVERRIDE=""
BUILDER_SCREEN="a2a-builder"
REVIEWER_SCREEN="a2a-reviewer"
LOGS_SCREEN="a2a-logs"
KILL_EXISTING="0"
SKIP_LOGS="0"
DRY_RUN="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --session)
      SESSION_ID="${2:-}"
      shift 2
      ;;
    --task)
      TASK="${2:-}"
      shift 2
      ;;
    --watch-path)
      WATCH_PATH="${2:-}"
      shift 2
      ;;
    --max-rounds)
      MAX_ROUNDS="${2:-}"
      shift 2
      ;;
    --round)
      ROUND_OVERRIDE="${2:-}"
      shift 2
      ;;
    --builder-screen)
      BUILDER_SCREEN="${2:-}"
      shift 2
      ;;
    --reviewer-screen)
      REVIEWER_SCREEN="${2:-}"
      shift 2
      ;;
    --logs-screen)
      LOGS_SCREEN="${2:-}"
      shift 2
      ;;
    --kill-existing)
      KILL_EXISTING="1"
      shift
      ;;
    --skip-logs)
      SKIP_LOGS="1"
      shift
      ;;
    --dry-run)
      DRY_RUN="1"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

require_cmd screen
require_cmd python

ROOT="$(pwd)"
if [[ ! -d "$ROOT/.a2a" ]]; then
  echo "No .a2a directory in $ROOT. Run this from A2A_CLI root." >&2
  exit 1
fi

if [[ -n "$SESSION_ID" && -n "$TASK" ]]; then
  echo "Use either --session or --task, not both." >&2
  exit 1
fi

if [[ -z "$SESSION_ID" && -z "$TASK" ]]; then
  echo "Provide --session <id> or --task <text>." >&2
  exit 1
fi

if [[ -n "$TASK" ]]; then
  if [[ -z "$WATCH_PATH" ]]; then
    echo "--watch-path is required when creating a new session with --task." >&2
    exit 1
  fi
  CREATE_OUT="$(
    python -m a2a_cli.main run \
      --task "$TASK" \
      --watch-path "$WATCH_PATH" \
      --max-rounds "$MAX_ROUNDS" \
      2>&1
  )" || {
    echo "$CREATE_OUT" >&2
    exit 1
  }
  echo "$CREATE_OUT"
  SESSION_ID="$(printf '%s\n' "$CREATE_OUT" | sed -n 's/^Started session: //p' | tail -n1)"
  if [[ -z "$SESSION_ID" ]]; then
    echo "Could not parse session id from a2a run output." >&2
    exit 1
  fi
fi

SESSION_JSON="$ROOT/.a2a/sessions/$SESSION_ID.json"
if [[ ! -f "$SESSION_JSON" ]]; then
  echo "Session not found: $SESSION_ID" >&2
  exit 1
fi

SESSION_INFO="$(
python - "$SESSION_JSON" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
watch = str(payload.get("watch_path") or "")
current_round = int(payload.get("current_round") or 1)
reviewer_name = str(payload.get("reviewer_name") or "aryabhatta")
print(watch)
print(current_round)
print(reviewer_name)
PY
)"

WATCH_FROM_SESSION="$(printf '%s\n' "$SESSION_INFO" | sed -n '1p')"
CURRENT_ROUND="$(printf '%s\n' "$SESSION_INFO" | sed -n '2p')"
REVIEWER_NAME="$(printf '%s\n' "$SESSION_INFO" | sed -n '3p')"

if [[ -n "$WATCH_PATH" ]]; then
  WATCH_RESOLVED="$WATCH_PATH"
else
  WATCH_RESOLVED="$WATCH_FROM_SESSION"
fi

if [[ -z "$WATCH_RESOLVED" ]]; then
  echo "Session has no watch_path and none provided via --watch-path." >&2
  exit 1
fi

if [[ -n "$ROUND_OVERRIDE" ]]; then
  ROUND="$ROUND_OVERRIDE"
else
  ROUND="$CURRENT_ROUND"
fi

REPORT_DIR="$ROOT/.a2a/reports/$SESSION_ID"
LOG_DIR="$ROOT/.a2a/logs/$SESSION_ID"
mkdir -p "$REPORT_DIR" "$LOG_DIR"

ROUND_PADDED="$(printf '%02d' "$ROUND")"
BUILDER_FILE="$REPORT_DIR/round-${ROUND_PADDED}-builder.md"
REVIEW_FILE="$REPORT_DIR/round-${ROUND_PADDED}-${REVIEWER_NAME}.md"
FINDINGS_FILE="$REPORT_DIR/round-${ROUND_PADDED}-findings.json"
BUILDER_LOG="$LOG_DIR/round-${ROUND_PADDED}-builder.log"
REVIEWER_LOG="$LOG_DIR/round-${ROUND_PADDED}-reviewer.log"
PRIOR_FILE="$REPORT_DIR/prior_comments.json"

if [[ "$KILL_EXISTING" == "1" ]]; then
  screen -S "$BUILDER_SCREEN" -X quit >/dev/null 2>&1 || true
  screen -S "$REVIEWER_SCREEN" -X quit >/dev/null 2>&1 || true
  if [[ "$SKIP_LOGS" == "0" ]]; then
    screen -S "$LOGS_SCREEN" -X quit >/dev/null 2>&1 || true
  fi
fi

if screen -list | grep -q "[[:space:]]${BUILDER_SCREEN}[[:space:]]"; then
  echo "Screen already exists: $BUILDER_SCREEN (use --kill-existing)" >&2
  exit 1
fi
if screen -list | grep -q "[[:space:]]${REVIEWER_SCREEN}[[:space:]]"; then
  echo "Screen already exists: $REVIEWER_SCREEN (use --kill-existing)" >&2
  exit 1
fi
if [[ "$SKIP_LOGS" == "0" ]] && screen -list | grep -q "[[:space:]]${LOGS_SCREEN}[[:space:]]"; then
  echo "Screen already exists: $LOGS_SCREEN (use --kill-existing)" >&2
  exit 1
fi

BUILDER_INNER=$(cat <<EOF
cd "$ROOT"
if [[ -d "/host/bin" ]]; then
  export PATH="/host/bin:\$PATH"
fi
if [[ -f "$ROOT/.runtime/qgenie-cli/config.toml" ]]; then
  export QGENIE_CLI_HOME="$ROOT/.runtime/qgenie-cli"
fi
export A2A_SESSION_ID="$SESSION_ID"
export A2A_TASK="${TASK:-manual-live}"
export A2A_ROUND="$ROUND"
export A2A_ROLE="builder"
export A2A_REPORT_DIR="$REPORT_DIR"
export A2A_BUILDER_FILE="$BUILDER_FILE"
export A2A_REVIEW_FILE="$REVIEW_FILE"
export A2A_FINDINGS_FILE="$FINDINGS_FILE"
export A2A_WATCH_PATH="$WATCH_RESOLVED"
export A2A_PRIOR_COMMENTS_FILE="$PRIOR_FILE"
export A2A_LLM_TIMEOUT_SEC="\${A2A_LLM_TIMEOUT_SEC:-900}"
bash scripts/agents/builder_llm_native.sh 2>&1 | tee "$BUILDER_LOG"
echo
echo "[builder screen] done. Press Enter to keep window open."
read -r
EOF
)

REVIEWER_INNER=$(cat <<EOF
cd "$ROOT"
if [[ -d "/host/bin" ]]; then
  export PATH="/host/bin:\$PATH"
fi
if [[ -f "$ROOT/.runtime/qgenie-cli/config.toml" ]]; then
  export QGENIE_CLI_HOME="$ROOT/.runtime/qgenie-cli"
fi
export A2A_SESSION_ID="$SESSION_ID"
export A2A_TASK="${TASK:-manual-live}"
export A2A_ROUND="$ROUND"
export A2A_ROLE="reviewer"
export A2A_REPORT_DIR="$REPORT_DIR"
export A2A_BUILDER_FILE="$BUILDER_FILE"
export A2A_REVIEW_FILE="$REVIEW_FILE"
export A2A_FINDINGS_FILE="$FINDINGS_FILE"
export A2A_WATCH_PATH="$WATCH_RESOLVED"
export A2A_PRIOR_COMMENTS_FILE="$PRIOR_FILE"
export A2A_LLM_TIMEOUT_SEC="\${A2A_LLM_TIMEOUT_SEC:-900}"
echo "[reviewer screen] waiting for builder report: $BUILDER_FILE"
while [[ ! -s "$BUILDER_FILE" ]]; do sleep 2; done
bash scripts/agents/reviewer_llm_native.sh 2>&1 | tee "$REVIEWER_LOG"
echo
echo "[reviewer screen] done. Press Enter to keep window open."
read -r
EOF
)

if [[ "$DRY_RUN" == "0" ]]; then
  screen -dmS "$BUILDER_SCREEN" bash -lc "$BUILDER_INNER"
  screen -dmS "$REVIEWER_SCREEN" bash -lc "$REVIEWER_INNER"
fi

if [[ "$SKIP_LOGS" == "0" ]]; then
  LOGS_INNER=$(cat <<EOF
cd "$ROOT"
touch "$BUILDER_LOG" "$REVIEWER_LOG"
tail -F "$BUILDER_LOG" "$REVIEWER_LOG"
EOF
)
  if [[ "$DRY_RUN" == "0" ]]; then
    screen -dmS "$LOGS_SCREEN" bash -lc "$LOGS_INNER"
  fi
fi

if [[ "$DRY_RUN" == "1" ]]; then
  echo "Dry run only. No screen sessions launched."
  echo "Session: $SESSION_ID"
  echo "Round: $ROUND"
  echo "Watch path: $WATCH_RESOLVED"
  echo "Builder screen: $BUILDER_SCREEN"
  echo "Reviewer screen: $REVIEWER_SCREEN"
  if [[ "$SKIP_LOGS" == "0" ]]; then
    echo "Logs screen: $LOGS_SCREEN"
  fi
  echo
  echo "Builder log: $BUILDER_LOG"
  echo "Reviewer log: $REVIEWER_LOG"
  exit 0
fi

echo "Launched live screens for session: $SESSION_ID"
echo "Round: $ROUND"
echo "Watch path: $WATCH_RESOLVED"
echo
echo "Attach:"
echo "  screen -r $BUILDER_SCREEN"
echo "  screen -r $REVIEWER_SCREEN"
if [[ "$SKIP_LOGS" == "0" ]]; then
  echo "  screen -r $LOGS_SCREEN"
fi
echo
echo "After both complete, validate+advance:"
echo "  python -m a2a_cli.main review --session $SESSION_ID --advance"
