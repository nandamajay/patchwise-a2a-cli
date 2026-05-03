from __future__ import annotations

import json
import re
from pathlib import Path

from .config import SETTINGS
from .models import RoundSummary, SessionReport


_ROUND_SUMMARY_RE = re.compile(r"^round-(\d+)-summary\.json$")


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def _load_findings(path: Path) -> list[dict]:
    if not path.exists():
        return []
    payload = _load_json(path)
    findings = payload.get("findings", [])
    return findings if isinstance(findings, list) else []


def list_sessions() -> list[dict]:
    sessions: list[dict] = []
    if not SETTINGS.sessions_dir.exists():
        return sessions

    for session_path in sorted(SETTINGS.sessions_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        payload = _load_json(session_path)
        status = str(payload.get("status") or "unknown")
        sessions.append(
            {
                "session_id": str(payload.get("id") or session_path.stem),
                "status": status,
                "task": str(payload.get("task") or ""),
                "start_time": str(payload.get("created_at") or ""),
                "final_status": status if status in {"lgtm", "failed", "stopped"} else None,
            }
        )
    return sessions


def _round_summary_from_files(report_dir: Path, summary_payload: dict) -> RoundSummary:
    round_no = int(summary_payload.get("round") or 0)

    findings_block = summary_payload.get("findings") if isinstance(summary_payload.get("findings"), dict) else {}
    gate_block = (
        summary_payload.get("validation_gate")
        if isinstance(summary_payload.get("validation_gate"), dict)
        else {}
    )
    builder_block = summary_payload.get("builder") if isinstance(summary_payload.get("builder"), dict) else {}
    reviewer_block = summary_payload.get("reviewer") if isinstance(summary_payload.get("reviewer"), dict) else {}
    prior_block = summary_payload.get("prior_comments") if isinstance(summary_payload.get("prior_comments"), dict) else {}
    prior_totals = prior_block.get("totals") if isinstance(prior_block.get("totals"), dict) else {}

    findings_file = report_dir / f"round-{round_no:02d}-findings.json"
    round_findings = _load_findings(findings_file)
    findings_items: list[dict] = []
    for item in round_findings:
        if not isinstance(item, dict):
            continue
        findings_items.append(
            {
                "id": str(item.get("source_comment_id") or ""),
                "severity": str(item.get("severity") or ""),
                "location": str(item.get("location") or ""),
                "description": str(item.get("title") or ""),
                "status": str(item.get("status") or ""),
            }
        )

    top_open = findings_block.get("open_items") if isinstance(findings_block.get("open_items"), list) else []
    if not top_open:
        top_open = [
            {
                "id": f.get("id", ""),
                "severity": f.get("severity", ""),
                "location": f.get("location", ""),
                "title": f.get("description", ""),
            }
            for f in findings_items
            if str(f.get("status", "")).lower() != "closed"
        ]

    gate_passed = gate_block.get("passed")
    gate = "pass" if gate_passed in {True, "true", "pass"} else "fail"

    findings = {
        "total": int(findings_block.get("total", len(findings_items))),
        "open": int(
            findings_block.get(
                "open",
                len([f for f in findings_items if str(f.get("status", "")).lower() != "closed"]),
            )
        ),
        "closed": int(
            findings_block.get(
                "closed",
                len([f for f in findings_items if str(f.get("status", "")).lower() == "closed"]),
            )
        ),
        "new": int(findings_block.get("new_since_prev", 0)),
        "resolved": int(findings_block.get("resolved_since_prev", 0)),
        "items": findings_items,
    }

    scores = {
        "builder_patch_gauge": int(builder_block.get("patch_gauge", 0)),
        "builder_confidence": int(builder_block.get("confidence", 0)),
        "reviewer_confidence": int(reviewer_block.get("confidence", 0)),
    }

    prior_comments = {
        "received": int(prior_totals.get("received_total", 0)),
        "open": int(prior_totals.get("open", 0)),
        "closed": int(prior_totals.get("closed", 0)),
    }

    return RoundSummary(
        round=round_no,
        gate=gate,
        scores=scores,
        findings=findings,
        prior_comments=prior_comments,
        top_open=top_open,
    )


def _build_lgtm_checklist(final_status: str, rounds: list[RoundSummary]) -> dict:
    latest = rounds[-1] if rounds else None
    open_findings = int(latest.findings.get("open", 0)) if latest else 0
    any_gate_fail = any(r.gate == "fail" for r in rounds)

    passed = "passed"
    failed = "failed"
    pending = "pending"

    if final_status == "lgtm":
        return {
            "functional": passed,
            "error_paths": passed,
            "checkpatch": passed if not any_gate_fail else failed,
            "commit_msg": passed,
            "v1_comments": passed if latest and int(latest.prior_comments.get("open", 0)) == 0 else pending,
            "fix_strategy": passed,
        }

    return {
        "functional": failed if open_findings > 0 else pending,
        "error_paths": failed if open_findings > 0 else pending,
        "checkpatch": failed if any_gate_fail else pending,
        "commit_msg": pending,
        "v1_comments": failed if latest and int(latest.prior_comments.get("open", 0)) > 0 else pending,
        "fix_strategy": pending,
    }


def load_session_report(session_id: str) -> SessionReport:
    session_path = SETTINGS.sessions_dir / f"{session_id}.json"
    if not session_path.exists():
        raise FileNotFoundError(f"Session not found: {session_id}")

    session_payload = _load_json(session_path)
    report_dir = SETTINGS.reports_dir / session_id

    round_summaries: list[tuple[int, Path]] = []
    if report_dir.exists():
        for path in report_dir.glob("round-*-summary.json"):
            match = _ROUND_SUMMARY_RE.match(path.name)
            if not match:
                continue
            round_summaries.append((int(match.group(1)), path))

    rounds: list[RoundSummary] = []
    for _, summary_path in sorted(round_summaries, key=lambda item: item[0]):
        payload = _load_json(summary_path)
        rounds.append(_round_summary_from_files(report_dir, payload))

    prior_comments_file = report_dir / "prior_comments.json"
    tracked: list[dict] = []
    if rounds:
        latest_summary = _load_json(report_dir / f"round-{rounds[-1].round:02d}-summary.json")
        prior = latest_summary.get("prior_comments") if isinstance(latest_summary.get("prior_comments"), dict) else {}
        tracked = prior.get("tracked") if isinstance(prior.get("tracked"), list) else []

    prior_comments: list[dict] = []
    if prior_comments_file.exists():
        comments_payload = _load_json(prior_comments_file)
        comments = comments_payload.get("comments") if isinstance(comments_payload.get("comments"), list) else []
        tracked_by_id = {
            str(item.get("source_comment_id") or ""): item
            for item in tracked
            if isinstance(item, dict)
        }
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            cid = str(comment.get("id") or "")
            tracked_row = tracked_by_id.get(cid, {})
            addressed = str(tracked_row.get("current_status") or "").lower() == "closed"
            prior_comments.append(
                {
                    "id": cid,
                    "from": str(comment.get("from") or ""),
                    "subject": str(comment.get("subject") or ""),
                    "addressed": addressed,
                }
            )

    final_status = str(session_payload.get("status") or "running")
    if final_status == "in_progress":
        final_status = "running"

    return SessionReport(
        session_id=str(session_payload.get("id") or session_id),
        task=str(session_payload.get("task") or ""),
        watch_path=str(session_payload.get("watch_path") or ""),
        max_rounds=int(session_payload.get("max_rounds") or 0),
        final_status=final_status,
        rounds=rounds,
        prior_comments=prior_comments,
        lgtm_checklist=_build_lgtm_checklist(final_status, rounds),
    )
