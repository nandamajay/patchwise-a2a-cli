from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .knowledge_base import load_kb
from .maintainer_tracker import load_profiles


_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_IDENTIFIER_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")
_KCONFIG_SYMBOL_RE = re.compile(r"^\s*config\s+([A-Z0-9_]+)\b")
_KCONFIG_DEP_RE = re.compile(r"\b(?:depends on|select|imply)\s+([A-Z0-9_]+)")
_MAKE_OBJ_RE = re.compile(r"^\s*(obj-[^\s]+)\s*\+=\s*(.+)$")
_DT_ASSIGN_RE = re.compile(r"^\s*([A-Za-z0-9,_+\-.]+)\s*=")
_DT_BOOL_RE = re.compile(r"^\s*([A-Za-z0-9,_+\-.]+)\s*;")
_YAML_KEY_RE = re.compile(r"^\s{2,}([A-Za-z0-9,_+\-.]+):")
_INCLUDE_RE = re.compile(r"^\s*#\s*include\s+[<\"]([^>\"]+)[>\"]")


_IGNORE_CALL_SYMBOLS = {
    "if",
    "for",
    "while",
    "switch",
    "return",
    "sizeof",
    "likely",
    "unlikely",
    "typeof",
    "defined",
    "min",
    "max",
    "clamp",
    "container_of",
    "ARRAY_SIZE",
}


_DEPRECATED_PATTERNS = {
    "audio": [
        r"\bsnd_soc_codec\b",
        r"\bsnd_soc_register_codec\b",
        r"\bsnd_soc_unregister_codec\b",
        r"\bsnd_soc_add_codec_controls\b",
        r"\bsnd_soc_codec_get_drvdata\b",
        r"\bsnd_soc_codec_set_drvdata\b",
    ],
    "camera": [
        r"\bmsm_camera_[A-Za-z0-9_]+\b",
        r"\bv4l2_subdev_[A-Za-z0-9_]+_ops\b",
    ],
    "drm": [
        r"\bmsm_drm_[A-Za-z0-9_]+\b",
        r"\bdrm_legacy_[A-Za-z0-9_]+\b",
    ],
    "networking": [
        r"\bndo_change_mtu\s*=\s*NULL\b",
        r"\binit_timer\b",
    ],
}


_VENDOR_TOKEN_RE = re.compile(
    r"\b("
    r"qcom_[A-Za-z0-9_]+|"
    r"qti_[A-Za-z0-9_]+|"
    r"msm_[A-Za-z0-9_]+|"
    r"vendor_[A-Za-z0-9_]+|"
    r"trace_android_vh_[A-Za-z0-9_]+|"
    r"android_[A-Za-z0-9_]+|"
    r"CONFIG_MSM_[A-Za-z0-9_]+"
    r")\b"
)


_SUBSYSTEM_HINTS = {
    "audio": [
        "sound",
        "drivers/soundwire",
        "drivers/soc/qcom",
        "techpack/audio",
        "include/sound",
        "Documentation/devicetree/bindings/sound",
        "arch/arm64/boot/dts/qcom",
    ],
    "camera": [
        "drivers/media",
        "techpack/camera",
        "include/media",
        "Documentation/devicetree/bindings/media",
        "arch/arm64/boot/dts/qcom",
    ],
    "drm": [
        "drivers/gpu/drm",
        "include/drm",
        "Documentation/devicetree/bindings/display",
        "arch/arm64/boot/dts/qcom",
    ],
    "networking": [
        "drivers/net",
        "net",
        "include/net",
        "Documentation/devicetree/bindings/net",
        "arch/arm64/boot/dts/qcom",
    ],
}


_ANALYZER_VERSION = "Driver_Gap_Analyzer_V1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_subsystem(name: str) -> str:
    raw = str(name or "").strip().lower()
    if raw in {"asoc", "sound", "soundwire"}:
        return "audio"
    if raw in {"net", "ethernet", "wireless"}:
        return "networking"
    return raw or "audio"


def _iter_files(root: Path, *, max_files: int = 20000) -> tuple[list[Path], bool]:
    files: list[Path] = []
    truncated = False
    skip_dirs = {".git", ".a2a", "out", "build", "dist", "__pycache__"}
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in sorted(names):
            path = Path(base) / name
            files.append(path)
            if len(files) >= max_files:
                truncated = True
                return files, truncated
    return files, truncated


def _is_relevant_source_file(path: Path) -> bool:
    suffix = path.suffix.lower()
    name = path.name
    if suffix in {".c", ".h", ".dts", ".dtsi", ".yaml", ".yml"}:
        return True
    if name == "Makefile" or name == "Kbuild" or name.startswith("Kconfig"):
        return True
    return False


def _select_scan_roots(tree_root: Path, subsystem: str, *, include_global_headers: bool = False) -> list[Path]:
    hints = _SUBSYSTEM_HINTS.get(subsystem, ["drivers", "include", "Documentation"])
    selected: list[Path] = []
    for hint in hints:
        candidate = (tree_root / hint).resolve()
        if candidate.exists():
            selected.append(candidate)

    if include_global_headers:
        include_root = (tree_root / "include").resolve()
        if include_root.exists() and include_root not in selected:
            selected.append(include_root)

    if not selected:
        selected.append(tree_root.resolve())
    return selected


def _collect_relevant_files(tree_root: Path, subsystem: str, *, include_global_headers: bool = False) -> dict[str, Any]:
    scan_roots = _select_scan_roots(tree_root, subsystem, include_global_headers=include_global_headers)
    all_files: list[Path] = []
    truncated = False
    for scan_root in scan_roots:
        files, is_truncated = _iter_files(scan_root)
        all_files.extend(files)
        truncated = truncated or is_truncated

    deduped: list[Path] = []
    seen: set[str] = set()
    for path in all_files:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        if _is_relevant_source_file(path):
            deduped.append(path)

    rel_files = [str(path.resolve().relative_to(tree_root.resolve())) for path in deduped if path.exists()]
    return {
        "scan_roots": [str(p) for p in scan_roots],
        "files": deduped,
        "rel_files": rel_files,
        "truncated": truncated,
    }


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _extract_call_sites(text: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for match in _CALL_RE.finditer(line):
            symbol = str(match.group(1) or "").strip()
            if not symbol or symbol in _IGNORE_CALL_SYMBOLS:
                continue
            if symbol.isupper():
                continue
            out.append((symbol, line_no))
    return out


def _extract_identifiers(text: str) -> set[str]:
    return {m.group(1) for m in _IDENTIFIER_RE.finditer(text)}


def _extract_kconfig_symbols(text: str) -> dict[str, set[str]]:
    configs: set[str] = set()
    deps: set[str] = set()
    for line in text.splitlines():
        m = _KCONFIG_SYMBOL_RE.match(line)
        if m:
            configs.add(str(m.group(1)).strip())
        for dep in _KCONFIG_DEP_RE.findall(line):
            deps.add(str(dep).strip())
    return {"configs": configs, "deps": deps}


def _extract_makefile_objects(text: str) -> set[str]:
    objs: set[str] = set()
    for line in text.splitlines():
        m = _MAKE_OBJ_RE.match(line)
        if not m:
            continue
        rhs = str(m.group(2) or "").strip()
        for token in rhs.split():
            clean = token.strip()
            if not clean:
                continue
            objs.add(clean)
    return objs


def _extract_dt_properties(text: str, path: Path) -> set[str]:
    props: set[str] = set()
    is_yaml = path.suffix.lower() in {".yaml", ".yml"}
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line or line.startswith("/*") or line.startswith("*"):
            continue
        if is_yaml:
            m = _YAML_KEY_RE.match(raw)
            if m:
                key = str(m.group(1) or "").strip()
                if key and key not in {"properties", "required", "allOf", "oneOf"}:
                    props.add(key)
            continue

        m_assign = _DT_ASSIGN_RE.match(line)
        if m_assign:
            props.add(str(m_assign.group(1) or "").strip())
            continue
        m_bool = _DT_BOOL_RE.match(line)
        if m_bool:
            key = str(m_bool.group(1) or "").strip()
            if key and key not in {"};", "{"}:
                props.add(key)
    return props


def _infer_arch_features(file_texts: dict[str, str]) -> dict[str, int]:
    patterns = {
        "probe_flow": r"\b[A-Za-z0-9_]+_probe\s*\(",
        "remove_flow": r"\b[A-Za-z0-9_]+_remove\s*\(",
        "runtime_pm": r"\bpm_runtime_[A-Za-z0-9_]+\b",
        "regmap": r"\bregmap_[A-Za-z0-9_]+\b",
        "soundwire": r"\b(?:sdw|soundwire|swrm?)_[A-Za-z0-9_]+\b",
        "dapm": r"\b(?:SND_SOC_DAPM|snd_soc_dapm_[A-Za-z0-9_]+)\b",
        "kcontrols": r"\b(?:SOC_[A-Z0-9_]+|snd_kcontrol_[A-Za-z0-9_]+)\b",
        "interrupts": r"\b(?:request_threaded_irq|request_irq|irqreturn_t)\b",
        "component_registration": r"\b(?:snd_soc_register_component|devm_snd_soc_register_component|component_add)\b",
    }
    counts: dict[str, int] = {}
    for key, pattern in patterns.items():
        rx = re.compile(pattern)
        value = 0
        for text in file_texts.values():
            value += len(rx.findall(text))
        counts[key] = value
    return counts


def _build_dependency_graph(file_texts: dict[str, str], downstream_rel_files: list[str], missing_symbols: set[str]) -> dict[str, Any]:
    nodes = sorted(downstream_rel_files)
    edges: list[dict[str, str]] = []

    for rel_path in sorted(file_texts.keys()):
        text = file_texts[rel_path]
        for line in text.splitlines():
            m_inc = _INCLUDE_RE.match(line)
            if m_inc:
                include_target = str(m_inc.group(1) or "").strip()
                if include_target:
                    edges.append({"type": "include", "from": rel_path, "to": include_target})

        for symbol, _line_no in _extract_call_sites(text):
            if symbol in missing_symbols:
                edges.append({"type": "missing_interface", "from": rel_path, "to": symbol})

    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": nodes,
        "edges": edges,
    }


def _collect_lore_evidence(aura_root: Path, subsystem: str) -> dict[str, Any]:
    sessions_dir = aura_root / ".a2a" / "sessions"
    if not sessions_dir.exists():
        return {"sessions_with_lore": 0, "sample_links": []}

    links: list[str] = []
    session_count = 0
    for path in sorted(sessions_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        watch = str(payload.get("watch_path") or "").lower()
        lore = payload.get("lore") if isinstance(payload.get("lore"), dict) else {}
        msgid = str(lore.get("message_id") or "").strip()
        if not msgid:
            continue
        if subsystem == "audio":
            if not any(tok in watch for tok in ("sound", "asoc", "lpass", "wsa", "wcd", "swr")):
                continue
        elif subsystem and subsystem not in watch:
            continue
        session_count += 1
        links.append(f"https://lore.kernel.org/r/{msgid}")

    return {
        "sessions_with_lore": session_count,
        "sample_links": links[:20],
    }


def _load_aura_assets(aura_root: Path, subsystem: str) -> dict[str, Any]:
    kb = load_kb(aura_root)
    entries = kb.get("entries", []) if isinstance(kb, dict) else []
    if not isinstance(entries, list):
        entries = []

    matching_entries = [
        row
        for row in entries
        if isinstance(row, dict)
        and str(row.get("subsystem") or "").strip().lower() in {subsystem, "sound", "asoc", "codec", "unknown"}
    ]
    matching_entries = sorted(matching_entries, key=lambda row: int(row.get("occurrences", 0)), reverse=True)

    profiles = load_profiles(aura_root)
    maint = profiles.get("maintainers", {}) if isinstance(profiles, dict) else {}
    maint_rows = []
    if isinstance(maint, dict):
        for name, row in maint.items():
            if not isinstance(row, dict):
                continue
            maint_rows.append(
                {
                    "name": name,
                    "priority": row.get("priority", "medium"),
                    "approval_rate": row.get("approval_rate", 0.0),
                    "common_concerns": row.get("common_concerns", []),
                    "review_count": row.get("review_count", 0),
                }
            )
    maint_rows = sorted(maint_rows, key=lambda row: (row.get("priority"), row.get("review_count", 0)), reverse=True)

    lore_evidence = _collect_lore_evidence(aura_root, subsystem)

    return {
        "accepted_patch_patterns": [
            {
                "pattern": row.get("pattern", ""),
                "resolution": row.get("resolution", ""),
                "occurrences": row.get("occurrences", 0),
                "subsystem": row.get("subsystem", "unknown"),
            }
            for row in matching_entries[:25]
        ],
        "subsystem_rules": [
            {
                "pattern": row.get("pattern", ""),
                "severity": row.get("severity", "medium"),
                "resolution": row.get("resolution", ""),
            }
            for row in matching_entries[:25]
        ],
        "maintainer_playbooks": maint_rows[:20],
        "lore_mining": lore_evidence,
    }


def _compute_risk(missing_count: int, deprecated_count: int, vendor_count: int, dt_gap_count: int) -> dict[str, Any]:
    risk_items: list[dict[str, Any]] = []

    if vendor_count > 0:
        severity = "high" if vendor_count >= 20 else "medium"
        risk_items.append(
            {
                "risk": "Vendor hook replacement complexity",
                "severity": severity,
                "detail": f"{vendor_count} vendor-prefixed hook/symbol usages detected.",
            }
        )

    if missing_count > 0:
        severity = "high" if missing_count >= 25 else "medium"
        risk_items.append(
            {
                "risk": "Missing upstream interfaces",
                "severity": severity,
                "detail": f"{missing_count} downstream-called symbols were not found in scanned upstream interfaces.",
            }
        )

    if deprecated_count > 0:
        risk_items.append(
            {
                "risk": "Deprecated downstream API usage",
                "severity": "medium",
                "detail": f"{deprecated_count} deprecated API call-sites detected.",
            }
        )

    if dt_gap_count > 0:
        risk_items.append(
            {
                "risk": "Device-tree divergence",
                "severity": "medium",
                "detail": f"{dt_gap_count} DT file/property differences detected.",
            }
        )

    if not risk_items:
        risk_items.append(
            {
                "risk": "No critical migration blockers detected by static gap analyzer",
                "severity": "low",
                "detail": "Proceed with standard upstream split and review flow.",
            }
        )

    severity_score = {"low": 1, "medium": 2, "high": 3}
    aggregate = sum(severity_score.get(str(row.get("severity")), 1) for row in risk_items)
    if aggregate >= 8:
        level = "high"
    elif aggregate >= 4:
        level = "medium"
    else:
        level = "low"

    return {
        "overall": level,
        "items": risk_items,
    }


def _estimate_patch_difficulty(scope_size: int, vendor_count: int, deprecated_count: int, missing_count: int) -> int:
    score = 1
    if scope_size >= 8:
        score += 1
    if vendor_count >= 8:
        score += 1
    if deprecated_count >= 4:
        score += 1
    if missing_count >= 10:
        score += 1
    return max(1, min(5, score))


def _build_upstream_plan(result: dict[str, Any]) -> dict[str, Any]:
    dt = result["device_tree_differences"]
    kcfg = result["kconfig_differences"]
    mk = result["makefile_differences"]
    missing = result["missing_upstream_interfaces"]
    deprecated = result["deprecated_downstream_apis"]
    vendor = result["vendor_hook_inventory"]

    patches: list[dict[str, Any]] = []

    if dt["missing_upstream_files"] or dt["property_differences"]:
        scope = len(dt["missing_upstream_files"]) + len(dt["property_differences"])
        patches.append(
            {
                "patch": 1,
                "title": "dt-bindings and DTS alignment",
                "purpose": "Align downstream DT properties/bindings to upstream schema and naming.",
                "difficulty": _estimate_patch_difficulty(scope, 0, 0, 0),
                "risk": "medium" if scope else "low",
            }
        )

    if kcfg["downstream_only_symbols"] or mk["downstream_only_objects"]:
        scope = len(kcfg["downstream_only_symbols"]) + len(mk["downstream_only_objects"])
        patches.append(
            {
                "patch": len(patches) + 1,
                "title": "Kconfig and Makefile upstream alignment",
                "purpose": "Port build-time symbols and object wiring without vendor-only dependencies.",
                "difficulty": _estimate_patch_difficulty(scope, 0, 0, 0),
                "risk": "medium" if scope >= 4 else "low",
            }
        )

    if deprecated["count"]:
        patches.append(
            {
                "patch": len(patches) + 1,
                "title": "Replace deprecated downstream APIs",
                "purpose": "Switch to upstream-supported frameworks and helper APIs.",
                "difficulty": _estimate_patch_difficulty(
                    deprecated["count"],
                    0,
                    deprecated["count"],
                    0,
                ),
                "risk": "medium",
            }
        )

    if vendor["count"]:
        patches.append(
            {
                "patch": len(patches) + 1,
                "title": "Vendor hook removal and upstream interface mapping",
                "purpose": "Replace vendor hooks with upstream subsystems, callbacks, and standard lifecycles.",
                "difficulty": _estimate_patch_difficulty(vendor["count"], vendor["count"], 0, missing["count"]),
                "risk": "high" if vendor["count"] >= 10 else "medium",
            }
        )

    if missing["count"]:
        patches.append(
            {
                "patch": len(patches) + 1,
                "title": "Missing interface reconciliation",
                "purpose": "Implement or remap unresolved downstream calls to upstream interfaces.",
                "difficulty": _estimate_patch_difficulty(missing["count"], vendor["count"], 0, missing["count"]),
                "risk": "high" if missing["count"] >= 20 else "medium",
            }
        )

    patches.append(
        {
            "patch": len(patches) + 1,
            "title": "Final cleanup, docs, and submission prep",
            "purpose": "Polish commit messages, split hygiene, and submit-ready checks.",
            "difficulty": 2,
            "risk": "low",
        }
    )

    roadmap = [
        "Freeze downstream baseline and enumerate subsystem-local files.",
        "Land DT/Kconfig/Makefile scaffolding first for bisect-safe bring-up.",
        "Port driver core while replacing deprecated APIs and vendor hooks.",
        "Resolve missing interfaces with upstream-native APIs and lifecycle hooks.",
        "Run checkpatch, compile smoke, and maintainers-alignment pass before posting.",
    ]

    return {
        "upstreaming_roadmap": roadmap,
        "patch_sequence": patches,
    }


def analyze_driver_gap(
    downstream_root: Path,
    upstream_root: Path,
    subsystem: str,
    *,
    aura_root: Path | None = None,
    driver_name: str | None = None,
) -> dict[str, Any]:
    subsystem_norm = _normalize_subsystem(subsystem)
    ds_root = downstream_root.resolve()
    us_root = upstream_root.resolve()
    aura = (aura_root or Path.cwd()).resolve()

    ds_files_info = _collect_relevant_files(ds_root, subsystem_norm, include_global_headers=False)
    us_files_info = _collect_relevant_files(us_root, subsystem_norm, include_global_headers=True)

    ds_texts: dict[str, str] = {}
    us_texts: dict[str, str] = {}

    for path in ds_files_info["files"]:
        rel = str(path.resolve().relative_to(ds_root))
        ds_texts[rel] = _read_text(path)

    for path in us_files_info["files"]:
        rel = str(path.resolve().relative_to(us_root))
        us_texts[rel] = _read_text(path)

    downstream_calls: Counter[str] = Counter()
    downstream_call_sites: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rel, text in ds_texts.items():
        for symbol, line_no in _extract_call_sites(text):
            downstream_calls[symbol] += 1
            if len(downstream_call_sites[symbol]) < 8:
                downstream_call_sites[symbol].append({"file": rel, "line": line_no})

    upstream_identifiers: set[str] = set()
    for text in us_texts.values():
        upstream_identifiers.update(_extract_identifiers(text))

    missing_symbols = [
        {
            "symbol": symbol,
            "call_count": int(count),
            "examples": downstream_call_sites.get(symbol, []),
        }
        for symbol, count in downstream_calls.most_common()
        if symbol not in upstream_identifiers
    ]

    deprecated_rx = [re.compile(pat) for pat in _DEPRECATED_PATTERNS.get(subsystem_norm, [])]
    deprecated_hits: list[dict[str, Any]] = []
    for rel, text in ds_texts.items():
        for line_no, line in enumerate(text.splitlines(), start=1):
            for rx in deprecated_rx:
                m = rx.search(line)
                if not m:
                    continue
                deprecated_hits.append(
                    {
                        "file": rel,
                        "line": line_no,
                        "match": m.group(0),
                        "pattern": rx.pattern,
                    }
                )

    vendor_hits: list[dict[str, Any]] = []
    for rel, text in ds_texts.items():
        for line_no, line in enumerate(text.splitlines(), start=1):
            for m in _VENDOR_TOKEN_RE.finditer(line):
                vendor_hits.append(
                    {
                        "file": rel,
                        "line": line_no,
                        "token": str(m.group(1) or "").strip(),
                    }
                )

    ds_dt_files = {
        rel: text
        for rel, text in ds_texts.items()
        if rel.endswith((".dts", ".dtsi", ".yaml", ".yml")) or "bindings" in rel
    }
    us_dt_files = {
        rel: text
        for rel, text in us_texts.items()
        if rel.endswith((".dts", ".dtsi", ".yaml", ".yml")) or "bindings" in rel
    }

    ds_dt_set = set(ds_dt_files.keys())
    us_dt_set = set(us_dt_files.keys())
    missing_upstream_dt_files = sorted(ds_dt_set - us_dt_set)

    property_diffs: list[dict[str, Any]] = []
    for rel in sorted(ds_dt_set & us_dt_set):
        ds_props = _extract_dt_properties(ds_dt_files[rel], Path(rel))
        us_props = _extract_dt_properties(us_dt_files[rel], Path(rel))
        down_only = sorted(ds_props - us_props)
        up_only = sorted(us_props - ds_props)
        if down_only or up_only:
            property_diffs.append(
                {
                    "file": rel,
                    "downstream_only_properties": down_only[:80],
                    "upstream_only_properties": up_only[:80],
                }
            )

    ds_kconfig_symbols: set[str] = set()
    us_kconfig_symbols: set[str] = set()
    ds_kconfig_deps: set[str] = set()
    us_kconfig_deps: set[str] = set()

    for rel, text in ds_texts.items():
        if Path(rel).name.startswith("Kconfig"):
            row = _extract_kconfig_symbols(text)
            ds_kconfig_symbols.update(row["configs"])
            ds_kconfig_deps.update(row["deps"])

    for rel, text in us_texts.items():
        if Path(rel).name.startswith("Kconfig"):
            row = _extract_kconfig_symbols(text)
            us_kconfig_symbols.update(row["configs"])
            us_kconfig_deps.update(row["deps"])

    ds_make_objs: set[str] = set()
    us_make_objs: set[str] = set()
    for rel, text in ds_texts.items():
        name = Path(rel).name
        if name in {"Makefile", "Kbuild"}:
            ds_make_objs.update(_extract_makefile_objects(text))

    for rel, text in us_texts.items():
        name = Path(rel).name
        if name in {"Makefile", "Kbuild"}:
            us_make_objs.update(_extract_makefile_objects(text))

    ds_arch = _infer_arch_features(ds_texts)
    us_arch = _infer_arch_features(us_texts)
    arch_delta = {
        key: {
            "downstream": ds_arch.get(key, 0),
            "upstream": us_arch.get(key, 0),
            "delta": ds_arch.get(key, 0) - us_arch.get(key, 0),
        }
        for key in sorted(set(ds_arch) | set(us_arch))
    }

    missing_symbols_set = {row["symbol"] for row in missing_symbols}
    dependency_graph = _build_dependency_graph(ds_texts, ds_files_info["rel_files"], missing_symbols_set)

    aura_assets = _load_aura_assets(aura, subsystem_norm)

    result: dict[str, Any] = {
        "analyzer": _ANALYZER_VERSION,
        "generated_at": _utc_now(),
        "driver_name": str(driver_name or ""),
        "subsystem": subsystem_norm,
        "downstream_root": str(ds_root),
        "upstream_root": str(us_root),
        "inventory": {
            "downstream_files_scanned": len(ds_files_info["rel_files"]),
            "upstream_files_scanned": len(us_files_info["rel_files"]),
            "downstream_scan_roots": ds_files_info["scan_roots"],
            "upstream_scan_roots": us_files_info["scan_roots"],
            "downstream_scan_truncated": bool(ds_files_info["truncated"]),
            "upstream_scan_truncated": bool(us_files_info["truncated"]),
        },
        "api_gap_report": {
            "downstream_call_symbol_count": int(sum(downstream_calls.values())),
            "unique_downstream_call_symbols": int(len(downstream_calls)),
            "missing_upstream_symbol_count": int(len(missing_symbols)),
        },
        "missing_upstream_interfaces": {
            "count": len(missing_symbols),
            "symbols": missing_symbols[:200],
        },
        "deprecated_downstream_apis": {
            "count": len(deprecated_hits),
            "hits": deprecated_hits[:300],
        },
        "vendor_hook_inventory": {
            "count": len(vendor_hits),
            "hooks": vendor_hits[:400],
        },
        "device_tree_differences": {
            "missing_upstream_files": missing_upstream_dt_files,
            "property_differences": property_diffs,
        },
        "kconfig_differences": {
            "downstream_only_symbols": sorted(ds_kconfig_symbols - us_kconfig_symbols),
            "upstream_only_symbols": sorted(us_kconfig_symbols - ds_kconfig_symbols),
            "downstream_only_dependencies": sorted(ds_kconfig_deps - us_kconfig_deps),
            "upstream_only_dependencies": sorted(us_kconfig_deps - ds_kconfig_deps),
        },
        "makefile_differences": {
            "downstream_only_objects": sorted(ds_make_objs - us_make_objs),
            "upstream_only_objects": sorted(us_make_objs - ds_make_objs),
        },
        "architecture_differences": arch_delta,
        "dependency_graph": dependency_graph,
        "aura_reused_assets": aura_assets,
    }

    plan = _build_upstream_plan(result)
    risk = _compute_risk(
        missing_count=result["missing_upstream_interfaces"]["count"],
        deprecated_count=result["deprecated_downstream_apis"]["count"],
        vendor_count=result["vendor_hook_inventory"]["count"],
        dt_gap_count=len(result["device_tree_differences"]["missing_upstream_files"]) + len(result["device_tree_differences"]["property_differences"]),
    )
    result.update(plan)
    result["risk_assessment"] = risk
    return result


def _render_api_gap_markdown(result: dict[str, Any]) -> str:
    gap = result["api_gap_report"]
    missing = result["missing_upstream_interfaces"]
    lines = [
        "# API Gap Report",
        "",
        f"- Analyzer: {result.get('analyzer')}",
        f"- Driver: {result.get('driver_name') or '-'}",
        f"- Subsystem: {result.get('subsystem')}",
        f"- Downstream root: `{result.get('downstream_root')}`",
        f"- Upstream root: `{result.get('upstream_root')}`",
        "",
        "## Summary",
        "",
        f"- Downstream call symbols (total): {gap.get('downstream_call_symbol_count', 0)}",
        f"- Unique downstream call symbols: {gap.get('unique_downstream_call_symbols', 0)}",
        f"- Missing upstream symbols: {gap.get('missing_upstream_symbol_count', 0)}",
        "",
        "## Top Missing Interfaces",
        "",
    ]
    symbols = missing.get("symbols", []) if isinstance(missing, dict) else []
    if not symbols:
        lines.append("- none")
    else:
        for row in symbols[:40]:
            lines.append(
                f"- `{row.get('symbol')}`: call_count={row.get('call_count')} examples={row.get('examples', [])[:2]}"
            )
    lines.append("")
    return "\n".join(lines)


def _render_roadmap_markdown(result: dict[str, Any]) -> str:
    lines = ["# Upstreaming Roadmap", ""]
    for idx, step in enumerate(result.get("upstreaming_roadmap", []), start=1):
        lines.append(f"{idx}. {step}")
    lines.append("")
    return "\n".join(lines)


def _render_patch_sequence_markdown(result: dict[str, Any]) -> str:
    lines = ["# Patch Sequence", ""]
    for row in result.get("patch_sequence", []):
        lines.extend(
            [
                f"## PATCH {row.get('patch')}: {row.get('title')}",
                "",
                f"- Purpose: {row.get('purpose')}",
                f"- Difficulty (1-5): {row.get('difficulty')}",
                f"- Risk: {row.get('risk')}",
                "",
            ]
        )
    return "\n".join(lines)


def _render_risk_markdown(result: dict[str, Any]) -> str:
    risk = result.get("risk_assessment", {}) if isinstance(result.get("risk_assessment"), dict) else {}
    lines = ["# Risk Assessment", "", f"- Overall risk: **{risk.get('overall', 'unknown')}**", "", "## Risk Items", ""]
    rows = risk.get("items", []) if isinstance(risk.get("items"), list) else []
    if not rows:
        lines.append("- none")
    else:
        for row in rows:
            lines.append(f"- [{row.get('severity', 'low')}] {row.get('risk')}: {row.get('detail')}")
    lines.append("")
    return "\n".join(lines)


def _render_architecture_document(result: dict[str, Any]) -> str:
    reuse = result.get("aura_reused_assets", {}) if isinstance(result.get("aura_reused_assets"), dict) else {}
    lines = [
        "# Driver_Gap_Analyzer_V1 Architecture",
        "",
        "## Objective",
        "",
        "Provide a static downstream-vs-upstream gap analysis focused on producing an actionable upstreaming plan for Linux driver conversion.",
        "",
        "## Inputs",
        "",
        "- downstream driver source tree",
        "- upstream kernel source tree",
        "- subsystem type",
        "",
        "## Pipeline",
        "",
        "1. File inventory and subsystem-scoped scan root selection.",
        "2. API extraction (call symbols, identifier corpus) and missing interface detection.",
        "3. Deprecated API and vendor hook detection.",
        "4. DT/Kconfig/Makefile structural diff.",
        "5. Architecture feature delta and dependency graph extraction.",
        "6. Upstreaming roadmap, patch sequence, difficulty, and risk synthesis.",
        "",
        "## Existing AURA Components Reused",
        "",
        "- `knowledge_base.py`: recurring accepted patterns / subsystem rules context.",
        "- `maintainer_tracker.py`: maintainer profile context for planning risk.",
        "- existing `.a2a/sessions` lore metadata: prior lore-thread evidence sampling.",
        "- existing CLI/report conventions for deterministic artifact output.",
        "",
        "## New Components Implemented",
        "",
        "- `a2a_cli/driver_gap_analyzer.py` (core analysis engine + report writers)",
        "- `a2a gap-analyze` CLI command (execution + artifact generation)",
        "",
        "## Reused Asset Coverage",
        "",
        f"- accepted_patch_patterns loaded: {len(reuse.get('accepted_patch_patterns', []))}",
        f"- subsystem_rules loaded: {len(reuse.get('subsystem_rules', []))}",
        f"- maintainer_playbooks loaded: {len(reuse.get('maintainer_playbooks', []))}",
        f"- lore evidence sessions: {((reuse.get('lore_mining') or {}).get('sessions_with_lore', 0) if isinstance(reuse.get('lore_mining'), dict) else 0)}",
        "",
    ]
    return "\n".join(lines)


def _render_implementation_plan(result: dict[str, Any]) -> str:
    lines = [
        "# Driver_Gap_Analyzer_V1 Implementation Plan",
        "",
        "## Scope",
        "",
        "Implement a deterministic, static analyzer that emits conversion-focused diffs and a patch roadmap.",
        "",
        "## Delivered",
        "",
        "1. API/missing-interface scanner.",
        "2. Deprecated API and vendor hook inventory.",
        "3. DT/Kconfig/Makefile/architecture diff engine.",
        "4. Dependency graph extraction.",
        "5. Roadmap + patch sequence + risk synthesis.",
        "6. Artifact bundle writer.",
        "",
        "## Immediate Next Increment",
        "",
        "1. Add symbol-resolution confidence tiers using compile_commands or ctags when available.",
        "2. Add subsystem plugin packs for finer deprecated API heuristics.",
        "3. Add optional lore patch-history join for patch split hints.",
        "",
    ]
    return "\n".join(lines)


def _render_mvp_scope(result: dict[str, Any]) -> str:
    lines = [
        "# Driver_Gap_Analyzer_V1 MVP Scope",
        "",
        "## In Scope",
        "",
        "- Static source-tree diffing for downstream vs upstream",
        "- API gap detection based on downstream calls vs upstream identifiers",
        "- vendor hook and deprecated API inventories",
        "- DT/Kconfig/Makefile/architecture differences",
        "- dependency graph extraction",
        "- roadmap, patch sequence, difficulty, and risk output",
        "",
        "## Out of Scope (V1)",
        "",
        "- automated code conversion",
        "- benchmark generation",
        "- full semantic compile-time type resolution",
        "- automatic patch generation",
        "",
    ]
    return "\n".join(lines)


def _render_first_milestone(result: dict[str, Any], output_dir: Path) -> str:
    lines = [
        "# First Executable Milestone",
        "",
        "## Milestone",
        "",
        "Run one real downstream Qualcomm driver analysis and produce a complete upstreaming-plan bundle.",
        "",
        "## Command",
        "",
        "```bash",
        "a2a gap-analyze \\",
        "  --downstream-root /path/to/downstream/kernel \\",
        "  --upstream-root /path/to/upstream/kernel \\",
        f"  --subsystem {result.get('subsystem', 'audio')} \\",
        f"  --driver-name {result.get('driver_name') or 'target_driver'} \\",
        f"  --output-dir {str(output_dir)}",
        "```",
        "",
        "## Success Criteria",
        "",
        "- all required report artifacts generated",
        "- missing interface set produced",
        "- patch sequence and risk assessment produced",
        "- architecture/implementation/MVP/milestone docs produced",
        "",
    ]
    return "\n".join(lines)


def write_gap_analysis_reports(result: dict[str, Any], output_dir: Path) -> dict[str, str]:
    output = output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    files: dict[str, str] = {}

    full_json_path = output / "driver_gap_analysis.json"
    full_json_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    files["driver_gap_analysis"] = str(full_json_path)

    api_md = output / "api_gap_report.md"
    api_md.write_text(_render_api_gap_markdown(result), encoding="utf-8")
    files["api_gap_report"] = str(api_md)

    missing_json = output / "missing_upstream_interfaces.json"
    missing_json.write_text(
        json.dumps(result.get("missing_upstream_interfaces", {}), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    files["missing_upstream_interfaces"] = str(missing_json)

    deprecated_json = output / "deprecated_downstream_apis.json"
    deprecated_json.write_text(
        json.dumps(result.get("deprecated_downstream_apis", {}), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    files["deprecated_downstream_apis"] = str(deprecated_json)

    vendor_json = output / "vendor_hook_inventory.json"
    vendor_json.write_text(
        json.dumps(result.get("vendor_hook_inventory", {}), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    files["vendor_hook_inventory"] = str(vendor_json)

    dt_json = output / "device_tree_differences.json"
    dt_json.write_text(
        json.dumps(result.get("device_tree_differences", {}), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    files["device_tree_differences"] = str(dt_json)

    kconfig_json = output / "kconfig_differences.json"
    kconfig_json.write_text(
        json.dumps(result.get("kconfig_differences", {}), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    files["kconfig_differences"] = str(kconfig_json)

    makefile_json = output / "makefile_differences.json"
    makefile_json.write_text(
        json.dumps(result.get("makefile_differences", {}), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    files["makefile_differences"] = str(makefile_json)

    arch_json = output / "architecture_differences.json"
    arch_json.write_text(
        json.dumps(result.get("architecture_differences", {}), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    files["architecture_differences"] = str(arch_json)

    dep_json = output / "dependency_graph.json"
    dep_json.write_text(
        json.dumps(result.get("dependency_graph", {}), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    files["dependency_graph"] = str(dep_json)

    roadmap_md = output / "upstreaming_roadmap.md"
    roadmap_md.write_text(_render_roadmap_markdown(result), encoding="utf-8")
    files["upstreaming_roadmap"] = str(roadmap_md)

    patch_md = output / "patch_sequence.md"
    patch_md.write_text(_render_patch_sequence_markdown(result), encoding="utf-8")
    files["patch_sequence"] = str(patch_md)

    risk_md = output / "risk_assessment.md"
    risk_md.write_text(_render_risk_markdown(result), encoding="utf-8")
    files["risk_assessment"] = str(risk_md)

    architecture_doc = output / "architecture_document.md"
    architecture_doc.write_text(_render_architecture_document(result), encoding="utf-8")
    files["architecture_document"] = str(architecture_doc)

    implementation_doc = output / "implementation_plan.md"
    implementation_doc.write_text(_render_implementation_plan(result), encoding="utf-8")
    files["implementation_plan"] = str(implementation_doc)

    mvp_doc = output / "mvp_scope.md"
    mvp_doc.write_text(_render_mvp_scope(result), encoding="utf-8")
    files["mvp_scope"] = str(mvp_doc)

    milestone_doc = output / "first_executable_milestone.md"
    milestone_doc.write_text(_render_first_milestone(result, output), encoding="utf-8")
    files["first_executable_milestone"] = str(milestone_doc)

    index_md = output / "driver_gap_analyzer_index.md"
    index_lines = [
        "# Driver_Gap_Analyzer_V1 Artifact Index",
        "",
        f"- generated_at: {_utc_now()}",
        f"- analyzer: {result.get('analyzer')}",
        f"- subsystem: {result.get('subsystem')}",
        f"- driver_name: {result.get('driver_name') or '-'}",
        "",
        "## Artifacts",
        "",
    ]
    for key, path in files.items():
        index_lines.append(f"- {key}: `{path}`")
    index_lines.append("")
    index_md.write_text("\n".join(index_lines), encoding="utf-8")
    files["index"] = str(index_md)

    return files
