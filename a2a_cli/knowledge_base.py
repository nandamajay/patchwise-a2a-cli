from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _kb_path(root: Path) -> Path:
    return root / ".a2a" / "knowledge_base.json"


def _default_kb(max_entries: int = 200) -> dict[str, Any]:
    return {
        "version": 1,
        "max_entries": max_entries,
        "entries": [],
    }


def load_kb(root: Path) -> dict[str, Any]:
    path = _kb_path(root)
    if not path.exists():
        kb = _default_kb()
        save_kb(root, kb)
        return kb
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        kb = _default_kb()
        save_kb(root, kb)
        return kb
    if not isinstance(payload, dict):
        kb = _default_kb()
        save_kb(root, kb)
        return kb
    if "entries" not in payload or not isinstance(payload.get("entries"), list):
        payload["entries"] = []
    if "max_entries" not in payload:
        payload["max_entries"] = 200
    if "version" not in payload:
        payload["version"] = 1
    return payload


def save_kb(root: Path, payload: dict[str, Any]) -> None:
    path = _kb_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _normalize_pattern(text: str) -> str:
    lowered = re.sub(r"\s+", " ", text.strip().lower())
    return lowered


def _subsystem_from_watch_path(watch_path: str) -> str:
    lowered = watch_path.lower()
    for name in ["pinctrl", "codec", "codecs", "lpass", "asoc", "sound"]:
        if name in lowered:
            return name
    return "unknown"


def infer_subsystem_from_watch_path(watch_path: str) -> str:
    return _subsystem_from_watch_path(watch_path)


def _match_entry(entries: list[dict], pattern: str, subsystem: str) -> dict | None:
    np = _normalize_pattern(pattern)
    for entry in entries:
        ep = _normalize_pattern(str(entry.get("pattern", "")))
        if subsystem == str(entry.get("subsystem", "unknown")) and np == ep:
            return entry
    return None


def _trim_entries(entries: list[dict], max_entries: int) -> list[dict]:
    sorted_entries = sorted(
        entries,
        key=lambda e: (
            int(e.get("occurrences", 0)),
            str(e.get("last_seen", "")),
        ),
        reverse=True,
    )
    return sorted_entries[:max_entries]


def update_kb_after_lgtm(
    root: Path,
    *,
    session_id: str,
    watch_path: str,
    resolved_findings: list[dict],
) -> dict[str, Any]:
    kb = load_kb(root)
    entries = list(kb.get("entries", []))
    subsystem = _subsystem_from_watch_path(watch_path)

    for finding in resolved_findings:
        title = str(finding.get("title") or "").strip()
        desc = str(finding.get("description") or "").strip()
        pattern = title or desc or "resolved finding"
        resolution = str(finding.get("required_action") or "fixed in respin").strip()
        severity = str(finding.get("severity") or "medium").lower()
        evidence_url = str(finding.get("evidence_url") or finding.get("source") or "").strip()

        existing = _match_entry(entries, pattern, subsystem)
        if existing:
            existing["occurrences"] = int(existing.get("occurrences", 0)) + 1
            existing["last_seen"] = session_id
            if resolution:
                existing["resolution"] = resolution
            if evidence_url and not existing.get("evidence_url"):
                existing["evidence_url"] = evidence_url
            continue

        entries.append(
            {
                "id": f"kb-{uuid.uuid4()}",
                "pattern": pattern,
                "severity": severity,
                "occurrences": 1,
                "first_seen": session_id,
                "last_seen": session_id,
                "resolution": resolution,
                "evidence_url": evidence_url,
                "subsystem": subsystem,
                "updated_at": _utc_now(),
            }
        )

    max_entries = int(kb.get("max_entries", 200))
    kb["entries"] = _trim_entries(entries, max_entries)
    save_kb(root, kb)
    return kb


def list_kb_entries(root: Path, subsystem: str | None = None) -> list[dict]:
    entries = list(load_kb(root).get("entries", []))
    if subsystem:
        return [e for e in entries if str(e.get("subsystem", "")) == subsystem]
    return entries


def clear_kb(root: Path) -> None:
    save_kb(root, _default_kb())


def build_chanakya_context(root: Path, subsystem: str, limit: int = 5) -> str:
    rows = [e for e in list_kb_entries(root, subsystem=subsystem)]
    rows = sorted(rows, key=lambda e: int(e.get("occurrences", 0)), reverse=True)[:limit]
    if not rows:
        return ""
    lines = ["Known recurring issues in this subsystem:"]
    for row in rows:
        lines.append(
            "- {pattern} (seen {seen} times) -> {resolution}".format(
                pattern=row.get("pattern", ""),
                seen=row.get("occurrences", 0),
                resolution=row.get("resolution", ""),
            )
        )
    return "\n".join(lines)


def build_aryabhata_context(root: Path, open_findings: list[dict], limit: int = 5) -> str:
    entries = list_kb_entries(root)
    if not entries or not open_findings:
        return ""
    found: list[dict] = []
    for finding in open_findings:
        title = _normalize_pattern(str(finding.get("title") or finding.get("description") or ""))
        for row in entries:
            pattern = _normalize_pattern(str(row.get("pattern", "")))
            if pattern and (pattern in title or title in pattern):
                found.append(row)
    # dedupe by id
    dedup: dict[str, dict] = {str(row.get("id")): row for row in found if row.get("id")}
    rows = sorted(dedup.values(), key=lambda e: int(e.get("occurrences", 0)), reverse=True)[:limit]
    if not rows:
        return ""
    lines = ["Knowledge base evidence for this finding:"]
    for row in rows:
        lines.append(f"- Seen {row.get('occurrences', 0)} times across sessions")
        lines.append(f"- Resolved by: {row.get('resolution', '')}")
        if row.get("evidence_url"):
            lines.append(f"- Reference: {row.get('evidence_url')}")
    return "\n".join(lines)
