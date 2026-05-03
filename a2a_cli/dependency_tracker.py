from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def track_symbol_changes(series_a: dict, series_b: dict) -> dict[str, Any]:
    shared_a = set(str(s) for s in series_a.get("shared_symbols", []) if str(s).strip())
    shared_b = set(str(s) for s in series_b.get("shared_symbols", []) if str(s).strip())
    changed = sorted(shared_a.intersection(shared_b))
    impact = "none"
    if changed:
        impact = f"{series_b.get('name')} series must be re-reviewed"
    return {
        "source_series": series_a.get("name"),
        "affected_series": series_b.get("name"),
        "changed_symbols": changed,
        "impact": impact,
        "unresolved": bool(changed),
    }


def block_patchset_lgtm_until_reconciled(series_results: list[dict], impacts: list[dict]) -> bool:
    status_map = {str(row.get("name")): str(row.get("status", "")).lower() for row in series_results}
    all_lgtm = all(status == "lgtm" for status in status_map.values()) if status_map else False
    if not all_lgtm:
        return True
    for impact in impacts:
        if not impact.get("changed_symbols"):
            continue
        affected = str(impact.get("affected_series", ""))
        if status_map.get(affected) != "lgtm":
            return True
    return False


def write_cross_series_impact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
