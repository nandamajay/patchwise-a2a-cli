from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _profiles_path(root: Path) -> Path:
    return root / ".a2a" / "maintainer_profiles.json"


def _default_profiles() -> dict[str, Any]:
    return {"version": 1, "updated_at": _utc_now(), "maintainers": {}}


def load_profiles(root: Path) -> dict[str, Any]:
    path = _profiles_path(root)
    if not path.exists():
        payload = _default_profiles()
        save_profiles(root, payload)
        return payload
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = _default_profiles()
        save_profiles(root, payload)
    if not isinstance(payload, dict):
        payload = _default_profiles()
    payload.setdefault("version", 1)
    payload.setdefault("maintainers", {})
    return payload


def save_profiles(root: Path, payload: dict[str, Any]) -> None:
    path = _profiles_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = _utc_now()
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def update_profile(root: Path, author: str, finding_types: list[str], verdict: str) -> dict[str, Any]:
    profiles = load_profiles(root)
    maintainers = profiles.setdefault("maintainers", {})
    row = dict(maintainers.get(author) or {})
    row.setdefault("priority", "medium")
    row.setdefault("avg_response_days", 3)
    row.setdefault("common_concerns", [])
    row.setdefault("approval_rate", 0.5)
    row.setdefault("sessions_seen", [])
    row.setdefault("feedback_patterns", {})
    row.setdefault("review_count", 0)
    row.setdefault("approval_count", 0)

    row["review_count"] = int(row.get("review_count", 0)) + 1
    if verdict.lower() == "lgtm":
        row["approval_count"] = int(row.get("approval_count", 0)) + 1
    row["approval_rate"] = round(
        float(row.get("approval_count", 0)) / max(1, int(row.get("review_count", 1))), 2
    )

    concerns = set(str(c) for c in row.get("common_concerns", []))
    for finding in finding_types:
        concerns.add(str(finding))
        row.setdefault("feedback_patterns", {})
        row["feedback_patterns"][str(finding)] = f"frequently flags {finding}"
    row["common_concerns"] = sorted(concerns)

    if row["approval_rate"] >= 0.8:
        row["priority"] = "high"
    elif row["approval_rate"] >= 0.4:
        row["priority"] = "medium"
    else:
        row["priority"] = "low"

    maintainers[author] = row
    save_profiles(root, profiles)
    return row


def get_priority(root: Path, author: str) -> str:
    profiles = load_profiles(root)
    row = profiles.get("maintainers", {}).get(author)
    if not row:
        return "low"
    return str(row.get("priority", "medium"))


def inject_maintainer_context(root: Path, author: str, prompt: str) -> str:
    profiles = load_profiles(root)
    row = profiles.get("maintainers", {}).get(author)
    if not row:
        return prompt
    concerns = ", ".join(row.get("common_concerns", [])) or "none"
    patterns = row.get("feedback_patterns", {})
    pattern_text = "; ".join(f"{k}: {v}" for k, v in patterns.items()) if patterns else "none"
    prefix = (
        f"This patch will be reviewed by {author}.\n"
        f"Their known concerns: {concerns}\n"
        f"Pay special attention to: {pattern_text}\n\n"
    )
    return prefix + prompt
