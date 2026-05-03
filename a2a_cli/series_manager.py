from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .dependency_tracker import (
    block_patchset_lgtm_until_reconciled,
    track_symbol_changes,
    write_cross_series_impact,
)


_SYMBOL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\s*\(")
_DEPENDS_RE = re.compile(r"^\s*Depends-on:\s*(.+)$", re.IGNORECASE)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_symbols_from_patch(path: Path, limit: int = 12) -> list[str]:
    symbols: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return symbols
    for match in _SYMBOL_RE.finditer(text):
        sym = match.group(1)
        if sym not in symbols:
            symbols.append(sym)
        if len(symbols) >= limit:
            break
    return symbols


def _extract_depends_from_cover(path: Path) -> list[str]:
    deps: list[str] = []
    if not path.exists():
        return deps
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _DEPENDS_RE.match(line)
        if m:
            raw = m.group(1).strip()
            for item in re.split(r"[,\s]+", raw):
                if item:
                    deps.append(item.lower())
    return deps


def auto_discover_series(root: Path, watch_path: Path) -> dict[str, Any]:
    if not watch_path.is_dir():
        raise RuntimeError("Series discovery requires a directory watch_path.")

    candidates: list[Path] = []
    for sub in sorted(watch_path.iterdir()):
        if not sub.is_dir():
            continue
        if list(sub.glob("*.patch")):
            candidates.append(sub)

    if not candidates:
        # fallback: single series at root watch path
        if list(watch_path.glob("*.patch")):
            candidates = [watch_path]
        else:
            raise RuntimeError(f"No patch series found under: {watch_path}")

    series_rows: list[dict] = []
    for sub in candidates:
        patches = sorted(sub.glob("*.patch"))
        cover = next(iter(sorted(sub.glob("0000*.patch"))), None)
        depends_on = _extract_depends_from_cover(cover) if cover else []
        # Heuristic for common topology if no explicit depends found.
        if not depends_on and "codec" in sub.name.lower():
            for cand in candidates:
                if cand == sub:
                    continue
                if "lpi" in cand.name.lower():
                    depends_on = [cand.name]
                    break

        symbols: list[str] = []
        for patch in patches:
            symbols.extend(_extract_symbols_from_patch(patch))
        seen: set[str] = set()
        shared_symbols = []
        for sym in symbols:
            if sym in seen:
                continue
            seen.add(sym)
            shared_symbols.append(sym)
        series_rows.append(
            {
                "name": sub.name,
                "path": str(sub),
                "depends_on": depends_on,
                "shared_symbols": shared_symbols[:20],
            }
        )

    manifest = {
        "version": 1,
        "generated_at": _utc_now(),
        "watch_path": str(watch_path),
        "series": series_rows,
    }
    manifest_path = root / ".a2a" / "series_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _topological_order(series_rows: list[dict]) -> list[dict]:
    name_map = {str(row.get("name")): row for row in series_rows}
    indegree = defaultdict(int)
    graph: dict[str, list[str]] = defaultdict(list)
    for row in series_rows:
        name = str(row.get("name"))
        deps = [str(dep) for dep in row.get("depends_on", []) if str(dep) in name_map]
        for dep in deps:
            graph[dep].append(name)
            indegree[name] += 1
        indegree[name] += 0

    q = deque(sorted([name for name in name_map if indegree[name] == 0]))
    order: list[str] = []
    while q:
        cur = q.popleft()
        order.append(cur)
        for nxt in sorted(graph.get(cur, [])):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                q.append(nxt)

    if len(order) != len(name_map):
        raise RuntimeError("Circular dependency detected in series manifest.")
    return [name_map[name] for name in order]


def run_all_series(
    root: Path,
    manifest: dict[str, Any],
    run_series: Callable[[dict], dict[str, Any]],
) -> dict[str, Any]:
    series_rows = list(manifest.get("series", []))
    ordered = _topological_order(series_rows)
    results: list[dict] = []
    for row in ordered:
        result = run_series(row)
        result = dict(result)
        result["name"] = row.get("name")
        results.append(result)

    impacts: list[dict] = []
    name_map = {str(r.get("name")): r for r in ordered}
    for row in ordered:
        deps = [str(dep) for dep in row.get("depends_on", []) if dep in name_map]
        for dep in deps:
            impacts.append(track_symbol_changes(name_map[dep], row))

    blocked = block_patchset_lgtm_until_reconciled(results, impacts)
    status = "lgtm" if (not blocked and all(str(r.get("status", "")).lower() == "lgtm" for r in results)) else "partial"
    payload = {
        "generated_at": _utc_now(),
        "status": status,
        "blocked_by_cross_series_impact": blocked,
        "series_results": results,
        "impacts": impacts,
    }
    reports_dir = root / ".a2a" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "patchset_summary.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_cross_series_impact(reports_dir / "cross_series_impact.json", {"impacts": impacts, "blocked": blocked})
    return payload
