#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python}"

usage() {
  cat <<'EOF'
Usage:
  ./run.sh [SOURCE_OR_SESSION] [options]
  ./run.sh --session <sess-id> [options]
  ./run.sh --wizard

SOURCE_OR_SESSION auto-detection:
  - lore URL:    https://lore.kernel.org/...
  - lore msgid:  20260413121824.375473-1-ajay.nandam@oss.qualcomm.com
  - session id:  sess-<task>-YYYYMMDD-<token>
  - path:        /path/to/patch_or_series

Options:
  -h, --help                 Show help
  --wizard                   Launch interactive wizard
  --session <id>             Resume existing session
  --task <name>              Task for new session
  --max-rounds <n>           Max rounds for new session
  --max-iterations <n>       Cap rounds for this invocation
  --lore-out-dir <path>      Output directory for b4 lore fetch
  --builder-cmd <cmd>        Override builder command
  --reviewer-cmd <cmd>       Override reviewer command
  --auto-respin              Enable auto next-version generation after LGTM
  --no-auto-respin           Disable auto next-version generation after LGTM
  --yes                      Skip execute confirmation prompt
EOF
}

die() {
  echo "Error: $*" >&2
  exit 1
}

prompt() {
  local text="$1"
  local default="${2-}"
  local required="${3-false}"
  local val
  while true; do
    if [[ -n "$default" ]]; then
      read -r -p "$text [$default]: " val || true
      [[ -z "$val" ]] && val="$default"
    else
      read -r -p "$text: " val || true
    fi
    if [[ -n "$val" || "$required" != "true" ]]; then
      printf '%s' "$val"
      return 0
    fi
    echo "Input required."
  done
}

prompt_yes_no() {
  local text="$1"
  local default_yes="${2-true}"
  local suffix="Y/n"
  local def_answer="y"
  if [[ "$default_yes" != "true" ]]; then
    suffix="y/N"
    def_answer="n"
  fi
  local ans
  while true; do
    read -r -p "$text [$suffix]: " ans || true
    ans="${ans,,}"
    [[ -z "$ans" ]] && ans="$def_answer"
    case "$ans" in
      y|yes) return 0 ;;
      n|no) return 1 ;;
      *) echo "Please answer y or n." ;;
    esac
  done
}

is_lore_url() {
  [[ "$1" =~ ^https?://lore\.kernel\.org/ ]]
}

is_session_id() {
  [[ "$1" =~ ^sess-([a-z0-9-]+-[0-9]{8}-[0-9a-f]{6}|[0-9]{8}-[0-9]{6}-[0-9]+(-[a-z0-9-]+)?)$ ]]
}

looks_like_msgid() {
  [[ "$1" == *"@"* && "$1" != *"/"* && "$1" != *" "* ]]
}

default_rounds() {
  "$PYTHON_BIN" - <<'PY' "$ROOT_DIR"
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
cfg_path = root / ".a2a" / "config.json"
val = 6
if cfg_path.exists():
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            v = data.get("default_max_rounds", 6)
            val = int(v)
    except Exception:
        pass
print(val)
PY
}

resolve_path() {
  "$PYTHON_BIN" - <<'PY' "$1"
from pathlib import Path
import sys

print(Path(sys.argv[1]).expanduser().resolve())
PY
}

ensure_prepare() {
  if [[ -f "$ROOT_DIR/.a2a/prepare.json" ]]; then
    return 0
  fi
  echo "Missing .a2a/prepare.json."
  if [[ -t 0 ]]; then
    if prompt_yes_no "Run 'a2a prepare' now?" true; then
      (cd "$ROOT_DIR" && "$PYTHON_BIN" -m a2a_cli.main prepare)
      return 0
    fi
    die "Cannot continue without prepare. Run: $PYTHON_BIN -m a2a_cli.main prepare"
  fi
  die "Cannot continue without prepare. Run: $PYTHON_BIN -m a2a_cli.main prepare"
}

SOURCE=""
SESSION_ID=""
TASK=""
MAX_ROUNDS=""
MAX_ITERATIONS=""
LORE_OUT_DIR=""
BUILDER_CMD=""
REVIEWER_CMD=""
AUTO_RESPIN_FLAG=""
ASSUME_YES="false"
WIZARD_MODE="false"

POSITIONAL=()
while (($#)); do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --wizard)
      WIZARD_MODE="true"
      shift
      ;;
    --session)
      (($# >= 2)) || die "--session requires a value"
      SESSION_ID="$2"
      shift 2
      ;;
    --task)
      (($# >= 2)) || die "--task requires a value"
      TASK="$2"
      shift 2
      ;;
    --max-rounds)
      (($# >= 2)) || die "--max-rounds requires a value"
      MAX_ROUNDS="$2"
      shift 2
      ;;
    --max-iterations)
      (($# >= 2)) || die "--max-iterations requires a value"
      MAX_ITERATIONS="$2"
      shift 2
      ;;
    --lore-out-dir)
      (($# >= 2)) || die "--lore-out-dir requires a value"
      LORE_OUT_DIR="$2"
      shift 2
      ;;
    --builder-cmd)
      (($# >= 2)) || die "--builder-cmd requires a value"
      BUILDER_CMD="$2"
      shift 2
      ;;
    --reviewer-cmd)
      (($# >= 2)) || die "--reviewer-cmd requires a value"
      REVIEWER_CMD="$2"
      shift 2
      ;;
    --auto-respin)
      AUTO_RESPIN_FLAG="--auto-respin"
      shift
      ;;
    --no-auto-respin)
      AUTO_RESPIN_FLAG="--no-auto-respin"
      shift
      ;;
    --yes)
      ASSUME_YES="true"
      shift
      ;;
    --)
      shift
      while (($#)); do
        POSITIONAL+=("$1")
        shift
      done
      ;;
    -*)
      die "Unknown option: $1"
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done

if [[ "$WIZARD_MODE" == "true" ]]; then
  exec "$PYTHON_BIN" "$ROOT_DIR/scripts/a2a_loop_wizard.py"
fi

if ((${#POSITIONAL[@]} > 1)); then
  die "Only one positional SOURCE_OR_SESSION is supported."
fi
if ((${#POSITIONAL[@]} == 1)); then
  SOURCE="${POSITIONAL[0]}"
fi

if [[ -n "$SESSION_ID" && -n "$SOURCE" ]]; then
  die "Use either positional SOURCE_OR_SESSION or --session, not both."
fi

SOURCE_KIND=""
if [[ -n "$SESSION_ID" ]]; then
  SOURCE_KIND="session"
elif [[ -n "$SOURCE" ]]; then
  if is_lore_url "$SOURCE"; then
    SOURCE_KIND="lore_url"
  elif is_session_id "$SOURCE"; then
    SOURCE_KIND="session"
    SESSION_ID="$SOURCE"
  elif [[ -e "$SOURCE" ]]; then
    SOURCE_KIND="path"
    SOURCE="$(resolve_path "$SOURCE")"
  elif looks_like_msgid "$SOURCE"; then
    SOURCE_KIND="msgid"
  fi
fi

if [[ -z "$SOURCE_KIND" && -t 0 ]]; then
  echo "No input provided (or could not auto-detect input type)."
  echo "1) Resume session"
  echo "2) Lore URL"
  echo "3) Lore message-id"
  echo "4) Local patch path"
  echo "5) Open wizard"
  choice="$(prompt "Choose 1/2/3/4/5" "5" true)"
  case "$choice" in
    1)
      SOURCE_KIND="session"
      SESSION_ID="$(prompt "Session id (sess-...)" "" true)"
      ;;
    2)
      SOURCE_KIND="lore_url"
      SOURCE="$(prompt "Lore URL" "" true)"
      ;;
    3)
      SOURCE_KIND="msgid"
      SOURCE="$(prompt "Lore message-id" "" true)"
      ;;
    4)
      SOURCE_KIND="path"
      SOURCE="$(resolve_path "$(prompt "Patch file or patch directory path" "" true)")"
      [[ -e "$SOURCE" ]] || die "Path not found: $SOURCE"
      ;;
    5)
      exec "$PYTHON_BIN" "$ROOT_DIR/scripts/a2a_loop_wizard.py"
      ;;
    *)
      die "Invalid selection: $choice"
      ;;
  esac
fi

[[ -n "$SOURCE_KIND" ]] || die "Could not determine input type. Pass lore URL/msgid/session/path."

ensure_prepare

CMD=("$PYTHON_BIN" -m a2a_cli.main loop)

if [[ "$SOURCE_KIND" == "session" ]]; then
  [[ -n "$SESSION_ID" ]] || die "Missing session id"
  CMD+=(--session "$SESSION_ID")
else
  if [[ -z "$TASK" ]]; then
    if [[ -t 0 ]]; then
      default_task="loop-$(date +%Y%m%d-%H%M%S)"
      TASK="$(prompt "Task name" "$default_task" true)"
    else
      TASK="loop-$(date +%Y%m%d-%H%M%S)"
    fi
  fi

  if [[ -z "$MAX_ROUNDS" ]]; then
    if [[ -t 0 ]]; then
      MAX_ROUNDS="$(prompt "Max rounds" "$(default_rounds)" true)"
    else
      MAX_ROUNDS="$(default_rounds)"
    fi
  fi

  CMD+=(--task "$TASK" --max-rounds "$MAX_ROUNDS")

  case "$SOURCE_KIND" in
    lore_url)
      CMD+=(--lore-url "$SOURCE")
      ;;
    msgid)
      CMD+=(--lore-msgid "$SOURCE")
      ;;
    path)
      CMD+=(--watch-path "$SOURCE")
      ;;
    *)
      die "Unsupported source kind: $SOURCE_KIND"
      ;;
  esac

if [[ -n "$LORE_OUT_DIR" ]]; then
  CMD+=(--lore-out-dir "$(resolve_path "$LORE_OUT_DIR")")
fi
fi

if [[ -n "$MAX_ITERATIONS" ]]; then
  CMD+=(--max-iterations "$MAX_ITERATIONS")
fi
if [[ -n "$BUILDER_CMD" ]]; then
  CMD+=(--builder-cmd "$BUILDER_CMD")
fi
if [[ -n "$REVIEWER_CMD" ]]; then
  CMD+=(--reviewer-cmd "$REVIEWER_CMD")
fi
if [[ -z "$AUTO_RESPIN_FLAG" && -t 0 ]]; then
  auto_default="false"
  case "$SOURCE_KIND" in
    lore_url|msgid)
      auto_default="true"
      ;;
  esac
  if prompt_yes_no "Enable auto-respin after LGTM?" "$auto_default"; then
    AUTO_RESPIN_FLAG="--auto-respin"
  else
    AUTO_RESPIN_FLAG="--no-auto-respin"
  fi
fi
if [[ -n "$AUTO_RESPIN_FLAG" ]]; then
  CMD+=("$AUTO_RESPIN_FLAG")
fi

echo
echo "Command:"
printf '  %q' "${CMD[@]}"
echo
echo

if [[ "$ASSUME_YES" != "true" && -t 0 ]]; then
  if ! prompt_yes_no "Run now?" true; then
    echo "Cancelled."
    exit 0
  fi
fi

cd "$ROOT_DIR"
exec "${CMD[@]}"
