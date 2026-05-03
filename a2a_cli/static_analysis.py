from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


_PATCH_FILE_RE = re.compile(r"^\+\+\+\s+b/(.+)$", re.MULTILINE)


def _extract_affected_files(patch_file: Path) -> list[str]:
    text = patch_file.read_text(encoding="utf-8", errors="replace")
    out: list[str] = []
    for match in _PATCH_FILE_RE.finditer(text):
        rel = match.group(1).strip()
        if rel.endswith((".c", ".h")) and rel not in out:
            out.append(rel)
    return out


def _tool_available(name: str) -> bool:
    return shutil.which(name) is not None


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return proc


def _clean_tree(repo: Path) -> bool:
    proc = _git(repo, "status", "--porcelain", check=False)
    return proc.returncode == 0 and not proc.stdout.strip()


def _warning_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if "warning:" in line.lower()]


def run_sparse(patch_file: str, kernel_tree: str, config: dict[str, Any]) -> dict[str, Any]:
    patch = Path(patch_file)
    repo = Path(kernel_tree)
    if not _tool_available("make"):
        return {"skipped": True, "reason": "make not installed", "new_warnings": [], "total_warnings": 0, "blocking": False}
    if not _tool_available("sparse"):
        return {"skipped": True, "reason": "sparse not installed", "new_warnings": [], "total_warnings": 0, "blocking": False}
    if not repo.exists() or not (repo / ".git").exists():
        return {"skipped": True, "reason": "invalid kernel tree", "new_warnings": [], "total_warnings": 0, "blocking": False}
    if not _clean_tree(repo):
        return {"skipped": True, "reason": "kernel tree dirty", "new_warnings": [], "total_warnings": 0, "blocking": False}

    affected = _extract_affected_files(patch)
    if not affected:
        return {"skipped": True, "reason": "no affected c/h files", "new_warnings": [], "total_warnings": 0, "blocking": False}

    current_ref = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "HEAD"
    temp_branch = f"patchwise-sa-{os.getpid()}"
    _git(repo, "checkout", "-b", temp_branch)
    try:
        apply_proc = _git(repo, "apply", str(patch), check=False)
        if apply_proc.returncode != 0:
            return {
                "skipped": True,
                "reason": "patch does not apply cleanly",
                "new_warnings": [],
                "total_warnings": 0,
                "blocking": False,
            }
        cmd = ["make", "C=1", "CHECK=sparse", *affected]
        proc = subprocess.run(cmd, cwd=repo, text=True, capture_output=True)
        warnings = _warning_lines(proc.stdout + "\n" + proc.stderr)
        baseline = [str(x) for x in config.get("baseline_warnings", [])]
        new_warnings = [w for w in warnings if w not in baseline]
        return {
            "skipped": False,
            "reason": "",
            "new_warnings": new_warnings,
            "total_warnings": len(warnings),
            "blocking": len(new_warnings) > 0 and bool(config.get("block_on_sparse", True)),
        }
    finally:
        _git(repo, "checkout", current_ref, check=False)
        _git(repo, "branch", "-D", temp_branch, check=False)


def run_coccinelle(patch_file: str, kernel_tree: str) -> dict[str, Any]:
    patch = Path(patch_file)
    repo = Path(kernel_tree)
    if not _tool_available("spatch"):
        return {"skipped": True, "reason": "coccinelle not installed", "matches": [], "blocking": False}
    if not repo.exists() or not (repo / ".git").exists():
        return {"skipped": True, "reason": "invalid kernel tree", "matches": [], "blocking": False}
    affected = _extract_affected_files(patch)
    if not affected:
        return {"skipped": True, "reason": "no affected files", "matches": [], "blocking": False}

    cocci_dir = repo / "scripts" / "coccinelle"
    cocci_files = sorted(cocci_dir.glob("*.cocci"))[:5]
    matches: list[str] = []
    for cocci in cocci_files:
        proc = subprocess.run(
            ["spatch", "--sp-file", str(cocci), *[str(repo / f) for f in affected]],
            cwd=repo,
            text=True,
            capture_output=True,
        )
        if proc.stdout.strip():
            matches.extend([line.strip() for line in proc.stdout.splitlines() if line.strip()])
    return {"skipped": False, "reason": "", "matches": matches, "blocking": False}


def run_gate(patch_file: str, kernel_tree: str, config: dict[str, Any]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    if bool(config.get("sparse", True)):
        results["sparse"] = run_sparse(patch_file, kernel_tree, config)
    if bool(config.get("coccinelle", True)):
        results["coccinelle"] = run_coccinelle(patch_file, kernel_tree)

    sparse_block = bool(results.get("sparse", {}).get("blocking", False))
    results["gate_passed"] = not sparse_block
    return results
