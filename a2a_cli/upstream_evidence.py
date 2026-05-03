from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any


_SYMBOL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\b")


def extract_symbol(text: str) -> str:
    matches = _SYMBOL_RE.findall(text or "")
    if not matches:
        return "unknown_symbol"
    preferred = [m for m in matches if "_" in m]
    if preferred:
        return preferred[0]
    return matches[0]


def search_elixir(symbol: str, subsystem: str, base: str = "https://elixir.bootlin.com/linux/latest") -> str:
    return f"{base}/search?q={symbol}"


def search_lore(pattern: str, author: str | None = None) -> str:
    if author:
        return f"https://lore.kernel.org/all/?q={pattern}%20{author}"
    return f"https://lore.kernel.org/all/?q={pattern}"


def search_kernel_git(symbol: str, kernel_tree: str) -> list[str]:
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(kernel_tree),
            "log",
            "--oneline",
            "--all",
            f"--grep={symbol}",
            "-10",
        ],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def kb_lookup(symbol: str, kb_entries: list[dict]) -> dict | None:
    sym = symbol.lower()
    for row in kb_entries:
        pattern = str(row.get("pattern", "")).lower()
        if sym and sym in pattern:
            return row
    return None


def build_evidence_package(
    finding: dict,
    kernel_tree: str,
    kb_entries: list[dict] | None = None,
    *,
    elixir_base: str = "https://elixir.bootlin.com/linux/latest",
) -> dict[str, Any]:
    symbol = extract_symbol(
        str(finding.get("description") or finding.get("required_action") or finding.get("title") or "")
    )
    subsystem = str(finding.get("subsystem") or "unknown")
    commits = search_kernel_git(symbol, kernel_tree) if kernel_tree else []
    kb_ref = kb_lookup(symbol, kb_entries or [])
    pkg = {
        "symbol": symbol,
        "elixir_url": search_elixir(symbol, subsystem, base=elixir_base),
        "lore_url": search_lore(symbol),
        "git_commits": commits,
        "kb_reference": kb_ref,
    }
    return pkg


def enrich_findings_with_evidence(
    findings: list[dict],
    *,
    kernel_tree: str,
    strict_mode: bool,
    block_on_no_evidence: bool,
    kb_entries: list[dict] | None = None,
    elixir_base: str = "https://elixir.bootlin.com/linux/latest",
) -> tuple[list[dict], list[str]]:
    out: list[dict] = []
    violations: list[str] = []
    for finding in findings:
        row = dict(finding)
        status = str(row.get("status", "open")).lower()
        if status == "closed":
            out.append(row)
            continue

        pkg = build_evidence_package(
            row,
            kernel_tree,
            kb_entries=kb_entries or [],
            elixir_base=elixir_base,
        )
        row["upstream_evidence"] = pkg
        has_any = bool(pkg.get("elixir_url") or pkg.get("lore_url") or pkg.get("git_commits") or pkg.get("kb_reference"))
        if strict_mode and block_on_no_evidence and not has_any:
            fid = str(row.get("id") or row.get("title") or "unknown")
            violations.append(f"finding {fid} has no upstream evidence")
        out.append(row)
    return out, violations


def kernel_tree_exists(path: str) -> bool:
    if not path:
        return False
    p = Path(path)
    return p.exists() and (p / ".git").exists()
