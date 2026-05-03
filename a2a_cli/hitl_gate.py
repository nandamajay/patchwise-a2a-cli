from __future__ import annotations

import builtins
import json
import os
import re
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .submission_mailer import send_dry_run


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_path(root: Path, session_id: str) -> Path:
    return root / ".a2a" / "reports" / session_id / "hitl_state.json"


def _session_path(root: Path, session_id: str) -> Path:
    return root / ".a2a" / "sessions" / f"{session_id}.json"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _input_with_timeout(
    prompt: str,
    timeout_secs: int,
    input_fn: Callable[[str], str],
) -> str:
    if timeout_secs <= 0:
        return input_fn(prompt)

    if input_fn is not builtins.input:
        return input_fn(prompt)

    if not hasattr(signal, "SIGALRM"):
        return input_fn(prompt)

    def _raise_timeout(_signum: int, _frame: Any) -> None:
        raise TimeoutError("input timeout")

    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.alarm(max(1, int(timeout_secs)))
    try:
        return input_fn(prompt)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _render_summary(
    *,
    session: dict[str, Any],
    patchset_summary: dict[str, Any],
    recipient: str,
    cc_list: list[str],
) -> str:
    lines = [
        "╔══════════════════════════════════════════════════════════╗",
        "║         HUMAN APPROVAL REQUIRED                         ║",
        "╠══════════════════════════════════════════════════════════╣",
        f"║  Session : {str(session.get('id', '')):<47}║",
        f"║  Task    : {str(session.get('task', '')):<47}║",
        "╠══════════════════════════════════════════════════════════╣",
        "║  Patchset Summary                                       ║",
    ]
    for series in patchset_summary.get("series", []):
        name = str(series.get("name", "series"))
        status = str(series.get("status", "unknown")).upper()
        marker = "✅" if status == "LGTM" else "❌"
        pcount = int(series.get("patch_count", 0))
        rounds = int(series.get("rounds", 0))
        row = f"║  Series {name:<8}: {marker} {status:<6} ({pcount} patches, {rounds} rounds)"
        lines.append(f"{row:<59}║")
    lines.extend(
        [
            f"║  Findings resolved : {patchset_summary.get('findings_resolved', 0)}/{patchset_summary.get('findings_total', 0):<31}║",
            f"║  Checkpatch errors : {patchset_summary.get('checkpatch_errors', 0):<39}║",
            f"║  Sparse warnings   : {patchset_summary.get('sparse_new_warnings', 0)} new{'':<34}║",
            (
                "║  Prior comments    : "
                f"{patchset_summary.get('prior_comments_closed', 0)}/{patchset_summary.get('prior_comments_total', 0)} addressed"
            ).ljust(59)
            + "║",
            "╠══════════════════════════════════════════════════════════╣",
            "║  Submission Target : DRY RUN ONLY                       ║",
            f"║  Will send to      : {recipient:<39}║",
            (
                "║  Cc               : "
                + (", ".join(cc_list) if cc_list else "(none)")
            ).ljust(59)
            + "║",
            "║  Community list    : NOT POPULATED                      ║",
            "╠══════════════════════════════════════════════════════════╣",
            "║  Options:                                                ║",
            "║  approve  -> send dry run email to you only             ║",
            "║  review   -> open patches in $EDITOR then return here   ║",
            "║  modify   -> change recipient, add cc                   ║",
            "║  abort    -> stop, keep patches for manual submission   ║",
            "╚══════════════════════════════════════════════════════════╝",
        ]
    )
    return "\n".join(lines)


def _review_file_path(root: Path, session_id: str) -> Path | None:
    report_dir = root / ".a2a" / "reports" / session_id
    candidates = sorted(report_dir.glob("round-*-summary.md"))
    if candidates:
        return candidates[-1]
    summary = report_dir / "summary.md"
    if summary.exists():
        return summary
    return None


def _open_in_editor(path: Path) -> None:
    editor = str(os.environ.get("EDITOR", "")).strip()
    if editor:
        subprocess.run([editor, str(path)], check=False)
        return
    subprocess.run(["cat", str(path)], check=False)


def _parse_cc(raw: str) -> list[str]:
    values: list[str] = []
    for token in raw.split(","):
        email = token.strip()
        if not email:
            continue
        if not _EMAIL_RE.match(email):
            raise RuntimeError(f"Invalid cc email: {email}")
        if email not in values:
            values.append(email)
    return values


def _load_state_if_resume(root: Path, session_id: str, resume: bool) -> dict[str, Any]:
    path = _state_path(root, session_id)
    if not resume or not path.exists():
        return {}
    try:
        return _load_json(path)
    except Exception:
        return {}


def run_hitl_gate(
    root: Path,
    session_id: str,
    patchset_summary: dict[str, Any],
    config: dict[str, Any],
    *,
    resume: bool = False,
    input_fn: Callable[[str], str] = builtins.input,
    open_editor_fn: Callable[[Path], None] | None = None,
    send_fn: Callable[[Path, str, str, list[str], dict[str, Any]], dict[str, Any]] = send_dry_run,
) -> dict[str, Any]:
    session_file = _session_path(root, session_id)
    if not session_file.exists():
        raise RuntimeError(f"Session not found: {session_id}")
    session = _load_json(session_file)
    if str(session.get("status", "")).lower() != "lgtm":
        raise RuntimeError("Session not in LGTM state; submit blocked.")

    submission_cfg = config.get("submission", {}) if isinstance(config, dict) else {}
    timeout_secs = int(submission_cfg.get("hitl_timeout_secs", 300))
    recipient = str(submission_cfg.get("dry_run_recipient", "")).strip() or "nandam@qti.qualcomm.com"
    cc_list: list[str] = []
    review_loops = 0

    state_payload = _load_state_if_resume(root, session_id, resume)
    if state_payload:
        recipient = str(state_payload.get("recipient", recipient)).strip() or recipient
        cc_list = [str(v).strip() for v in state_payload.get("cc", []) if str(v).strip()]
        review_loops = int(state_payload.get("review_loops", 0))

    open_editor_impl = open_editor_fn or _open_in_editor
    review_target = _review_file_path(root, session_id)
    state_file = _state_path(root, session_id)

    def _persist(status: str, reason: str) -> dict[str, Any]:
        payload = {
            "session_id": session_id,
            "status": status,
            "reason": reason,
            "recipient": recipient,
            "cc": cc_list,
            "review_loops": review_loops,
            "saved_at": _utc_now(),
            "resume_command": f"a2a submit --session {session_id} --resume",
        }
        _save_json(state_file, payload)
        return payload

    while True:
        print(
            _render_summary(
                session=session,
                patchset_summary=patchset_summary,
                recipient=recipient,
                cc_list=cc_list,
            )
        )
        try:
            choice = _input_with_timeout("Type your choice: ", timeout_secs, input_fn).strip().lower()
        except TimeoutError:
            payload = _persist("aborted", "timeout")
            print("HITL timeout reached. Submission aborted.")
            return {"status": "aborted", "reason": "timeout", "state_path": str(state_file), "state": payload}

        if choice == "approve":
            try:
                confirm = _input_with_timeout("Type APPROVE to confirm: ", timeout_secs, input_fn).strip()
            except TimeoutError:
                payload = _persist("aborted", "timeout")
                print("HITL timeout reached. Submission aborted.")
                return {"status": "aborted", "reason": "timeout", "state_path": str(state_file), "state": payload}
            if confirm != "APPROVE":
                print("Confirmation mismatch. Approval cancelled.")
                continue
            send_result = send_fn(root, session_id, recipient, cc_list, config)
            if state_file.exists():
                state_file.unlink()
            return {"status": "sent", "send_result": send_result}

        if choice == "review":
            if review_target is None:
                print("No report/cover file available for review.")
                continue
            if review_loops >= 3:
                print("Maximum review loops reached for this HITL run.")
                continue
            open_editor_impl(review_target)
            review_loops += 1
            continue

        if choice == "modify":
            try:
                new_recipient = _input_with_timeout("Enter recipient email: ", timeout_secs, input_fn).strip()
                if not _EMAIL_RE.match(new_recipient):
                    raise RuntimeError(f"Invalid recipient email: {new_recipient}")
                cc_raw = _input_with_timeout(
                    "Enter cc emails (comma separated, or enter to skip): ",
                    timeout_secs,
                    input_fn,
                ).strip()
                new_cc = _parse_cc(cc_raw) if cc_raw else []
            except TimeoutError:
                payload = _persist("aborted", "timeout")
                print("HITL timeout reached. Submission aborted.")
                return {"status": "aborted", "reason": "timeout", "state_path": str(state_file), "state": payload}
            except RuntimeError as exc:
                print(str(exc))
                continue
            recipient = new_recipient
            cc_list = new_cc
            continue

        if choice == "abort":
            payload = _persist("aborted", "user_abort")
            print(f"Patches preserved for manual submission. Resume with: {payload['resume_command']}")
            return {"status": "aborted", "reason": "user_abort", "state_path": str(state_file), "state": payload}

        print("Invalid choice. Use one of: approve, review, modify, abort.")
