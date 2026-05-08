from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .conflict_resolver import ConflictError, ConflictResolver


_REV_RE = re.compile(r"^(?P<prefix>.*?)(?P<sep>[_-])v(?P<num>\d+)$", re.IGNORECASE)
_SERIES_LINK_RE = re.compile(r"^(?:Link:|v\d+:)", re.IGNORECASE)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def detect_version_number(path: Path) -> int:
    name = path.name
    m = _REV_RE.match(name)
    if m:
        return int(m.group("num"))
    return 1


def next_version_path(path: Path, *, auto_increment: bool = True) -> tuple[Path, int, int]:
    base_version = detect_version_number(path)
    candidate = base_version + 1
    while True:
        m = _REV_RE.match(path.name)
        if m:
            next_name = f"{m.group('prefix')}{m.group('sep')}v{candidate}"
        else:
            next_name = f"{path.name}_v{candidate}"
        out = path.parent / next_name
        if not out.exists():
            return out, base_version, candidate
        if not auto_increment:
            raise RuntimeError(f"Version directory already exists: {out}")
        candidate += 1


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _normalize_status(value: str) -> str:
    raw = value.strip().lower()
    if raw == "lgtm":
        return "lgtm"
    return raw


def _find_git_root(path: Path) -> Path:
    cur = path.resolve()
    if cur.is_file():
        cur = cur.parent
    for candidate in [cur, *cur.parents]:
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError(f"No git repository found for path: {path}")


def _is_cover_patch(path: Path) -> bool:
    return path.name.startswith("0000")


def _read_series_file(series_path: Path) -> list[Path]:
    rows: list[Path] = []
    try:
        lines = series_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return rows
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patch = (series_path.parent / line).resolve()
        if patch.is_file() and patch.suffix == ".patch":
            rows.append(patch)
    return rows


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path.resolve())
    return deduped


def _collect_patch_series(watch_path: Path) -> list[Path]:
    if watch_path.is_file():
        if watch_path.suffix != ".patch":
            raise RuntimeError(f"watch_path file is not a patch: {watch_path}")
        if _is_cover_patch(watch_path):
            raise RuntimeError(f"watch_path points to a cover-letter patch only: {watch_path}")
        return [watch_path.resolve()]
    if not watch_path.is_dir():
        raise RuntimeError(f"watch_path not found: {watch_path}")

    # Prefer explicit series ordering when present (supports nested lore layouts).
    series_files = sorted(watch_path.rglob("series"))
    if series_files:
        ordered: list[Path] = []
        for series_file in series_files:
            ordered.extend(_read_series_file(series_file))
        patches = [p for p in _dedupe_paths(ordered) if not _is_cover_patch(p)]
        if patches:
            return patches

    # Fallback: recurse for all patches in lexical order.
    patches = sorted(watch_path.rglob("*.patch"))
    patches = [p.resolve() for p in patches if not _is_cover_patch(p)]
    if not patches:
        raise RuntimeError(f"No non-cover patch files found in watch_path: {watch_path}")
    return patches


def _run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"git {' '.join(args)} failed")
    return proc


def _ensure_clean_tree(repo: Path) -> None:
    status = _run_git(repo, "status", "--porcelain", check=True).stdout.strip()
    if status:
        raise RuntimeError("Source tree has uncommitted changes; aborting respin.")


def _load_resolved_findings(report_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(report_dir.glob("round-*-findings.json")):
        payload = _load_json(path)
        findings = payload.get("findings", [])
        if not isinstance(findings, list):
            continue
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            if str(finding.get("status", "")).lower() == "closed":
                rows.append(finding)
    return rows


def _find_cover_letter(path: Path) -> Path | None:
    if not path.is_dir():
        return None

    for candidate in sorted(path.rglob("0000*.patch")):
        return candidate
    for candidate in sorted(path.rglob("*.cover")):
        return candidate
    return None


def _collect_previous_links(previous_cover: Path | None) -> list[str]:
    links: list[str] = []
    if previous_cover is None or not previous_cover.exists():
        return links
    for line in previous_cover.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip()
        if _SERIES_LINK_RE.match(text):
            links.append(text)
    return links


def _clean_changelog_row(raw: str) -> str:
    line = _MARKDOWN_LINK_RE.sub(r"\1", str(raw or "")).strip()
    line = _LIST_PREFIX_RE.sub("", line)
    line = re.sub(r"\s+", " ", line).strip()
    if line.endswith((".", ":")):
        line = line[:-1].strip()
    return line


def _resolved_finding_changelog_rows(resolved_findings: list[dict], limit: int = 8) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    by_source: dict[str, str] = {}
    for finding in resolved_findings:
        title = _clean_changelog_row(str(finding.get("title") or finding.get("description") or ""))
        if not title:
            continue
        loc = str(finding.get("location") or "").strip()
        row = f"{title} ({loc})" if loc else title
        source = str(finding.get("source_comment_id") or finding.get("id") or "").strip()
        if source:
            by_source[source] = row
            continue
        key = row.lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
        if len(rows) >= limit:
            break
    merged = rows + list(by_source.values())
    if not merged:
        return []
    deduped: list[str] = []
    merged_seen: set[str] = set()
    for row in merged:
        key = row.lower()
        if key in merged_seen:
            continue
        merged_seen.add(key)
        deduped.append(row)
        if len(deduped) >= limit:
            break
    return deduped


def _latest_builder_change_rows(report_dir: Path, limit: int = 8) -> list[str]:
    files = sorted(report_dir.glob("round-*-builder.md"))
    if not files:
        return []
    latest = files[-1]
    try:
        lines = latest.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    rows: list[str] = []
    seen: set[str] = set()
    in_changes = False
    for line in lines:
        heading = line.strip().lower()
        if heading == "## changes":
            in_changes = True
            continue
        if in_changes and heading.startswith("## "):
            break
        if not in_changes:
            continue
        if not _LIST_PREFIX_RE.match(line):
            continue
        clean = _clean_changelog_row(line)
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append(clean)
        if len(rows) >= limit:
            break
    return rows


def _build_cover_changelog_lines(report_dir: Path, resolved_findings: list[dict], limit: int = 8) -> list[str]:
    rows = _latest_builder_change_rows(report_dir, limit=limit)
    if rows:
        return rows[:limit]
    rows = _resolved_finding_changelog_rows(resolved_findings, limit=limit)
    if rows:
        return rows[:limit]
    return [
        "Technical delta summary unavailable from session artifacts; update this section with manual vN changes before posting"
    ]


def _generate_cover_letter_template(
    out_dir: Path,
    next_version: int,
    changelog_lines: list[str],
    previous_cover: Path | None,
) -> Path:
    cover = _find_cover_letter(out_dir)
    if cover is None:
        cover = out_dir / "0000-cover-letter.patch"

    old_links = _collect_previous_links(previous_cover)
    prev_version = max(1, next_version - 1)

    lines = [
        f"Changes since v{prev_version}:",
    ]
    if changelog_lines:
        for row in changelog_lines:
            clean = _clean_changelog_row(row)
            if not clean:
                continue
            lines.append(f"- {clean}")
    else:
        lines.append("- Technical delta summary unavailable from session artifacts; add manual vN changelog before posting.")

    if old_links:
        lines.append("")
        lines.extend(old_links)

    content = "\n".join(lines) + "\n"
    if cover.exists():
        existing = cover.read_text(encoding="utf-8", errors="replace")
        if "Changes in v" not in existing and "Changes since v" not in existing:
            cover.write_text(existing + "\n" + content, encoding="utf-8")
        else:
            cover.write_text(existing, encoding="utf-8")
    else:
        cover.write_text(content, encoding="utf-8")
    return cover


def _write_series_manifest(out_dir: Path) -> Path | None:
    patches = sorted(p for p in out_dir.glob("*.patch") if p.is_file())
    if not patches:
        return None
    series = out_dir / "series"
    lines = [p.name for p in patches]
    series.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return series


def _detect_upstream_drift(repo: Path) -> dict:
    has_origin = _run_git(repo, "rev-parse", "--verify", "origin/main", check=False)
    if has_origin.returncode != 0:
        return {"detected": False, "commits_ahead": 0, "commits": []}
    count_proc = _run_git(repo, "rev-list", "--count", "HEAD..origin/main")
    ahead = int((count_proc.stdout or "0").strip() or 0)
    commits = []
    if ahead > 0:
        commits_proc = _run_git(repo, "log", "--oneline", "HEAD..origin/main", "-20")
        commits = [line.strip() for line in commits_proc.stdout.splitlines() if line.strip()]
    return {"detected": ahead > 0, "commits_ahead": ahead, "commits": commits}


@dataclass
class RespinResult:
    session_id: str
    status: str
    source_watch_path: str
    source_repo: str
    source_version: int
    next_version: int
    output_dir: str
    output_copy_dir: str
    temp_branch: str
    patch_count: int
    dry_run: bool
    notes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "source_watch_path": self.source_watch_path,
            "source_repo": self.source_repo,
            "source_version": self.source_version,
            "next_version": self.next_version,
            "output_dir": self.output_dir,
            "output_copy_dir": self.output_copy_dir,
            "temp_branch": self.temp_branch,
            "patch_count": self.patch_count,
            "dry_run": self.dry_run,
            "notes": list(self.notes),
            "generated_at": _utc_now(),
        }


def respin(
    root: Path,
    session_id: str,
    *,
    dry_run: bool = False,
    conflict_strategy: str | None = None,
    resume_id: str | None = None,
) -> dict[str, Any]:
    a2a_dir = root / ".a2a"
    session_path = a2a_dir / "sessions" / f"{session_id}.json"
    if not session_path.exists():
        raise RuntimeError(f"Session not found: {session_id}")
    session = _load_json(session_path)
    status = _normalize_status(str(session.get("status", "")))
    if status != "lgtm":
        raise RuntimeError("Respin blocked: session is not LGTM.")

    watch_path = Path(str(session.get("watch_path") or "")).resolve()
    if not watch_path.exists():
        raise RuntimeError(f"Session watch_path not found: {watch_path}")

    patches = _collect_patch_series(watch_path)
    repo = _find_git_root(watch_path)
    cfg_path = a2a_dir / "config.json"
    cfg = _load_json(cfg_path) if cfg_path.exists() else {}
    respin_cfg = cfg.get("respin", {}) if isinstance(cfg, dict) else {}
    auto_increment = bool(respin_cfg.get("auto_increment_version", True))
    keep_temp_branch = bool(respin_cfg.get("keep_temp_branch", False))
    strategy = conflict_strategy or str(respin_cfg.get("conflict_strategy", "abort"))

    source_dir = watch_path if watch_path.is_dir() else watch_path.parent
    out_dir, source_version, next_version = next_version_path(source_dir, auto_increment=auto_increment)
    output_copy_dir = a2a_dir / "patches" / session_id / f"v{next_version}"
    report_dir = a2a_dir / "reports" / session_id
    state_dir = a2a_dir / "respin_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    temp_branch = f"patchwise-respin-v{next_version}"
    notes: list[str] = []
    drift = _detect_upstream_drift(repo)
    if drift.get("detected"):
        drift_path = report_dir / "upstream_drift.json"
        _dump_json(drift_path, drift)
        notes.append(f"upstream drift detected: {drift.get('commits_ahead')} commits")

    if dry_run:
        result = RespinResult(
            session_id=session_id,
            status="dry_run",
            source_watch_path=str(watch_path),
            source_repo=str(repo),
            source_version=source_version,
            next_version=next_version,
            output_dir=str(out_dir),
            output_copy_dir=str(output_copy_dir),
            temp_branch=temp_branch,
            patch_count=len(patches),
            dry_run=True,
            notes=notes + ["dry run only; no writes"],
        )
        return result.as_dict()

    _ensure_clean_tree(repo)
    original_ref = _run_git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "HEAD"
    resolver = ConflictResolver(repo_root=repo, report_dir=report_dir, strategy=strategy)

    # Ensure clean temp branch lifecycle.
    existing = _run_git(repo, "branch", "--list", temp_branch).stdout.strip()
    if existing:
        _run_git(repo, "branch", "-D", temp_branch)
    _run_git(repo, "checkout", "-b", temp_branch)

    try:
        for patch in patches:
            am = _run_git(repo, "am", str(patch), check=False)
            if am.returncode == 0:
                continue
            conflict_files = _run_git(repo, "diff", "--name-only", "--diff-filter=U", check=False)
            conflict_info = {
                "stderr": am.stderr.strip(),
                "stdout": am.stdout.strip(),
                "conflicted_files": [line.strip() for line in conflict_files.stdout.splitlines() if line.strip()],
            }
            resolver.resolve(patch, conflict_info)

        if drift.get("detected"):
            rebase = _run_git(repo, "rebase", "origin/main", check=False)
            if rebase.returncode != 0:
                conflict_files = _run_git(repo, "diff", "--name-only", "--diff-filter=U", check=False)
                info = {
                    "stage": "rebase",
                    "stderr": rebase.stderr.strip(),
                    "stdout": rebase.stdout.strip(),
                    "conflicted_files": [line.strip() for line in conflict_files.stdout.splitlines() if line.strip()],
                }
                try:
                    resolver.resolve(Path("rebase"), info)
                except ConflictError:
                    state_path = state_dir / f"{resume_id or session_id}.json"
                    _dump_json(
                        state_path,
                        {
                            "session_id": session_id,
                            "repo": str(repo),
                            "temp_branch": temp_branch,
                            "message": (
                                f"Upstream has moved {drift.get('commits_ahead')} commits. "
                                "Manual rebase needed."
                            ),
                            "conflicting_files": info.get("conflicted_files", []),
                            "resume_command": f"a2a respin --resume {resume_id or session_id}",
                            "saved_at": _utc_now(),
                        },
                    )
                    raise

        out_dir.mkdir(parents=True, exist_ok=True)
        base_ref = "origin/main"
        if _run_git(repo, "rev-parse", "--verify", base_ref, check=False).returncode != 0:
            base_ref = f"HEAD~{len(patches)}"
        _run_git(
            repo,
            "format-patch",
            "--cover-letter",
            base_ref,
            "--output-directory",
            str(out_dir),
        )
        _write_series_manifest(out_dir)

        previous_cover = _find_cover_letter(source_dir)
        resolved_findings = _load_resolved_findings(report_dir)
        changelog_lines = _build_cover_changelog_lines(report_dir, resolved_findings)
        _generate_cover_letter_template(out_dir, next_version, changelog_lines, previous_cover)

        if output_copy_dir.exists():
            shutil.rmtree(output_copy_dir)
        shutil.copytree(out_dir, output_copy_dir)

        result = RespinResult(
            session_id=session_id,
            status="ok",
            source_watch_path=str(watch_path),
            source_repo=str(repo),
            source_version=source_version,
            next_version=next_version,
            output_dir=str(out_dir),
            output_copy_dir=str(output_copy_dir),
            temp_branch=temp_branch,
            patch_count=len(patches),
            dry_run=False,
            notes=notes,
        )
        return result.as_dict()
    finally:
        _run_git(repo, "checkout", original_ref, check=False)
        if not keep_temp_branch:
            _run_git(repo, "branch", "-D", temp_branch, check=False)
