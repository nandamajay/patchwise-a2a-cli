from __future__ import annotations

import email
import imaplib
import json
import os
import re
import secrets
import shlex
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from .email_notify import send_email

_LORE_URL_RE = re.compile(r"https?://lore\.kernel\.org/(?:all|r)/[^\s<>()\"']+", re.IGNORECASE)
_GITHUB_PR_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<number>\d+)[^\s<>()\"']*",
    re.IGNORECASE,
)
_GERRIT_CHANGE_URL_RE = re.compile(
    r"https?://[^\s<>()\"']+/\S*/\+/\d+(?:[/?#][^\s<>()\"']*)?",
    re.IGNORECASE,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _is_yes(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    norm = str(value).strip().lower()
    if not norm:
        return default
    return norm in {"1", "true", "yes", "y", "on"}


def _sanitize_name(value: str) -> str:
    out = []
    for ch in value:
        if ch.isalnum() or ch in {".", "_", "-", "@"}:
            out.append(ch)
        else:
            out.append("-")
    text = "".join(out).strip("-")
    return text or "item"


@dataclass
class BridgeConfig:
    root: Path
    poll_sec: int
    imap_host: str
    imap_port: int
    imap_user: str
    imap_password: str
    mailbox: str
    smtp_from: str
    allowed_senders: set[str]
    notify_to: list[str]
    inbox_dir: Path
    state_db: Path
    default_lore_out_dir: str
    default_max_rounds: int
    approval_token_ttl_min: int
    auto_detect_requests: bool
    python_bin: str


def load_bridge_config(root: Path, overrides: dict[str, Any]) -> BridgeConfig:
    cfg_path = root / ".a2a" / "config.json"
    payload: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            payload = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
    bridge_cfg = payload.get("email_bridge", {}) if isinstance(payload, dict) else {}
    upstream = payload.get("upstream_evidence", {}) if isinstance(payload, dict) else {}

    imap_host = str(overrides.get("imap_host") or bridge_cfg.get("imap_host") or "").strip()
    imap_port = _safe_int(overrides.get("imap_port") or bridge_cfg.get("imap_port") or 993, 993)
    imap_user = str(overrides.get("imap_user") or bridge_cfg.get("imap_user") or "").strip()
    pass_env = str(
        overrides.get("imap_password_env") or bridge_cfg.get("imap_password_env") or "A2A_EMAIL_IMAP_PASSWORD"
    ).strip()
    imap_password = str(overrides.get("imap_password") or "").strip()
    if not imap_password and pass_env:
        imap_password = str(os.getenv(pass_env, "")).strip()

    mailbox = str(overrides.get("mailbox") or bridge_cfg.get("mailbox") or "INBOX").strip() or "INBOX"
    poll_sec = _safe_int(overrides.get("poll_sec") or bridge_cfg.get("poll_sec") or 60, 60)
    smtp_from = str(overrides.get("smtp_from") or bridge_cfg.get("smtp_from") or "").strip()
    lore_fetch_dir = str(
        overrides.get("lore_out_dir")
        or bridge_cfg.get("lore_fetch_dir")
        or payload.get("lore_fetch_dir")
        or ""
    ).strip()
    if not lore_fetch_dir:
        lore_fetch_dir = str((upstream.get("kernel_tree") if isinstance(upstream, dict) else "") or "").strip()

    allowed_rows = overrides.get("allowed_senders") or bridge_cfg.get("allowed_senders") or []
    notify_rows = overrides.get("notify_to") or bridge_cfg.get("notify_to") or []
    if isinstance(allowed_rows, str):
        allowed_rows = [allowed_rows]
    if isinstance(notify_rows, str):
        notify_rows = [notify_rows]
    allowed_senders = {str(row).strip().lower() for row in allowed_rows if str(row).strip()}
    notify_to = [str(row).strip() for row in notify_rows if str(row).strip()]

    inbox_dir = Path(
        str(overrides.get("inbox_dir") or bridge_cfg.get("inbox_dir") or root / ".a2a" / "email_bridge" / "inbox")
    ).expanduser()
    state_db = Path(
        str(overrides.get("state_db") or bridge_cfg.get("state_db") or root / ".a2a" / "email_bridge" / "bridge.db")
    ).expanduser()
    approval_token_ttl_min = _safe_int(
        overrides.get("approval_token_ttl_min") or bridge_cfg.get("approval_token_ttl_min") or 720,
        720,
    )
    auto_detect_requests = _is_yes(
        overrides.get("auto_detect_requests"),
        default=_is_yes(bridge_cfg.get("auto_detect_requests"), default=False),
    )

    return BridgeConfig(
        root=root,
        poll_sec=max(5, poll_sec),
        imap_host=imap_host,
        imap_port=imap_port,
        imap_user=imap_user,
        imap_password=imap_password,
        mailbox=mailbox,
        smtp_from=smtp_from,
        allowed_senders=allowed_senders,
        notify_to=notify_to,
        inbox_dir=inbox_dir.resolve(),
        state_db=state_db.resolve(),
        default_lore_out_dir=lore_fetch_dir,
        default_max_rounds=_safe_int(payload.get("default_max_rounds", 3), 3),
        approval_token_ttl_min=max(10, approval_token_ttl_min),
        auto_detect_requests=bool(auto_detect_requests),
        python_bin=str(overrides.get("python_bin") or os.getenv("PYTHON", "") or "python"),
    )


class BridgeStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
              message_id TEXT PRIMARY KEY,
              sender TEXT NOT NULL,
              subject TEXT NOT NULL,
              command TEXT NOT NULL,
              status TEXT NOT NULL,
              session_id TEXT NOT NULL,
              received_at TEXT NOT NULL,
              responded_at TEXT NOT NULL,
              error TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS approvals (
              token TEXT PRIMARY KEY,
              session_id TEXT NOT NULL,
              action TEXT NOT NULL,
              issued_to TEXT NOT NULL,
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              used_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS session_alerts (
              session_id TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              current_round INTEGER NOT NULL,
              open_findings INTEGER NOT NULL,
              last_notified_at TEXT NOT NULL,
              last_token TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS session_watchers (
              session_id TEXT NOT NULL,
              email TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY (session_id, email)
            )
            """
        )
        self.conn.commit()

    def is_processed(self, message_id: str) -> bool:
        cur = self.conn.cursor()
        row = cur.execute(
            "SELECT 1 FROM processed_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()
        return row is not None

    def mark_processed(
        self,
        *,
        message_id: str,
        sender: str,
        subject: str,
        command: str,
        status: str,
        session_id: str = "",
        error: str = "",
    ) -> None:
        now = utc_now()
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO processed_messages
              (message_id, sender, subject, command, status, session_id, received_at, responded_at, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (message_id, sender, subject, command, status, session_id, now, now, error),
        )
        self.conn.commit()

    def create_approval_token(self, *, session_id: str, action: str, issued_to: str, ttl_min: int) -> str:
        token = secrets.token_urlsafe(18)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=ttl_min)
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO approvals (token, session_id, action, issued_to, created_at, expires_at, used_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token,
                session_id,
                action,
                issued_to,
                now.isoformat(),
                expires.isoformat(),
                "",
            ),
        )
        self.conn.commit()
        return token

    def consume_approval_token(self, *, token: str, session_id: str, action: str, sender: str) -> tuple[bool, str]:
        cur = self.conn.cursor()
        row = cur.execute(
            "SELECT * FROM approvals WHERE token=? AND session_id=? AND action=?",
            (token, session_id, action),
        ).fetchone()
        if row is None:
            return False, "token not found"
        if str(row["used_at"] or "").strip():
            return False, "token already used"
        issued_to = str(row["issued_to"] or "").strip().lower()
        if issued_to and issued_to != sender.strip().lower():
            return False, "token sender mismatch"
        expires_raw = str(row["expires_at"] or "").strip()
        if not expires_raw:
            return False, "token missing expiry"
        try:
            expires_at = datetime.fromisoformat(expires_raw)
        except ValueError:
            return False, "token expiry invalid"
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires_at:
            return False, "token expired"
        cur.execute(
            "UPDATE approvals SET used_at=? WHERE token=?",
            (utc_now(), token),
        )
        self.conn.commit()
        return True, ""

    def upsert_session_alert(
        self,
        *,
        session_id: str,
        status: str,
        current_round: int,
        open_findings: int,
        last_token: str = "",
    ) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO session_alerts
              (session_id, status, current_round, open_findings, last_notified_at, last_token)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, status, current_round, open_findings, utc_now(), last_token),
        )
        self.conn.commit()

    def get_session_alert(self, session_id: str) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        row = cur.execute(
            "SELECT * FROM session_alerts WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def add_session_watcher(self, session_id: str, email_addr: str) -> None:
        norm = email_addr.strip().lower()
        if not norm:
            return
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO session_watchers (session_id, email, created_at)
            VALUES (?, ?, ?)
            """,
            (session_id, norm, utc_now()),
        )
        self.conn.commit()

    def session_watchers(self, session_id: str) -> list[str]:
        cur = self.conn.cursor()
        rows = cur.execute(
            "SELECT email FROM session_watchers WHERE session_id=? ORDER BY email",
            (session_id,),
        ).fetchall()
        return [str(row["email"]) for row in rows]


def _extract_sender_addr(msg: Message) -> str:
    _name, addr = parseaddr(str(msg.get("From", "")))
    return addr.strip().lower()


def _extract_text_body(msg: Message) -> str:
    if msg.is_multipart():
        out: list[str] = []
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", "")).lower()
            if ctype != "text/plain" or "attachment" in disp:
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                out.append(payload.decode(charset, errors="replace"))
            except Exception:
                out.append(payload.decode("utf-8", errors="replace"))
        return "\n".join(out).strip()
    payload = msg.get_payload(decode=True)
    if payload is None:
        return ""
    charset = msg.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace").strip()
    except Exception:
        return payload.decode("utf-8", errors="replace").strip()


def _save_patch_attachments(cfg: BridgeConfig, msg: Message, message_id: str) -> list[Path]:
    safe_id = _sanitize_name(message_id)
    out_dir = cfg.inbox_dir / safe_id
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    if not msg.is_multipart():
        return saved
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = str(part.get_filename() or "").strip()
        if not filename:
            continue
        lower = filename.lower()
        if not (lower.endswith(".patch") or lower.endswith(".diff")):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        target = out_dir / _sanitize_name(Path(filename).name)
        target.write_bytes(payload)
        saved.append(target)
    return saved


def _first_non_quoted_line(text: str) -> str:
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(">"):
            continue
        if line.lower().startswith("on ") and " wrote:" in line.lower():
            continue
        return line
    return ""


def parse_a2a_command(subject: str, body: str) -> dict[str, Any]:
    subject_line = subject.strip()
    cmd_line = ""
    if subject_line.upper().startswith("A2A "):
        cmd_line = subject_line
    if not cmd_line:
        for raw in body.splitlines():
            line = raw.strip()
            if not line or line.startswith(">"):
                continue
            if line.upper().startswith("A2A "):
                cmd_line = line
                break
    if not cmd_line:
        return {"command": "none", "mode": "", "params": {}, "raw": ""}

    try:
        parts = shlex.split(cmd_line)
    except ValueError:
        parts = cmd_line.split()
    if not parts or parts[0].upper() != "A2A":
        return {"command": "invalid", "mode": "", "params": {}, "raw": cmd_line}

    params: dict[str, str] = {}
    for token in parts[1:]:
        if "=" in token:
            key, value = token.split("=", 1)
            params[key.strip().upper()] = value.strip()

    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith(">"):
            continue
        if line.upper().startswith("A2A "):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        params[key.strip().upper()] = value.strip()

    words = [w.upper() for w in parts[1:] if "=" not in w]
    command = words[0] if words else "help"
    mode = words[1] if len(words) > 1 else ""
    return {
        "command": command.lower(),
        "mode": mode.lower(),
        "params": params,
        "raw": cmd_line,
    }


def _trim_url_token(value: str) -> str:
    out = value.strip()
    while out and out[-1] in {".", ",", ";", ":", "!", "?", ")", "]", "}"}:
        out = out[:-1]
    return out


def _extract_lore_url(text: str) -> str:
    if not text.strip():
        return ""
    match = _LORE_URL_RE.search(text)
    if not match:
        return ""
    return _trim_url_token(match.group(0))


def _extract_github_pr_url(text: str) -> str:
    if not text.strip():
        return ""
    match = _GITHUB_PR_URL_RE.search(text)
    if not match:
        return ""
    return _trim_url_token(match.group(0))


def _extract_gerrit_change_url(text: str) -> str:
    if not text.strip():
        return ""
    match = _GERRIT_CHANGE_URL_RE.search(text)
    if not match:
        return ""
    return _trim_url_token(match.group(0))


def _infer_auto_run_request(subject: str, body: str, attachments: list[Path]) -> dict[str, Any] | None:
    patch_files = [p for p in attachments if p.suffix.lower() in {".patch", ".diff"}]
    if patch_files:
        return {
            "command": "run",
            "mode": "attachment",
            "params": {},
            "raw": "AUTO RUN ATTACHMENT",
        }

    combined = "\n".join([subject or "", body or ""])
    github_pr_url = _extract_github_pr_url(combined)
    if github_pr_url:
        return {
            "command": "run",
            "mode": "github",
            "params": {"PR": github_pr_url},
            "raw": f"AUTO RUN GITHUB PR={github_pr_url}",
        }

    gerrit_change_url = _extract_gerrit_change_url(combined)
    if gerrit_change_url:
        return {
            "command": "run",
            "mode": "gerrit",
            "params": {"CHANGE": gerrit_change_url},
            "raw": f"AUTO RUN GERRIT CHANGE={gerrit_change_url}",
        }

    lore_url = _extract_lore_url(combined)
    if lore_url:
        return {
            "command": "run",
            "mode": "lore",
            "params": {"URL": lore_url},
            "raw": f"AUTO RUN LORE URL={lore_url}",
        }
    return None


def _list_sessions(root: Path, limit: int = 12) -> list[dict[str, Any]]:
    sess_dir = root / ".a2a" / "sessions"
    if not sess_dir.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(sess_dir.glob("sess-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        rows.append(payload)
    return rows


def _session_ids(root: Path) -> set[str]:
    sess_dir = root / ".a2a" / "sessions"
    if not sess_dir.exists():
        return set()
    return {p.stem for p in sess_dir.glob("sess-*.json")}


def _load_session(root: Path, session_id: str) -> dict[str, Any]:
    path = root / ".a2a" / "sessions" / f"{session_id}.json"
    if not path.exists():
        raise RuntimeError(f"Session not found: {session_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def extend_stopped_session_once(root: Path, session_id: str) -> dict[str, Any]:
    path = root / ".a2a" / "sessions" / f"{session_id}.json"
    if not path.exists():
        raise RuntimeError(f"Session not found: {session_id}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    status = str(payload.get("status", "")).lower()
    if status == "lgtm":
        raise RuntimeError("Session already LGTM; cannot extend.")
    if status != "stopped":
        raise RuntimeError(f"Session status is '{status}', expected 'stopped'.")
    current = _safe_int(payload.get("current_round"), 1)
    max_rounds = _safe_int(payload.get("max_rounds"), current)
    next_round = current + 1
    payload["current_round"] = next_round
    payload["max_rounds"] = max(max_rounds, next_round)
    payload["status"] = "in_progress"
    payload["updated_at"] = utc_now()
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def _spawn_command(root: Path, cmd: list[str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab", buffering=0) as fh:
        proc = subprocess.Popen(
            cmd,
            cwd=root,
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return int(proc.pid)


def _wait_for_new_session(root: Path, before_ids: set[str], timeout_sec: int = 180) -> str:
    deadline = time.time() + max(5, timeout_sec)
    while time.time() < deadline:
        now_ids = _session_ids(root)
        new_ids = sorted(now_ids - before_ids)
        if new_ids:
            return new_ids[-1]
        time.sleep(1)
    return ""


def _build_help_text() -> str:
    return (
        "A2A email commands\n"
        "\n"
        "1) Status\n"
        "A2A STATUS\n"
        "A2A STATUS SESSION=sess-...\n"
        "\n"
        "2) Start lore review\n"
        "A2A RUN LORE URL=https://lore.kernel.org/all/<msgid>/ TASK=my-task MAX_ROUNDS=3\n"
        "\n"
        "3) Start file-based review\n"
        "A2A RUN FILE WATCH_PATH=/abs/path/to/patch_or_series TASK=my-task MAX_ROUNDS=3\n"
        "\n"
        "4) Start GitHub PR review\n"
        "A2A RUN GITHUB PR=https://github.com/<owner>/<repo>/pull/<n> TASK=my-task MAX_ROUNDS=3\n"
        "\n"
        "5) Start Gerrit change review\n"
        "A2A RUN GERRIT CHANGE=https://review.example.com/c/project/+/12345 TASK=my-task MAX_ROUNDS=3\n"
        "A2A RUN GERRIT CHANGE=12345 GERRIT_BASE_URL=https://review.example.com TASK=my-task\n"
        "\n"
        "6) Start from email patch attachment\n"
        "A2A RUN ATTACHMENT TASK=my-task MAX_ROUNDS=3\n"
        "Attach .patch files in the same email.\n"
        "\n"
        "7) Resume session\n"
        "A2A RESUME SESSION=sess-...\n"
        "\n"
        "8) Extend stopped session by one round\n"
        "A2A EXTEND SESSION=sess-... TOKEN=<token> AUTO_RUN=yes\n"
    )


def _status_text(root: Path, session_id: str | None = None) -> str:
    if session_id:
        sess = _load_session(root, session_id)
        return (
            f"Session: {session_id}\n"
            f"Status: {sess.get('status')}\n"
            f"Task: {sess.get('task')}\n"
            f"Round: {sess.get('current_round')}/{sess.get('max_rounds')}\n"
            f"Open findings: {sess.get('open_findings')}\n"
            f"Watch path: {sess.get('watch_path')}\n"
        )
    rows = _list_sessions(root, limit=12)
    if not rows:
        return "No sessions found."
    out = ["Recent sessions:"]
    for row in rows:
        sid = str(row.get("id", ""))
        status = str(row.get("status", "unknown"))
        cur = _safe_int(row.get("current_round"), 0)
        mx = _safe_int(row.get("max_rounds"), 0)
        task = str(row.get("task", ""))[:80]
        out.append(f"- {sid}  status={status}  round={cur}/{mx}  task={task}")
    return "\n".join(out) + "\n"


def _send_response(cfg: BridgeConfig, to_addr: str, subject: str, body: str) -> None:
    send_email(
        subject=subject,
        body=body,
        to_addrs=[to_addr],
        override_from=cfg.smtp_from or None,
    )


def _build_loop_cmd_for_request(
    cfg: BridgeConfig,
    *,
    mode: str,
    params: dict[str, str],
    watch_path: str = "",
    lore_url: str = "",
    github_pr: str = "",
    gerrit_change: str = "",
    gerrit_base_url: str = "",
) -> list[str]:
    task = params.get("TASK", "").strip() or f"email-review-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    max_rounds = _safe_int(params.get("MAX_ROUNDS"), cfg.default_max_rounds or 3)
    max_iterations = _safe_int(params.get("MAX_ITERATIONS"), 0)
    builder_cmd = params.get("BUILDER_CMD", "").strip()
    reviewer_cmd = params.get("REVIEWER_CMD", "").strip()
    cmd = [
        cfg.python_bin,
        "-m",
        "a2a_cli.main",
        "loop",
        "--task",
        task,
        "--max-rounds",
        str(max_rounds),
    ]
    fetch_out_dir = params.get("FETCH_OUT_DIR", "").strip() or params.get("LORE_OUT_DIR", "").strip()
    if mode == "file":
        cmd.extend(["--watch-path", watch_path])
    elif mode == "lore":
        cmd.extend(["--lore-url", lore_url])
        lore_out_dir = params.get("LORE_OUT_DIR", "").strip() or cfg.default_lore_out_dir
        if lore_out_dir:
            cmd.extend(["--lore-out-dir", str(Path(lore_out_dir).expanduser().resolve())])
    elif mode == "github":
        cmd.extend(["--github-pr", github_pr])
        if fetch_out_dir:
            cmd.extend(["--fetch-out-dir", str(Path(fetch_out_dir).expanduser().resolve())])
    elif mode == "gerrit":
        cmd.extend(["--gerrit-change", gerrit_change])
        if gerrit_base_url:
            cmd.extend(["--gerrit-base-url", gerrit_base_url])
        if fetch_out_dir:
            cmd.extend(["--fetch-out-dir", str(Path(fetch_out_dir).expanduser().resolve())])
    if max_iterations > 0:
        cmd.extend(["--max-iterations", str(max_iterations)])
    if builder_cmd:
        cmd.extend(["--builder-cmd", builder_cmd])
    if reviewer_cmd:
        cmd.extend(["--reviewer-cmd", reviewer_cmd])
    if _is_yes(params.get("AUTO_RESPIN"), default=(mode == "lore")):
        cmd.append("--auto-respin")
    else:
        cmd.append("--no-auto-respin")
    return cmd


def _spawn_loop_and_track(
    cfg: BridgeConfig,
    store: BridgeStore,
    sender: str,
    cmd: list[str],
    *,
    known_session: str = "",
    detect_new_session: bool = False,
) -> tuple[int, str, Path]:
    runs_dir = cfg.root / ".a2a" / "email_bridge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log_path = runs_dir / f"run-{stamp}.log"
    before_ids = _session_ids(cfg.root) if detect_new_session else set()
    pid = _spawn_command(cfg.root, cmd, log_path)
    session_id = known_session
    if detect_new_session:
        session_id = _wait_for_new_session(cfg.root, before_ids, timeout_sec=180)
    if session_id:
        store.add_session_watcher(session_id, sender)
    return pid, session_id, log_path


def handle_command(
    cfg: BridgeConfig,
    store: BridgeStore,
    sender: str,
    parsed: dict[str, Any],
    attachments: list[Path],
) -> tuple[str, str]:
    command = str(parsed.get("command", "none")).lower()
    mode = str(parsed.get("mode", "")).lower()
    params = parsed.get("params", {})
    if not isinstance(params, dict):
        params = {}

    if command in {"none", ""}:
        return "ignored", "No A2A command found in subject/body. Send `A2A HELP` for examples."
    if command in {"help", "options"}:
        return "ok", _build_help_text()
    if command in {"status", "sessions", "list"}:
        sid = str(params.get("SESSION", "")).strip()
        return "ok", _status_text(cfg.root, session_id=sid or None)
    if command == "resume":
        sid = str(params.get("SESSION", "")).strip()
        if not sid:
            return "error", "Missing SESSION for resume command."
        cmd = [cfg.python_bin, "-m", "a2a_cli.main", "loop", "--session", sid]
        pid, session_id, log_path = _spawn_loop_and_track(cfg, store, sender, cmd, known_session=sid)
        return "ok", (
            f"Resume scheduled.\nSession: {session_id or sid}\nPID: {pid}\nLog: {log_path}\n"
            f"Command: {shlex.join(cmd)}\n"
        )
    if command == "extend":
        sid = str(params.get("SESSION", "")).strip()
        token = str(params.get("TOKEN", "")).strip()
        if not sid:
            return "error", "Missing SESSION for extend command."
        if not token:
            return "error", "Missing TOKEN for extend command."
        ok, reason = store.consume_approval_token(
            token=token,
            session_id=sid,
            action="extend",
            sender=sender,
        )
        if not ok:
            return "error", f"Extend approval denied: {reason}"
        payload = extend_stopped_session_once(cfg.root, sid)
        auto_run = _is_yes(str(params.get("AUTO_RUN", "yes")), default=True)
        if auto_run:
            cmd = [cfg.python_bin, "-m", "a2a_cli.main", "loop", "--session", sid]
            pid, session_id, log_path = _spawn_loop_and_track(cfg, store, sender, cmd, known_session=sid)
            return "ok", (
                f"Session extended and resumed.\nSession: {session_id or sid}\n"
                f"Round: {payload.get('current_round')}/{payload.get('max_rounds')}\n"
                f"PID: {pid}\nLog: {log_path}\nCommand: {shlex.join(cmd)}\n"
            )
        return "ok", (
            f"Session extended.\nSession: {sid}\n"
            f"Round: {payload.get('current_round')}/{payload.get('max_rounds')}\n"
            "Auto run skipped (AUTO_RUN=no).\n"
        )
    if command == "run":
        if mode == "lore":
            lore_url = str(params.get("URL", "") or params.get("LORE_URL", "")).strip()
            if not lore_url:
                return "error", "Missing URL/LORE_URL for `A2A RUN LORE`."
            cmd = _build_loop_cmd_for_request(cfg, mode="lore", params=params, lore_url=lore_url)
            pid, session_id, log_path = _spawn_loop_and_track(
                cfg,
                store,
                sender,
                cmd,
                detect_new_session=True,
            )
            return "ok", (
                "Lore review scheduled.\n"
                f"Session: {session_id or 'pending'}\nPID: {pid}\nLog: {log_path}\n"
                f"Command: {shlex.join(cmd)}\n"
            )
        if mode == "github":
            pr_ref = str(params.get("PR", "") or params.get("GITHUB_PR", "")).strip()
            if not pr_ref:
                return "error", "Missing PR/GITHUB_PR for `A2A RUN GITHUB`."
            cmd = _build_loop_cmd_for_request(
                cfg,
                mode="github",
                params=params,
                github_pr=pr_ref,
            )
            pid, session_id, log_path = _spawn_loop_and_track(
                cfg,
                store,
                sender,
                cmd,
                detect_new_session=True,
            )
            return "ok", (
                "GitHub PR review scheduled.\n"
                f"Session: {session_id or 'pending'}\nPID: {pid}\nLog: {log_path}\n"
                f"Command: {shlex.join(cmd)}\n"
            )
        if mode == "gerrit":
            change_ref = str(params.get("CHANGE", "") or params.get("GERRIT_CHANGE", "")).strip()
            if not change_ref:
                return "error", "Missing CHANGE/GERRIT_CHANGE for `A2A RUN GERRIT`."
            gerrit_base_url = str(params.get("GERRIT_BASE_URL", "")).strip()
            cmd = _build_loop_cmd_for_request(
                cfg,
                mode="gerrit",
                params=params,
                gerrit_change=change_ref,
                gerrit_base_url=gerrit_base_url,
            )
            pid, session_id, log_path = _spawn_loop_and_track(
                cfg,
                store,
                sender,
                cmd,
                detect_new_session=True,
            )
            return "ok", (
                "Gerrit change review scheduled.\n"
                f"Session: {session_id or 'pending'}\nPID: {pid}\nLog: {log_path}\n"
                f"Command: {shlex.join(cmd)}\n"
            )
        if mode == "file":
            watch_path = str(params.get("WATCH_PATH", "")).strip()
            if not watch_path:
                return "error", "Missing WATCH_PATH for `A2A RUN FILE`."
            resolved = Path(watch_path).expanduser().resolve()
            cmd = _build_loop_cmd_for_request(
                cfg,
                mode="file",
                params=params,
                watch_path=str(resolved),
            )
            pid, session_id, log_path = _spawn_loop_and_track(
                cfg,
                store,
                sender,
                cmd,
                detect_new_session=True,
            )
            return "ok", (
                "File-based review scheduled.\n"
                f"Session: {session_id or 'pending'}\nPID: {pid}\nLog: {log_path}\n"
                f"Command: {shlex.join(cmd)}\n"
            )
        if mode == "attachment":
            patch_files = [p for p in attachments if p.suffix.lower() in {".patch", ".diff"}]
            if not patch_files:
                return "error", "No .patch/.diff attachments found for `A2A RUN ATTACHMENT`."
            if len(patch_files) == 1:
                watch_path = str(patch_files[0])
            else:
                watch_path = str(patch_files[0].parent)
            cmd = _build_loop_cmd_for_request(
                cfg,
                mode="file",
                params=params,
                watch_path=watch_path,
            )
            pid, session_id, log_path = _spawn_loop_and_track(
                cfg,
                store,
                sender,
                cmd,
                detect_new_session=True,
            )
            return "ok", (
                "Attachment review scheduled.\n"
                f"Session: {session_id or 'pending'}\nPID: {pid}\nLog: {log_path}\n"
                f"Watch path: {watch_path}\n"
                f"Command: {shlex.join(cmd)}\n"
            )
        return "error", "Unsupported RUN mode. Use: RUN LORE, RUN GITHUB, RUN GERRIT, RUN FILE, or RUN ATTACHMENT."
    return "error", "Unsupported command. Send `A2A HELP`."


def _collect_notification_recipients(cfg: BridgeConfig, store: BridgeStore, session_id: str) -> list[str]:
    out: list[str] = []
    for addr in cfg.notify_to + store.session_watchers(session_id):
        norm = str(addr).strip().lower()
        if norm and norm not in out:
            out.append(norm)
    return out


def _load_latest_round_summary(root: Path, session_id: str) -> dict[str, Any]:
    report_dir = root / ".a2a" / "reports" / session_id
    rows = sorted(report_dir.glob("round-*-summary.json"))
    if not rows:
        return {}
    try:
        payload = json.loads(rows[-1].read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def process_session_notifications_once(cfg: BridgeConfig, store: BridgeStore) -> int:
    sessions = _list_sessions(cfg.root, limit=200)
    sent = 0
    for row in sessions:
        sid = str(row.get("id") or "").strip()
        if not sid:
            continue
        status = str(row.get("status") or "unknown").lower()
        current_round = _safe_int(row.get("current_round"), 0)
        open_findings = _safe_int(row.get("open_findings"), 0)
        prev = store.get_session_alert(sid)
        if prev is None:
            # Baseline at startup; avoid notification flood for historical sessions.
            store.upsert_session_alert(
                session_id=sid,
                status=status,
                current_round=current_round,
                open_findings=open_findings,
                last_token="",
            )
            continue
        changed = (
            status != str(prev.get("status") or "")
            or current_round != _safe_int(prev.get("current_round"), 0)
            or open_findings != _safe_int(prev.get("open_findings"), 0)
        )
        if not changed:
            continue

        token = str(prev.get("last_token") or "")
        summary = _load_latest_round_summary(cfg.root, sid)
        top_issue = ""
        if isinstance(summary, dict):
            findings = summary.get("findings", {})
            if isinstance(findings, dict):
                open_items = findings.get("open_items", [])
                if isinstance(open_items, list) and open_items:
                    first = open_items[0]
                    if isinstance(first, dict):
                        top_issue = str(first.get("title") or "").strip()

        body_lines = [
            f"Session: {sid}",
            f"Task: {row.get('task')}",
            f"Status: {status}",
            f"Round: {current_round}/{_safe_int(row.get('max_rounds'), current_round)}",
            f"Open findings: {open_findings}",
        ]
        if top_issue:
            body_lines.append(f"Top issue: {top_issue}")

        if status == "stopped" and open_findings > 0:
            token = store.create_approval_token(
                session_id=sid,
                action="extend",
                issued_to="",
                ttl_min=cfg.approval_token_ttl_min,
            )
            body_lines += [
                "",
                "Action required: session stopped with open findings.",
                f"Approve one more round by replying:",
                f"A2A EXTEND SESSION={sid} TOKEN={token} AUTO_RUN=yes",
            ]
        elif status == "lgtm":
            body_lines += ["", "Session reached LGTM."]

        recipients = _collect_notification_recipients(cfg, store, sid)
        if recipients:
            _send_response(
                cfg,
                to_addr=recipients[0],
                subject=f"[A2A] Session update {sid} -> {status}",
                body="\n".join(body_lines) + "\n",
            )
            for extra in recipients[1:]:
                _send_response(
                    cfg,
                    to_addr=extra,
                    subject=f"[A2A] Session update {sid} -> {status}",
                    body="\n".join(body_lines) + "\n",
                )
            sent += 1

        store.upsert_session_alert(
            session_id=sid,
            status=status,
            current_round=current_round,
            open_findings=open_findings,
            last_token=token,
        )
    return sent


def process_incoming_once(cfg: BridgeConfig, store: BridgeStore) -> int:
    if not (cfg.imap_host and cfg.imap_user and cfg.imap_password):
        return 0
    processed = 0
    conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port)
    try:
        conn.login(cfg.imap_user, cfg.imap_password)
        conn.select(cfg.mailbox)
        typ, data = conn.search(None, "UNSEEN")
        if typ != "OK":
            return 0
        ids = data[0].split() if data and data[0] else []
        for uid in ids:
            typ, msg_rows = conn.fetch(uid, "(RFC822)")
            if typ != "OK" or not msg_rows:
                continue
            raw_bytes = b""
            for row in msg_rows:
                if isinstance(row, tuple) and len(row) > 1:
                    raw_bytes = row[1]
                    break
            if not raw_bytes:
                continue
            msg = email.message_from_bytes(raw_bytes)
            message_id = str(msg.get("Message-ID") or "").strip().strip("<>").strip()
            if not message_id:
                message_id = f"imap-uid-{uid.decode(errors='ignore')}"
            sender = _extract_sender_addr(msg)
            subject = str(msg.get("Subject") or "").strip()
            body = _extract_text_body(msg)
            if store.is_processed(message_id):
                continue

            if cfg.allowed_senders and sender.lower() not in cfg.allowed_senders:
                store.mark_processed(
                    message_id=message_id,
                    sender=sender,
                    subject=subject,
                    command="",
                    status="denied",
                    error="sender not allowlisted",
                )
                continue

            attachments = _save_patch_attachments(cfg, msg, message_id)
            parsed = parse_a2a_command(subject, body)
            if str(parsed.get("command") or "").lower() in {"", "none"} and cfg.auto_detect_requests:
                inferred = _infer_auto_run_request(subject, body, attachments)
                if inferred is not None:
                    parsed = inferred
            status, response = handle_command(cfg, store, sender, parsed, attachments)
            _send_response(
                cfg,
                to_addr=sender,
                subject=f"[A2A Bridge] {status.upper()} - {subject or 'command'}",
                body=response,
            )
            store.mark_processed(
                message_id=message_id,
                sender=sender,
                subject=subject,
                command=str(parsed.get("raw") or ""),
                status=status,
                session_id="",
                error="" if status == "ok" else response[:300],
            )
            processed += 1
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return processed


def run_bridge_once(root: Path, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = load_bridge_config(root, overrides or {})
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
    store = BridgeStore(cfg.state_db)
    try:
        incoming = process_incoming_once(cfg, store)
        notifications = process_session_notifications_once(cfg, store)
        return {
            "incoming_processed": int(incoming),
            "notifications_sent": int(notifications),
            "imap_enabled": bool(cfg.imap_host and cfg.imap_user and cfg.imap_password),
            "state_db": str(cfg.state_db),
            "inbox_dir": str(cfg.inbox_dir),
            "auto_detect_requests": bool(cfg.auto_detect_requests),
            "poll_sec": int(cfg.poll_sec),
        }
    finally:
        store.close()


def run_bridge_loop(
    root: Path,
    *,
    overrides: dict[str, Any] | None = None,
    once: bool = False,
    max_loops: int | None = None,
) -> dict[str, Any]:
    cfg = load_bridge_config(root, overrides or {})
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
    store = BridgeStore(cfg.state_db)
    loops = 0
    incoming_total = 0
    notifications_total = 0
    try:
        while True:
            incoming = process_incoming_once(cfg, store)
            notified = process_session_notifications_once(cfg, store)
            incoming_total += int(incoming)
            notifications_total += int(notified)
            loops += 1
            if once:
                break
            if max_loops is not None and loops >= max_loops:
                break
            time.sleep(max(5, int(cfg.poll_sec)))
    finally:
        store.close()
    return {
        "loops": loops,
        "incoming_processed": incoming_total,
        "notifications_sent": notifications_total,
        "imap_enabled": bool(cfg.imap_host and cfg.imap_user and cfg.imap_password),
        "state_db": str(cfg.state_db),
        "inbox_dir": str(cfg.inbox_dir),
        "auto_detect_requests": bool(cfg.auto_detect_requests),
        "poll_sec": int(cfg.poll_sec),
    }
