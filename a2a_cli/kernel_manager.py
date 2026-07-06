"""Managed kernel repository for A2A patch review sessions.

Provides a singleton linux-next kernel that is automatically kept fresh
and used as the base for applying lore-fetched patches before review.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

LINUX_NEXT_URL = "https://git.kernel.org/pub/scm/linux/kernel/git/next/linux-next.git"
MANAGED_KERNEL_DIRNAME = "kernel/linux-next"


def _run_git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        text=True,
        capture_output=True,
        check=check,
    )


def _git_ok(cwd: Path, *args: str) -> bool:
    proc = _run_git(cwd, *args, check=False)
    return proc.returncode == 0


def managed_kernel_path(a2a_root: Path) -> Path:
    return a2a_root / ".a2a" / MANAGED_KERNEL_DIRNAME


def ensure_kernel_repo(kernel_path: Path, clone_url: str = LINUX_NEXT_URL) -> None:
    """Clone the kernel repo if it doesn't exist."""
    if (kernel_path / ".git").exists() or (kernel_path / "HEAD").exists():
        return
    kernel_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth=1", "--no-single-branch", clone_url, str(kernel_path)],
        text=True,
        capture_output=True,
        check=True,
    )


def fetch_latest(kernel_path: Path) -> str:
    """Fetch latest tags and return the most recent next-YYYYMMDD tag."""
    _run_git(kernel_path, "fetch", "origin", "--tags", "--depth=1")

    proc = _run_git(kernel_path, "tag", "-l", "next-*", "--sort=-creatordate")
    tags = [t.strip() for t in proc.stdout.splitlines() if t.strip()]
    if not tags:
        raise RuntimeError("No next-* tags found after fetch")
    return tags[0]


def checkout_tag(kernel_path: Path, tag: str) -> None:
    """Checkout a tag in detached HEAD state, discarding local changes."""
    _run_git(kernel_path, "checkout", "--force", "--detach", tag)
    _run_git(kernel_path, "clean", "-fdx", "--quiet")


def patches_already_applied(kernel_path: Path, patch_files: list[Path]) -> bool:
    """Check if patches are already applied by attempting reverse apply."""
    for patch in patch_files:
        proc = _run_git(
            kernel_path, "apply", "--reverse", "--check", str(patch), check=False
        )
        if proc.returncode != 0:
            return False
    return True


def apply_patches(kernel_path: Path, patch_files: list[Path]) -> dict[str, Any]:
    """Apply patch files with git am. Returns result dict."""
    result: dict[str, Any] = {
        "ok": True,
        "applied_count": 0,
        "total_patches": len(patch_files),
        "conflict_detail": "",
        "already_applied_reset": False,
    }

    if not patch_files:
        result["ok"] = False
        result["conflict_detail"] = "No patch files to apply"
        return result

    # Check if already applied
    if patches_already_applied(kernel_path, patch_files):
        # Get current HEAD ref for reset
        proc = _run_git(kernel_path, "rev-parse", "HEAD")
        base_ref = proc.stdout.strip()
        # Reset to base and re-apply
        _run_git(kernel_path, "reset", "--hard", base_ref)
        result["already_applied_reset"] = True

    # Apply with git am
    am_proc = _run_git(
        kernel_path,
        "am",
        "--keep-cr",
        "--whitespace=nowarn",
        *[str(p) for p in patch_files],
        check=False,
    )

    if am_proc.returncode == 0:
        result["applied_count"] = len(patch_files)
        return result

    # Apply failed — collect details
    result["ok"] = False
    detail_lines = (am_proc.stderr or am_proc.stdout or "").splitlines()
    result["conflict_detail"] = "\n".join(line.strip() for line in detail_lines[:10] if line.strip())

    # Abort the failed am
    _run_git(kernel_path, "am", "--abort", check=False)

    return result


def prepare_kernel_with_patches(
    a2a_root: Path,
    patch_files: list[Path],
    kernel_path_override: str | None = None,
) -> dict[str, Any]:
    """
    Main entry point: ensure kernel is fresh, apply patches, return status.

    Args:
        a2a_root: Root of the A2A CLI project
        patch_files: List of patch file paths (from lore fetch)
        kernel_path_override: User-specified kernel path (skips clone/fetch)

    Returns:
        Dict with keys: ok, kernel_path, tag, applied_count, conflict_detail, etc.
    """
    report: dict[str, Any] = {
        "ok": False,
        "kernel_path": "",
        "tag": "",
        "applied_count": 0,
        "total_patches": len(patch_files),
        "conflict_detail": "",
        "already_applied_reset": False,
        "fetch_skipped": False,
    }

    # Resolve kernel path
    if kernel_path_override:
        kpath = Path(kernel_path_override).expanduser().resolve()
        if not kpath.exists():
            report["conflict_detail"] = f"Kernel path not found: {kpath}"
            return report
        report["fetch_skipped"] = True
    else:
        kpath = managed_kernel_path(a2a_root)
        # Clone if needed
        try:
            ensure_kernel_repo(kpath)
        except subprocess.CalledProcessError as exc:
            report["conflict_detail"] = f"Failed to clone kernel: {exc.stderr or exc.stdout}"
            return report

        # Fetch latest
        try:
            tag = fetch_latest(kpath)
        except (subprocess.CalledProcessError, RuntimeError) as exc:
            report["conflict_detail"] = f"Failed to fetch latest kernel: {exc}"
            return report

        report["tag"] = tag

        # Checkout
        try:
            checkout_tag(kpath, tag)
        except subprocess.CalledProcessError as exc:
            report["conflict_detail"] = f"Failed to checkout {tag}: {exc.stderr}"
            return report

    report["kernel_path"] = str(kpath)

    if not patch_files:
        report["ok"] = True
        return report

    # Apply patches
    apply_result = apply_patches(kpath, patch_files)
    report.update(apply_result)

    return report
