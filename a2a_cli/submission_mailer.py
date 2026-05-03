from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .email_notify import send_email


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_VERSION_RE = re.compile(r"\bv(?P<num>\d+)\b", re.IGNORECASE)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _session_path(root: Path, session_id: str) -> Path:
    return root / ".a2a" / "sessions" / f"{session_id}.json"


def _report_dir(root: Path, session_id: str) -> Path:
    return root / ".a2a" / "reports" / session_id


def _latest_output_dir(root: Path, session_id: str) -> Path | None:
    out_root = root / ".a2a" / "output" / session_id
    if not out_root.exists():
        return None
    candidates = [p for p in out_root.iterdir() if p.is_dir()]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.name)
    return candidates[-1]


def _patch_dir_from_session(session: dict[str, Any]) -> Path | None:
    watch_raw = str(session.get("watch_path") or "").strip()
    if not watch_raw:
        return None
    watch_path = Path(watch_raw)
    if watch_path.is_file() and watch_path.suffix == ".patch":
        return watch_path.parent
    if watch_path.is_dir():
        return watch_path
    return None


def _find_patch_root(root: Path, session_id: str, session: dict[str, Any]) -> Path | None:
    out_dir = _latest_output_dir(root, session_id)
    if out_dir and list(out_dir.glob("*.patch")):
        return out_dir
    from_session = _patch_dir_from_session(session)
    if from_session and list(from_session.glob("*.patch")):
        return from_session
    return None


def list_patch_files(root: Path, session_id: str) -> list[Path]:
    session_file = _session_path(root, session_id)
    if not session_file.exists():
        raise RuntimeError(f"Session not found: {session_id}")
    session = _load_json(session_file)
    patch_root = _find_patch_root(root, session_id, session)
    if patch_root is None:
        return []
    return sorted(p for p in patch_root.glob("*.patch") if p.is_file())


def _parse_subject_from_cover(cover_letter: Path | None) -> str | None:
    if cover_letter is None or not cover_letter.exists():
        return None
    for line in cover_letter.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lower().startswith("subject:"):
            return line.split(":", 1)[1].strip()
    return None


def _detect_version(session: dict[str, Any], patch_files: list[Path]) -> int:
    sources: list[str] = []
    watch_path = str(session.get("watch_path") or "").strip()
    if watch_path:
        sources.append(Path(watch_path).name)
    for patch in patch_files:
        sources.append(patch.name)
    for token in sources:
        match = _VERSION_RE.search(token)
        if match:
            return int(match.group("num"))
    return 1


def _latest_round_summary(report_dir: Path) -> dict[str, Any]:
    rows = sorted(report_dir.glob("round-*-summary.json"))
    if not rows:
        return {}
    try:
        return _load_json(rows[-1])
    except Exception:
        return {}


def _latest_static_result(report_dir: Path) -> dict[str, Any]:
    rows = sorted(report_dir.glob("round-*-static-analysis.json"))
    if not rows:
        return {}
    try:
        return _load_json(rows[-1])
    except Exception:
        return {}


def _load_score_decisions(report_dir: Path) -> list[dict[str, Any]]:
    path = report_dir / "score_decisions.json"
    if not path.exists():
        return []
    try:
        payload = _load_json(path)
    except Exception:
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def build_patchset_summary(root: Path, session_id: str) -> dict[str, Any]:
    session_file = _session_path(root, session_id)
    if not session_file.exists():
        raise RuntimeError(f"Session not found: {session_id}")
    session = _load_json(session_file)
    report_dir = _report_dir(root, session_id)
    summary = _latest_round_summary(report_dir)
    findings = summary.get("findings", {}) if isinstance(summary, dict) else {}
    gate = summary.get("validation_gate", {}) if isinstance(summary, dict) else {}
    prior = summary.get("prior_comments", {}) if isinstance(summary, dict) else {}
    prior_totals = prior.get("totals", {}) if isinstance(prior, dict) else {}
    static_payload = _latest_static_result(report_dir)
    score_decisions = _load_score_decisions(report_dir)

    patch_files = list_patch_files(root, session_id)
    rounds = int(len(session.get("rounds", [])))
    total_findings = int(findings.get("total", 0))
    open_findings = int(findings.get("open", 0))
    resolved = max(0, total_findings - open_findings)
    checkpatch_errors = int(gate.get("failures", 0))
    sparse_new = len(static_payload.get("sparse", {}).get("new_warnings", [])) if isinstance(static_payload, dict) else 0
    score_warning = any(bool(row.get("block_lgtm")) for row in score_decisions)

    return {
        "generated_at": _utc_now(),
        "session_id": session_id,
        "task": str(session.get("task") or ""),
        "status": str(session.get("status") or "unknown").lower(),
        "series": [
            {
                "name": Path(str(session.get("watch_path") or "series")).name or "series",
                "status": str(session.get("status") or "unknown").lower(),
                "patch_count": len(patch_files),
                "rounds": rounds,
            }
        ],
        "findings_resolved": resolved,
        "findings_total": total_findings,
        "checkpatch_errors": checkpatch_errors,
        "sparse_new_warnings": sparse_new,
        "prior_comments_closed": int(prior_totals.get("closed", 0)),
        "prior_comments_total": int(prior_totals.get("received_total", 0)),
        "score_warning": score_warning,
    }


def build_submission_email(root: Path, session_id: str) -> dict[str, Any]:
    session_file = _session_path(root, session_id)
    if not session_file.exists():
        raise RuntimeError(f"Session not found: {session_id}")
    session = _load_json(session_file)
    report_dir = _report_dir(root, session_id)
    patch_files = list_patch_files(root, session_id)
    if not patch_files:
        raise RuntimeError("Patch files missing for submission.")

    cover = next((p for p in patch_files if p.name.startswith("0000")), None)
    subject_hint = _parse_subject_from_cover(cover) or "<cover letter subject>"
    version = _detect_version(session, patch_files)
    summary = build_patchset_summary(root, session_id)

    cover_text = ""
    if cover and cover.exists():
        cover_text = cover.read_text(encoding="utf-8", errors="replace")
    else:
        cover_text = f"PatchWise dry-run submission for session {session_id}."

    footer = [
        "",
        "--- PatchWise A2A Review Summary ---",
        f"Rounds: {summary.get('series', [{}])[0].get('rounds', 0)} | Findings resolved: {summary.get('findings_resolved', 0)}/{summary.get('findings_total', 0)}",
        f"Checkpatch: {summary.get('checkpatch_errors', 0)} errors | Sparse: {summary.get('sparse_new_warnings', 0)} new warnings",
        f"Prior comments: {summary.get('prior_comments_closed', 0)}/{summary.get('prior_comments_total', 0)} addressed",
    ]

    return {
        "subject": f"[PATCH v{version}] {subject_hint}",
        "body": (cover_text.rstrip() + "\n" + "\n".join(footer)).strip() + "\n",
        "attachments": [str(p) for p in patch_files],
        "report_dir": str(report_dir),
    }


def _normalized_recipients(recipient: str, cc_list: list[str]) -> tuple[str, list[str]]:
    to_addr = recipient.strip()
    if not _EMAIL_RE.match(to_addr):
        raise RuntimeError(f"Invalid recipient email: {recipient}")
    clean_cc: list[str] = []
    for row in cc_list:
        email = row.strip()
        if not email:
            continue
        if not _EMAIL_RE.match(email):
            raise RuntimeError(f"Invalid cc email: {row}")
        if email not in clean_cc and email != to_addr:
            clean_cc.append(email)
    return to_addr, clean_cc


def _assert_safety(config: dict[str, Any], recipient: str, cc_list: list[str]) -> None:
    submission_cfg = config.get("submission", {}) if isinstance(config, dict) else {}
    community_to = {
        str(v).strip().lower()
        for v in submission_cfg.get("community_to", [])
        if str(v).strip()
    }
    community_cc = {
        str(v).strip().lower()
        for v in submission_cfg.get("community_cc", [])
        if str(v).strip()
    }
    blocked = community_to.union(community_cc)
    if recipient.strip().lower() in blocked:
        raise RuntimeError("Safety check failed: recipient resolves to community list.")
    for cc in cc_list:
        if cc.strip().lower() in blocked:
            raise RuntimeError("Safety check failed: cc resolves to community list.")
    if not bool(submission_cfg.get("dry_run", True)):
        raise RuntimeError("Safety check failed: dry_run must remain true for automated submit flow.")


def send_dry_run(
    root: Path,
    session_id: str,
    recipient: str,
    cc_list: list[str],
    config: dict[str, Any],
) -> dict[str, Any]:
    to_addr, clean_cc = _normalized_recipients(recipient, cc_list)
    _assert_safety(config, to_addr, clean_cc)

    email_payload = build_submission_email(root, session_id)
    result = send_email(
        subject=str(email_payload["subject"]),
        body=str(email_payload["body"]),
        to_addrs=[to_addr],
        cc_addrs=clean_cc,
        attachments=[str(p) for p in email_payload["attachments"]],
    )

    return {
        "sent": bool(result.get("sent")),
        "to": to_addr,
        "cc": clean_cc,
        "subject": email_payload["subject"],
        "attachments": email_payload["attachments"],
        "fallback": result.get("fallback") or "",
        "error": result.get("error") or "",
    }
