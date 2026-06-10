#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python}"

usage() {
  cat <<'EOF'
Usage:
  ./run.sh [SOURCE_OR_SESSION] [options]
  ./run.sh --session <sess-id> [options]
  ./run.sh --watch-replies --msgid <lore-msgid> [options]
  ./run.sh --wizard

SOURCE_OR_SESSION auto-detection:
  - lore URL:    https://lore.kernel.org/...
  - GitHub PR:   https://github.com/<owner>/<repo>/pull/<n> or <owner>/<repo>#<n>
  - Gerrit URL:  https://<gerrit-host>/.../+/12345
  - lore msgid:  20260413121824.375473-1-ajay.nandam@oss.qualcomm.com
  - session id:  sess-<task>-YYYYMMDD-<token>
  - path:        /path/to/patch_or_series

Options:
  -h, --help                 Show help
  --wizard                   Launch interactive wizard
  --session <id>             Resume existing session
  --task <name>              Task for new session
  --max-rounds <n>           Max rounds for new session
  --extend-rounds <n>        With --session, add N rounds before resume
  --max-iterations <n>       Cap rounds for this invocation
  --lore-out-dir <path>      Output directory for b4 lore fetch
  --fetch-out-dir <path>     Output directory for externally fetched patch sources
  --source-msgid <id>        Attach lore message-id context for local path runs (enables prior/bot ingest)
  --github-pr <ref>          GitHub PR source (URL or owner/repo#number)
  --gerrit-change <ref>      Gerrit change source (URL, change number, or Change-Id)
  --gerrit-base-url <url>    Gerrit base URL for non-URL --gerrit-change values
  --kernel-workspace <path>  Kernel repo path; runs 'a2a prepare --repo <path> --force' before loop/watch
  --prepare-branch <name>    Branch for 'a2a prepare' when --kernel-workspace is used
  --builder-cmd <cmd>        Override builder command
  --reviewer-cmd <cmd>       Override reviewer command
  --focus-issue <text>       Force explicit coverage of issue/topic (repeatable)
  --auto-respin              Enable auto next-version generation after LGTM
  --no-auto-respin           Disable auto next-version generation after LGTM
  --watch-replies            Run lore watcher mode (a2a watch)
  --msgid <id>               Lore thread message-id for watcher mode (or local path source context)
  --poll <sec>               Poll interval for watcher mode (default: 300)
  --max-loops <n>            Optional loop cap for watcher mode
  --auto-followup            On new replies, trigger a2a loop automatically
  --notify-email <addr>      Observation email recipient (repeatable)
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

is_github_pr_url() {
  [[ "$1" =~ ^https?://github\.com/[^/]+/[^/]+/pull/[0-9]+([/?#].*)?$ ]]
}

looks_like_github_pr_short() {
  [[ "$1" =~ ^[^/[:space:]]+/[^#[:space:]]+#[0-9]+$ ]]
}

is_gerrit_change_url() {
  [[ "$1" =~ ^https?://[^[:space:]]+/.*/\+/[0-9]+([/?#].*)?$ ]]
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

session_snapshot() {
  "$PYTHON_BIN" - <<'PY' "$ROOT_DIR" "$1"
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
sid = str(sys.argv[2] or "").strip()
if not sid:
    raise SystemExit(2)

path = root / ".a2a" / "sessions" / f"{sid}.json"
if not path.exists():
    raise SystemExit(2)

try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(2)

status = str(payload.get("status") or "")
current_round = int(payload.get("current_round", 0) or 0)
max_rounds = int(payload.get("max_rounds", 0) or 0)
open_findings = payload.get("open_findings")
open_findings_text = "unknown" if open_findings is None else str(int(open_findings))
print(f"{status}|{current_round}|{max_rounds}|{open_findings_text}")
PY
}

extract_lore_msgid() {
  "$PYTHON_BIN" - <<'PY' "$1"
from urllib.parse import urlparse, unquote
import sys

raw = str(sys.argv[1] or "").strip().strip("<>").strip()
if not raw:
    raise SystemExit(2)

if "://" not in raw and "/" not in raw:
    print(raw)
    raise SystemExit(0)

if "://" not in raw:
    parsed = urlparse("https://" + raw)
else:
    parsed = urlparse(raw)

path = parsed.path.strip("/")
if not path:
    raise SystemExit(2)

for prefix in ("r/", "all/"):
    if path.startswith(prefix):
        token = path[len(prefix):].split("/", 1)[0].strip().strip("<>").strip()
        if token:
            print(unquote(token))
            raise SystemExit(0)

token = path.split("/", 1)[0].strip().strip("<>").strip()
if token:
    print(unquote(token))
    raise SystemExit(0)

raise SystemExit(2)
PY
}

extract_session_lore_msgid() {
  "$PYTHON_BIN" - <<'PY' "$ROOT_DIR" "$1"
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
sid = str(sys.argv[2] or "").strip()
if not sid:
    raise SystemExit(2)

path = root / ".a2a" / "sessions" / f"{sid}.json"
if not path.exists():
    raise SystemExit(2)

try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(2)

lore = payload.get("lore")
if isinstance(lore, dict):
    msgid = str(lore.get("message_id") or "").strip()
    if msgid:
        print(msgid)
        raise SystemExit(0)

raise SystemExit(2)
PY
}

resolve_path() {
  "$PYTHON_BIN" - <<'PY' "$1"
from pathlib import Path
import sys

print(Path(sys.argv[1]).expanduser().resolve())
PY
}

default_prepare_branch() {
  local branch
  branch="$(git -C "$ROOT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  if [[ -z "$branch" || "$branch" == "HEAD" ]]; then
    branch="a2a/work"
  fi
  printf '%s' "$branch"
}

existing_prepare_branch() {
  "$PYTHON_BIN" - <<'PY' "$ROOT_DIR"
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
path = root / ".a2a" / "prepare.json"
if not path.exists():
    raise SystemExit(0)

try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)

branch = str(payload.get("branch") or "").strip() if isinstance(payload, dict) else ""
if branch:
    print(branch)
PY
}

prepare_with_kernel_workspace() {
  local repo_path="$1"
  local branch="$2"
  local -a cmd=("$PYTHON_BIN" -m a2a_cli.main prepare --repo "$repo_path" --branch "$branch" --force)
  echo "Preparing A2A worktrees:"
  printf '  %q' "${cmd[@]}"
  echo
  (cd "$ROOT_DIR" && "${cmd[@]}")
}

ensure_prepare() {
  if [[ -f "$ROOT_DIR/.a2a/prepare.json" ]]; then
    return 0
  fi
  echo "Missing .a2a/prepare.json."
  if [[ -t 0 ]]; then
    if prompt_yes_no "Run 'a2a prepare' now?" true; then
      local prepare_branch
      prepare_branch="$(default_prepare_branch)"
      prepare_branch="$(prompt "Branch for 'a2a prepare --branch'" "$prepare_branch" true)"
      (cd "$ROOT_DIR" && "$PYTHON_BIN" -m a2a_cli.main prepare --branch "$prepare_branch")
      return 0
    fi
    die "Cannot continue without prepare. Run: $PYTHON_BIN -m a2a_cli.main prepare --branch <name>"
  fi
  die "Cannot continue without prepare. Run: $PYTHON_BIN -m a2a_cli.main prepare --branch <name>"
}

SOURCE=""
SESSION_ID=""
TASK=""
MAX_ROUNDS=""
EXTEND_ROUNDS=""
MAX_ITERATIONS=""
LORE_OUT_DIR=""
FETCH_OUT_DIR=""
SOURCE_MSGID=""
GITHUB_PR=""
GERRIT_CHANGE=""
GERRIT_BASE_URL=""
KERNEL_WORKSPACE=""
PREPARE_BRANCH_OVERRIDE=""
BUILDER_CMD=""
REVIEWER_CMD=""
FOCUS_ISSUES=()
AUTO_RESPIN_FLAG=""
WATCH_REPLIES="false"
WATCH_MSGID=""
WATCH_POLL=""
WATCH_MAX_LOOPS=""
AUTO_FOLLOWUP="false"
WATCH_AFTER_LOOP="false"
NOTIFY_EMAILS=()
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
    --extend-rounds)
      (($# >= 2)) || die "--extend-rounds requires a value"
      EXTEND_ROUNDS="$2"
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
    --fetch-out-dir)
      (($# >= 2)) || die "--fetch-out-dir requires a value"
      FETCH_OUT_DIR="$2"
      shift 2
      ;;
    --source-msgid)
      (($# >= 2)) || die "--source-msgid requires a value"
      SOURCE_MSGID="$2"
      shift 2
      ;;
    --github-pr)
      (($# >= 2)) || die "--github-pr requires a value"
      GITHUB_PR="$2"
      shift 2
      ;;
    --gerrit-change)
      (($# >= 2)) || die "--gerrit-change requires a value"
      GERRIT_CHANGE="$2"
      shift 2
      ;;
    --gerrit-base-url)
      (($# >= 2)) || die "--gerrit-base-url requires a value"
      GERRIT_BASE_URL="$2"
      shift 2
      ;;
    --kernel-workspace)
      (($# >= 2)) || die "--kernel-workspace requires a value"
      KERNEL_WORKSPACE="$2"
      shift 2
      ;;
    --prepare-branch)
      (($# >= 2)) || die "--prepare-branch requires a value"
      PREPARE_BRANCH_OVERRIDE="$2"
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
    --focus-issue)
      (($# >= 2)) || die "--focus-issue requires a value"
      FOCUS_ISSUES+=("$2")
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
    --watch-replies)
      WATCH_REPLIES="true"
      shift
      ;;
    --msgid)
      (($# >= 2)) || die "--msgid requires a value"
      WATCH_MSGID="$2"
      shift 2
      ;;
    --poll)
      (($# >= 2)) || die "--poll requires a value"
      WATCH_POLL="$2"
      shift 2
      ;;
    --max-loops)
      (($# >= 2)) || die "--max-loops requires a value"
      WATCH_MAX_LOOPS="$2"
      shift 2
      ;;
    --auto-followup)
      AUTO_FOLLOWUP="true"
      shift
      ;;
    --notify-email)
      (($# >= 2)) || die "--notify-email requires a value"
      NOTIFY_EMAILS+=("$2")
      shift 2
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

if [[ -n "$PREPARE_BRANCH_OVERRIDE" && -z "$KERNEL_WORKSPACE" ]]; then
  die "--prepare-branch requires --kernel-workspace."
fi

if [[ -n "$KERNEL_WORKSPACE" ]]; then
  KERNEL_WORKSPACE="$(resolve_path "$KERNEL_WORKSPACE")"
  [[ -d "$KERNEL_WORKSPACE" ]] || die "Kernel workspace path not found: $KERNEL_WORKSPACE"
  if ! git -C "$KERNEL_WORKSPACE" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    die "Not a git repository: $KERNEL_WORKSPACE"
  fi

  PREPARE_BRANCH="${PREPARE_BRANCH_OVERRIDE:-$(existing_prepare_branch)}"
  if [[ -z "$PREPARE_BRANCH" ]]; then
    PREPARE_BRANCH="$(default_prepare_branch)"
  fi
  prepare_with_kernel_workspace "$KERNEL_WORKSPACE" "$PREPARE_BRANCH"
fi

if [[ "$WATCH_REPLIES" == "true" ]]; then
  if [[ -n "$SOURCE" || ${#POSITIONAL[@]} -gt 0 ]]; then
    die "--watch-replies cannot be combined with SOURCE_OR_SESSION."
  fi
  [[ -n "$WATCH_MSGID" ]] || die "--watch-replies requires --msgid <lore-msgid>."
  ensure_prepare

  CMD=("$PYTHON_BIN" -m a2a_cli.main watch --msgid "$WATCH_MSGID")
  if [[ -n "$WATCH_POLL" ]]; then
    CMD+=(--poll "$WATCH_POLL")
  fi
  if [[ -n "$WATCH_MAX_LOOPS" ]]; then
    CMD+=(--max-loops "$WATCH_MAX_LOOPS")
  fi
  if [[ "$AUTO_FOLLOWUP" == "true" ]]; then
    CMD+=(--auto-followup)
    if [[ -n "$SESSION_ID" ]]; then
      CMD+=(--session "$SESSION_ID")
    fi
    if [[ -n "$TASK" ]]; then
      CMD+=(--task "$TASK")
    fi
    if [[ -n "$MAX_ROUNDS" ]]; then
      CMD+=(--max-rounds "$MAX_ROUNDS")
    fi
    if [[ -n "$MAX_ITERATIONS" ]]; then
      CMD+=(--max-iterations "$MAX_ITERATIONS")
    fi
    if [[ -n "$LORE_OUT_DIR" ]]; then
      CMD+=(--lore-out-dir "$(resolve_path "$LORE_OUT_DIR")")
    fi
    if [[ -n "$BUILDER_CMD" ]]; then
      CMD+=(--builder-cmd "$BUILDER_CMD")
    fi
    if [[ -n "$REVIEWER_CMD" ]]; then
      CMD+=(--reviewer-cmd "$REVIEWER_CMD")
    fi
    if [[ ${#FOCUS_ISSUES[@]} -gt 0 ]]; then
      for issue in "${FOCUS_ISSUES[@]}"; do
        [[ -n "$issue" ]] || continue
        CMD+=(--focus-issue "$issue")
      done
    fi
  fi
  if ((${#NOTIFY_EMAILS[@]} > 0)); then
    for addr in "${NOTIFY_EMAILS[@]}"; do
      CMD+=(--notify-email "$addr")
    done
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
if [[ -n "$GITHUB_PR" && -n "$GERRIT_CHANGE" ]]; then
  die "Use only one of --github-pr or --gerrit-change."
fi
if [[ -n "$SOURCE_MSGID" && -n "$SESSION_ID" ]]; then
  die "--source-msgid cannot be combined with --session."
fi
if [[ -n "$SESSION_ID" && ( -n "$GITHUB_PR" || -n "$GERRIT_CHANGE" ) ]]; then
  die "--session cannot be combined with --github-pr/--gerrit-change."
fi
if [[ -n "$SOURCE_MSGID" && ( -n "$GITHUB_PR" || -n "$GERRIT_CHANGE" ) ]]; then
  die "--source-msgid is only supported with local path runs."
fi

if [[ -n "$GITHUB_PR" ]]; then
  SOURCE_KIND="github_pr"
  SOURCE="$GITHUB_PR"
elif [[ -n "$GERRIT_CHANGE" ]]; then
  SOURCE_KIND="gerrit_change"
  SOURCE="$GERRIT_CHANGE"
elif [[ -n "$SESSION_ID" ]]; then
  SOURCE_KIND="session"
elif [[ -n "$SOURCE" ]]; then
  if is_lore_url "$SOURCE"; then
    SOURCE_KIND="lore_url"
  elif is_github_pr_url "$SOURCE" || looks_like_github_pr_short "$SOURCE"; then
    SOURCE_KIND="github_pr"
  elif is_gerrit_change_url "$SOURCE"; then
    SOURCE_KIND="gerrit_change"
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
  echo "3) GitHub PR"
  echo "4) Gerrit change"
  echo "5) Lore message-id"
  echo "6) Local patch path"
  echo "7) Open wizard"
  choice="$(prompt "Choose 1/2/3/4/5/6/7" "7" true)"
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
      SOURCE_KIND="github_pr"
      SOURCE="$(prompt "GitHub PR (URL or owner/repo#number)" "" true)"
      ;;
    4)
      SOURCE_KIND="gerrit_change"
      SOURCE="$(prompt "Gerrit change URL/number/Change-Id" "" true)"
      GERRIT_BASE_URL="$(prompt "Gerrit base URL (optional for URL input)" "$GERRIT_BASE_URL" false)"
      ;;
    5)
      SOURCE_KIND="msgid"
      SOURCE="$(prompt "Lore message-id" "" true)"
      ;;
    6)
      SOURCE_KIND="path"
      SOURCE="$(resolve_path "$(prompt "Patch file or patch directory path" "" true)")"
      [[ -e "$SOURCE" ]] || die "Path not found: $SOURCE"
      ;;
    7)
      exec "$PYTHON_BIN" "$ROOT_DIR/scripts/a2a_loop_wizard.py"
      ;;
    *)
      die "Invalid selection: $choice"
      ;;
  esac
fi

[[ -n "$SOURCE_KIND" ]] || die "Could not determine input type. Pass lore URL/msgid/session/path."

# Backward-compatible shortcut: in local path mode, allow --msgid to seed prior/bot source context.
if [[ "$WATCH_REPLIES" != "true" && "$SOURCE_KIND" == "path" && -z "$SOURCE_MSGID" && -n "$WATCH_MSGID" ]]; then
  SOURCE_MSGID="$WATCH_MSGID"
fi

ensure_prepare

CMD=("$PYTHON_BIN" -m a2a_cli.main loop)

if [[ "$SOURCE_KIND" == "session" ]]; then
  [[ -n "$SESSION_ID" ]] || die "Missing session id"
  if [[ -n "$EXTEND_ROUNDS" ]] && ! [[ "$EXTEND_ROUNDS" =~ ^[0-9]+$ ]]; then
    die "--extend-rounds must be a non-negative integer."
  fi
  if [[ -z "$EXTEND_ROUNDS" && -t 0 ]]; then
    if snapshot="$(session_snapshot "$SESSION_ID" 2>/dev/null)"; then
      IFS='|' read -r sess_status sess_round sess_max sess_open <<<"$snapshot"
      if [[ "$sess_status" == "stopped" || "${sess_round:-0}" -ge "${sess_max:-0}" ]]; then
        if prompt_yes_no "Session $SESSION_ID is $sess_status at ${sess_round}/${sess_max} (open findings: ${sess_open}). Extend rounds before resume?" true; then
          EXTEND_ROUNDS="$(prompt "Extend rounds by" "5" true)"
          [[ "$EXTEND_ROUNDS" =~ ^[0-9]+$ ]] || die "Extend rounds must be a non-negative integer."
        fi
      fi
    fi
  fi
  CMD+=(--session "$SESSION_ID")
  if [[ -n "$MAX_ROUNDS" ]]; then
    CMD+=(--max-rounds "$MAX_ROUNDS")
  fi
  if [[ -n "$EXTEND_ROUNDS" && "$EXTEND_ROUNDS" != "0" ]]; then
    CMD+=(--extend-rounds "$EXTEND_ROUNDS")
  fi
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
    github_pr)
      CMD+=(--github-pr "$SOURCE")
      ;;
    gerrit_change)
      CMD+=(--gerrit-change "$SOURCE")
      if [[ -n "$GERRIT_BASE_URL" ]]; then
        CMD+=(--gerrit-base-url "$GERRIT_BASE_URL")
      fi
      ;;
    path)
      CMD+=(--watch-path "$SOURCE")
      if [[ -n "$SOURCE_MSGID" ]]; then
        CMD+=(--source-msgid "$SOURCE_MSGID")
      fi
      ;;
    *)
      die "Unsupported source kind: $SOURCE_KIND"
      ;;
  esac

if [[ -n "$LORE_OUT_DIR" ]]; then
  CMD+=(--lore-out-dir "$(resolve_path "$LORE_OUT_DIR")")
fi
if [[ -n "$FETCH_OUT_DIR" ]]; then
  CMD+=(--fetch-out-dir "$(resolve_path "$FETCH_OUT_DIR")")
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
if [[ ${#FOCUS_ISSUES[@]} -gt 0 ]]; then
  for issue in "${FOCUS_ISSUES[@]}"; do
    [[ -n "$issue" ]] || continue
    CMD+=(--focus-issue "$issue")
  done
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

# Interactive convenience: lore loop can immediately offer watcher auto-followup.
if [[ "$WATCH_REPLIES" != "true" && "$AUTO_FOLLOWUP" != "true" && -t 0 ]]; then
  case "$SOURCE_KIND" in
    lore_url|msgid)
      if prompt_yes_no "Enable lore auto-followup watcher after this run?" false; then
        WATCH_AFTER_LOOP="true"
        AUTO_FOLLOWUP="true"
        if [[ "$SOURCE_KIND" == "msgid" ]]; then
          WATCH_MSGID="$SOURCE"
        else
          if WATCH_MSGID="$(extract_lore_msgid "$SOURCE")"; then
            :
          else
            die "Could not extract lore message-id from URL: $SOURCE"
          fi
        fi
      fi
      ;;
  esac
fi

# Non-watch mode: if --auto-followup was explicitly provided, enable post-run watcher.
if [[ "$WATCH_REPLIES" != "true" && "$AUTO_FOLLOWUP" == "true" ]]; then
  WATCH_AFTER_LOOP="true"
  if [[ -z "$WATCH_MSGID" ]]; then
    case "$SOURCE_KIND" in
      msgid)
        WATCH_MSGID="$SOURCE"
        ;;
      lore_url)
        if WATCH_MSGID="$(extract_lore_msgid "$SOURCE")"; then
          :
        else
          die "Could not extract lore message-id from URL: $SOURCE"
        fi
        ;;
      session)
        if WATCH_MSGID="$(extract_session_lore_msgid "$SESSION_ID")"; then
          :
        else
          die "--auto-followup with --session requires lore message-id in session metadata (or pass --msgid)."
        fi
        ;;
      path)
        die "--auto-followup with local path requires --msgid <lore-msgid>."
        ;;
      *)
        die "--auto-followup is supported for lore URL/msgid/session (with lore metadata)."
        ;;
    esac
  fi
fi

WATCH_CMD=()
if [[ "$WATCH_AFTER_LOOP" == "true" ]]; then
  WATCH_CMD=("$PYTHON_BIN" -m a2a_cli.main watch --msgid "$WATCH_MSGID" --auto-followup)
  if [[ -n "$TASK" ]]; then
    WATCH_CMD+=(--task "$TASK")
  fi
  if [[ -n "$MAX_ROUNDS" ]]; then
    WATCH_CMD+=(--max-rounds "$MAX_ROUNDS")
  fi
  if [[ -n "$MAX_ITERATIONS" ]]; then
    WATCH_CMD+=(--max-iterations "$MAX_ITERATIONS")
  fi
  if [[ -n "$LORE_OUT_DIR" ]]; then
    WATCH_CMD+=(--lore-out-dir "$(resolve_path "$LORE_OUT_DIR")")
  fi
  if [[ -n "$BUILDER_CMD" ]]; then
    WATCH_CMD+=(--builder-cmd "$BUILDER_CMD")
  fi
  if [[ -n "$REVIEWER_CMD" ]]; then
    WATCH_CMD+=(--reviewer-cmd "$REVIEWER_CMD")
  fi
  if [[ ${#FOCUS_ISSUES[@]} -gt 0 ]]; then
    for issue in "${FOCUS_ISSUES[@]}"; do
      [[ -n "$issue" ]] || continue
      WATCH_CMD+=(--focus-issue "$issue")
    done
  fi
fi

echo
echo "Command:"
printf '  %q' "${CMD[@]}"
echo
if [[ "$WATCH_AFTER_LOOP" == "true" ]]; then
  echo
  echo "Post-run watcher command:"
  printf '  %q' "${WATCH_CMD[@]}"
  echo
fi
echo

if [[ "$ASSUME_YES" != "true" && -t 0 ]]; then
  if ! prompt_yes_no "Run now?" true; then
    echo "Cancelled."
    exit 0
  fi
fi

cd "$ROOT_DIR"
if [[ "$WATCH_AFTER_LOOP" != "true" ]]; then
  exec "${CMD[@]}"
fi

"${CMD[@]}"
LOOP_RC=$?
if [[ $LOOP_RC -ne 0 ]]; then
  if [[ -t 0 ]]; then
    if ! prompt_yes_no "Loop exited with rc=$LOOP_RC. Start watcher anyway?" false; then
      exit "$LOOP_RC"
    fi
  else
    exit "$LOOP_RC"
  fi
fi

exec "${WATCH_CMD[@]}"
