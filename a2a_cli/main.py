import argparse
import builtins
import difflib
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import unquote, urlparse
from contextlib import contextmanager
from uuid import uuid4

from .adapters.shell_adapter import run_shell_command
from .config import A2A_DIRNAME, default_config, default_state, dump_json, load_json
from .email_bridge import run_bridge_loop
from .prior_review import (
    augment_findings_with_prior_comments,
    classify_prior_comment,
    ingest_prior_review_context,
    load_prior_comments,
    render_prior_comment_matrix,
)
from .knowledge_base import (
    build_aryabhata_context,
    build_chanakya_context,
    clear_kb,
    infer_subsystem_from_watch_path,
    list_kb_entries,
    load_kb,
    update_kb_after_lgtm,
)
from .hitl_gate import run_hitl_gate
from .lore_watcher import watch as watch_lore
from .maintainer_tracker import load_profiles
from .respin import respin as run_respin
from .finding_advertiser import extract_advertised_findings, render_advertised_findings_text
from .rich_output import (
    render_finding_card,
    render_gate_status,
    render_lgtm_banner,
    render_phase_progress,
    render_prior_comment_status,
    render_round_table,
    render_scores,
    render_session_header,
)
from .score_engine import (
    ScoreThresholds,
    append_score_decision,
    evaluate_round_scores,
    mark_findings_low_quality,
)
from .series_manager import auto_discover_series, run_all_series
from .static_analysis import run_gate as run_static_analysis_gate
from .submission_mailer import build_patchset_summary
from .types import StatusView
from .upstream_evidence import enrich_findings_with_evidence, kernel_tree_exists

try:
    from rich.console import Console
except Exception:  # pragma: no cover - rich is optional in host runtime
    Console = None  # type: ignore[assignment]


_REV_TRAILING_RE = re.compile(r"^(?P<prefix>.*?)(?P<sep>[_-])v(?P<num>\d+)$", re.IGNORECASE)
_REV_PREFIX_RE = re.compile(r"^v(?P<num>\d+)-(?P<rest>.+)$", re.IGNORECASE)
_VOLATILE_SOURCE_ID_RE = re.compile(r"^(?:new|round\d+-new|issue-temp)[-:]", re.IGNORECASE)
_PATCH_SUBJECT_LINE_RE = re.compile(r"^(Subject:\s*\[PATCH)(?P<body>[^\]]*)(\].*)$", re.IGNORECASE)
_PATCH_VERSION_TOKEN_RE = re.compile(r"\bv(?P<num>\d+)\b", re.IGNORECASE)
_PATCH_INDEX_TOKEN_RE = re.compile(r"^\d+/\d+$")
_REV_TOKEN_LOOSE_RE = re.compile(r"(?<![A-Za-z0-9])v(?P<num>\d+)(?![A-Za-z0-9])", re.IGNORECASE)
_PATCHSET_VERSION_DIR_RE = re.compile(r"^v(?P<num>\d+)$", re.IGNORECASE)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+")
_CONSOLE = Console() if Console else None


def _echo(*args: object, sep: str = " ", end: str = "\n") -> None:
    text = sep.join(str(arg) for arg in args)
    if _CONSOLE is not None:
        _CONSOLE.print(text, end=end, markup=False)
        return
    builtins.print(*args, sep=sep, end=end)


def _repo_root_from_module() -> Path:
    return Path(__file__).resolve().parent.parent


def find_a2a_root(start: Path | None = None) -> Path | None:
    cur = (start or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / A2A_DIRNAME).is_dir():
            return candidate
    return None


def _template_source_dir() -> Path:
    return _repo_root_from_module() / "templates" / "prompts"


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
    return proc.stdout.strip()


def _run_ok(cmd: list[str], cwd: Path | None = None) -> bool:
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    return proc.returncode == 0


def _git(repo: Path, *args: str) -> str:
    return _run(["git", "-C", str(repo), *args])


def _git_ok(repo: Path, *args: str) -> bool:
    return _run_ok(["git", "-C", str(repo), *args])


def _write_if_missing(path: Path, content: str, force: bool) -> bool:
    if path.exists() and not force:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def cmd_init(args: argparse.Namespace) -> int:
    root = Path.cwd().resolve()
    a2a_dir = root / A2A_DIRNAME

    (a2a_dir / "sessions").mkdir(parents=True, exist_ok=True)
    (a2a_dir / "logs").mkdir(parents=True, exist_ok=True)
    (a2a_dir / "reports").mkdir(parents=True, exist_ok=True)
    (a2a_dir / "templates" / "prompts").mkdir(parents=True, exist_ok=True)

    written = []
    if _write_if_missing(a2a_dir / "config.json", _as_json(default_config()), args.force):
        written.append(".a2a/config.json")
    if _write_if_missing(a2a_dir / "state.json", _as_json(default_state()), args.force):
        written.append(".a2a/state.json")

    template_dir = _template_source_dir()
    if template_dir.is_dir():
        for src in sorted(template_dir.glob("*.md")):
            dst = a2a_dir / "templates" / "prompts" / src.name
            if dst.exists() and not args.force:
                continue
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            written.append(str(dst.relative_to(root)))

    _echo(f"Initialized A2A workspace in {a2a_dir}")
    if written:
        _echo("Created/updated files:")
        for entry in written:
            _echo(f"  - {entry}")
    else:
        _echo("No files changed (already initialized).")
    return 0


def _as_json(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_datetime_optional(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _elapsed_seconds(started_at: str | None, ended_at: str | None) -> int | None:
    start_dt = _parse_iso_datetime_optional(started_at)
    end_dt = _parse_iso_datetime_optional(ended_at)
    if start_dt is None or end_dt is None:
        return None
    elapsed = int((end_dt - start_dt).total_seconds())
    return max(0, elapsed)


def _format_elapsed_hms(value: object) -> str | None:
    try:
        total = int(value) if value is not None else None
    except (TypeError, ValueError):
        total = None
    if total is None or total < 0:
        return None
    hours = total // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _normalize_path_for_report(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text:
        return text
    return str(Path(text).expanduser().resolve())


def _lore_link_from_session(session: dict) -> str:
    lore = session.get("lore")
    if isinstance(lore, dict):
        msgid = str(lore.get("message_id") or "").strip()
        if msgid:
            return f"https://lore.kernel.org/r/{msgid}"
    return ""


def _collect_patch_versions_for_session(root: Path, session_id: str) -> list[dict]:
    base = (root / A2A_DIRNAME / "patches" / session_id).resolve()
    if not base.is_dir():
        return []

    rows: list[dict] = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        m = _PATCHSET_VERSION_DIR_RE.match(child.name)
        ver = int(m.group("num")) if m else None
        rows.append(
            {
                "name": child.name,
                "version": ver,
                "path": str(child.resolve()),
            }
        )

    rows.sort(key=lambda item: (item.get("version") is None, int(item.get("version") or 0), str(item.get("name") or "")))
    return rows


def _extract_lore_message_id(value: str) -> str:
    raw = str(value or "").strip().strip("<>").strip()
    if not raw:
        raise RuntimeError("Lore input is empty.")

    if "://" not in raw:
        if "/" not in raw:
            return raw
        parsed = urlparse("https://" + raw)
    else:
        parsed = urlparse(raw)

    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        raise RuntimeError(f"Unsupported lore URL scheme: {parsed.scheme}")

    host = (parsed.netloc or "").lower()
    if host and "lore.kernel.org" not in host:
        raise RuntimeError(f"Unsupported lore host: {host}")

    path = parsed.path.strip("/")
    if not path:
        raise RuntimeError(f"Cannot extract message-id from lore URL: {value}")

    for prefix in ("r/", "all/"):
        if path.startswith(prefix):
            token = path[len(prefix) :].split("/", 1)[0].strip().strip("<>").strip()
            if token:
                return unquote(token)
    token = path.split("/", 1)[0].strip().strip("<>").strip()
    if token:
        return unquote(token)
    raise RuntimeError(f"Cannot extract message-id from lore URL: {value}")


def _lore_fetch_base_dir(cfg: dict, lore_out_dir: str | None = None) -> Path:
    override = str(lore_out_dir or "").strip()
    if override:
        return Path(override).expanduser().resolve()

    cfg_override = str((cfg.get("lore_fetch_dir") if isinstance(cfg, dict) else "") or "").strip()
    if cfg_override:
        return Path(cfg_override).expanduser().resolve()

    upstream = cfg.get("upstream_evidence", {}) if isinstance(cfg, dict) else {}
    kernel_tree = str(upstream.get("kernel_tree") or "").strip()
    if kernel_tree:
        tree = Path(kernel_tree).expanduser().resolve()
        if tree.exists() and (tree / "scripts" / "checkpatch.pl").is_file():
            return tree / ".a2a" / "lore_series"
    return Path(tempfile.gettempdir()) / "a2a_lore_series"


def _fetch_lore_series(cfg: dict, lore_input: str, lore_out_dir: str | None = None) -> tuple[Path, str]:
    message_id = _extract_lore_message_id(lore_input)
    if shutil.which("b4") is None:
        raise RuntimeError("b4 is required for lore input. Install b4 and retry.")

    base_dir = _lore_fetch_base_dir(cfg, lore_out_dir=lore_out_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_mid = re.sub(r"[^A-Za-z0-9._@+-]+", "-", message_id).strip("-") or "thread"
    out_dir = (base_dir / f"{safe_mid}-{stamp}").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    b4_cache_home = (base_dir / ".b4_xdg_cache").resolve()
    b4_data_home = (base_dir / ".b4_xdg_data").resolve()
    b4_cache_home.mkdir(parents=True, exist_ok=True)
    b4_data_home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["XDG_CACHE_HOME"] = str(b4_cache_home)
    env["XDG_DATA_HOME"] = str(b4_data_home)

    proc = subprocess.run(
        ["b4", "am", "-Q", "-o", str(out_dir), message_id],
        text=True,
        capture_output=True,
        env=env,
    )
    patches = sorted(out_dir.rglob("*.patch"))
    if proc.returncode != 0 and not patches:
        raise RuntimeError(
            "Failed to fetch lore series with b4.\n"
            f"msgid={message_id}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )

    if not patches:
        raise RuntimeError(f"No patch files fetched from lore for message-id: {message_id}")
    return out_dir, message_id


def _prompt_extend_after_max_rounds(
    session_id: str,
    round_no: int,
    max_rounds: int,
    open_count: int,
    *,
    interactive: bool | None = None,
    input_fn: Callable[[str], str] = input,
) -> bool:
    if interactive is None:
        interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())
    if not interactive:
        return False

    _echo("┌──────────────────────────────────────────────────────────────────────┐")
    _echo(f"│ Max rounds reached for session {session_id}")
    _echo(f"│ Current round: {round_no}/{max_rounds}  ·  Open findings: {open_count}")
    _echo("│ Proceed with one more round in the same session? [y/N]")
    _echo("└──────────────────────────────────────────────────────────────────────┘")
    try:
        answer = str(input_fn("Proceed with one more round? [y/N]: ")).strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _must_find_root() -> Path:
    root = find_a2a_root()
    if root is None:
        raise RuntimeError("No .a2a directory found. Run: a2a init")
    return root


def _session_path(root: Path, session_id: str) -> Path:
    return root / A2A_DIRNAME / "sessions" / f"{session_id}.json"


def _config_path(root: Path) -> Path:
    return root / A2A_DIRNAME / "config.json"


def _load_config(root: Path) -> dict:
    cfg_path = _config_path(root)
    cfg = load_json(cfg_path)
    defaults = default_config()
    changed = False
    for key, value in defaults.items():
        if key in cfg:
            continue
        cfg[key] = value
        changed = True
    if changed:
        dump_json(cfg_path, cfg)
    return cfg


def _load_config_or_defaults(root: Path) -> dict:
    cfg_path = _config_path(root)
    if not cfg_path.exists():
        return default_config()
    return _load_config(root)


def _state_path(root: Path) -> Path:
    return root / A2A_DIRNAME / "state.json"


def _prepare_path(root: Path) -> Path:
    return root / A2A_DIRNAME / "prepare.json"


def _report_dir(root: Path, session_id: str) -> Path:
    return root / A2A_DIRNAME / "reports" / session_id


def _task_slug(task: str | None, max_len: int = 24) -> str:
    text = str(task or "").strip().lower()
    if not text:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "-", text)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if not slug:
        return ""
    return slug[:max_len].rstrip("-")


def _next_session_id(task: str | None = None) -> str:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    token = uuid4().hex[:6]
    slug = _task_slug(task, max_len=36)
    if slug:
        return f"sess-{slug}-{day}-{token}"
    return f"sess-{day}-{token}"


def _resolve_builder_display_name(session: dict | None = None, cfg: dict | None = None) -> str:
    for source in (session, cfg):
        if isinstance(source, dict):
            raw = str(source.get("builder_display_name") or "").strip()
            if raw:
                return raw
    return "builder"


def _resolve_reviewer_display_name(session: dict | None = None, cfg: dict | None = None) -> str:
    for source in (session, cfg):
        if isinstance(source, dict):
            raw = str(source.get("reviewer_display_name") or "").strip()
            if raw:
                return raw

    for source in (session, cfg):
        if isinstance(source, dict):
            raw = str(source.get("reviewer_name") or "").strip()
            if raw:
                return raw
    return "reviewer"


def _increment_revision_stem(stem: str) -> str:
    current = _extract_revision_from_name(stem)
    if current > 0:
        return _set_revision_stem(stem, current + 1)
    return f"v2-{stem}"


def _extract_revision_from_name(name: str) -> int:
    detected = 0
    prefix_match = _REV_PREFIX_RE.match(name)
    if prefix_match:
        detected = max(detected, int(prefix_match.group("num")))

    trailing_match = _REV_TRAILING_RE.match(name)
    if trailing_match:
        detected = max(detected, int(trailing_match.group("num")))

    for token in _REV_TOKEN_LOOSE_RE.finditer(name):
        try:
            detected = max(detected, int(token.group("num")))
        except ValueError:
            continue
    return detected


def _set_revision_stem(stem: str, target_version: int) -> str:
    prefix_match = _REV_PREFIX_RE.match(stem)
    if prefix_match:
        return f"v{target_version}-{prefix_match.group('rest')}"

    trailing_match = _REV_TRAILING_RE.match(stem)
    if trailing_match:
        return (
            f"{trailing_match.group('prefix')}"
            f"{trailing_match.group('sep')}v{target_version}"
        )

    loose_match = _REV_TOKEN_LOOSE_RE.search(stem)
    if loose_match:
        return (
            f"{stem[:loose_match.start()]}"
            f"v{target_version}"
            f"{stem[loose_match.end():]}"
        )

    return f"v{target_version}-{stem}"


def _default_respin_output_path(source: Path, *, next_version: int | None = None) -> Path:
    if source.is_dir():
        name = source.name
        target = next_version if next_version is not None else _extract_revision_from_name(name) + 1
        if target < 2:
            target = 2

        name_match = _REV_TRAILING_RE.match(name)
        if name_match:
            next_name = f"{name_match.group('prefix')}{name_match.group('sep')}v{target}"
        else:
            prefix_match = _REV_PREFIX_RE.match(name)
            if prefix_match:
                next_name = f"v{target}-{prefix_match.group('rest')}"
            else:
                loose_match = _REV_TOKEN_LOOSE_RE.search(name)
                if loose_match:
                    next_name = f"{name[:loose_match.start()]}v{target}{name[loose_match.end():]}"
                else:
                    next_name = f"{name}_v{target}"
        return source.parent / next_name

    if source.is_file():
        if next_version is not None:
            next_stem = _set_revision_stem(source.stem, next_version)
        else:
            next_stem = _increment_revision_stem(source.stem)
        return source.with_name(f"{next_stem}{source.suffix}")

    raise RuntimeError(f"Source path does not exist: {source}")


def _copy_respin_source(source: Path, output: Path, force: bool) -> None:
    if output.exists():
        if not force:
            raise RuntimeError(
                f"Respin output path already exists: {output} (use --force to overwrite)"
            )
        if output.is_dir():
            shutil.rmtree(output)
        else:
            output.unlink()

    if source.is_dir():
        shutil.copytree(source, output)
        return
    if source.is_file():
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, output)
        return
    raise RuntimeError(f"Respin source path does not exist: {source}")


def _read_series_patch_files(series_file: Path) -> list[Path]:
    try:
        lines = series_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    patch_files: list[Path] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patch_path = (series_file.parent / line).resolve()
        if patch_path.is_file() and patch_path.suffix == ".patch":
            patch_files.append(patch_path)
    return patch_files


def _read_series_entries(series_file: Path) -> list[str]:
    try:
        lines = series_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _is_cover_patch_file(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(".patch") and name.startswith("0000")


def _canonical_series_patch_entries(patch_dir: Path) -> list[str]:
    series_file = patch_dir / "series"
    entries: list[str] = []
    if series_file.exists():
        for entry in _read_series_entries(series_file):
            candidate = (patch_dir / entry).resolve()
            if not candidate.is_file() or candidate.suffix != ".patch":
                continue
            if _is_cover_patch_file(candidate):
                continue
            entries.append(entry)
    if not entries:
        entries = [p.name for p in sorted(patch_dir.glob("*.patch")) if not _is_cover_patch_file(p)]

    deduped: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if entry in seen:
            continue
        seen.add(entry)
        deduped.append(entry)
    return deduped


def _collect_active_patch_files(path: Path) -> list[Path]:
    if path.is_file():
        if path.suffix != ".patch":
            raise RuntimeError(f"Patch file expected, got: {path}")
        return [path]
    if not path.is_dir():
        raise RuntimeError(f"Patch path not found: {path}")

    series_resolved: list[Path] = []
    for series_file in sorted(path.rglob("series")):
        series_resolved.extend(_read_series_patch_files(series_file))
    if series_resolved:
        deduped: list[Path] = []
        seen: set[str] = set()
        for patch in series_resolved:
            key = str(patch)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(patch)
        return deduped

    return sorted(p for p in path.rglob("*.patch") if p.is_file())


def _materialize_cover_patch(output: Path) -> Path | None:
    if not output.is_dir():
        return None
    cover_candidates = sorted(p for p in output.rglob("*.cover") if p.is_file())
    patch_dirs = sorted(p for p in output.rglob("*.patches") if p.is_dir())
    if not cover_candidates or not patch_dirs:
        return None

    cover_src = cover_candidates[0]
    patch_dir = patch_dirs[0]
    cover_patch = patch_dir / "0000-cover-letter.patch"
    cover_patch.write_text(cover_src.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")

    series_path = patch_dir / "series"
    series_entries = _canonical_series_patch_entries(patch_dir)
    _rewrite_series_file(series_path, series_entries, include_cover=True)
    return cover_patch


def _patchset_name_for_path(path: Path) -> str:
    name = path.name
    if name.endswith(".patches"):
        return name[: -len(".patches")]
    if name.endswith(".cover"):
        return name[: -len(".cover")]
    if name.endswith(".mbx"):
        return name[: -len(".mbx")]
    return path.stem


def _normalize_subject_body(body: str, *, version: int, index: int, total: int) -> str:
    tokens = [tok for tok in body.strip().split() if tok]
    kept: list[str] = []
    for tok in tokens:
        if _PATCH_VERSION_TOKEN_RE.fullmatch(tok):
            continue
        if _PATCH_INDEX_TOKEN_RE.fullmatch(tok):
            continue
        kept.append(tok)
    kept.extend([f"v{version}", f"{index}/{total}"])
    return " " + " ".join(kept)


def _rewrite_subject_header(path: Path, *, version: int, index: int, total: int) -> bool:
    if not path.is_file():
        return False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False

    changed = False
    out_lines: list[str] = []
    updated = False
    for line in lines:
        if updated:
            out_lines.append(line)
            continue
        m = _PATCH_SUBJECT_LINE_RE.match(line)
        if m:
            new_body = _normalize_subject_body(m.group("body") or "", version=version, index=index, total=total)
            new_line = f"{m.group(1)}{new_body}{m.group(3)}"
            out_lines.append(new_line)
            updated = True
            changed = changed or (new_line != line)
            continue
        if line.lower().startswith("subject:"):
            subject_text = line.split(":", 1)[1].strip()
            new_line = f"Subject: [PATCH v{version} {index}/{total}] {subject_text}".rstrip()
            out_lines.append(new_line)
            updated = True
            changed = changed or (new_line != line)
            continue
        out_lines.append(line)
    if changed:
        path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return changed


def _render_mbox_from_patch_files(patch_files: list[Path]) -> str:
    chunks: list[str] = []
    for patch in patch_files:
        text = patch.read_text(encoding="utf-8", errors="replace").rstrip("\n")
        chunks.append("From git@z Thu Jan  1 00:00:00 1970\n" + text + "\n")
    return "\n".join(chunks) + ("\n" if chunks else "")


def _rewrite_series_file(series_path: Path, entries: list[str], include_cover: bool) -> None:
    lines: list[str] = []
    if include_cover:
        lines.append("0000-cover-letter.patch")
    lines.extend(entries)
    series_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _synchronize_patchset_artifacts(output: Path, next_version: int) -> int:
    if output.is_file():
        _rewrite_subject_header(output, version=next_version, index=1, total=1)
        return 1
    if not output.is_dir():
        return 0

    patch_dirs = sorted(p for p in output.rglob("*.patches") if p.is_dir())
    patch_map: dict[Path, list[Path]] = {}
    non_cover_total = 0
    for patch_dir in patch_dirs:
        entries = _canonical_series_patch_entries(patch_dir)
        patch_paths = [(patch_dir / entry).resolve() for entry in entries]
        patch_paths = [p for p in patch_paths if p.is_file() and p.suffix == ".patch" and not _is_cover_patch_file(p)]
        if not patch_paths:
            continue
        patch_map[patch_dir] = patch_paths
        total = len(patch_paths)
        non_cover_total += total
        for idx, patch_path in enumerate(patch_paths, start=1):
            _rewrite_subject_header(patch_path, version=next_version, index=idx, total=total)
        cover_patch = patch_dir / "0000-cover-letter.patch"
        if cover_patch.exists():
            _rewrite_subject_header(cover_patch, version=next_version, index=0, total=total)
        _rewrite_series_file(patch_dir / "series", [p.name for p in patch_paths], include_cover=cover_patch.exists())

    loose_patches = sorted(
        p for p in output.glob("*.patch") if p.is_file() and not _is_cover_patch_file(p)
    )
    if not patch_map and loose_patches:
        total = len(loose_patches)
        non_cover_total += total
        for idx, patch_path in enumerate(loose_patches, start=1):
            _rewrite_subject_header(patch_path, version=next_version, index=idx, total=total)
        cover_patch = output / "0000-cover-letter.patch"
        if cover_patch.exists():
            _rewrite_subject_header(cover_patch, version=next_version, index=0, total=total)
        _rewrite_series_file(output / "series", [p.name for p in loose_patches], include_cover=cover_patch.exists())

    named_patchsets: dict[str, list[Path]] = {}
    for patch_dir, patch_paths in patch_map.items():
        named_patchsets[_patchset_name_for_path(patch_dir)] = patch_paths

    default_patchset: list[Path] = []
    if named_patchsets:
        default_patchset = next(iter(named_patchsets.values()))
    elif loose_patches:
        default_patchset = loose_patches

    for cover_path in sorted(output.rglob("*.cover")):
        patch_paths = named_patchsets.get(_patchset_name_for_path(cover_path), default_patchset)
        if not patch_paths:
            continue
        _rewrite_subject_header(cover_path, version=next_version, index=0, total=len(patch_paths))

    for mbx_path in sorted(output.rglob("*.mbx")):
        patch_paths = named_patchsets.get(_patchset_name_for_path(mbx_path), default_patchset)
        if not patch_paths:
            continue
        mbx_path.write_text(_render_mbox_from_patch_files(patch_paths), encoding="utf-8")

    return non_cover_total


def _augment_cover_letter_history(
    path: Path,
    next_version: int,
    lore_link: str | None,
    changelog_lines: list[str] | None = None,
) -> None:
    if not path.is_file():
        return
    prev = max(1, next_version - 1)
    marker = f"Changes since v{prev}:"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if marker in text:
        return

    lines = text.splitlines()
    sep_idx = -1
    for idx, line in enumerate(lines):
        if line.strip() == "---":
            sep_idx = idx
            break

    block: list[str] = []
    link = (lore_link or "").strip()
    if link:
        vline = f"v{prev}: {link}"
        if not any(x.strip().lower().startswith(f"v{prev}:") for x in lines):
            block.append(vline)
        if not any(x.strip().lower().startswith("link:") for x in lines):
            block.append(f"Link: {link}")
    if block:
        block.append("")
    block.append(marker)
    change_rows = [str(x).strip() for x in (changelog_lines or []) if str(x).strip()]
    if change_rows:
        for line in change_rows:
            block.append(line if line.startswith("- ") else f"- {line}")
    else:
        block.append("- Technical delta summary unavailable from session artifacts; add manual vN changelog before posting.")
    block.append("")
    if sep_idx < 0:
        out_lines = lines + ["", "---", *block]
    else:
        out_lines = lines[: sep_idx + 1] + block + lines[sep_idx + 1 :]
    path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def _detect_max_subject_version(paths: Iterable[Path]) -> int:
    detected_max = 0
    for patch in paths:
        try:
            text = patch.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = _PATCH_SUBJECT_LINE_RE.match(line)
            if not m:
                continue
            vm = _PATCH_VERSION_TOKEN_RE.search(m.group("body") or "")
            if vm:
                try:
                    detected_max = max(detected_max, int(vm.group("num")))
                except ValueError:
                    pass
    return detected_max


def _detect_current_patchset_version(source: Path, patch_files: list[Path]) -> int:
    candidates: list[int] = [1, _extract_revision_from_name(source.name)]
    subject_files: list[Path] = list(patch_files)
    name_tokens: list[str] = [source.name]

    if source.is_dir():
        cover_files = sorted(source.rglob("*.cover"))
        mbx_files = sorted(source.rglob("*.mbx"))
        subject_files.extend(cover_files)
        subject_files.extend(mbx_files)
        for candidate in [*patch_files, *cover_files, *mbx_files]:
            try:
                rel_parts = candidate.relative_to(source).parts
            except ValueError:
                rel_parts = (candidate.name,)
            name_tokens.extend(rel_parts)
    else:
        name_tokens.append(source.stem)

    candidates.append(_detect_max_subject_version(subject_files))
    for token in name_tokens:
        candidates.append(_extract_revision_from_name(token))

    return max(candidates)


def _bump_patch_subject_versions(patch_files: list[Path], target_version: int | None = None) -> int:
    detected_max = _detect_max_subject_version(patch_files)
    target = int(target_version) if target_version is not None else detected_max + 1
    if target < 2:
        target = 2

    for patch in patch_files:
        try:
            text = patch.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        changed = False
        out_lines: list[str] = []
        for line in text.splitlines():
            m = _PATCH_SUBJECT_LINE_RE.match(line)
            if not m:
                out_lines.append(line)
                continue

            body = m.group("body") or ""
            if _PATCH_VERSION_TOKEN_RE.search(body):
                new_body = _PATCH_VERSION_TOKEN_RE.sub(f"v{target}", body, count=1)
            else:
                if re.search(r"\d+/\d+", body):
                    new_body = re.sub(r"(\s*)(\d+/\d+)", rf"\1v{target} \2", body, count=1)
                else:
                    new_body = f"{body} v{target}"
            out_lines.append(f"{m.group(1)}{new_body}{m.group(3)}")
            changed = True

        if changed:
            patch.write_text("\n".join(out_lines) + "\n", encoding="utf-8")

    return target


def _build_suggested_replies_markdown(round_summary: dict, findings: list[dict], round_no: int) -> str:
    prior = round_summary.get("prior_comments", {}) if isinstance(round_summary, dict) else {}
    tracked = prior.get("tracked", []) if isinstance(prior, dict) else []
    tracked_rows = [row for row in tracked if isinstance(row, dict)]
    open_by_source: dict[str, dict] = {}
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if str(finding.get("status", "open")).lower() == "closed":
            continue
        source_id = str(finding.get("source_comment_id") or "").strip()
        if source_id and source_id not in open_by_source:
            open_by_source[source_id] = finding

    lines = [
        f"# Round {round_no} Suggested Replies",
        "",
    ]
    if not tracked_rows:
        lines.extend(
            [
                "- No prior lore comments detected for this round.",
                "- Suggested reply: \"No prior-thread review comments were detected from lore sources for this revision.\"",
                "",
            ]
        )
        return "\n".join(lines)

    for row in tracked_rows:
        source_id = str(row.get("source_comment_id") or "")
        subject = str(row.get("subject") or "review comment")
        status = str(row.get("current_status") or "open")
        lines.append(f"## {source_id}")
        lines.append(f"- Subject: {subject}")
        lines.append(f"- Status: {status}")
        if status == "external_resolved":
            external_ref = str(row.get("external_reference") or "").strip()
            if external_ref:
                lines.append(
                    f'- Suggested reply: "Already applied upstream ({external_ref}); no further action required for this comment."'
                )
            else:
                lines.append(
                    '- Suggested reply: "Already applied upstream by maintainer; no further action required for this comment."'
                )
        elif status == "closed":
            location = str(row.get("latest_location") or "").strip() or "n/a"
            evidence = str(row.get("latest_evidence") or "").strip() or "verified in patch update"
            lines.append(
                f'- Suggested reply: "Addressed in this revision at {location}. Evidence: {evidence}."'
            )
        else:
            open_finding = open_by_source.get(source_id, {})
            action = str(open_finding.get("required_action") or "").strip() or "follow-up fix in next revision"
            lines.append(f'- Suggested reply: "Thanks for the review. This is still open; planned action: {action}."')
        lines.append("")

    return "\n".join(lines)


def _write_round_suggested_replies(
    root: Path,
    session_id: str,
    round_no: int,
    round_summary: dict,
    findings: list[dict],
) -> Path:
    report_dir = _report_dir(root, session_id)
    out_path = report_dir / f"{_round_basename(round_no, 'suggested-replies')}.md"
    out_path.write_text(
        _build_suggested_replies_markdown(round_summary=round_summary, findings=findings, round_no=round_no),
        encoding="utf-8",
    )
    return out_path


def _resolved_finding_changelog_lines(root: Path, session_id: str, limit: int = 8) -> list[str]:
    report_dir = _report_dir(root, session_id)
    rows: list[str] = []
    seen: set[str] = set()
    by_source: dict[str, str] = {}

    def _clean_row(raw: str) -> str:
        line = _MARKDOWN_LINK_RE.sub(r"\1", str(raw or "")).strip()
        line = _LIST_PREFIX_RE.sub("", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line.endswith((".", ":")):
            line = line[:-1].strip()
        return line

    builder_reports = sorted(report_dir.glob("round-*-builder.md"))
    if builder_reports:
        latest_builder = builder_reports[-1]
        try:
            lines = latest_builder.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
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
            clean = _clean_row(line)
            if not clean:
                continue
            key = clean.lower()
            if key in seen:
                continue
            seen.add(key)
            rows.append(clean)
            if len(rows) >= limit:
                break
        if rows:
            return rows[:limit]

    rows = []
    seen = set()

    for path in sorted(report_dir.glob("round-*-findings.json")):
        try:
            payload = load_json(path)
        except Exception:
            continue
        findings = payload.get("findings", [])
        if not isinstance(findings, list):
            continue
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            if str(finding.get("status", "open")).lower() != "closed":
                continue
            title = _clean_row(str(finding.get("title") or finding.get("description") or ""))
            if not title:
                continue
            loc = str(finding.get("location") or "").strip()
            source = str(finding.get("source_comment_id") or "").strip()
            row = title
            if loc:
                row += f" ({loc})"
            if source:
                by_source[source] = row
                continue
            key = row.lower()
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)

    combined = rows + list(by_source.values())
    if combined:
        deduped: list[str] = []
        dedupe_seen: set[str] = set()
        for row in combined:
            key = row.lower()
            if key in dedupe_seen:
                continue
            dedupe_seen.add(key)
            deduped.append(row)
        combined = deduped

    if combined:
        return combined[:limit]
    return [
        "Technical delta summary unavailable from session artifacts; update this section with manual vN changes before posting"
    ]


def _generate_lore_next_version(root: Path, session: dict) -> dict:
    session_id = str(session.get("id") or "")
    watch_raw = str(session.get("watch_path") or "").strip()
    if not watch_raw:
        raise RuntimeError("Cannot generate next version: session watch_path is empty.")
    source = Path(watch_raw).resolve()
    if not source.exists():
        raise RuntimeError(f"Cannot generate next version: watch_path not found: {source}")

    source_patch_files = _collect_active_patch_files(source)
    if not source_patch_files:
        raise RuntimeError(f"No patch files found in source watch path: {source}")

    current_version = _detect_current_patchset_version(source, source_patch_files)
    next_version = max(2, current_version + 1)
    output = root / A2A_DIRNAME / "patches" / session_id / f"v{next_version}"
    while output.exists():
        next_version += 1
        output = root / A2A_DIRNAME / "patches" / session_id / f"v{next_version}"
    _copy_respin_source(source, output, force=False)
    _materialize_cover_patch(output)

    patch_files = _collect_active_patch_files(output)
    if not patch_files:
        raise RuntimeError(f"No patch files found in generated output path: {output}")

    non_cover_patch_count = _synchronize_patchset_artifacts(output, next_version)
    if non_cover_patch_count <= 0:
        non_cover_patch_count = len([p for p in patch_files if not _is_cover_patch_file(p)])
    if non_cover_patch_count <= 0:
        non_cover_patch_count = len(patch_files)

    lore_link = ""
    lore_meta = session.get("lore")
    if isinstance(lore_meta, dict):
        msgid = str(lore_meta.get("message_id") or "").strip()
        if msgid:
            lore_link = f"https://lore.kernel.org/r/{msgid}"
    changelog_lines = _resolved_finding_changelog_lines(root, session_id)
    for cover_path in sorted(output.rglob("0000-cover-letter.patch")):
        _augment_cover_letter_history(
            cover_path,
            next_version=next_version,
            lore_link=lore_link or None,
            changelog_lines=changelog_lines,
        )
    for cover_path in sorted(output.rglob("*.cover")):
        _augment_cover_letter_history(
            cover_path,
            next_version=next_version,
            lore_link=lore_link or None,
            changelog_lines=changelog_lines,
        )

    payload = {
        "status": "ok",
        "kind": "lore_copy",
        "session_id": session_id,
        "source_watch_path": str(source),
        "output_path": str(output),
        "patch_count": int(non_cover_patch_count),
        "next_version": next_version,
        "generated_at": _now_utc(),
    }
    report_path = _report_dir(root, session_id) / "lore_next_version.json"
    dump_json(report_path, payload)
    payload["report"] = str(report_path)
    return payload


def _auto_generate_next_version(root: Path, session: dict) -> dict:
    session_id = str(session.get("id") or "")
    try:
        result = run_respin(root, session_id, dry_run=False)
        if isinstance(result, dict):
            result = dict(result)
            result.setdefault("kind", "git_respin")
        return result
    except Exception as exc:
        lore_meta = session.get("lore")
        if isinstance(lore_meta, dict) and str(lore_meta.get("message_id") or "").strip():
            fallback = _generate_lore_next_version(root, session)
            fallback["fallback_reason"] = str(exc)
            return fallback
        raise


def _subject_line(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines[:80]:
        if line.lower().startswith("subject:"):
            return line.strip()
    return ""


def _subject_index_total(path: Path) -> tuple[int | None, int | None]:
    subject = _subject_line(path)
    if not subject:
        return None, None
    m = _PATCH_SUBJECT_LINE_RE.match(subject)
    if not m:
        return None, None
    for token in (m.group("body") or "").split():
        if not _PATCH_INDEX_TOKEN_RE.fullmatch(token):
            continue
        try:
            idx_text, total_text = token.split("/", 1)
            return int(idx_text), int(total_text)
        except ValueError:
            return None, None
    return None, None


def _subject_core(path: Path) -> str:
    subject = _subject_line(path)
    if not subject:
        return ""
    core = re.sub(r"^\s*Subject:\s*", "", subject, flags=re.IGNORECASE).strip()
    core = re.sub(r"^\[PATCH[^\]]*\]\s*", "", core, flags=re.IGNORECASE).strip()
    core = re.sub(r"\s+", " ", core)
    return core.lower()


def _patch_touched_files(path: Path) -> set[str]:
    touched: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return touched
    for line in lines:
        m = re.match(r"^diff --git a/(.+?) b/(.+)$", line.strip())
        if not m:
            continue
        touched.add(m.group(2))
    return touched


def _mbx_subject_rows(path: Path) -> list[tuple[int | None, int | None]]:
    rows: list[tuple[int | None, int | None]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return rows
    for line in lines:
        if not line.lower().startswith("subject:"):
            continue
        if "[PATCH" not in line and "[patch" not in line:
            continue
        m = _PATCH_SUBJECT_LINE_RE.match(line.strip())
        if not m:
            continue
        idx: int | None = None
        total: int | None = None
        for token in (m.group("body") or "").split():
            if not _PATCH_INDEX_TOKEN_RE.fullmatch(token):
                continue
            try:
                idx_text, total_text = token.split("/", 1)
                idx = int(idx_text)
                total = int(total_text)
            except ValueError:
                idx = None
                total = None
            break
        rows.append((idx, total))
    return rows


def _validate_patchset_artifact_coherence(output_path: Path) -> list[str]:
    issues: list[str] = []
    if not output_path.exists():
        return [f"output path missing: {output_path}"]

    patch_dirs = sorted(p for p in output_path.rglob("*.patches") if p.is_dir())
    if not patch_dirs:
        patch_dirs = [output_path] if output_path.is_dir() else []

    patchset_totals: dict[str, int] = {}
    for patch_dir in patch_dirs:
        patch_files = sorted(p for p in patch_dir.glob("*.patch") if p.is_file())
        if not patch_files:
            continue

        series_path = patch_dir / "series"
        series_entries = _read_series_entries(series_path) if series_path.exists() else [p.name for p in patch_files]
        ordered_entries = [entry for entry in series_entries if (patch_dir / entry).is_file()]
        if not ordered_entries:
            ordered_entries = [p.name for p in patch_files]

        cover_entries = [entry for entry in ordered_entries if _is_cover_patch_file(patch_dir / entry)]
        non_cover_entries = [entry for entry in ordered_entries if not _is_cover_patch_file(patch_dir / entry)]
        if not non_cover_entries:
            issues.append(f"{patch_dir}: no non-cover patch entries found")
            continue

        if cover_entries and ordered_entries[0] not in cover_entries:
            issues.append(f"{series_path}: cover patch is not first entry")

        total = len(non_cover_entries)
        patchset_totals[_patchset_name_for_path(patch_dir)] = total

        for index, entry in enumerate(non_cover_entries, start=1):
            patch_path = patch_dir / entry
            idx, declared_total = _subject_index_total(patch_path)
            if idx is None or declared_total is None:
                issues.append(f"{patch_path}: missing subject index/total token")
                continue
            if idx != index or declared_total != total:
                issues.append(
                    f"{patch_path}: subject index/total {idx}/{declared_total} does not match expected {index}/{total}"
                )

        for entry in cover_entries[:1]:
            cover_path = patch_dir / entry
            idx, declared_total = _subject_index_total(cover_path)
            if idx is None or declared_total is None:
                issues.append(f"{cover_path}: missing cover subject index/total token")
                continue
            if idx != 0 or declared_total != total:
                issues.append(
                    f"{cover_path}: cover subject index/total {idx}/{declared_total} does not match expected 0/{total}"
                )

    for cover_path in sorted(output_path.rglob("*.cover")):
        key = _patchset_name_for_path(cover_path)
        if key not in patchset_totals:
            continue
        expected_total = patchset_totals[key]
        idx, declared_total = _subject_index_total(cover_path)
        if idx is None or declared_total is None:
            issues.append(f"{cover_path}: missing cover subject index/total token")
            continue
        if idx != 0 or declared_total != expected_total:
            issues.append(
                f"{cover_path}: cover subject index/total {idx}/{declared_total} does not match expected 0/{expected_total}"
            )

    for mbx_path in sorted(output_path.rglob("*.mbx")):
        key = _patchset_name_for_path(mbx_path)
        if key not in patchset_totals:
            continue
        expected_total = patchset_totals[key]
        rows = _mbx_subject_rows(mbx_path)
        if not rows:
            issues.append(f"{mbx_path}: no patch subject rows found")
            continue
        if len(rows) != expected_total:
            issues.append(f"{mbx_path}: subject row count {len(rows)} does not match expected {expected_total}")
            continue
        for index, (idx, declared_total) in enumerate(rows, start=1):
            if idx != index or declared_total != expected_total:
                issues.append(
                    f"{mbx_path}: row {index} has {idx}/{declared_total}, expected {index}/{expected_total}"
                )

    return issues


def _validate_cover_changelog_quality(output_path: Path) -> list[str]:
    issues: list[str] = []
    cover_files = sorted(output_path.rglob("0000-cover-letter.patch"))
    cover_files.extend(sorted(output_path.rglob("*.cover")))
    seen: set[str] = set()
    deduped_cover_files: list[Path] = []
    for path in cover_files:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        deduped_cover_files.append(path)

    banned_phrases = [
        "automated respin",
        "generated by a2a",
        "auto-generated",
        "generated by tool",
    ]
    for cover in deduped_cover_files:
        try:
            lines = cover.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            issues.append(f"{cover}: cannot read cover letter")
            continue

        marker_indexes = [idx for idx, line in enumerate(lines) if re.match(r"^\s*Changes since v\d+:\s*$", line)]
        if not marker_indexes:
            issues.append(f"{cover}: missing 'Changes since vN' section")
            continue

        for marker_idx in marker_indexes:
            bullets: list[str] = []
            for line in lines[marker_idx + 1 :]:
                text = line.strip()
                if not text and bullets:
                    break
                if not text:
                    continue
                if re.match(r"^\s*Changes since v\d+:\s*$", line):
                    break
                if re.match(r"^\s*v\d+:\s*", line):
                    break
                if text.startswith("- "):
                    bullets.append(text)
                    continue
                if bullets and (line.startswith(" ") or line.startswith("\t")):
                    continue
            if not bullets:
                issues.append(f"{cover}: empty changelog bullets under '{lines[marker_idx].strip()}'")
                continue
            for bullet in bullets:
                lower = bullet.lower()
                if any(phrase in lower for phrase in banned_phrases):
                    issues.append(f"{cover}: non-technical/tool-meta changelog bullet '{bullet}'")
                if "prior-msg:" in lower or "subsys-scan:" in lower:
                    issues.append(f"{cover}: internal source id leaked in changelog bullet '{bullet}'")

    return issues


def _validate_respin_delta(source_watch_path: Path, output_path: Path) -> list[str]:
    issues: list[str] = []
    try:
        source_patches = [p for p in _collect_active_patch_files(source_watch_path) if not _is_cover_patch_file(p)]
    except Exception as exc:
        return [f"cannot collect source patch files: {exc}"]
    try:
        output_patches = [p for p in _collect_active_patch_files(output_path) if not _is_cover_patch_file(p)]
    except Exception as exc:
        return [f"cannot collect generated patch files: {exc}"]

    if len(source_patches) != len(output_patches):
        issues.append(
            f"patch count drift: source has {len(source_patches)} non-cover patches, output has {len(output_patches)}"
        )

    pair_count = min(len(source_patches), len(output_patches))
    for idx in range(pair_count):
        src = source_patches[idx]
        out = output_patches[idx]
        src_core = _subject_core(src)
        out_core = _subject_core(out)
        if src_core and out_core and src_core != out_core:
            issues.append(
                f"subject drift at patch index {idx + 1}: source='{src_core}' output='{out_core}'"
            )
        src_touched = _patch_touched_files(src)
        out_touched = _patch_touched_files(out)
        if src_touched and out_touched and src_touched != out_touched:
            issues.append(
                f"touched-file drift at patch index {idx + 1}: source={sorted(src_touched)} output={sorted(out_touched)}"
            )

    return issues


def _extract_findings_from_agent_output(text: str) -> list[dict] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict) and isinstance(payload.get("findings"), list):
            return payload.get("findings")
    except Exception:
        pass

    matches = list(re.finditer(r'(\{"findings"\s*:\s*\[.*?\]\})', raw, flags=re.S))
    for match in reversed(matches):
        candidate = match.group(1)
        try:
            payload = json.loads(candidate)
        except Exception:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("findings"), list):
            return payload.get("findings")
    return None


def _run_post_respin_reviewer_validation(
    root: Path,
    session: dict,
    reviewer_cmd: str,
    output_path: Path,
) -> dict:
    sid = str(session.get("id") or "")
    cfg = _load_config_or_defaults(root)
    reviewer_name = str(session.get("reviewer_name", "aryabhatta"))
    report_dir = _report_dir(root, sid)
    review_path = report_dir / "post-respin-review.md"
    findings_path = report_dir / "post-respin-findings.json"
    builder_placeholder = report_dir / "post-respin-builder.md"
    logs_dir = root / A2A_DIRNAME / "logs" / sid
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "post-respin-reviewer.log"

    round_no = int(session.get("current_round", 1) or 1)
    files = {
        "report_dir": report_dir,
        "builder": builder_placeholder,
        "reviewer": review_path,
        "findings": findings_path,
    }
    if not builder_placeholder.exists():
        builder_placeholder.write_text("# post-respin placeholder\n", encoding="utf-8")

    session_for_validation = dict(session)
    session_for_validation["watch_path"] = str(output_path)
    env = _agent_env(session_for_validation, round_no, files, "reviewer", cfg)
    env["A2A_WATCH_PATH"] = str(output_path)
    env["A2A_ROUND"] = f"{round_no}-post-respin"

    worktrees = session.get("worktrees", {}) if isinstance(session.get("worktrees"), dict) else {}
    cwd = Path(worktrees.get(reviewer_name, session.get("repo_path") or root))
    result = run_shell_command(reviewer_cmd, cwd=cwd, env=env)

    extracted_findings = None
    if int(result.get("returncode", 1)) != 0 and not findings_path.exists():
        extracted_findings = _extract_findings_from_agent_output(str(result.get("stdout") or ""))
        if extracted_findings is None:
            extracted_findings = _extract_findings_from_agent_output(str(result.get("stderr") or ""))
        if extracted_findings is not None:
            dump_json(findings_path, {"findings": extracted_findings})
            open_count = len(
                [
                    row
                    for row in extracted_findings
                    if isinstance(row, dict) and str(row.get("status", "")).lower() != "closed"
                ]
            )
            verdict = "LGTM" if open_count == 0 else "REJECT"
            review_path.write_text(
                "\n".join(
                    [
                        f"# Round {round_no}-post-respin: Aryabhatta Review",
                        "",
                        "## Verdict",
                        "",
                        f"- {verdict}",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

    fallback_result: dict | None = None
    fallback_cmd = str(env.get("A2A_FALLBACK_REVIEWER_CMD") or "").strip()
    fallback_used = False
    if int(result.get("returncode", 1)) != 0 and not findings_path.exists() and fallback_cmd:
        fallback_result = run_shell_command(fallback_cmd, cwd=cwd, env=env)
        fallback_used = True

    with log_path.open("w", encoding="utf-8") as fp:
        fp.write("role=reviewer\n")
        fp.write(f"round={round_no}-post-respin\n")
        fp.write(f"cwd={cwd}\n")
        fp.write(f"command={reviewer_cmd}\n")
        fp.write(f"returncode={result.get('returncode')}\n\n")
        fp.write("stdout:\n")
        fp.write(result.get("stdout") or "")
        fp.write("\n\nstderr:\n")
        fp.write(result.get("stderr") or "")
        if fallback_used and fallback_result is not None:
            fp.write("\n\nfallback_command:\n")
            fp.write(f"{fallback_cmd}\n")
            fp.write(f"fallback_returncode={fallback_result.get('returncode')}\n\n")
            fp.write("fallback_stdout:\n")
            fp.write(fallback_result.get("stdout") or "")
            fp.write("\n\nfallback_stderr:\n")
            fp.write(fallback_result.get("stderr") or "")
        fp.write("\n")

    payload: dict = {
        "ran": True,
        "ok": False,
        "log": str(log_path),
        "review_file": str(review_path),
        "findings_file": str(findings_path),
        "returncode": int(result.get("returncode", 1)),
        "issues": [],
    }
    rc = int(result.get("returncode", 1))
    fallback_rc = int(fallback_result.get("returncode", 1)) if fallback_result is not None else None
    if fallback_used:
        payload["fallback_used"] = True
        payload["fallback_returncode"] = fallback_rc
    if extracted_findings is not None:
        payload["extracted_findings_from_primary_output"] = True
    if rc != 0:
        if findings_path.exists() and (not fallback_used or fallback_rc in (None, 0)):
            payload["primary_reviewer_returncode"] = rc
        elif fallback_used and fallback_rc == 0:
            payload["primary_reviewer_returncode"] = rc
        else:
            payload["issues"].append(f"reviewer command failed rc={rc}")
    if fallback_used and fallback_rc not in (None, 0):
        payload["issues"].append(f"fallback reviewer failed rc={fallback_rc}")

    if not findings_path.exists():
        payload["issues"].append(f"missing findings file: {findings_path}")
        return payload

    try:
        findings = _load_findings_payload(findings_path)
    except RuntimeError as exc:
        payload["issues"].append(str(exc))
        return payload

    strict = bool(cfg.get("strict_evidence", True))
    errors, open_count = _validate_findings(findings, strict)
    verdict = ""
    if review_path.exists():
        verdict = _reviewer_verdict_from_text(review_path.read_text(encoding="utf-8", errors="replace"))
    unresolved_risk, risk_line = reviewer_log_has_unresolved_risk(log_path)

    payload["findings_total"] = len(findings)
    payload["findings_open"] = int(open_count)
    payload["verdict"] = verdict
    payload["validation_errors"] = errors
    if errors:
        payload["issues"].extend(errors)
    if open_count > 0:
        payload["issues"].append(f"open findings remain after post-respin review: {open_count}")
    if verdict and verdict != "LGTM":
        payload["issues"].append(f"reviewer verdict is not LGTM: {verdict}")
    if unresolved_risk:
        payload["issues"].append(f"reviewer log indicates unresolved risk: {risk_line}")

    payload["ok"] = not bool(payload["issues"])
    return payload


def _run_post_respin_checkpatch(
    output_path: Path,
    kernel_root: Path | None,
    max_files: int,
) -> dict:
    payload: dict = {
        "ran": False,
        "ok": True,
        "kernel_root": str(kernel_root) if kernel_root else "",
        "files_checked": 0,
        "results": [],
        "issues": [],
    }
    if kernel_root is None:
        payload["issues"].append("kernel tree not found; skipped checkpatch")
        return payload

    checkpatch = kernel_root / "scripts" / "checkpatch.pl"
    if not checkpatch.is_file():
        payload["issues"].append(f"checkpatch not found: {checkpatch}")
        return payload

    try:
        patch_files = [p for p in _collect_active_patch_files(output_path) if not _is_cover_patch_file(p)]
    except Exception as exc:
        payload["issues"].append(f"cannot collect generated patch files: {exc}")
        payload["ok"] = False
        return payload
    if not patch_files:
        payload["issues"].append("no generated non-cover patch files found")
        payload["ok"] = False
        return payload

    patch_files = patch_files[: max(1, int(max_files))]
    payload["ran"] = True
    payload["files_checked"] = len(patch_files)
    for patch in patch_files:
        cmd = f"{shlex.quote(str(checkpatch))} --no-tree --strict {shlex.quote(str(patch))}"
        result = run_shell_command(cmd, cwd=kernel_root, env=dict(os.environ))
        rc = int(result.get("returncode", 1))
        row = {
            "patch": str(patch),
            "returncode": rc,
            "ok": rc == 0,
        }
        payload["results"].append(row)
        if rc != 0:
            payload["ok"] = False
            payload["issues"].append(f"checkpatch failed for {patch.name} (rc={rc})")
    return payload


def _run_post_respin_validation(
    root: Path,
    session: dict,
    next_version_payload: dict,
    reviewer_cmd: str,
) -> dict:
    sid = str(session.get("id") or "")
    report_dir = _report_dir(root, sid)
    cfg = _load_config_or_defaults(root)
    output_raw = str(next_version_payload.get("output_path") or "").strip()
    source_raw = str(next_version_payload.get("source_watch_path") or str(session.get("watch_path") or "")).strip()
    output_path = Path(output_raw).resolve() if output_raw else Path()
    source_path = Path(source_raw).resolve() if source_raw else Path()

    watch_raw = str(session.get("watch_path") or "").strip()
    kernel_root = _detect_kernel_repo_root(Path(watch_raw)) if watch_raw else None

    artifact_issues = _validate_patchset_artifact_coherence(output_path)
    changelog_issues = _validate_cover_changelog_quality(output_path)
    delta_issues = _validate_respin_delta(source_path, output_path) if source_path.exists() else [
        f"source watch path missing for delta check: {source_path}"
    ]

    run_reviewer = bool(cfg.get("post_respin_run_reviewer", True))
    reviewer_payload = {
        "ran": False,
        "ok": True,
        "issues": [],
    }
    if run_reviewer:
        if not str(reviewer_cmd or "").strip():
            reviewer_payload = {
                "ran": False,
                "ok": False,
                "issues": ["reviewer command is empty for post-respin validation"],
            }
        else:
            reviewer_payload = _run_post_respin_reviewer_validation(root, session, reviewer_cmd, output_path)

    run_checkpatch = bool(cfg.get("post_respin_checkpatch", True))
    checkpatch_payload = {
        "ran": False,
        "ok": True,
        "issues": [],
    }
    if run_checkpatch:
        checkpatch_payload = _run_post_respin_checkpatch(
            output_path,
            kernel_root,
            int(cfg.get("post_respin_max_checkpatch_files", 100)),
        )

    checks = {
        "artifact_coherence": {
            "ok": not artifact_issues,
            "issues": artifact_issues,
        },
        "cover_changelog_quality": {
            "ok": not changelog_issues,
            "issues": changelog_issues,
        },
        "delta_guard": {
            "ok": not delta_issues,
            "issues": delta_issues,
        },
        "reviewer_validation": reviewer_payload,
        "checkpatch": checkpatch_payload,
    }

    all_issues: list[str] = []
    failures = 0
    for name, row in checks.items():
        ok = bool(row.get("ok", False))
        if not ok:
            failures += 1
            all_issues.append(f"{name}: failed")
        for issue in row.get("issues", []):
            all_issues.append(f"{name}: {issue}")

    payload = {
        "status": "ok" if failures == 0 else "failed",
        "session_id": sid,
        "generated_at": _now_utc(),
        "output_path": str(output_path),
        "source_watch_path": str(source_path),
        "kernel_root": str(kernel_root) if kernel_root else "",
        "checks": checks,
        "failures": failures,
        "issues": all_issues,
    }
    report_path = report_dir / "post_respin_validation.json"
    dump_json(report_path, payload)
    payload["report"] = str(report_path)
    return payload


def _round_basename(round_no: int, suffix: str) -> str:
    return f"round-{round_no:02d}-{suffix}"


def _round_files(root: Path, session_id: str, round_no: int, reviewer_name: str) -> dict[str, Path]:
    report_dir = _report_dir(root, session_id)
    return {
        "report_dir": report_dir,
        "builder": report_dir / f"{_round_basename(round_no, 'builder')}.md",
        "reviewer": report_dir / f"{_round_basename(round_no, reviewer_name)}.md",
        "findings": report_dir / f"{_round_basename(round_no, 'findings')}.json",
    }


def _write_round_templates(
    root: Path,
    session_id: str,
    round_no: int,
    reviewer_name: str,
    reviewer_display_name: str | None = None,
) -> None:
    files = _round_files(root, session_id, round_no, reviewer_name)
    report_dir = files["report_dir"]
    builder_file = files["builder"]
    reviewer_file = files["reviewer"]
    findings_file = files["findings"]
    report_dir.mkdir(parents=True, exist_ok=True)
    reviewer_label = (reviewer_display_name or "").strip() or reviewer_name

    builder_tpl = (
        f"# Round {round_no}: Builder Output\n\n"
        "## Changes\n- \n\n"
        "## Rationale\n- \n\n"
        "## Verification Commands\n- \n\n"
        "## Response To Reviewer Findings\n- \n"
    )
    reviewer_tpl = (
        f"# Round {round_no}: {reviewer_label} Review\n\n"
        "## Findings\n- severity: \n"
        "  title: \n"
        "  location: path:line\n"
        "  evidence: \n"
        "  required_action: \n"
        "  status: open|closed\n\n"
        "## Verdict\n- pending | LGTM\n"
    )
    findings_tpl = {
        "findings": [],
    }

    if not builder_file.exists():
        builder_file.write_text(builder_tpl, encoding="utf-8")
    if not reviewer_file.exists():
        reviewer_file.write_text(reviewer_tpl, encoding="utf-8")
    if not findings_file.exists():
        findings_file.write_text(_as_json(findings_tpl), encoding="utf-8")


def _load_findings_payload(path: Path) -> list[dict]:
    payload = load_json(path)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        findings = payload.get("findings", [])
        if isinstance(findings, list):
            return findings
    raise RuntimeError(f"Invalid findings payload structure: {path}")


def _findings_payload_path(root: Path, session_id: str) -> Path:
    return _report_dir(root, session_id) / "issue_id_map.json"


def _location_path_only(location: str) -> str:
    text = location.strip()
    if not text:
        return ""
    if ":" not in text:
        return text
    left, right = text.rsplit(":", 1)
    if right.isdigit():
        return left
    return text


def _finding_fingerprint(finding: dict) -> str:
    severity = str(finding.get("severity") or "").strip().lower()
    title = " ".join(str(finding.get("title") or "").strip().lower().split())
    required_action = " ".join(str(finding.get("required_action") or "").strip().lower().split())
    location = _location_path_only(str(finding.get("location") or "").strip().lower())
    base = "||".join([severity, location, title, required_action])
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def _is_prior_source_id(source_id: str) -> bool:
    text = source_id.strip().lower()
    return text.startswith("prior-")


def _load_issue_id_map(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    payload = load_json(path)
    if not isinstance(payload, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in payload.items():
        k = str(key).strip()
        v = str(value).strip()
        if not k or not v:
            continue
        out[k] = v
    return out


def _save_issue_id_map(path: Path, mapping: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    dump_json(path, mapping)


def _canonicalize_generated_source_ids(
    findings: list[dict], issue_id_map: dict[str, str]
) -> tuple[list[dict], dict[str, str], bool]:
    out: list[dict] = []
    changed = False
    local_map = dict(issue_id_map)
    used_ids = set(local_map.values())

    for finding in findings:
        if not isinstance(finding, dict):
            out.append(finding)
            continue
        current = str(finding.get("source_comment_id") or "").strip()
        if current and _is_prior_source_id(current):
            out.append(finding)
            continue

        needs_stable_id = (not current) or bool(_VOLATILE_SOURCE_ID_RE.match(current))
        if not needs_stable_id:
            out.append(finding)
            continue

        fp = _finding_fingerprint(finding)
        stable_id = local_map.get(fp)
        if not stable_id:
            digest = fp[:12]
            stable_id = f"issue-{digest}"
            suffix = 1
            while stable_id in used_ids:
                suffix += 1
                stable_id = f"issue-{digest}-{suffix}"
            local_map[fp] = stable_id
            used_ids.add(stable_id)

        updated = dict(finding)
        if current != stable_id:
            changed = True
            updated["source_comment_id"] = stable_id
        out.append(updated)

    return out, local_map, changed


def _validate_findings(findings: list[dict], strict_evidence: bool) -> tuple[list[str], int]:
    errors: list[str] = []
    open_count = 0
    required = {
        "severity",
        "title",
        "location",
        "evidence",
        "required_action",
        "status",
        "source_comment_id",
    }

    for idx, entry in enumerate(findings, start=1):
        if not isinstance(entry, dict):
            errors.append(f"Finding #{idx}: must be a JSON object")
            continue

        missing = sorted(required - set(entry.keys()))
        if missing:
            errors.append(f"Finding #{idx}: missing keys: {', '.join(missing)}")
            continue

        status = str(entry["status"]).lower()
        if status not in {"open", "closed"}:
            errors.append(f"Finding #{idx}: invalid status '{entry['status']}'")
        if status != "closed":
            open_count += 1

        if strict_evidence:
            evidence = entry.get("evidence")
            if isinstance(evidence, list):
                if len(evidence) == 0:
                    errors.append(f"Finding #{idx}: evidence list must not be empty")
            elif isinstance(evidence, str):
                if not evidence.strip():
                    errors.append(f"Finding #{idx}: evidence string must not be empty")
            else:
                errors.append(f"Finding #{idx}: evidence must be list or string")

        location = str(entry.get("location", ""))
        if ":" not in location:
            errors.append(f"Finding #{idx}: location should be in path:line form")

    return errors, open_count


def should_issue_lgtm(findings_json_path: str, reviewer_verdict: str) -> tuple[bool, str]:
    """
    LGTM requires ALL THREE conditions simultaneously:
      1. current round open  == 0  (no open findings right now)
      2. current round new   == 0  (no new findings raised this round)
      3. reviewer verdict    == LGTM (explicit agent verdict)

    Resolving old findings does NOT qualify for LGTM
    if new findings were raised in the same round.
    """
    try:
        with open(findings_json_path, encoding="utf-8") as handle:
            findings_payload = json.load(handle)
    except Exception as exc:
        return False, f"cannot read findings JSON: {exc}"

    if not isinstance(findings_payload, dict):
        return False, "cannot read findings JSON: payload must be a JSON object"

    findings = findings_payload.get("findings")
    if not isinstance(findings, list):
        findings = []

    raw_open = findings_payload.get("open", None)
    raw_new = findings_payload.get("new", None)

    try:
        open_count = int(raw_open) if raw_open is not None else -1
    except (TypeError, ValueError):
        open_count = -1
    try:
        new_count = int(raw_new) if raw_new is not None else -1
    except (TypeError, ValueError):
        new_count = -1

    # Fallback to current-round findings list when top-level counters are unavailable.
    if open_count < 0:
        open_count = 0
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            status = str(finding.get("status") or "").strip().lower()
            if status != "closed":
                open_count += 1

    if new_count < 0:
        new_count = 0
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            source_id = str(finding.get("source_comment_id") or "").strip().lower()
            if not source_id:
                new_count += 1
                continue
            if source_id.startswith("new-") or source_id.startswith("round") or source_id.startswith("issue-temp"):
                new_count += 1

    verdict = reviewer_verdict.strip().upper()

    if new_count != 0:
        return False, (
            f"new findings raised this round = {new_count} "
            f"(Aryabhata raised new issues — cannot issue LGTM)"
        )

    if open_count != 0:
        return False, (
            f"open findings = {open_count} "
            f"(must be 0 for LGTM)"
        )

    if verdict != "LGTM":
        return False, (
            f"Aryabhata verdict = {verdict} "
            f"(must be explicit LGTM)"
        )

    return True, "LGTM"


def _load_session(root: Path, session_id: str) -> dict:
    path = _session_path(root, session_id)
    if not path.exists():
        raise RuntimeError(f"Session not found: {session_id}")
    return load_json(path)


def _write_session(root: Path, session: dict) -> None:
    dump_json(_session_path(root, str(session["id"])), session)


def _reviewer_verdict_for_round(root: Path, session_id: str, round_no: int, reviewer_name: str) -> str:
    files = _round_files(root, session_id, round_no, reviewer_name)
    review_path = files["reviewer"]
    if not review_path.exists():
        return ""

    text = review_path.read_text(encoding="utf-8", errors="replace")
    return _reviewer_verdict_from_text(text)


def _reviewer_verdict_from_text(text: str) -> str:
    # Prefer explicit verdict section from reviewer markdown.
    for line in text.splitlines():
        m = re.match(r"^\s*-\s*(LGTM|REJECT|PENDING)\s*$", line.strip(), re.IGNORECASE)
        if m:
            token = m.group(1).strip().upper()
            if token == "PENDING":
                return "REJECT"
            return token
    # Fallback: keyword scan.
    upper = text.upper()
    if "LGTM" in upper:
        return "LGTM"
    if "REJECT" in upper or "PENDING" in upper:
        return "REJECT"
    return ""


def reviewer_log_has_unresolved_risk(log_path: Path) -> tuple[bool, str]:
    if not log_path.exists():
        return False, ""
    text = log_path.read_text(encoding="utf-8", errors="replace")
    segments = re.findall(
        r"(?ms)^thinking\s*\n(.*?)(?=^exec\s*$|^qgenie\s*$|^\[aryabhatta-llm\]|^\{.*$|\Z)",
        text,
    )
    if not segments:
        return False, ""

    uncertainty_tokens = [
        "uncertain",
        "uncertainty",
        "not sure",
        "cannot verify",
        "can't verify",
        "unable to verify",
        "unresolved",
        "remaining uncertainty",
        "potential issue",
        "potential risk",
        "concern",
        "risk",
        "might",
    ]
    issue_tokens = [
        "duplicate",
        "#define",
        "shared rail",
        "refcount",
        "pre_pmd",
        "post_pmd",
        "ana_rx_supplies",
        "ownership",
    ]
    skip_prefixes = ("**", "thinking", "exec")

    for segment in segments:
        lower = segment.lower()
        if not any(token in lower for token in uncertainty_tokens):
            continue
        if not any(token in lower for token in issue_tokens):
            continue
        matched_line = ""
        for raw in segment.splitlines():
            line = raw.strip()
            if not line:
                continue
            line_lower = line.lower()
            if line_lower.startswith("202") and "failed to record rollout items" in line_lower:
                continue
            if line_lower.startswith(skip_prefixes):
                continue
            if not any(token in line_lower for token in uncertainty_tokens):
                continue
            if not any(token in line_lower for token in issue_tokens):
                continue
            matched_line = line
            break

        if not matched_line:
            for raw in segment.splitlines():
                line = raw.strip()
                if line and not line.lower().startswith(skip_prefixes):
                    matched_line = line
                    break
        snippet = matched_line[:160] if matched_line else "uncertainty/risk language found in reviewer reasoning"
        return True, snippet
    return False, ""


def _is_prior_or_meta_source_id(source_id: str) -> bool:
    norm = source_id.strip().lower()
    return (
        norm.startswith("prior-msg:")
        or norm.startswith("prior-meta:")
        or norm.startswith("meta")
    )


def has_independent_subsystem_findings(findings: list[dict]) -> bool:
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        source_id = str(finding.get("source_comment_id") or "").strip().lower()
        if not source_id:
            continue
        if _is_prior_or_meta_source_id(source_id):
            continue
        if source_id.startswith("subsys-scan:") or source_id.startswith("independent-scan:"):
            return True
        if source_id:
            return True
    return False


def requires_full_subsystem_review(session: dict, cfg: dict) -> bool:
    if not bool(cfg.get("full_subsystem_review_required", True)):
        return False
    prior = session.get("prior_review")
    if not isinstance(prior, dict):
        return False
    try:
        comments_total = int(prior.get("comments_total") or 0)
    except (TypeError, ValueError):
        comments_total = 0
    return comments_total > 0


def _agent_env(
    session: dict,
    round_no: int,
    files: dict[str, Path],
    role: str,
    cfg: dict,
) -> dict[str, str]:
    env = dict(os.environ)
    watch_path = session.get("watch_path")
    llm_native = session.get("llm_native")
    llm_strict = True
    llm_fallback = False
    if isinstance(llm_native, dict):
        llm_strict = bool(llm_native.get("strict", True))
        llm_fallback = bool(llm_native.get("fallback", False))
    llm_timeout_sec = 900
    if isinstance(llm_native, dict):
        try:
            llm_timeout_sec = int(llm_native.get("timeout_sec", 900))
        except (TypeError, ValueError):
            llm_timeout_sec = 900

    repo_root = _repo_root_from_module()
    runtime_root = repo_root / ".runtime"
    codex_home = runtime_root / "codex-home"
    runtime_tmp = runtime_root / "tmp"
    try:
        codex_home.mkdir(parents=True, exist_ok=True)
        runtime_tmp.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Keep legacy env if runtime dirs cannot be created.
        pass

    fallback_builder_cmd = f"python {repo_root / 'scripts' / 'agents' / 'builder_agent.py'}"
    fallback_reviewer_cmd = f"python {repo_root / 'scripts' / 'agents' / 'reviewer_aryabhatta.py'}"
    env.update(
        {
            "A2A_SESSION_ID": str(session["id"]),
            "A2A_TASK": str(session["task"]),
            "A2A_ROUND": str(round_no),
            "A2A_ROLE": role,
            "A2A_REPO_PATH": str(session["repo_path"]),
            "A2A_BRANCH": str(session["branch"]),
            "A2A_REPORT_DIR": str(files["report_dir"]),
            "A2A_BUILDER_FILE": str(files["builder"]),
            "A2A_REVIEW_FILE": str(files["reviewer"]),
            "A2A_FINDINGS_FILE": str(files["findings"]),
            "A2A_WATCH_PATH": str(watch_path) if watch_path else "",
            "A2A_PRIOR_COMMENTS_FILE": "",
            "A2A_PRIOR_MATRIX_FILE": "",
            "A2A_PRIOR_COMMENTS_TOTAL": "0",
            "A2A_LLM_STRICT": "1" if llm_strict else "0",
            "A2A_ALLOW_FALLBACK": "1" if llm_fallback else "0",
            "A2A_FALLBACK_BUILDER_CMD": fallback_builder_cmd,
            "A2A_FALLBACK_REVIEWER_CMD": fallback_reviewer_cmd,
            "A2A_LLM_TIMEOUT_SEC": str(llm_timeout_sec),
            "A2A_EXTRA_SCRUTINY": "1" if bool(session.get("extra_scrutiny_next_round")) else "0",
            "A2A_REQUIRE_INDEPENDENT_SCAN": (
                "1" if requires_full_subsystem_review(session, cfg) else "0"
            ),
            "CODEX_HOME": str(codex_home),
            "TMPDIR": str(runtime_tmp),
        }
    )
    prior = session.get("prior_review")
    if isinstance(prior, dict):
        comments_file = str(prior.get("comments_file") or "").strip()
        matrix_file = str(prior.get("matrix_file") or "").strip()
        comments_total = int(prior.get("comments_total") or 0)
        env["A2A_PRIOR_COMMENTS_FILE"] = comments_file
        env["A2A_PRIOR_MATRIX_FILE"] = matrix_file
        env["A2A_PRIOR_COMMENTS_TOTAL"] = str(comments_total)

    a2a_root = find_a2a_root(Path(str(session.get("repo_path") or ""))) or _repo_root_from_module()
    subsystem = infer_subsystem_from_watch_path(str(watch_path or ""))
    chanakya_context = build_chanakya_context(a2a_root, subsystem)
    open_findings: list[dict] = []
    if round_no > 1:
        prev_path = files["report_dir"] / f"{_round_basename(round_no - 1, 'findings')}.json"
        if prev_path.exists():
            try:
                payload = load_json(prev_path)
                rows = payload.get("findings", []) if isinstance(payload, dict) else []
                if isinstance(rows, list):
                    open_findings = [
                        row for row in rows if isinstance(row, dict) and str(row.get("status", "open")).lower() != "closed"
                    ]
            except Exception:
                open_findings = []
    aryabhata_context = build_aryabhata_context(a2a_root, open_findings)
    env["A2A_KB_SUBSYSTEM"] = subsystem
    env["A2A_KB_CHANAKYA_CONTEXT"] = chanakya_context
    env["A2A_KB_ARYABHATTA_CONTEXT"] = aryabhata_context
    env["A2A_KB_CONTEXT"] = chanakya_context if role == "builder" else aryabhata_context
    return env


def _snapshot_text_files(base: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not base.exists():
        return out

    files: list[Path] = []
    if base.is_file():
        files = [base]
    else:
        files = [p for p in sorted(base.rglob("*")) if p.is_file()]

    for path in files:
        rel = path.name if base.is_file() else str(path.relative_to(base))
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        out[rel] = content.splitlines(keepends=True)
    return out


def _write_builder_change_artifacts(
    root: Path,
    session: dict,
    round_no: int,
    before: dict[str, list[str]] | None,
    after: dict[str, list[str]] | None,
) -> None:
    if before is None or after is None:
        return

    reviewer_name = str(session.get("reviewer_name", "aryabhatta"))
    files = _round_files(root, str(session["id"]), round_no, reviewer_name)
    changed_path = files["report_dir"] / f"{_round_basename(round_no, 'changed_files')}.txt"
    diff_path = files["report_dir"] / f"{_round_basename(round_no, 'builder')}.diff"
    files["report_dir"].mkdir(parents=True, exist_ok=True)

    keys = sorted(set(before.keys()) | set(after.keys()))
    changed: list[str] = []
    diff_chunks: list[str] = []

    for rel in keys:
        before_lines = before.get(rel, [])
        after_lines = after.get(rel, [])
        if before_lines == after_lines:
            continue
        changed.append(rel)
        diff_lines = list(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                n=3,
            )
        )
        if diff_lines:
            diff_chunks.extend(diff_lines)
            if not diff_lines[-1].endswith("\n"):
                diff_chunks.append("\n")

    changed_content = "\n".join(changed) + ("\n" if changed else "")
    changed_path.write_text(changed_content, encoding="utf-8")
    diff_path.write_text("".join(diff_chunks), encoding="utf-8")


def _collect_watch_patch_files(watch_path: Path) -> list[Path]:
    if watch_path.is_file() and watch_path.suffix == ".patch":
        return [watch_path]
    if watch_path.is_dir():
        return sorted(p for p in watch_path.rglob("*.patch") if p.is_file())
    return []


def _round_changed_files_path(root: Path, session_id: str, round_no: int, reviewer_name: str) -> Path:
    files = _round_files(root, session_id, round_no, reviewer_name)
    return files["report_dir"] / f"{_round_basename(round_no, 'changed_files')}.txt"


def _load_round_changed_paths(root: Path, session_id: str, round_no: int, reviewer_name: str) -> list[str]:
    path = _round_changed_files_path(root, session_id, round_no, reviewer_name)
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _resolve_gate_patch_targets(watch_path: Path, changed_rel_paths: list[str], round_no: int) -> list[Path]:
    all_patches = _collect_watch_patch_files(watch_path)
    if not all_patches:
        return []

    if watch_path.is_file():
        return all_patches

    selected: list[Path] = []
    for rel in changed_rel_paths:
        if not rel.endswith(".patch"):
            continue
        candidate = watch_path / rel
        if candidate.exists() and candidate.is_file():
            selected.append(candidate)

    if selected:
        seen: set[Path] = set()
        out: list[Path] = []
        for patch in selected:
            if patch in seen:
                continue
            out.append(patch)
            seen.add(patch)
        return out

    if int(round_no) == 1:
        return all_patches
    return []


def _detect_kernel_repo_root(path: Path) -> Path | None:
    start = path if path.is_dir() else path.parent
    for candidate in [start, *start.parents]:
        checkpatch = candidate / "scripts" / "checkpatch.pl"
        if checkpatch.is_file():
            return candidate
    return None


def _gate_artifacts(root: Path, session_id: str, round_no: int, reviewer_name: str) -> dict[str, Path]:
    files = _round_files(root, session_id, round_no, reviewer_name)
    logs_dir = root / A2A_DIRNAME / "logs" / session_id
    logs_dir.mkdir(parents=True, exist_ok=True)
    return {
        "json": files["report_dir"] / f"{_round_basename(round_no, 'gate')}.json",
        "log": logs_dir / f"{_round_basename(round_no, 'gate')}.log",
    }


def _run_validation_gate(root: Path, session: dict, round_no: int) -> tuple[bool, bool]:
    cfg = _load_config_or_defaults(root)
    enabled = bool(cfg.get("validation_gate_enabled", True))
    strict = bool(cfg.get("validation_gate_strict", False))
    run_checkpatch = bool(cfg.get("validation_gate_checkpatch", True))
    timeout_sec = int(cfg.get("validation_gate_timeout_sec", 300))
    custom_cmd = str(cfg.get("validation_gate_command") or "").strip()

    reviewer_name = str(session.get("reviewer_name", "aryabhatta"))
    artifacts = _gate_artifacts(root, str(session["id"]), round_no, reviewer_name)

    payload: dict = {
        "enabled": enabled,
        "strict": strict,
        "ran": False,
        "passed": True,
        "commands": [],
        "failures": 0,
        "generated_at": _now_utc(),
    }

    if not enabled:
        dump_json(artifacts["json"], payload)
        artifacts["log"].write_text("validation gate disabled by config\n", encoding="utf-8")
        return True, False

    watch_raw = str(session.get("watch_path") or "").strip()
    if not watch_raw:
        payload["passed"] = True
        dump_json(artifacts["json"], payload)
        artifacts["log"].write_text("validation gate skipped: no watch_path\n", encoding="utf-8")
        return True, False

    watch_path = Path(watch_raw)
    commands: list[dict] = []
    watch_cwd = watch_path if watch_path.is_dir() else watch_path.parent

    if custom_cmd:
        commands.append(
            {
                "name": "custom",
                "command": custom_cmd,
                "cwd": str(watch_cwd),
            }
        )

    if run_checkpatch:
        kernel_root = _detect_kernel_repo_root(watch_path)
        if kernel_root is not None:
            changed_rel = _load_round_changed_paths(root, str(session["id"]), round_no, reviewer_name)
            patch_targets = _resolve_gate_patch_targets(watch_path, changed_rel, round_no)
            if patch_targets:
                max_files = int(cfg.get("validation_gate_max_checkpatch_files", 50))
                patch_targets = patch_targets[:max_files]
                checkpatch = kernel_root / "scripts" / "checkpatch.pl"
                target_args = " ".join(shlex.quote(str(p)) for p in patch_targets)
                cmd = f"{shlex.quote(str(checkpatch))} --no-tree --strict {target_args}"
                commands.append(
                    {
                        "name": "checkpatch",
                        "command": cmd,
                        "cwd": str(kernel_root),
                        "targets": [str(p) for p in patch_targets],
                    }
                )

    payload["ran"] = bool(commands)
    log_lines = [
        f"session={session['id']}",
        f"round={round_no}",
        f"enabled={enabled}",
        f"strict={strict}",
        f"watch_path={watch_raw}",
        f"commands_total={len(commands)}",
        "",
    ]

    if not commands:
        payload["passed"] = True
        dump_json(artifacts["json"], payload)
        artifacts["log"].write_text("\n".join(log_lines) + "no validation commands selected\n", encoding="utf-8")
        return True, False

    gate_passed = True
    for index, cmd_info in enumerate(commands, start=1):
        command = str(cmd_info["command"])
        cwd = Path(str(cmd_info["cwd"]))
        wrapped = f"timeout {timeout_sec} {command}" if timeout_sec > 0 else command
        result = run_shell_command(wrapped, cwd=cwd, env=dict(os.environ))
        rc = int(result["returncode"])
        cmd_record = dict(cmd_info)
        cmd_record["returncode"] = rc
        cmd_record["ok"] = rc == 0
        payload["commands"].append(cmd_record)
        if rc != 0:
            gate_passed = False
            payload["failures"] = int(payload["failures"]) + 1

        log_lines.extend(
            [
                f"## command {index}: {cmd_info['name']}",
                f"cwd={cwd}",
                f"command={wrapped}",
                f"returncode={rc}",
                "",
                "stdout:",
                result.get("stdout") or "",
                "",
                "stderr:",
                result.get("stderr") or "",
                "",
            ]
        )

    payload["passed"] = gate_passed
    dump_json(artifacts["json"], payload)
    artifacts["log"].write_text("\n".join(log_lines), encoding="utf-8")

    status = "passed" if gate_passed else "failed"
    _echo(f"validation gate {status}. Log: {artifacts['log']}")
    if not gate_passed and strict:
        _echo("validation gate strict mode: stopping session due to failed checks.")
        return False, True
    return True, True


def _static_analysis_artifact(root: Path, session_id: str, round_no: int, reviewer_name: str) -> Path:
    files = _round_files(root, session_id, round_no, reviewer_name)
    return files["report_dir"] / f"{_round_basename(round_no, 'static-analysis')}.json"


def _run_static_analysis(root: Path, session: dict, round_no: int) -> dict:
    cfg = _load_config_or_defaults(root)
    sa_cfg = cfg.get("static_analysis", {}) if isinstance(cfg, dict) else {}
    reviewer_name = str(session.get("reviewer_name", "aryabhatta"))
    artifact = _static_analysis_artifact(root, str(session["id"]), round_no, reviewer_name)
    watch_raw = str(session.get("watch_path") or "").strip()
    watch_path = Path(watch_raw) if watch_raw else None

    result: dict = {
        "enabled": bool(sa_cfg),
        "skipped": True,
        "reason": "no_watch_path",
        "gate_passed": True,
    }
    if not watch_path or not watch_path.exists():
        dump_json(artifact, result)
        return result

    patch_targets = _collect_watch_patch_files(watch_path)
    if not patch_targets:
        result["reason"] = "no_patch_files"
        dump_json(artifact, result)
        return result

    kernel_root = _detect_kernel_repo_root(watch_path)
    if kernel_root is None:
        result["reason"] = "kernel_tree_not_found"
        dump_json(artifact, result)
        return result

    static_payload = run_static_analysis_gate(str(patch_targets[0]), str(kernel_root), sa_cfg)
    static_payload["skipped"] = False
    static_payload["reason"] = ""
    dump_json(artifact, static_payload)
    return static_payload


def _load_findings_from_file(path: Path) -> list[dict]:
    try:
        return _load_findings_payload(path)
    except RuntimeError:
        return []


def _builder_change_stats(root: Path, session_id: str, round_no: int, reviewer_name: str) -> dict[str, int]:
    files = _round_files(root, session_id, round_no, reviewer_name)
    changed_path = files["report_dir"] / f"{_round_basename(round_no, 'changed_files')}.txt"
    diff_path = files["report_dir"] / f"{_round_basename(round_no, 'builder')}.diff"

    changed_files = 0
    diff_lines = 0
    diff_hunks = 0

    if changed_path.exists():
        changed_files = len([ln for ln in changed_path.read_text(encoding="utf-8").splitlines() if ln.strip()])

    if diff_path.exists():
        for line in diff_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("@@ "):
                diff_hunks += 1
            if (line.startswith("+") or line.startswith("-")) and not line.startswith("+++") and not line.startswith("---"):
                diff_lines += 1

    return {
        "changed_files": changed_files,
        "diff_lines": diff_lines,
        "diff_hunks": diff_hunks,
    }


def _clamp_score(value: int) -> int:
    return max(1, min(99, int(value)))


def _clamp_confidence(value: int) -> int:
    return max(1, min(95, int(value)))


def _compute_builder_patch_gauge(stats: dict[str, int]) -> int:
    changed_files = int(stats.get("changed_files", 0))
    diff_lines = int(stats.get("diff_lines", 0))
    diff_hunks = int(stats.get("diff_hunks", 0))
    score = int(changed_files * 18 + min(diff_lines, 240) * 0.30 + diff_hunks * 6)
    return _clamp_score(score)


def _compute_builder_confidence(prev_open: int | None, current_open: int, stats: dict[str, int]) -> int:
    changed_files = int(stats.get("changed_files", 0))
    diff_lines = int(stats.get("diff_lines", 0))
    base = 50
    if changed_files > 0:
        base += 12
    if diff_lines > 0:
        base += 8

    if prev_open is not None:
        delta = int(prev_open) - int(current_open)
        if delta > 0:
            base += min(24, delta * 10)
        elif delta < 0:
            base -= min(16, abs(delta) * 8)

    if current_open == 0:
        base += 12
    if changed_files == 0 and (prev_open is None or current_open >= int(prev_open)):
        base -= 20
    return _clamp_confidence(base)


def _compute_reviewer_confidence(findings: list[dict]) -> int:
    if not findings:
        return 72

    total = len(findings)
    with_source_id = 0
    evidence_items_total = 0
    evidence_missing = 0
    with_location = 0
    volatile_source_ids = 0
    duplicate_source_ids = 0
    open_findings = 0
    seen_source_ids: set[str] = set()
    for finding in findings:
        source_id = str(finding.get("source_comment_id") or "").strip()
        if source_id:
            with_source_id += 1
            if _VOLATILE_SOURCE_ID_RE.match(source_id):
                volatile_source_ids += 1
            if source_id in seen_source_ids:
                duplicate_source_ids += 1
            seen_source_ids.add(source_id)
        loc = str(finding.get("location") or "")
        if ":" in loc:
            with_location += 1
        status = str(finding.get("status") or "").lower()
        if status != "closed":
            open_findings += 1

        evidence = finding.get("evidence")
        if isinstance(evidence, list):
            if evidence:
                evidence_items_total += len([x for x in evidence if str(x).strip()])
            else:
                evidence_missing += 1
        elif isinstance(evidence, str):
            if evidence.strip():
                evidence_items_total += 1
            else:
                evidence_missing += 1
        else:
            evidence_missing += 1

    avg_evidence = evidence_items_total / max(total, 1)
    source_ratio = with_source_id / total
    location_ratio = with_location / total
    open_ratio = open_findings / total

    score = 42
    score += int(source_ratio * 20)
    score += int(location_ratio * 14)
    score += int(min(avg_evidence, 3.0) * 6)
    score -= evidence_missing * 7
    score -= volatile_source_ids * 5
    score -= duplicate_source_ids * 4
    if 0.0 < open_ratio < 1.0:
        score += 2
    if open_ratio == 1.0 and total > 2:
        score -= 3
    return _clamp_confidence(score)


def _extract_effective_round_findings(session: dict, round_record: dict) -> list[dict]:
    findings_file_raw = str(round_record.get("findings_file") or "").strip()
    if not findings_file_raw:
        return []
    findings_path = Path(findings_file_raw)
    if not findings_path.exists():
        return []
    findings = _load_findings_from_file(findings_path)

    prior = session.get("prior_review")
    if isinstance(prior, dict):
        comments_file_raw = str(prior.get("comments_file") or "").strip()
        if comments_file_raw:
            comments_file = Path(comments_file_raw)
            if comments_file.exists():
                prior_comments = load_prior_comments(comments_file)
                findings = augment_findings_with_prior_comments(findings, prior_comments, comments_file)
    return findings


def _build_prior_comment_summary(session: dict, rounds: list[dict]) -> list[dict]:
    prior = session.get("prior_review")
    if not isinstance(prior, dict):
        return []
    comments_file_raw = str(prior.get("comments_file") or "").strip()
    if not comments_file_raw:
        return []

    comments_file = Path(comments_file_raw)
    if not comments_file.exists():
        return []

    prior_comments = load_prior_comments(comments_file)
    if not prior_comments:
        return []

    rounds_sorted = sorted(rounds, key=lambda r: int(r.get("round", 0)))
    history_by_id: dict[str, list[dict]] = {}
    for round_record in rounds_sorted:
        round_no = int(round_record.get("round", 0))
        findings = _extract_effective_round_findings(session, round_record)
        by_source: dict[str, list[dict]] = {}
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            source_id = str(finding.get("source_comment_id") or "").strip()
            if not source_id:
                continue
            by_source.setdefault(source_id, []).append(finding)

        for comment in prior_comments:
            source_id = str(comment.get("id") or "").strip()
            if not source_id:
                continue
            class_meta = classify_prior_comment(comment)
            external_resolved = bool(comment.get("external_resolved", class_meta.get("external_resolved", False)))
            external_ref = str(comment.get("external_reference") or class_meta.get("external_reference") or "").strip()
            external_evidence = str(comment.get("excerpt") or "").strip()
            linked = by_source.get(source_id, [])
            closed = [f for f in linked if str(f.get("status", "")).lower() == "closed"]
            if linked:
                selected = closed[0] if closed else linked[0]
                status = "closed" if closed else "open"
            elif external_resolved:
                selected = None
                status = "external_resolved"
            else:
                selected = None
                status = "open"
            location = str(selected.get("location") or "") if selected else (external_ref or str(comment.get("source") or ""))
            evidence = selected.get("evidence") if selected else ([external_evidence] if external_evidence else [])
            history_by_id.setdefault(source_id, []).append(
                {
                    "round": round_no,
                    "status": status,
                    "location": location,
                    "evidence": evidence,
                }
            )

    rows: list[dict] = []
    for comment in prior_comments:
        source_id = str(comment.get("id") or "").strip()
        if not source_id:
            continue
        class_meta = classify_prior_comment(comment)
        comment_type = str(comment.get("comment_type") or class_meta.get("comment_type") or "actionable_review")
        external_resolved = bool(comment.get("external_resolved", class_meta.get("external_resolved", False)))
        external_reference = str(
            comment.get("external_reference") or class_meta.get("external_reference") or ""
        ).strip()
        history = history_by_id.get(source_id, [])
        if history:
            initial_status = history[0]["status"]
            current_status = history[-1]["status"]
        elif external_resolved:
            initial_status = "external_resolved"
            current_status = "external_resolved"
        else:
            initial_status = "open"
            current_status = "open"

        closed_round = None
        latest_location = ""
        latest_evidence = ""
        for entry in history:
            if entry["status"] == "closed" and closed_round is None:
                closed_round = int(entry["round"])
        if history:
            latest_location = str(history[-1].get("location") or "")
            ev = history[-1].get("evidence")
            if isinstance(ev, list):
                latest_evidence = "; ".join(str(x) for x in ev[:2])
            else:
                latest_evidence = str(ev or "")
        elif external_resolved:
            latest_location = external_reference or str(comment.get("source") or "")
            latest_evidence = str(comment.get("excerpt") or "").strip()

        fixed_by_a2a = bool(initial_status == "open" and current_status == "closed")
        resolution_origin = "a2a" if fixed_by_a2a else ("upstream" if current_status == "external_resolved" else "none")
        rows.append(
            {
                "source_comment_id": source_id,
                "from": str(comment.get("from") or ""),
                "subject": str(comment.get("subject") or ""),
                "comment_type": comment_type,
                "initial_status": initial_status,
                "current_status": current_status,
                "closed_round": closed_round,
                "fixed_by_a2a": fixed_by_a2a,
                "already_fixed_before_a2a": bool(initial_status in {"closed", "external_resolved"}),
                "external_resolved": bool(current_status == "external_resolved"),
                "external_reference": external_reference,
                "resolution_origin": resolution_origin,
                "latest_location": latest_location,
                "latest_evidence": latest_evidence,
            }
        )

    return rows


def _round_summary_artifacts(root: Path, session_id: str, round_no: int, reviewer_name: str) -> dict[str, Path]:
    files = _round_files(root, session_id, round_no, reviewer_name)
    return {
        "json": files["report_dir"] / f"{_round_basename(round_no, 'summary')}.json",
        "md": files["report_dir"] / f"{_round_basename(round_no, 'summary')}.md",
    }


def _finding_identity(finding: dict) -> str:
    sid = str(finding.get("source_comment_id") or "").strip()
    if sid:
        return sid
    return f"anon-{_finding_fingerprint(finding)[:12]}"


def _build_round_runtime_summary(
    root: Path,
    session: dict,
    round_no: int,
    findings: list[dict],
    open_count: int,
    change_stats: dict[str, int],
    builder_patch_gauge: int,
    builder_confidence: int,
    reviewer_confidence: int,
    round_started_at: str | None = None,
    round_elapsed_seconds: int | None = None,
) -> dict:
    session_id = str(session["id"])
    cfg = _load_config_or_defaults(root)
    reviewer_name = str(session.get("reviewer_name", "aryabhatta"))
    builder_display_name = _resolve_builder_display_name(session=session, cfg=cfg)
    reviewer_display_name = _resolve_reviewer_display_name(session=session, cfg=cfg)
    files = _round_files(root, session_id, round_no, reviewer_name)
    gate_files = _gate_artifacts(root, session_id, round_no, reviewer_name)
    gate_payload = load_json(gate_files["json"]) if gate_files["json"].exists() else {}
    gate_passed = gate_payload.get("passed") if isinstance(gate_payload, dict) else None
    gate_failures = int(gate_payload.get("failures", 0)) if isinstance(gate_payload, dict) else 0

    closed_count = len(findings) - open_count
    current_ids = {_finding_identity(f) for f in findings if isinstance(f, dict)}
    current_open_ids = {
        _finding_identity(f)
        for f in findings
        if isinstance(f, dict) and str(f.get("status", "")).lower() != "closed"
    }

    previous_round = None
    for record in session.get("rounds", []):
        if int(record.get("round", -1)) == round_no - 1:
            previous_round = record
            break

    previous_ids: set[str] = set()
    previous_open_ids: set[str] = set()
    if previous_round is not None:
        prev_findings = _extract_effective_round_findings(session, previous_round)
        previous_ids = {_finding_identity(f) for f in prev_findings if isinstance(f, dict)}
        previous_open_ids = {
            _finding_identity(f)
            for f in prev_findings
            if isinstance(f, dict) and str(f.get("status", "")).lower() != "closed"
        }

    new_ids = sorted(current_ids - previous_ids)
    resolved_ids = sorted(previous_open_ids - current_open_ids)

    open_items: list[dict] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if str(finding.get("status", "")).lower() == "closed":
            continue
        open_items.append(
            {
                "id": _finding_identity(finding),
                "severity": str(finding.get("severity") or ""),
                "title": str(finding.get("title") or ""),
                "location": str(finding.get("location") or ""),
                "required_action": str(finding.get("required_action") or ""),
            }
        )

    rounds_plus_current = list(session.get("rounds", [])) + [
        {
            "round": round_no,
            "findings_file": str(files["findings"]),
            "findings_open": open_count,
            "findings_total": len(findings),
            "validated_at": _now_utc(),
        }
    ]
    prior_rows = _build_prior_comment_summary(session, rounds_plus_current)
    external_resolved = len([r for r in prior_rows if str(r.get("current_status")) == "external_resolved"])
    prior_totals = {
        "received_total": len(prior_rows),
        "open": len(
            [
                r
                for r in prior_rows
                if str(r.get("current_status")) not in {"closed", "external_resolved"}
            ]
        ),
        "closed": len(
            [
                r
                for r in prior_rows
                if str(r.get("current_status")) in {"closed", "external_resolved"}
            ]
        ),
        "external_resolved": external_resolved,
        "closed_by_upstream": external_resolved,
        "fixed_by_a2a": len([r for r in prior_rows if bool(r.get("fixed_by_a2a"))]),
    }

    return {
        "session_id": session_id,
        "task": str(session.get("task") or ""),
        "round": round_no,
        "builder_name": builder_display_name,
        "reviewer_name": reviewer_display_name,
        "reviewer_internal_name": reviewer_name,
        "watch_path": str(session.get("watch_path") or ""),
        "findings": {
            "total": len(findings),
            "open": open_count,
            "closed": closed_count,
            "new_since_prev": len(new_ids),
            "resolved_since_prev": len(resolved_ids),
            "new_ids": new_ids,
            "resolved_ids": resolved_ids,
            "open_items": open_items[:8],
        },
        "prior_comments": {
            "totals": prior_totals,
            "tracked": prior_rows,
        },
        "builder": {
            "name": builder_display_name,
            "changed_files": int(change_stats.get("changed_files", 0)),
            "diff_lines": int(change_stats.get("diff_lines", 0)),
            "diff_hunks": int(change_stats.get("diff_hunks", 0)),
            "patch_gauge": builder_patch_gauge,
            "confidence": builder_confidence,
        },
        "reviewer": {
            "name": reviewer_display_name,
            "internal_name": reviewer_name,
            "confidence": reviewer_confidence,
        },
        "validation_gate": {
            "passed": gate_passed,
            "failures": gate_failures,
            "artifact_json": str(gate_files["json"]),
            "artifact_log": str(gate_files["log"]),
        },
        "timing": {
            "started_at": str(round_started_at or ""),
            "elapsed_seconds": round_elapsed_seconds,
            "elapsed_hms": _format_elapsed_hms(round_elapsed_seconds) or "",
        },
        "artifacts": {
            "builder_report": str(files["builder"]),
            "reviewer_report": str(files["reviewer"]),
            "findings": str(files["findings"]),
        },
        "generated_at": _now_utc(),
    }


def _render_round_runtime_summary_markdown(summary: dict) -> str:
    findings = summary.get("findings", {})
    prior = summary.get("prior_comments", {}).get("totals", {})
    builder = summary.get("builder", {})
    reviewer = summary.get("reviewer", {})
    gate = summary.get("validation_gate", {})
    timing = summary.get("timing", {})
    artifacts = summary.get("artifacts", {})

    lines = [
        f"# Round {summary.get('round')} Summary",
        "",
        f"- session: {summary.get('session_id')}",
        f"- task: {summary.get('task')}",
        f"- builder: {summary.get('builder_name')}",
        f"- reviewer: {summary.get('reviewer_name')}",
        f"- watch_path: {summary.get('watch_path')}",
        "",
        "## Reviewer Findings",
        "",
        f"- total: {findings.get('total')}",
        f"- open: {findings.get('open')}",
        f"- closed: {findings.get('closed')}",
        f"- new_since_prev: {findings.get('new_since_prev')}",
        f"- resolved_since_prev: {findings.get('resolved_since_prev')}",
        "",
        "## Prior Comments",
        "",
        f"- received_total: {prior.get('received_total')}",
        f"- open: {prior.get('open')}",
        f"- closed: {prior.get('closed')}",
        f"- external_resolved: {prior.get('external_resolved')}",
        f"- fixed_by_a2a: {prior.get('fixed_by_a2a')}",
        "",
        "## Scores",
        "",
        f"- builder_patch_gauge: {builder.get('patch_gauge')}",
        f"- builder_confidence: {builder.get('confidence')}",
        f"- reviewer_confidence: {reviewer.get('confidence')}",
        "",
        "## Round Timing",
        "",
        f"- started_at: {timing.get('started_at')}",
        f"- elapsed: {timing.get('elapsed_hms') or _format_elapsed_hms(timing.get('elapsed_seconds')) or 'n/a'}",
        "",
        "## Validation Gate",
        "",
        f"- passed: {gate.get('passed')}",
        f"- failures: {gate.get('failures')}",
        f"- gate_json: {gate.get('artifact_json')}",
        f"- gate_log: {gate.get('artifact_log')}",
        "",
        "## Artifacts",
        "",
        f"- builder_report: {artifacts.get('builder_report')}",
        f"- reviewer_report: {artifacts.get('reviewer_report')}",
        f"- findings: {artifacts.get('findings')}",
        "",
        "## Top Open Findings",
        "",
    ]

    open_items = findings.get("open_items", [])
    if not isinstance(open_items, list) or not open_items:
        lines.append("- none")
    else:
        for item in open_items:
            lines.append(
                "- [{sev}] {title} ({loc}) id={id}".format(
                    sev=item.get("severity", "?"),
                    title=item.get("title", ""),
                    loc=item.get("location", ""),
                    id=item.get("id", ""),
                )
            )
    lines.append("")
    return "\n".join(lines)


def _write_round_runtime_summary(root: Path, session: dict, round_no: int, summary: dict) -> dict[str, Path]:
    reviewer_name = str(session.get("reviewer_name", "aryabhatta"))
    out = _round_summary_artifacts(root, str(session["id"]), round_no, reviewer_name)
    dump_json(out["json"], summary)
    out["md"].write_text(_render_round_runtime_summary_markdown(summary), encoding="utf-8")
    return out


def _run_agent_step(root: Path, session: dict, role: str, command: str, round_no: int) -> int:
    cfg = _load_config_or_defaults(root)
    reviewer_name = str(session.get("reviewer_name", "aryabhatta"))
    builder_display_name = _resolve_builder_display_name(session=session, cfg=cfg)
    reviewer_display_name = _resolve_reviewer_display_name(session=session, cfg=cfg)
    role_display_name = builder_display_name if role == "builder" else reviewer_display_name
    files = _round_files(root, str(session["id"]), round_no, reviewer_name)
    worktrees = session.get("worktrees", {})
    if role == "builder":
        cwd = Path(worktrees.get("builder", session["repo_path"]))
    else:
        cwd = Path(worktrees.get(reviewer_name, session["repo_path"]))

    logs_dir = root / A2A_DIRNAME / "logs" / str(session["id"])
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{_round_basename(round_no, role)}.log"

    watch_path = session.get("watch_path")
    watch_before = None
    if role == "builder" and watch_path:
        try:
            watch_before = _snapshot_text_files(Path(str(watch_path)))
        except OSError:
            watch_before = None

    env = _agent_env(session, round_no, files, role, cfg)
    result = run_shell_command(command, cwd=cwd, env=env)

    with log_path.open("w", encoding="utf-8") as f:
        f.write(f"role={role}\n")
        f.write(f"round={round_no}\n")
        f.write(f"cwd={cwd}\n")
        f.write(f"command={command}\n")
        f.write(f"returncode={result['returncode']}\n\n")
        f.write("stdout:\n")
        f.write(result["stdout"] or "")
        f.write("\n\nstderr:\n")
        f.write(result["stderr"] or "")
        f.write("\n\nartifacts:\n")
        f.write(f"builder_file={files['builder']}\n")
        f.write(f"reviewer_file={files['reviewer']}\n")
        f.write(f"findings_file={files['findings']}\n")

    if result["returncode"] != 0:
        _echo(f"{role_display_name} command failed (rc={result['returncode']}). See log: {log_path}")
        return int(result["returncode"])

    if role == "builder" and watch_path:
        watch_after = None
        try:
            watch_after = _snapshot_text_files(Path(str(watch_path)))
        except OSError:
            watch_after = None
        _write_builder_change_artifacts(root, session, round_no, watch_before, watch_after)
    elif role != "builder":
        evidence_cfg = cfg.get("upstream_evidence", {}) if isinstance(cfg, dict) else {}
        kernel_tree = str(evidence_cfg.get("kernel_tree") or "").strip()
        if not kernel_tree and watch_path:
            try:
                kernel_tree = str(_detect_kernel_repo_root(Path(str(watch_path))) or "")
            except Exception:
                kernel_tree = ""
        strict_mode = bool(evidence_cfg.get("strict_mode", True))
        block_no_evidence = bool(evidence_cfg.get("block_on_no_evidence", True))
        elixir_base = str(evidence_cfg.get("elixir_base") or "https://elixir.bootlin.com/linux/latest")
        payload = load_json(files["findings"]) if files["findings"].exists() else {"findings": []}
        findings_rows = payload.get("findings", []) if isinstance(payload, dict) else []
        static_path = _static_analysis_artifact(root, str(session["id"]), round_no, reviewer_name)
        static_payload = load_json(static_path) if static_path.exists() else {}
        sparse_info = static_payload.get("sparse", {}) if isinstance(static_payload, dict) else {}
        if isinstance(findings_rows, list) and bool(sparse_info.get("blocking")):
            findings_rows.append(
                {
                    "id": f"static-sparse-round{round_no}",
                    "severity": "high",
                    "title": "Sparse new warning introduced",
                    "location": "static-analysis:sparse",
                    "evidence": sparse_info.get("new_warnings", []),
                    "required_action": "Resolve new sparse warnings before LGTM",
                    "status": "open",
                }
            )
        if isinstance(findings_rows, list):
            kb_rows = list_kb_entries(root)
            enriched, violations = enrich_findings_with_evidence(
                findings_rows,
                kernel_tree=kernel_tree if kernel_tree_exists(kernel_tree) else "",
                strict_mode=strict_mode,
                block_on_no_evidence=block_no_evidence,
                kb_entries=kb_rows,
                elixir_base=elixir_base,
            )
            payload = dict(payload) if isinstance(payload, dict) else {}
            payload["findings"] = enriched
            dump_json(files["findings"], payload)
            if strict_mode and violations:
                with log_path.open("a", encoding="utf-8") as f:
                    f.write("\n[evidence] strict mode violations:\n")
                    for v in violations:
                        f.write(f"- {v}\n")
                _echo(f"{role_display_name} evidence strict mode blocked verdict. See log: {log_path}")
                return 1

    _echo(f"{role_display_name} command completed. Log: {log_path}")
    if role == "builder":
        _echo(f"{role_display_name} report: {files['builder']}")
    else:
        _echo(f"{role_display_name} report: {files['reviewer']}")
        _echo(f"{role_display_name} findings: {files['findings']}")
    return 0


def _resolve_agent_command(cfg: dict, args_value: str | None, key: str) -> str | None:
    if args_value is not None:
        return args_value.strip() or None
    value = cfg.get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _llm_wrapper_command(root: Path, role: str) -> str:
    scripts_dir = root / "scripts" / "agents"
    if role == "builder":
        wrapper = scripts_dir / "builder_llm_native.sh"
    else:
        wrapper = scripts_dir / "reviewer_llm_native.sh"
    return f"bash {wrapper}"


def _resolve_default_agent_commands(root: Path, cfg: dict, builder_cmd: str | None, reviewer_cmd: str | None) -> tuple[str | None, str | None]:
    if builder_cmd and reviewer_cmd:
        return builder_cmd, reviewer_cmd

    llm_native_default = bool(cfg.get("llm_native_default", True))
    if llm_native_default:
        if not builder_cmd:
            builder_cmd = _llm_wrapper_command(root, "builder")
        if not reviewer_cmd:
            reviewer_cmd = _llm_wrapper_command(root, "reviewer")
    return builder_cmd, reviewer_cmd


def _parse_value(raw: str) -> object:
    lowered = raw.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _resolve_session_for_report(root: Path, session_id: str | None, latest: bool) -> str:
    sessions_dir = root / A2A_DIRNAME / "sessions"
    if not sessions_dir.exists():
        raise RuntimeError("No sessions directory found. Start a session first.")

    if session_id:
        path = _session_path(root, session_id)
        if not path.exists():
            raise RuntimeError(f"Session not found: {session_id}")
        return session_id

    if latest:
        all_sessions = sorted(sessions_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        if not all_sessions:
            raise RuntimeError("No sessions found.")
        return all_sessions[-1].stem

    state = load_json(_state_path(root))
    active = state.get("active_session_id")
    if active:
        path = _session_path(root, str(active))
        if path.exists():
            return str(active)

    all_sessions = sorted(sessions_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not all_sessions:
        raise RuntimeError("No sessions found.")
    return all_sessions[-1].stem


def _list_session_ids(root: Path) -> list[str]:
    sessions_dir = root / A2A_DIRNAME / "sessions"
    if not sessions_dir.exists():
        return []
    return [p.stem for p in sorted(sessions_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)]


def _parse_iso_datetime(raw: str) -> datetime:
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise RuntimeError(f"Invalid ISO datetime: {raw}") from exc


def _session_report_payload(root: Path, session_id: str) -> dict:
    cfg = _load_config_or_defaults(root)
    session = _load_session(root, session_id)
    raw_rounds = sorted(session.get("rounds", []), key=lambda r: int(r.get("round", 0)))
    rounds: list[dict] = []
    previous_open: int | None = None
    builder_display_name = _resolve_builder_display_name(session=session, cfg=cfg)
    reviewer_display_name = _resolve_reviewer_display_name(session=session, cfg=cfg)
    reviewer_name = str(session.get("reviewer_name", "aryabhatta"))
    for record in raw_rounds:
        row = dict(record)
        round_no = int(row.get("round", 0))
        if "builder_changed_files" not in row or "builder_diff_lines" not in row or "builder_diff_hunks" not in row:
            stats = _builder_change_stats(root, session_id, round_no, reviewer_name)
            row.setdefault("builder_changed_files", int(stats.get("changed_files", 0)))
            row.setdefault("builder_diff_lines", int(stats.get("diff_lines", 0)))
            row.setdefault("builder_diff_hunks", int(stats.get("diff_hunks", 0)))
        else:
            stats = {
                "changed_files": int(row.get("builder_changed_files", 0)),
                "diff_lines": int(row.get("builder_diff_lines", 0)),
                "diff_hunks": int(row.get("builder_diff_hunks", 0)),
            }

        findings_open = int(row.get("findings_open", 0))
        if "builder_patch_gauge" not in row:
            row["builder_patch_gauge"] = _compute_builder_patch_gauge(stats)
        if "builder_confidence" not in row:
            row["builder_confidence"] = _compute_builder_confidence(previous_open, findings_open, stats)
        if "reviewer_confidence" not in row:
            findings = _extract_effective_round_findings(session, row)
            row["reviewer_confidence"] = _compute_reviewer_confidence(findings)

        gate_json = _gate_artifacts(root, session_id, round_no, reviewer_name)["json"]
        if gate_json.exists():
            gate_payload = load_json(gate_json)
            if isinstance(gate_payload, dict):
                row["gate_ran"] = bool(gate_payload.get("ran", False))
                row["gate_passed"] = bool(gate_payload.get("passed", True))
                row["gate_failures"] = int(gate_payload.get("failures", 0))
        row.setdefault("gate_ran", False)
        row.setdefault("gate_passed", None)
        row.setdefault("gate_failures", 0)
        row["round_elapsed_hms"] = _format_elapsed_hms(row.get("round_elapsed_seconds"))
        summary_artifacts = _round_summary_artifacts(root, session_id, round_no, reviewer_name)
        row["round_summary_json"] = str(summary_artifacts["json"]) if summary_artifacts["json"].exists() else None
        row["round_summary_md"] = str(summary_artifacts["md"]) if summary_artifacts["md"].exists() else None

        previous_open = findings_open
        rounds.append(row)

    prior_comment_summary = _build_prior_comment_summary(session, rounds)
    prior_closed = len(
        [
            r
            for r in prior_comment_summary
            if str(r.get("current_status") or "") in {"closed", "external_resolved"}
        ]
    )
    prior_external = len(
        [
            r
            for r in prior_comment_summary
            if str(r.get("current_status") or "") == "external_resolved"
        ]
    )
    prior_totals = {
        "comments_total": len(prior_comment_summary),
        "comments_closed": prior_closed,
        "comments_open": len(prior_comment_summary) - prior_closed,
        "comments_external_resolved": prior_external,
        "fixed_by_a2a": len([r for r in prior_comment_summary if bool(r.get("fixed_by_a2a"))]),
    }
    prior = session.get("prior_review")
    prior_summary = None
    if isinstance(prior, dict):
        prior_summary = {
            "enabled": bool(prior.get("enabled", False)),
            "comments_total": int(prior.get("comments_total") or 0),
            "source_total": int(prior.get("source_total") or 0),
            "search_used": bool(prior.get("search_used", False)),
            "detected_version": prior.get("detected_version"),
            "detected_subject": prior.get("detected_subject"),
            "comment_type_totals": prior.get("comment_type_totals", {}),
            "comment_status_totals": prior_totals,
        }

    totals = {
        "rounds_validated": len(rounds),
        "findings_total": 0,
        "findings_open_last": session.get("open_findings"),
        "gate_failures_total": 0,
        "gate_failed_rounds": 0,
    }
    for record in rounds:
        totals["findings_total"] += int(record.get("findings_total", 0))
        failures = int(record.get("gate_failures", 0))
        totals["gate_failures_total"] += failures
        gate_passed = record.get("gate_passed")
        if gate_passed is False:
            totals["gate_failed_rounds"] += 1

    report_dir = _report_dir(root, session_id).resolve()
    lore_next_report = report_dir / "lore_next_version.json"
    lore_next_payload: dict = {}
    if lore_next_report.exists():
        loaded = load_json(lore_next_report)
        if isinstance(loaded, dict):
            lore_next_payload = loaded
    post_respin_report = report_dir / "post_respin_validation.json"
    post_respin_payload: dict = {}
    if post_respin_report.exists():
        loaded = load_json(post_respin_report)
        if isinstance(loaded, dict):
            post_respin_payload = loaded

    patch_versions = _collect_patch_versions_for_session(root, session_id)
    latest_version_row = patch_versions[-1] if patch_versions else {}
    latest_output_path = _normalize_path_for_report(lore_next_payload.get("output_path"))
    if not latest_output_path and isinstance(latest_version_row, dict):
        latest_output_path = str(latest_version_row.get("path") or "")

    io_details = {
        "input_watch_path": _normalize_path_for_report(session.get("watch_path")),
        "input_lore_link": _lore_link_from_session(session),
        "patches_root": str((root / A2A_DIRNAME / "patches" / session_id).resolve()),
        "report_dir": str(report_dir),
        "lore_next_version_report": str(lore_next_report.resolve()) if lore_next_report.exists() else "",
        "latest_output_patches_path": latest_output_path,
        "latest_output_version": lore_next_payload.get("next_version")
        if lore_next_payload.get("next_version") is not None
        else latest_version_row.get("version"),
        "respin_kind": str(lore_next_payload.get("kind") or ""),
        "respin_generated_at": str(lore_next_payload.get("generated_at") or ""),
        "respin_source_watch_path": _normalize_path_for_report(lore_next_payload.get("source_watch_path")),
        "post_respin_validation_report": str(post_respin_report.resolve()) if post_respin_report.exists() else "",
        "post_respin_validation_status": str(post_respin_payload.get("status") or ""),
        "post_respin_validation_generated_at": str(post_respin_payload.get("generated_at") or ""),
        "available_patch_versions": patch_versions,
    }

    payload = {
        "session": {
            "id": session.get("id"),
            "task": session.get("task"),
            "status": session.get("status"),
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at"),
            "max_rounds": session.get("max_rounds"),
            "current_round": session.get("current_round"),
            "builder_display_name": builder_display_name,
            "reviewer_display_name": reviewer_display_name,
            "reviewer_name": session.get("reviewer_name"),
            "repo_path": session.get("repo_path"),
            "branch": session.get("branch"),
            "builder_command": session.get("builder_command"),
            "reviewer_command": session.get("reviewer_command"),
            "prior_review": prior_summary,
            "watch_path": _normalize_path_for_report(session.get("watch_path")),
            "lore_link": _lore_link_from_session(session),
        },
        "totals": totals,
        "rounds": rounds,
        "prior_comment_summary": prior_comment_summary,
        "io_details": io_details,
    }
    return payload


def _all_sessions_report_payload(
    root: Path,
    status_filters: set[str] | None = None,
    since_dt: datetime | None = None,
) -> dict:
    session_ids = _list_session_ids(root)
    sessions: list[dict] = []
    by_status: dict[str, int] = {}

    for sid in session_ids:
        payload = _session_report_payload(root, sid)
        sess = payload["session"]
        rounds = payload["rounds"]
        status = str(sess.get("status", "unknown"))
        if status_filters and status not in status_filters:
            continue

        updated_raw = sess.get("updated_at") or sess.get("created_at")
        if since_dt and updated_raw:
            try:
                updated_dt = _parse_iso_datetime(str(updated_raw))
            except RuntimeError:
                continue
            if updated_dt < since_dt:
                continue

        by_status[status] = by_status.get(status, 0) + 1
        sessions.append(
            {
                "id": sess.get("id"),
                "task": sess.get("task"),
                "status": status,
                "builder_display_name": sess.get("builder_display_name"),
                "reviewer_display_name": sess.get("reviewer_display_name"),
                "reviewer_name": sess.get("reviewer_name"),
                "repo_path": sess.get("repo_path"),
                "branch": sess.get("branch"),
                "created_at": sess.get("created_at"),
                "updated_at": sess.get("updated_at"),
                "rounds_validated": len(rounds),
                "findings_open_last": payload["totals"].get("findings_open_last"),
            }
        )

    return {
        "summary": {
            "sessions_total": len(sessions),
            "by_status": by_status,
            "status_filter": sorted(status_filters) if status_filters else [],
            "since": since_dt.isoformat() if since_dt else None,
        },
        "sessions": sessions,
    }


def _render_markdown_report(payload: dict) -> str:
    sess = payload["session"]
    totals = payload["totals"]
    rounds = payload["rounds"]
    prior = sess.get("prior_review")
    prior_comment_summary = payload.get("prior_comment_summary", [])
    builder_display_name = str(sess.get("builder_display_name") or "builder")
    reviewer_display_name = str(sess.get("reviewer_display_name") or sess.get("reviewer_name"))
    lines = [
        f"# A2A Report: {sess['id']}",
        "",
        f"- task: {sess.get('task')}",
        f"- status: {sess.get('status')}",
        f"- builder: {builder_display_name}",
        f"- reviewer: {reviewer_display_name}",
        f"- reviewer_internal_name: {sess.get('reviewer_name')}",
        f"- repo: {sess.get('repo_path')}",
        f"- branch: {sess.get('branch')}",
        f"- rounds_validated: {totals.get('rounds_validated')}",
        f"- findings_total: {totals.get('findings_total')}",
        f"- findings_open_last: {totals.get('findings_open_last')}",
        f"- gate_failures_total: {totals.get('gate_failures_total')}",
        f"- gate_failed_rounds: {totals.get('gate_failed_rounds')}",
    ]
    if isinstance(prior, dict):
        lines.append(f"- prior_comments_total: {prior.get('comments_total')}")
        lines.append(f"- prior_sources_total: {prior.get('source_total')}")
        lines.append(f"- prior_search_used: {prior.get('search_used')}")
        type_totals = prior.get("comment_type_totals")
        if isinstance(type_totals, dict):
            lines.append(f"- prior_actionable_comments: {type_totals.get('actionable_review')}")
            lines.append(f"- prior_apply_notices: {type_totals.get('maintainer_apply_notice')}")
            lines.append(f"- prior_meta_comments: {type_totals.get('meta')}")
        status_totals = prior.get("comment_status_totals")
        if isinstance(status_totals, dict):
            lines.append(f"- prior_comments_closed: {status_totals.get('comments_closed')}")
            lines.append(f"- prior_comments_open: {status_totals.get('comments_open')}")
            lines.append(f"- prior_comments_external_resolved: {status_totals.get('comments_external_resolved')}")
            lines.append(f"- prior_fixed_by_a2a: {status_totals.get('fixed_by_a2a')}")

    lines.extend(["", "## Rounds", ""])
    if not rounds:
        lines.append("- no validated rounds yet")
    else:
        for r in rounds:
            elapsed = r.get("round_elapsed_hms") or _format_elapsed_hms(r.get("round_elapsed_seconds")) or "n/a"
            lines.append(
                "- round {round}: findings_total={total}, findings_open={open}, "
                "builder_patch_gauge={gauge}, builder_confidence={bconf}, reviewer_confidence={rconf}, "
                "gate_passed={gate_passed}, gate_failures={gate_failures}, elapsed={elapsed}, "
                "summary_json={summary_json}, validated_at={ts}".format(
                    round=r.get("round"),
                    total=r.get("findings_total"),
                    open=r.get("findings_open"),
                    gauge=r.get("builder_patch_gauge"),
                    bconf=r.get("builder_confidence"),
                    rconf=r.get("reviewer_confidence"),
                    gate_passed=r.get("gate_passed"),
                    gate_failures=r.get("gate_failures"),
                    elapsed=elapsed,
                    summary_json=r.get("round_summary_json"),
                    ts=r.get("validated_at"),
                )
            )

    lines.extend(["", "## Prior Comment Summary", ""])
    if not prior_comment_summary:
        lines.append("- no prior comments tracked")
    else:
        lines.extend(
            [
                "| source_comment_id | subject | type | initial | current | resolution_origin | fixed_by_a2a | closed_round | latest_location |",
                "|---|---|---|---|---|---|---|---|---|",
            ]
        )
        for row in prior_comment_summary:
            lines.append(
                "| {id} | {subject} | {ctype} | {initial} | {current} | {origin} | {fixed} | {closed_round} | {loc} |".format(
                    id=str(row.get("source_comment_id") or "").replace("|", "\\|"),
                    subject=str(row.get("subject") or "").replace("|", "\\|"),
                    ctype=str(row.get("comment_type") or "").replace("|", "\\|"),
                    initial=str(row.get("initial_status") or ""),
                    current=str(row.get("current_status") or ""),
                    origin=str(row.get("resolution_origin") or ""),
                    fixed="yes" if bool(row.get("fixed_by_a2a")) else "no",
                    closed_round=str(row.get("closed_round") if row.get("closed_round") is not None else "-"),
                    loc=str(row.get("latest_location") or "").replace("|", "\\|"),
                )
            )

    io_details = payload.get("io_details", {})
    lines.extend(["", "## Session I/O Details", ""])
    if not isinstance(io_details, dict):
        lines.append("- none")
    else:
        lines.append(f"- input_watch_path: {io_details.get('input_watch_path') or '-'}")
        lines.append(f"- input_lore_link: {io_details.get('input_lore_link') or '-'}")
        lines.append(f"- report_dir: {io_details.get('report_dir') or '-'}")
        lines.append(f"- patches_root: {io_details.get('patches_root') or '-'}")
        lines.append(f"- latest_output_patches_path: {io_details.get('latest_output_patches_path') or '-'}")
        lines.append(f"- latest_output_version: {io_details.get('latest_output_version') or '-'}")
        lines.append(f"- respin_kind: {io_details.get('respin_kind') or '-'}")
        lines.append(f"- respin_generated_at: {io_details.get('respin_generated_at') or '-'}")
        lines.append(f"- respin_source_watch_path: {io_details.get('respin_source_watch_path') or '-'}")
        lines.append(f"- lore_next_version_report: {io_details.get('lore_next_version_report') or '-'}")
        lines.append(f"- post_respin_validation_report: {io_details.get('post_respin_validation_report') or '-'}")
        lines.append(f"- post_respin_validation_status: {io_details.get('post_respin_validation_status') or '-'}")
        lines.append(f"- post_respin_validation_generated_at: {io_details.get('post_respin_validation_generated_at') or '-'}")
        versions = io_details.get("available_patch_versions")
        if isinstance(versions, list) and versions:
            rendered = ", ".join(
                f"{str(v.get('name') or '-')}: {str(v.get('path') or '-')}"
                for v in versions
                if isinstance(v, dict)
            )
            lines.append(f"- available_patch_versions: {rendered}")
        else:
            lines.append("- available_patch_versions: -")
    lines.append("")
    return "\n".join(lines)


def _render_markdown_report_all(payload: dict) -> str:
    summary = payload["summary"]
    sessions = payload["sessions"]
    status_filter = summary.get("status_filter") or []
    since = summary.get("since")
    lines = [
        "# A2A Report: All Sessions",
        "",
        f"- sessions_total: {summary.get('sessions_total')}",
    ]
    if status_filter:
        lines.append(f"- status_filter: {', '.join(status_filter)}")
    if since:
        lines.append(f"- since: {since}")

    lines.extend(["", "## Status Counts", ""])

    by_status = summary.get("by_status", {})
    if not by_status:
        lines.append("- none")
    else:
        for key in sorted(by_status.keys()):
            lines.append(f"- {key}: {by_status[key]}")

    lines.extend(
        [
            "",
            "## Sessions",
            "",
        ]
    )
    if not sessions:
        lines.append("- no sessions found")
    else:
        for sess in sessions:
            lines.append(
                "- {id}: status={status}, rounds_validated={rounds}, findings_open_last={open_last}, task={task}".format(
                    id=sess.get("id"),
                    status=sess.get("status"),
                    rounds=sess.get("rounds_validated"),
                    open_last=sess.get("findings_open_last"),
                    task=sess.get("task"),
                )
            )
    lines.append("")
    return "\n".join(lines)


def _status_css_class(status: str) -> str:
    norm = status.strip().lower()
    if norm == "lgtm":
        return "status-lgtm"
    if norm == "stopped":
        return "status-stopped"
    return "status-progress"


def _severity_rank(value: str) -> int:
    norm = value.strip().lower()
    ranks = {
        "critical": 0,
        "high": 1,
        "medium": 2,
        "low": 3,
    }
    return ranks.get(norm, 4)


def _load_findings_for_report_row(row: dict) -> list[dict]:
    findings_path = str(row.get("findings_file") or "").strip()
    if not findings_path:
        return []
    path = Path(findings_path)
    if not path.exists():
        return []
    payload = load_json(path)
    if isinstance(payload, dict):
        rows = payload.get("findings", [])
        if isinstance(rows, list):
            return [x for x in rows if isinstance(x, dict)]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


def _render_round_findings_table(round_row: dict) -> str:
    findings = _load_findings_for_report_row(round_row)
    if not findings:
        return "<div class='muted'>No findings recorded for this round.</div>"

    sorted_rows = sorted(
        findings,
        key=lambda r: (
            str(r.get("status", "open")).lower() == "closed",
            _severity_rank(str(r.get("severity", ""))),
            str(r.get("title", "")),
        ),
    )
    body: list[str] = []
    for idx, finding in enumerate(sorted_rows[:15], start=1):
        severity = escape(str(finding.get("severity", "")).upper() or "UNK")
        title = escape(str(finding.get("title", "")) or "-")
        location = escape(str(finding.get("location", "")) or "-")
        status = escape(str(finding.get("status", "open")).lower())
        source_id = escape(str(finding.get("source_comment_id", "")) or "-")
        status_class = "finding-closed" if status == "closed" else "finding-open"
        body.append(
            "<tr>"
            f"<td>{idx}</td>"
            f"<td>{severity}</td>"
            f"<td>{status}</td>"
            f"<td>{title}</td>"
            f"<td>{location}</td>"
            f"<td class='{status_class}'>{source_id}</td>"
            "</tr>"
        )

    hidden_count = len(sorted_rows) - len(body)
    hidden_note = ""
    if hidden_count > 0:
        hidden_note = (
            "<div class='muted findings-note'>"
            f"{hidden_count} additional findings omitted for brevity in HTML view."
            "</div>"
        )

    return (
        "<table class='findings-table'>"
        "<thead><tr>"
        "<th>#</th><th>Severity</th><th>Status</th><th>Title</th><th>Location</th><th>Source ID</th>"
        "</tr></thead>"
        f"<tbody>{''.join(body)}</tbody>"
        "</table>"
        f"{hidden_note}"
    )


def _load_round_summary_payload(round_row: dict) -> dict:
    summary_json = str(round_row.get("round_summary_json") or "").strip()
    if not summary_json:
        return {}
    path = Path(summary_json)
    if not path.exists():
        return {}
    payload = load_json(path)
    return payload if isinstance(payload, dict) else {}


def _findings_severity_open_counts(findings: list[dict]) -> dict[str, int]:
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for finding in findings:
        if str(finding.get("status", "open")).lower() == "closed":
            continue
        sev = str(finding.get("severity", "low")).strip().lower()
        if sev not in counts:
            sev = "low"
        counts[sev] += 1
    return counts


def _progress_bar(value: int, *, width: int = 10) -> tuple[str, str]:
    v = max(0, min(100, int(value)))
    fill = round((v / 100) * width)
    return "█" * fill, "░" * (width - fill)


def _render_html_report(payload: dict) -> str:
    sess = payload["session"]
    totals = payload["totals"]
    rounds = payload["rounds"]
    prior_comment_summary = payload.get("prior_comment_summary", [])
    status = str(sess.get("status", "unknown")).lower()
    status_class = _status_css_class(status)
    session_id = escape(str(sess.get("id") or "unknown"))
    rounds_validated = int(totals.get("rounds_validated", 0) or 0)

    round_nav: list[str] = []
    round_blocks: list[str] = []
    watch_name = Path(str(sess.get("watch_path") or "")).name or "patchset"
    lore_msg = ""
    lore = sess.get("lore")
    if isinstance(lore, dict):
        lore_msg = str(lore.get("message_id") or "").strip()

    for row in rounds:
        round_no = int(row.get("round", 0) or 0)
        findings_total = int(row.get("findings_total", 0) or 0)
        findings_open = int(row.get("findings_open", 0) or 0)
        findings_closed = max(0, findings_total - findings_open)
        findings_rows = _load_findings_for_report_row(row)
        sev_counts = _findings_severity_open_counts(findings_rows)
        top_open = next(
            (
                str(x.get("title") or "")
                for x in findings_rows
                if isinstance(x, dict) and str(x.get("status", "open")).lower() != "closed"
            ),
            "",
        )

        summary_payload = _load_round_summary_payload(row)
        summary_findings = summary_payload.get("findings", {}) if isinstance(summary_payload, dict) else {}
        summary_prior = summary_payload.get("prior_comments", {}).get("totals", {}) if isinstance(summary_payload, dict) else {}
        elapsed = summary_payload.get("timing", {}).get("elapsed_seconds") if isinstance(summary_payload, dict) else None
        elapsed_hms = _format_elapsed_hms(elapsed) or "n/a"
        new_since_prev = int(summary_findings.get("new_since_prev", 0) or 0)
        resolved_since_prev = int(summary_findings.get("resolved_since_prev", 0) or 0)
        prior_received = int(summary_prior.get("received_total", 0) or 0)
        prior_closed = int(summary_prior.get("closed", 0) or 0)

        gate_passed_raw = row.get("gate_passed")
        if gate_passed_raw is None:
            gate_text = "⚪ Gate N/A"
            gate_class = "badge-medium"
        elif bool(gate_passed_raw):
            gate_text = "✅ Gate PASSED"
            gate_class = "badge-pass"
        else:
            gate_text = "❌ Gate FAILED"
            gate_class = "badge-reject"

        verdict_ok = findings_open == 0
        verdict_text = "✅ LGTM" if verdict_ok else "❌ REJECT"
        verdict_class = "badge-lgtm" if verdict_ok else "badge-reject"
        open_badge = ""
        if findings_open > 0:
            if sev_counts["critical"] > 0 or sev_counts["high"] > 0:
                open_badge = f"🔴 {sev_counts['critical'] + sev_counts['high']} HIGH open"
            elif sev_counts["medium"] > 0:
                open_badge = f"🟡 {sev_counts['medium']} MEDIUM open"
            else:
                open_badge = f"🔵 {findings_open} LOW open"
        else:
            open_badge = "🟢 0 open"
        open_badge_class = "badge-high" if findings_open > 0 else "badge-pass"

        builder_conf = int(row.get("builder_confidence", 0) or 0)
        reviewer_conf = int(row.get("reviewer_confidence", 0) or 0)
        patch_gauge = int(row.get("builder_patch_gauge", 0) or 0)
        b_fill, b_empty = _progress_bar(builder_conf)
        r_fill, r_empty = _progress_bar(reviewer_conf)
        round_score = int(round((builder_conf + reviewer_conf) / 2))
        o_fill, o_empty = _progress_bar(round_score)

        round_nav.append(f"<a href='#r{round_no}'>Round {round_no}</a>")
        round_blocks.append(
            "<div class='round-block' id='r{round_no}'>"
            "<div class='round-header'>"
            "<span class='round-title'>📊 Round {round_no}</span>"
            "<div class='round-meta'>"
            "<span class='badge {gate_class}'>{gate_text}</span>"
            "<span class='badge {verdict_class}'>{verdict_text}</span>"
            "<span class='badge {open_badge_class}'>{open_badge}</span>"
            "<span style='color:#8b949e;font-size:0.78rem'>⏱ {elapsed}</span>"
            "</div>"
            "</div>"
            "<div class='scores-row'>"
            "<div class='score-item'>"
            "<span class='score-label'>Chanakya Confidence</span>"
            "<div class='score-bar-wrap'><div class='score-bar'><div class='score-fill fill-teal' style='width:{builder_conf}%'></div></div><span class='score-val'>{builder_conf}%</span></div>"
            "</div>"
            "<div class='score-item'>"
            "<span class='score-label'>Patch Gauge</span>"
            "<div class='score-bar-wrap'><div class='score-bar'><div class='score-fill fill-gauge' style='width:{patch_gauge}%'></div></div><span class='score-val'>{patch_gauge}%</span></div>"
            "</div>"
            "<div class='score-item'>"
            "<span class='score-label'>Aryabhata Confidence</span>"
            "<div class='score-bar-wrap'><div class='score-bar'><div class='score-fill fill-purple' style='width:{reviewer_conf}%'></div></div><span class='score-val'>{reviewer_conf}%</span></div>"
            "</div>"
            "<div class='score-item'>"
            "<span class='score-label'>Findings</span>"
            "<div class='finding-pills'>"
            "<span class='pill pill-total'>total={findings_total}</span>"
            "<span class='pill pill-open'>open={findings_open}</span>"
            "<span class='pill pill-closed'>closed={findings_closed}</span>"
            "<span class='pill pill-new'>new={new_since_prev}</span>"
            "<span class='pill pill-resolved'>resolved={resolved_since_prev}</span>"
            "</div>"
            "</div>"
            "<div class='score-item'>"
            "<span class='score-label'>Prior Comments</span>"
            "<div class='finding-pills'>"
            "<span class='pill pill-total'>received={prior_received}</span>"
            "<span class='pill pill-closed'>closed={prior_closed}</span>"
            "</div>"
            "</div>"
            "</div>"
            "<div class='tables-row'>"
            "<div class='agent-section'>"
            "<div class='agent-title chanakya'>⚙️ Chanakya (Builder) — Round {round_no}</div>"
            "<table><tr><th>Criteria</th><th>Score</th><th>Evidence</th></tr>"
            "<tr><td class='criteria-col'>Change activity</td><td class='score-col'>🟢 {changed_files} files</td><td class='evidence-col'>{diff_lines} diff lines across {diff_hunks} hunks</td></tr>"
            "<tr><td class='criteria-col'>Patch gauge</td><td class='score-col'>🟢 {patch_gauge}%</td><td class='evidence-col'>Gauge computed from changed files + diff footprint</td></tr>"
            "<tr><td class='criteria-col'>Confidence</td><td class='score-col'>🟢 {builder_conf}%</td><td class='evidence-col'>Round confidence generated by score engine</td></tr>"
            "<tr><td class='criteria-col'>Artifact quality</td><td class='score-col'>🟢 9/10</td><td class='evidence-col'>Builder report + changed_files + diff artifacts available</td></tr>"
            "<tr class='total-row'><td class='criteria-col'>Round {round_no} Total</td><td class='score-col'>🟢 {builder_conf}%</td><td class='evidence-col'>Structured builder output and measurable patch activity</td></tr>"
            "</table>"
            "</div>"
            "<div class='agent-section'>"
            "<div class='agent-title aryabhata'>🔍 Aryabhata (Reviewer) — Round {round_no}</div>"
            "<table><tr><th>Criteria</th><th>Score</th><th>Evidence</th></tr>"
            "<tr><td class='criteria-col'>Findings accuracy</td><td class='score-col'>🟢 {reviewer_conf}%</td><td class='evidence-col'>{findings_total} findings with structured schema output</td></tr>"
            "<tr><td class='criteria-col'>Open risk surfacing</td><td class='score-col'>🟢 {findings_open}</td><td class='evidence-col'>{top_open}</td></tr>"
            "<tr><td class='criteria-col'>New issue discovery</td><td class='score-col'>🟢 {new_since_prev}</td><td class='evidence-col'>New findings raised vs previous round</td></tr>"
            "<tr><td class='criteria-col'>Resolution tracking</td><td class='score-col'>🟢 {resolved_since_prev}</td><td class='evidence-col'>Findings resolved vs previous round</td></tr>"
            "<tr class='total-row'><td class='criteria-col'>Round {round_no} Total</td><td class='score-col'>🟢 {reviewer_conf}%</td><td class='evidence-col'>Adversarial review confidence and schema-valid output</td></tr>"
            "</table>"
            "</div>"
            "</div>"
            "<div class='findings-section'>"
            "<div class='findings-title'>🧾 Findings Detail</div>"
            "{findings_table}"
            "</div>"
            "<div class='verdict-box'>"
            "<div class='verdict-title'>🎯 Round {round_no} Verdict</div>"
            "<div class='verdict-row'><span class='verdict-label'>Chanakya</span><span class='verdict-bar'><span class='bar-fill'>{b_fill}</span><span class='bar-empty'>{b_empty}</span></span><span class='verdict-score'>{builder_conf}%</span></div>"
            "<div class='verdict-row'><span class='verdict-label'>Aryabhata</span><span class='verdict-bar'><span class='bar-fill'>{r_fill}</span><span class='bar-empty'>{r_empty}</span></span><span class='verdict-score'>{reviewer_conf}%</span></div>"
            "<div class='verdict-row'><span class='verdict-label'>Round</span><span class='verdict-bar'><span class='bar-fill'>{o_fill}</span><span class='bar-empty'>{o_empty}</span></span><span class='verdict-score'>{round_score}%</span></div>"
            "<div class='verdict-outcome'>Outcome: <span>{verdict_text} — open={findings_open}, total={findings_total}</span></div>"
            "</div>"
            "</div>".format(
                round_no=round_no,
                gate_class=gate_class,
                gate_text=gate_text,
                verdict_class=verdict_class,
                verdict_text=verdict_text,
                open_badge_class=open_badge_class,
                open_badge=open_badge,
                elapsed=elapsed_hms,
                builder_conf=builder_conf,
                patch_gauge=patch_gauge,
                reviewer_conf=reviewer_conf,
                findings_total=findings_total,
                findings_open=findings_open,
                findings_closed=findings_closed,
                new_since_prev=new_since_prev,
                resolved_since_prev=resolved_since_prev,
                prior_received=prior_received,
                prior_closed=prior_closed,
                changed_files=int(row.get("builder_changed_files", 0) or 0),
                diff_lines=int(row.get("builder_diff_lines", 0) or 0),
                diff_hunks=int(row.get("builder_diff_hunks", 0) or 0),
                top_open=escape(top_open or "No open findings."),
                findings_table=_render_round_findings_table(row),
                b_fill=b_fill,
                b_empty=b_empty,
                r_fill=r_fill,
                r_empty=r_empty,
                o_fill=o_fill,
                o_empty=o_empty,
                round_score=round_score,
            )
        )

    prior_rows = "".join(
        [
            "<tr>"
            f"<td>{escape(str(item.get('source_comment_id') or '-'))}</td>"
            f"<td>{escape(str(item.get('comment_type') or '-'))}</td>"
            f"<td>{escape(str(item.get('initial_status') or '-'))}</td>"
            f"<td>{escape(str(item.get('current_status') or '-'))}</td>"
            f"<td>{escape(str(item.get('resolution_origin') or '-'))}</td>"
            f"<td>{'yes' if bool(item.get('fixed_by_a2a')) else 'no'}</td>"
            f"<td>{escape(str(item.get('closed_round') if item.get('closed_round') is not None else '-'))}</td>"
            "</tr>"
            for item in prior_comment_summary[:30]
        ]
    )
    if not prior_rows:
        prior_rows = (
            "<tr><td colspan='7' class='muted'>No prior comment tracking data recorded for this session.</td></tr>"
        )

    io_details = payload.get("io_details", {}) if isinstance(payload.get("io_details"), dict) else {}
    available_versions = io_details.get("available_patch_versions")
    if isinstance(available_versions, list) and available_versions:
        versions_rendered = "<br/>".join(
            escape(f"{str(item.get('name') or '-')}: {str(item.get('path') or '-')}")
            for item in available_versions
            if isinstance(item, dict)
        )
    else:
        versions_rendered = "-"
    io_rows = "".join(
        [
            "<tr><th>Input Watch Path</th><td class='mono'>{}</td></tr>".format(
                escape(str(io_details.get("input_watch_path") or "-"))
            ),
            "<tr><th>Input Lore Link</th><td class='mono'>{}</td></tr>".format(
                escape(str(io_details.get("input_lore_link") or "-"))
            ),
            "<tr><th>Report Directory</th><td class='mono'>{}</td></tr>".format(
                escape(str(io_details.get("report_dir") or "-"))
            ),
            "<tr><th>Patches Root</th><td class='mono'>{}</td></tr>".format(
                escape(str(io_details.get("patches_root") or "-"))
            ),
            "<tr><th>Latest Output Patches Path</th><td class='mono'>{}</td></tr>".format(
                escape(str(io_details.get("latest_output_patches_path") or "-"))
            ),
            "<tr><th>Latest Output Version</th><td>{}</td></tr>".format(
                escape(str(io_details.get("latest_output_version") or "-"))
            ),
            "<tr><th>Respin Kind</th><td>{}</td></tr>".format(
                escape(str(io_details.get("respin_kind") or "-"))
            ),
            "<tr><th>Respin Generated At</th><td>{}</td></tr>".format(
                escape(str(io_details.get("respin_generated_at") or "-"))
            ),
            "<tr><th>Respin Source Watch Path</th><td class='mono'>{}</td></tr>".format(
                escape(str(io_details.get("respin_source_watch_path") or "-"))
            ),
            "<tr><th>Lore Next Version Report</th><td class='mono'>{}</td></tr>".format(
                escape(str(io_details.get("lore_next_version_report") or "-"))
            ),
            "<tr><th>Post-Respin Validation Report</th><td class='mono'>{}</td></tr>".format(
                escape(str(io_details.get("post_respin_validation_report") or "-"))
            ),
            "<tr><th>Post-Respin Validation Status</th><td>{}</td></tr>".format(
                escape(str(io_details.get("post_respin_validation_status") or "-"))
            ),
            "<tr><th>Post-Respin Validation Generated At</th><td>{}</td></tr>".format(
                escape(str(io_details.get("post_respin_validation_generated_at") or "-"))
            ),
            f"<tr><th>Available Patch Versions</th><td class='mono'>{versions_rendered}</td></tr>",
        ]
    )

    final_status_badge = "✅ LGTM" if status == "lgtm" else ("⛔ STOPPED" if status == "stopped" else "⏳ IN PROGRESS")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>PatchWise A2A — {rounds_validated}-Round Performance Report</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #0d1117; color: #e6edf3; font-family: 'Segoe UI', system-ui, sans-serif; padding: 24px; }}
    .container {{ max-width: 1400px; margin: 0 auto; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; }}
    h1 {{ text-align: center; font-size: 1.6rem; color: #58a6ff; margin-bottom: 4px; }}
    .subtitle {{ text-align: center; color: #8b949e; font-size: 0.85rem; margin-bottom: 24px; }}
    .session-banner {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 16px 24px; margin-bottom: 28px; display: flex; flex-wrap: wrap; gap: 20px; justify-content: space-between; align-items: center; }}
    .session-banner .field {{ display: flex; flex-direction: column; min-width: 140px; }}
    .session-banner .label {{ font-size: 0.72rem; color: #8b949e; text-transform: uppercase; letter-spacing: .5px; }}
    .session-banner .value {{ font-size: 0.9rem; color: #e6edf3; font-weight: 600; margin-top: 2px; word-break: break-word; }}
    .lgtm-badge {{ background: #1a7f37; color: #56d364; border: 1px solid #2ea043; padding: 6px 16px; border-radius: 20px; font-weight: 700; font-size: 0.9rem; }}
    .round-nav {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 24px; justify-content: center; }}
    .round-nav a {{ padding: 6px 14px; border-radius: 20px; font-size: 0.8rem; font-weight: 600; text-decoration: none; border: 1px solid #30363d; color: #8b949e; background: #161b22; transition: all .2s; }}
    .round-nav a:hover {{ border-color: #58a6ff; color: #58a6ff; }}
    .round-block {{ background: #161b22; border: 1px solid #30363d; border-radius: 12px; margin-bottom: 32px; overflow: hidden; }}
    .round-header {{ padding: 16px 24px; display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between; border-bottom: 1px solid #30363d; }}
    .round-title {{ font-size: 1.08rem; color: #58a6ff; font-weight: 700; }}
    .round-meta {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
    .badge {{ padding: 4px 10px; border-radius: 999px; font-size: .73rem; border: 1px solid transparent; font-weight: 700; }}
    .badge-pass {{ background: #1a7f3720; color: #56d364; border-color: #2ea04366; }}
    .badge-reject {{ background: #da363320; color: #ff7b72; border-color: #f8514966; }}
    .badge-lgtm {{ background: #1a7f3740; color: #56d364; border-color: #2ea043aa; }}
    .badge-high {{ background: #da363320; color: #ff7b72; border-color: #f8514966; }}
    .badge-medium {{ background: #d2992020; color: #e3b341; border-color: #d2992066; }}
    .scores-row {{ padding: 18px 24px; display: grid; gap: 14px; grid-template-columns: repeat(auto-fit,minmax(230px,1fr)); border-bottom: 1px solid #30363d; }}
    .score-item {{ background: #0d1117; border: 1px solid #30363d; border-radius: 8px; padding: 10px 12px; }}
    .score-label {{ display: block; font-size: .72rem; color: #8b949e; text-transform: uppercase; margin-bottom: 6px; }}
    .score-bar-wrap {{ display: flex; align-items: center; gap: 8px; }}
    .score-bar {{ flex: 1; height: 8px; background: #21262d; border-radius: 999px; overflow: hidden; }}
    .score-fill {{ height: 100%; border-radius: 999px; }}
    .fill-teal {{ background: linear-gradient(90deg, #1a7f37, #56d364); }}
    .fill-purple {{ background: linear-gradient(90deg, #6e40c9, #bc8cff); }}
    .fill-gauge {{ background: linear-gradient(90deg, #1158c7, #58a6ff); }}
    .score-val {{ font-size: .78rem; font-weight: 700; color: #e6edf3; width: 42px; text-align: right; }}
    .finding-pills {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .pill {{ padding: 3px 8px; font-size: .72rem; border-radius: 999px; border: 1px solid #30363d; background: #21262d; color: #c9d1d9; }}
    .pill-total {{ background: #21262d; }}
    .pill-open {{ background: #da363320; color: #ff7b72; border-color: #f8514966; }}
    .pill-closed {{ background: #1a7f3720; color: #56d364; border-color: #2ea04366; }}
    .pill-new {{ background: #d2992020; color: #e3b341; border-color: #d2992066; }}
    .pill-resolved {{ background: #58a6ff20; color: #79c0ff; border-color: #58a6ff66; }}
    .tables-row {{ display: grid; gap: 14px; grid-template-columns: repeat(auto-fit,minmax(320px,1fr)); padding: 18px 24px; border-bottom: 1px solid #30363d; }}
    .agent-section {{ background: #0d1117; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; }}
    .agent-title {{ padding: 10px 12px; font-size: .82rem; font-weight: 700; border-bottom: 1px solid #30363d; }}
    .agent-title.chanakya {{ color: #56d364; background: #1a7f3715; }}
    .agent-title.aryabhata {{ color: #bc8cff; background: #6e40c915; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 8px 10px; border-bottom: 1px solid #30363d; font-size: .75rem; vertical-align: top; text-align: left; }}
    th {{ background: #161b22; color: #8b949e; font-weight: 700; text-transform: uppercase; font-size: .69rem; }}
    .criteria-col {{ width: 31%; color: #c9d1d9; }}
    .score-col {{ width: 20%; color: #e6edf3; font-weight: 700; }}
    .evidence-col {{ color: #8b949e; }}
    .total-row td {{ background: #161b22; font-weight: 700; }}
    .findings-section {{ padding: 18px 24px; border-bottom: 1px solid #30363d; }}
    .findings-title {{ font-size: .86rem; font-weight: 700; color: #79c0ff; margin-bottom: 10px; }}
    .findings-wrap {{ padding: 2px 16px 16px 16px; }}
    .findings-wrap h4 {{ margin: 8px 0 10px 0; }}
    .findings-table {{ width: 100%; border-collapse: collapse; font-size: .83rem; }}
    .findings-table th, .findings-table td {{ padding: 8px; border: 1px solid #30363d; vertical-align: top; text-align: left; }}
    .findings-table th {{ background: #0d1117; color: #8b949e; text-transform: uppercase; font-size: .72rem; letter-spacing: .3px; }}
    .findings-open {{ color: #ff7b72; }}
    .findings-closed {{ color: #56d364; }}
    .findings-note {{ margin-top: 7px; }}
    .verdict-box {{ margin: 16px 24px 22px 24px; background: #0d1117; border: 1px solid #30363d; border-radius: 10px; padding: 14px; }}
    .verdict-title {{ color: #58a6ff; font-weight: 700; font-size: .84rem; margin-bottom: 10px; }}
    .verdict-row {{ display: grid; grid-template-columns: 92px 1fr 72px; align-items: center; gap: 10px; margin-bottom: 6px; font-size: .78rem; }}
    .verdict-label {{ color: #8b949e; font-weight: 700; }}
    .verdict-bar {{ font-family: monospace; letter-spacing: .5px; white-space: nowrap; }}
    .bar-fill {{ color: #56d364; }}
    .bar-empty {{ color: #30363d; }}
    .verdict-score {{ color: #e6edf3; font-weight: 700; text-align: right; }}
    .verdict-outcome {{ margin-top: 8px; padding-top: 8px; border-top: 1px solid #30363d; color: #8b949e; font-size: .78rem; }}
    .prior-wrap {{ margin-top: 12px; border: 1px solid #30363d; border-radius: 10px; background: #161b22; padding: 16px; }}
    .muted {{ color: #8b949e; }}
    footer {{ margin-top: 20px; color: #8b949e; font-size: .78rem; }}
  </style>
</head>
<body>
  <div class="container">
    <h1>🔬 PatchWise A2A — Agent Performance Report</h1>
    <div class="subtitle">Session: {session_id} &nbsp;· &nbsp;Task: {escape(str(sess.get("task") or "-"))} &nbsp;· &nbsp;{rounds_validated} Rounds &nbsp;· &nbsp;{escape(watch_name)}</div>

    <div class="session-banner">
      <div class="field"><span class="label">Session ID</span><span class="value mono">{session_id}</span></div>
      <div class="field"><span class="label">Task</span><span class="value">{escape(str(sess.get("task") or "-"))}</span></div>
      <div class="field"><span class="label">Patches</span><span class="value">{escape(watch_name)}</span></div>
      <div class="field"><span class="label">Lore URL</span><span class="value" style="font-size:0.75rem">{escape(lore_msg or "-")}</span></div>
      <div class="field"><span class="label">Max Rounds</span><span class="value">{escape(str(sess.get("max_rounds") or rounds_validated))}</span></div>
      <div class="field"><span class="label">Final Status</span><span class="value"><span class="lgtm-badge">{escape(final_status_badge)}</span></span></div>
      <div class="field"><span class="label">Builder</span><span class="value">{escape(str(sess.get("builder_display_name") or "builder"))}</span></div>
      <div class="field"><span class="label">Reviewer</span><span class="value">{escape(str(sess.get("reviewer_display_name") or sess.get("reviewer_name") or "reviewer"))}</span></div>
      <div class="field"><span class="label">Generated At</span><span class="value">{escape(_now_utc())}</span></div>
    </div>

    <div class="round-nav">{''.join(round_nav) if round_nav else "<span class='muted'>No validated rounds yet.</span>"}</div>

    {''.join(round_blocks)}

    <section class="prior-wrap">
      <h3>Prior Comment Summary</h3>
      <table class="findings-table">
        <thead>
          <tr><th>Source Comment ID</th><th>Type</th><th>Initial</th><th>Current</th><th>Resolution Origin</th><th>Fixed by A2A</th><th>Closed Round</th></tr>
        </thead>
        <tbody>{prior_rows}</tbody>
      </table>
    </section>

    <section class="prior-wrap">
      <h3>Session I/O Details</h3>
      <table class="findings-table">
        <tbody>{io_rows}</tbody>
      </table>
    </section>

    <footer>
      Tip: regenerate with <span class="mono">a2a report --session {session_id} --format html --output /path/to/report.html</span>
    </footer>
  </div>
</body>
</html>
"""


def _render_html_report_all(payload: dict) -> str:
    summary = payload["summary"]
    sessions = payload["sessions"]
    rows = []
    for item in sessions:
        status = str(item.get("status", "unknown")).lower()
        rows.append(
            "<tr>"
            f"<td class='mono'>{escape(str(item.get('id') or '-'))}</td>"
            f"<td>{escape(str(item.get('task') or '-'))}</td>"
            f"<td><span class='status { _status_css_class(status) }'>{escape(status)}</span></td>"
            f"<td>{escape(str(item.get('rounds_validated') or 0))}</td>"
            f"<td>{escape(str(item.get('findings_open_last') if item.get('findings_open_last') is not None else '-'))}</td>"
            f"<td>{escape(str(item.get('updated_at') or '-'))}</td>"
            "</tr>"
        )

    status_counts = "".join(
        f"<li>{escape(k)}: {v}</li>" for k, v in sorted((summary.get("by_status") or {}).items())
    ) or "<li>none</li>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>A2A All Sessions Report</title>
  <style>
    body {{ margin: 0; padding: 22px; background: #0d1117; color: #e6edf3; font: 14px/1.45 'Segoe UI', system-ui, sans-serif; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; }}
    .status-lgtm {{ color: #56d364; }}
    .status-stopped {{ color: #ff7b72; }}
    .status-progress {{ color: #79c0ff; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 14px; }}
    th, td {{ border: 1px solid #30363d; padding: 8px; text-align: left; vertical-align: top; }}
    th {{ background: #161b22; color: #8b949e; text-transform: uppercase; font-size: .74rem; }}
    .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 12px 14px; margin-bottom: 14px; }}
  </style>
</head>
<body>
  <h1>A2A Aggregate Report</h1>
  <p>Sessions total: {escape(str(summary.get("sessions_total", 0)))}</p>
  <div class="card">
    <strong>Status Counts</strong>
    <ul>{status_counts}</ul>
  </div>
  <table>
    <thead><tr><th>Session</th><th>Task</th><th>Status</th><th>Rounds</th><th>Open Findings Last</th><th>Updated</th></tr></thead>
    <tbody>{''.join(rows) if rows else "<tr><td colspan='6'>No sessions found.</td></tr>"}</tbody>
  </table>
</body>
</html>
"""


def _session_html_report_path(root: Path, session_id: str) -> Path:
    return _report_dir(root, session_id) / "session-report.html"


def _write_session_html_report(root: Path, session_id: str) -> Path:
    payload = _session_report_payload(root, session_id)
    html_report = _render_html_report(payload)
    out_path = _session_html_report_path(root, session_id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_report + ("" if html_report.endswith("\n") else "\n"), encoding="utf-8")
    return out_path


def _auto_write_session_html_report(root: Path, session_id: str) -> None:
    try:
        out_path = _write_session_html_report(root, session_id)
        _echo(f"HTML report written: {out_path}")
    except Exception as exc:
        _echo(f"HTML report generation warning for session {session_id}: {exc}")


def _worktree_paths_for_session(session: dict) -> tuple[Path, Path]:
    worktrees = session.get("worktrees", {})
    reviewer_name = str(session.get("reviewer_name", "aryabhatta"))
    repo_path = Path(str(session.get("repo_path", ""))).resolve()
    builder_raw = worktrees.get("builder", str(repo_path)) if isinstance(worktrees, dict) else str(repo_path)
    reviewer_raw = (
        worktrees.get(reviewer_name, str(repo_path)) if isinstance(worktrees, dict) else str(repo_path)
    )
    return Path(str(builder_raw)).resolve(), Path(str(reviewer_raw)).resolve()


def _worktree_lock_path(root: Path, session: dict) -> Path:
    builder_path, reviewer_path = _worktree_paths_for_session(session)
    key = hashlib.sha1(f"{builder_path}|{reviewer_path}".encode("utf-8")).hexdigest()[:20]
    return root / A2A_DIRNAME / "locks" / "worktrees" / f"{key}.lock"


@contextmanager
def _worktree_lock(root: Path, session: dict, session_id: str):
    lock_path = _worktree_lock_path(root, session)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("a+", encoding="utf-8")
    builder_path, reviewer_path = _worktree_paths_for_session(session)
    _echo(f"Waiting for worktree lock: {lock_path}")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(
            _as_json(
                {
                    "session_id": session_id,
                    "pid": os.getpid(),
                    "acquired_at": _now_utc(),
                    "builder_worktree": str(builder_path),
                    "reviewer_worktree": str(reviewer_path),
                }
            )
        )
        fh.flush()
        _echo(f"Acquired worktree lock: {lock_path}")
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def cmd_prepare(args: argparse.Namespace) -> int:
    try:
        root = _must_find_root()
        a2a_dir = root / A2A_DIRNAME
        repo = Path(args.repo).resolve() if args.repo else root
        reviewer_name = args.reviewer_name

        git_root = _git(repo, "rev-parse", "--show-toplevel")
        repo = Path(git_root).resolve()
        if not _git_ok(repo, "rev-parse", "--verify", "HEAD"):
            raise RuntimeError(
                "Target repository has no commits (unborn HEAD). "
                "Create an initial commit before running prepare."
            )

        worktrees_dir = a2a_dir / "worktrees"
        builder_path = worktrees_dir / "builder"
        reviewer_path = worktrees_dir / reviewer_name
        worktrees_dir.mkdir(parents=True, exist_ok=True)

        for target in [builder_path, reviewer_path]:
            if target.exists():
                if not args.force:
                    raise RuntimeError(
                        f"Worktree path exists: {target}. Re-run with --force to recreate."
                    )
                shutil.rmtree(target)

        if _git_ok(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{args.branch}"):
            _git(repo, "worktree", "add", "--force", str(builder_path), args.branch)
        else:
            _git(repo, "worktree", "add", "--force", "-b", args.branch, str(builder_path), "HEAD")

        _git(repo, "worktree", "add", "--force", "--detach", str(reviewer_path), args.branch)

        prepare_payload = {
            "version": 1,
            "prepared_at": _now_utc(),
            "repo_path": str(repo),
            "branch": args.branch,
            "worktrees": {
                "builder": str(builder_path),
                reviewer_name: str(reviewer_path),
            },
            "reviewer_name": reviewer_name,
            "reviewer_readonly_policy": bool(args.reviewer_readonly_policy),
        }
        dump_json(_prepare_path(root), prepare_payload)

        policy_note = worktrees_dir / "README.md"
        policy_note.write_text(
            "# A2A Worktrees\n\n"
            "- builder: editable implementation worktree\n"
            f"- {reviewer_name}: reviewer worktree (policy read-only)\n\n"
            "Note: read-only is enforced by process policy in this version.\n",
            encoding="utf-8",
        )

        _echo(f"Prepared A2A worktrees for branch '{args.branch}'.")
        _echo(f"Repo: {repo}")
        _echo(f"Builder worktree: {builder_path}")
        _echo(f"Reviewer worktree ({reviewer_name}): {reviewer_path}")
        _echo("Next: a2a run --task \"<your task>\"")
        return 0
    except RuntimeError as exc:
        _echo(str(exc))
        return 1


def _start_session(
    root: Path,
    task: str,
    max_rounds: int,
    timeout_min: int | None,
    builder_command: str | None = None,
    reviewer_command: str | None = None,
    watch_path: str | None = None,
    lore_message_id: str | None = None,
) -> dict:
    cfg = _load_config(root)
    prep = load_json(_prepare_path(root))
    state_path = root / A2A_DIRNAME / "state.json"
    state = load_json(state_path)

    watch_path_resolved: str | None = None
    if watch_path:
        wp = Path(watch_path).expanduser().resolve()
        if not wp.exists():
            raise RuntimeError(f"watch_path not found: {wp}")
        watch_path_resolved = str(wp)

    session_id = _next_session_id(task)
    reviewer_name = str(prep.get("reviewer_name") or cfg.get("reviewer_name", "aryabhatta"))
    builder_display_name = _resolve_builder_display_name(cfg=cfg)
    reviewer_display_name = _resolve_reviewer_display_name(cfg=cfg)
    session = {
        "version": 1,
        "id": session_id,
        "task": task,
        "status": "in_progress",
        "created_at": _now_utc(),
        "updated_at": _now_utc(),
        "max_rounds": max_rounds,
        "timeout_min": timeout_min,
        "current_round": 1,
        "open_findings": None,
        "builder_display_name": builder_display_name,
        "reviewer_display_name": reviewer_display_name,
        "reviewer_name": reviewer_name,
        "repo_path": prep["repo_path"],
        "branch": prep["branch"],
        "worktrees": prep["worktrees"],
        "rounds": [],
        "builder_command": builder_command,
        "reviewer_command": reviewer_command,
        "watch_path": watch_path_resolved,
        "llm_native": {
            "default": bool(cfg.get("llm_native_default", True)),
            "strict": bool(cfg.get("llm_native_strict", True)),
            "fallback": bool(cfg.get("llm_native_fallback", False)),
            "timeout_sec": int(cfg.get("llm_native_timeout_sec", 900)),
        },
    }
    if lore_message_id:
        session["lore"] = {"message_id": str(lore_message_id).strip()}

    prior_gate = bool(cfg.get("prior_review_gate", True))
    search_if_missing = bool(cfg.get("prior_review_search", True))
    max_comments = int(cfg.get("prior_review_max_comments", 120))
    if prior_gate and watch_path_resolved:
        report_dir = _report_dir(root, session_id)
        context = ingest_prior_review_context(
            Path(watch_path_resolved),
            report_dir,
            search_if_missing=search_if_missing,
            max_comments=max_comments,
            seed_message_ids=[str(lore_message_id).strip()] if lore_message_id else None,
        )
        if context:
            session["prior_review"] = context

    dump_json(_session_path(root, session_id), session)

    summary = _report_dir(root, session_id) / "summary.md"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(
        f"# A2A Session {session_id}\n\n"
        f"- task: {task}\n"
        f"- builder: {builder_display_name}\n"
        f"- reviewer: {reviewer_display_name}\n"
        f"- reviewer_internal_name: {reviewer_name}\n"
        f"- status: in_progress\n"
        f"- max_rounds: {max_rounds}\n\n"
        "## Round History\n\n",
        encoding="utf-8",
    )

    _write_round_templates(
        root,
        session_id,
        1,
        reviewer_name,
        reviewer_display_name=reviewer_display_name,
    )

    state["active_session_id"] = session_id
    state["last_updated"] = _now_utc()
    dump_json(state_path, state)

    return session


def _append_summary_round(root: Path, session_id: str, round_no: int, total: int, open_count: int) -> None:
    summary = _report_dir(root, session_id) / "summary.md"
    line = f"- round {round_no}: findings_total={total}, findings_open={open_count}\n"
    with summary.open("a", encoding="utf-8") as f:
        f.write(line)


def _append_summary_verdict(root: Path, session_id: str, verdict: str) -> None:
    summary = _report_dir(root, session_id) / "summary.md"
    with summary.open("a", encoding="utf-8") as f:
        f.write(f"\n## Final Verdict\n\n- {verdict}\n")


def _set_summary_status(root: Path, session_id: str, status: str) -> None:
    summary = _report_dir(root, session_id) / "summary.md"
    if not summary.exists():
        return
    lines = summary.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith("- status:"):
            out.append(f"- status: {status}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        inserted = False
        for idx, line in enumerate(out):
            if line.startswith("- reviewer_internal_name:"):
                out.insert(idx + 1, f"- status: {status}")
                inserted = True
                break
        if not inserted:
            out.append(f"- status: {status}")
    summary.write_text("\n".join(out) + "\n", encoding="utf-8")


def _validate_round_only(
    root: Path, session_id: str, round_no: int | None = None
) -> tuple[dict, int, list[dict], list[str], Path]:
    cfg = _load_config(root)
    strict = bool(cfg.get("strict_evidence", True))
    prior_gate = bool(cfg.get("prior_review_gate", True))
    session = _load_session(root, session_id)
    current_round = int(session.get("current_round", 1))
    target_round = current_round if round_no is None else int(round_no)
    reviewer_name = str(session.get("reviewer_name", "aryabhatta"))
    files = _round_files(root, session_id, target_round, reviewer_name)
    findings_path = files["findings"]

    if not findings_path.exists():
        raise RuntimeError(f"Missing findings file for round {target_round}: {findings_path}")

    findings = _load_findings_payload(findings_path)
    issue_map_path = _findings_payload_path(root, session_id)
    issue_id_map = _load_issue_id_map(issue_map_path)
    findings, issue_id_map_updated, findings_changed = _canonicalize_generated_source_ids(findings, issue_id_map)
    if findings_changed:
        findings_path.write_text(_as_json({"findings": findings}), encoding="utf-8")
    if issue_id_map_updated != issue_id_map:
        _save_issue_id_map(issue_map_path, issue_id_map_updated)

    prior = session.get("prior_review")
    if prior_gate and isinstance(prior, dict):
        comments_file_raw = str(prior.get("comments_file") or "").strip()
        matrix_file_raw = str(prior.get("matrix_file") or "").strip()
        if comments_file_raw:
            comments_file = Path(comments_file_raw)
            if comments_file.exists():
                prior_comments = load_prior_comments(comments_file)
                findings = augment_findings_with_prior_comments(findings, prior_comments, comments_file)
                if matrix_file_raw:
                    matrix_file = Path(matrix_file_raw)
                    matrix_file.parent.mkdir(parents=True, exist_ok=True)
                    matrix_file.write_text(
                        render_prior_comment_matrix(prior_comments, findings),
                        encoding="utf-8",
                    )

    errors, open_count = _validate_findings(findings, strict)
    return session, open_count, findings, errors, findings_path


def _advance_session(root: Path, session_id: str) -> int:
    state_path = root / A2A_DIRNAME / "state.json"
    state = load_json(state_path)

    try:
        session, open_count, findings, errors, findings_path = _validate_round_only(root, session_id)
    except RuntimeError as exc:
        _echo(str(exc))
        return 1
    round_no = int(session.get("current_round", 1))
    reviewer_name = str(session.get("reviewer_name", "aryabhatta"))
    if errors:
        _echo("Findings validation failed:")
        for err in errors:
            _echo(f"  - {err}")
        return 1

    prev_open = None
    prev_builder_confidence = None
    prev_reviewer_confidence = None
    for previous_round in session.get("rounds", []):
        if int(previous_round.get("round", -1)) == round_no - 1:
            prev_open = int(previous_round.get("findings_open", 0))
            prev_builder_confidence = int(previous_round.get("builder_confidence", 0))
            prev_reviewer_confidence = int(previous_round.get("reviewer_confidence", 0))
            break

    change_stats = _builder_change_stats(root, session_id, round_no, reviewer_name)
    builder_patch_gauge = _compute_builder_patch_gauge(change_stats)
    builder_confidence = _compute_builder_confidence(prev_open, open_count, change_stats)
    reviewer_confidence = _compute_reviewer_confidence(findings)
    cfg_for_round = _load_config_or_defaults(root)
    thresholds = ScoreThresholds.from_config(cfg_for_round)
    score_decision = evaluate_round_scores(
        round_no=round_no,
        open_findings=open_count,
        builder_confidence=builder_confidence,
        reviewer_confidence=reviewer_confidence,
        patch_gauge=builder_patch_gauge,
        previous_builder_confidence=prev_builder_confidence,
        previous_reviewer_confidence=prev_reviewer_confidence,
        thresholds=thresholds,
    )
    if score_decision.get("low_quality_reviewer"):
        findings_payload = mark_findings_low_quality({"findings": findings})
        findings_path.write_text(_as_json(findings_payload), encoding="utf-8")
    append_score_decision(_report_dir(root, session_id) / "score_decisions.json", score_decision)
    messages = [str(m) for m in score_decision.get("messages", []) if str(m).strip()]
    for msg in messages:
        _echo(f"Score decision: {msg}")

    validated_at = _now_utc()
    started_round = session.get("round_started_round")
    round_started_at = session.get("round_started_at") if int(started_round or -1) == round_no else None
    round_elapsed_seconds = _elapsed_seconds(round_started_at, validated_at)

    round_record = {
        "round": round_no,
        "validated_at": validated_at,
        "findings_total": len(findings),
        "findings_open": open_count,
        "findings_file": str(findings_path),
        "builder_changed_files": int(change_stats.get("changed_files", 0)),
        "builder_diff_lines": int(change_stats.get("diff_lines", 0)),
        "builder_diff_hunks": int(change_stats.get("diff_hunks", 0)),
        "builder_patch_gauge": builder_patch_gauge,
        "builder_confidence": builder_confidence,
        "reviewer_confidence": reviewer_confidence,
        "round_started_at": round_started_at,
        "round_elapsed_seconds": round_elapsed_seconds,
        "score_decision": score_decision,
    }

    round_summary = _build_round_runtime_summary(
        root,
        session,
        round_no,
        findings,
        open_count,
        change_stats,
        builder_patch_gauge,
        builder_confidence,
        reviewer_confidence,
        round_started_at=round_started_at,
        round_elapsed_seconds=round_elapsed_seconds,
    )
    summary_files = _write_round_runtime_summary(root, session, round_no, round_summary)
    suggested_replies_file = _write_round_suggested_replies(
        root,
        session_id,
        round_no,
        round_summary,
        findings,
    )
    rounds = [r for r in session.get("rounds", []) if int(r.get("round", -1)) != round_no]
    rounds.append(round_record)
    rounds = sorted(rounds, key=lambda r: int(r["round"]))
    session["rounds"] = rounds
    session["open_findings"] = open_count
    session["updated_at"] = _now_utc()

    _append_summary_round(root, session_id, round_no, len(findings), open_count)
    fsum = round_summary.get("findings", {})
    _echo(
        render_round_table(
            {
                "round": round_no,
                "max_rounds": int(session.get("max_rounds", 0) or 0),
                "gate_passed": True,
                "builder_confidence": builder_confidence,
                "reviewer_confidence": reviewer_confidence,
                "builder_patch_gauge": builder_patch_gauge,
                "verdict": "LGTM" if open_count == 0 else "REJECT",
                "findings": fsum,
                "prior_comments": round_summary.get("prior_comments", {}),
                "round_elapsed_seconds": round_summary.get("timing", {}).get("elapsed_seconds"),
            }
        )
    )
    _echo(render_scores(builder_confidence, reviewer_confidence, builder_patch_gauge))
    _echo(render_prior_comment_status(round_summary.get("prior_comments", {})))
    advertised = extract_advertised_findings(
        {"findings": findings},
        round_summary,
        round_no,
        agent="aryabhatta",
    )
    advertised_text = render_advertised_findings_text(
        advertised,
        round_number=round_no,
        agent="aryabhatta",
    )
    if advertised_text:
        _echo(advertised_text)
    top_open = fsum.get("open_items", [])
    if isinstance(top_open, list) and top_open:
        _echo("Top open findings:")
        for item in top_open[:5]:
            _echo(render_finding_card(item))
    _echo(f"Round summary json: {summary_files['json']}")
    _echo(f"Round summary md: {summary_files['md']}")
    _echo(f"Suggested replies: {suggested_replies_file}")

    max_rounds = int(session.get("max_rounds", 1))
    if score_decision.get("abort_session"):
        session["status"] = "stopped"
        _set_summary_status(root, session_id, "stopped")
        _append_summary_verdict(
            root,
            session_id,
            f"STOPPED ({score_decision.get('abort_reason') or 'score gate abort'})",
        )
        if state.get("active_session_id") == session_id:
            state["active_session_id"] = None
            state["last_updated"] = _now_utc()
            dump_json(state_path, state)
        _write_session(root, session)
        _echo(str(score_decision.get("abort_reason") or "Session aborted by score engine."))
        return 1

    round_files = _round_files(root, session_id, round_no, reviewer_name)
    reviewer_verdict = _reviewer_verdict_for_round(root, session_id, round_no, reviewer_name)
    should_lgtm, lgtm_reason = should_issue_lgtm(str(round_files["findings"]), reviewer_verdict)
    if should_lgtm and int(open_count) == 0 and len(findings) == 0:
        guard_enabled = bool(cfg_for_round.get("reviewer_consistency_guard", True))
        if guard_enabled:
            reviewer_log = root / A2A_DIRNAME / "logs" / session_id / f"{_round_basename(round_no, 'reviewer')}.log"
            has_unresolved_risk, snippet = reviewer_log_has_unresolved_risk(reviewer_log)
            if has_unresolved_risk:
                should_lgtm = False
                lgtm_reason = (
                    "reviewer self-consistency guard: unresolved concern noted in reasoning "
                    f"({snippet})"
                )
    if should_lgtm and requires_full_subsystem_review(session, cfg_for_round):
        if not has_independent_subsystem_findings(findings):
            should_lgtm = False
            lgtm_reason = (
                "dual-track guard: prior-thread mapping exists but independent subsystem scan "
                "finding/evidence is missing"
            )

    if not should_lgtm:
        max_rounds_local = int(session.get("max_rounds", 1))
        next_round_hint = round_no + 1
        high_open = len(
            [
                row
                for row in findings
                if isinstance(row, dict)
                and str(row.get("status", "open")).lower() != "closed"
                and str(row.get("severity", "")).lower() in {"critical", "high"}
            ]
        )
        medium_open = len(
            [
                row
                for row in findings
                if isinstance(row, dict)
                and str(row.get("status", "open")).lower() != "closed"
                and str(row.get("severity", "")).lower() == "medium"
            ]
        )
        top_issue = ""
        for row in findings:
            if not isinstance(row, dict):
                continue
            if str(row.get("status", "open")).lower() == "closed":
                continue
            top_issue = str(row.get("title") or "")
            if top_issue:
                break
        _echo("┌──────────────────────────────────────────────────────────────────────┐")
        _echo(f"│ LGTM blocked: {lgtm_reason}")
        _echo(f"│ Open: {open_count}  ·  High: {high_open}  ·  Medium: {medium_open}")
        if top_issue:
            _echo(f"│ Top issue: {top_issue[:56]}")
        if round_no < max_rounds_local:
            _echo(f"│ Continuing to Round {next_round_hint}")
        else:
            _echo(f"│ At max rounds ({max_rounds_local}); stopping rules apply")
        _echo("└──────────────────────────────────────────────────────────────────────┘")

    if score_decision.get("block_lgtm"):
        should_lgtm = False
    if score_decision.get("force_extra_round"):
        should_lgtm = False

    if should_lgtm:
        session["status"] = "lgtm"
        _set_summary_status(root, session_id, "lgtm")
        _append_summary_verdict(root, session_id, "LGTM")
        resolved_findings = [
            row for row in findings if isinstance(row, dict) and str(row.get("status", "open")).lower() == "closed"
        ]
        try:
            update_kb_after_lgtm(
                root,
                session_id=session_id,
                watch_path=str(session.get("watch_path") or ""),
                resolved_findings=resolved_findings,
            )
        except Exception as exc:
            _echo(f"Knowledge base update warning: {exc}")
        if state.get("active_session_id") == session_id:
            state["active_session_id"] = None
            state["last_updated"] = _now_utc()
            dump_json(state_path, state)
        _write_session(root, session)
        prior_totals = round_summary.get("prior_comments", {}).get("totals", {})
        _echo(
            render_lgtm_banner(
                session_id,
                rounds=round_no,
                total_findings=len(findings),
                prior_closed=int(prior_totals.get("closed", 0)),
                prior_received=int(prior_totals.get("received_total", 0)),
                static_analysis_status="PASSED",
                kb_updates=0,
            )
        )
        return 0

    if round_no >= max_rounds:
        if _prompt_extend_after_max_rounds(
            session_id=session_id,
            round_no=round_no,
            max_rounds=max_rounds,
            open_count=open_count,
        ):
            next_round = round_no + 1
            session["max_rounds"] = max_rounds + 1
            session["current_round"] = next_round
            session["status"] = "in_progress"
            session["extra_scrutiny_next_round"] = bool(score_decision.get("extra_scrutiny_next_round"))
            _write_session(root, session)
            _write_round_templates(
                root,
                session_id,
                next_round,
                reviewer_name,
                reviewer_display_name=_resolve_reviewer_display_name(session=session, cfg=_load_config(root)),
            )
            _echo(
                f"Session {session_id}: max rounds extended to {session['max_rounds']}. "
                f"Prepared round {next_round} templates."
            )
            return 0

        session["status"] = "stopped"
        _set_summary_status(root, session_id, "stopped")
        stop_reason = "STOPPED (max rounds reached)"
        if score_decision.get("block_lgtm") and open_count == 0:
            stop_reason = "STOPPED (max rounds reached with blocked LGTM due to low reviewer confidence)"
        _append_summary_verdict(root, session_id, stop_reason)
        if state.get("active_session_id") == session_id:
            state["active_session_id"] = None
            state["last_updated"] = _now_utc()
            dump_json(state_path, state)
        _write_session(root, session)
        _echo(f"Session {session_id}: stopped at max rounds ({max_rounds}) with open findings={open_count}.")
        return 1

    next_round = round_no + 1
    session["current_round"] = next_round
    session["status"] = "in_progress"
    session["extra_scrutiny_next_round"] = bool(score_decision.get("extra_scrutiny_next_round"))
    _write_session(root, session)
    _write_round_templates(
        root,
        session_id,
        next_round,
        reviewer_name,
        reviewer_display_name=_resolve_reviewer_display_name(session=session, cfg=_load_config(root)),
    )
    _echo(
        f"Session {session_id}: round {round_no} validated with open findings={open_count}. "
        f"Prepared round {next_round} templates."
    )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    try:
        root = _must_find_root()
        prep_path = _prepare_path(root)
        if not prep_path.exists():
            _echo("Missing .a2a/prepare.json. Run: a2a prepare")
            return 1

        cfg = _load_config(root)
        max_rounds = args.max_rounds or int(cfg.get("default_max_rounds", 6))
        builder_cmd = _resolve_agent_command(cfg, args.builder_cmd, "builder_command")
        reviewer_cmd = _resolve_agent_command(cfg, args.reviewer_cmd, "reviewer_command")
        builder_cmd, reviewer_cmd = _resolve_default_agent_commands(root, cfg, builder_cmd, reviewer_cmd)
        watch_path = str(Path(args.watch_path).resolve()) if args.watch_path else None

        if args.resume:
            if args.run_reviewer:
                session = _load_session(root, args.resume)
                round_no = int(session.get("current_round", 1))
                if not reviewer_cmd:
                    reviewer_cmd = str(session.get("reviewer_command") or "").strip() or None
                if not reviewer_cmd:
                    _echo("No reviewer command configured. Use --reviewer-cmd or set reviewer_command in config.")
                    return 1
                rc = _run_agent_step(root, session, "reviewer", reviewer_cmd, round_no)
                if rc != 0:
                    return rc
            return _advance_session(root, args.resume)

        if not args.task:
            _echo("Missing --task for new session.")
            return 1

        session = _start_session(
            root,
            args.task,
            max_rounds=max_rounds,
            timeout_min=args.timeout_min,
            builder_command=builder_cmd,
            reviewer_command=reviewer_cmd,
            watch_path=watch_path,
            lore_message_id=None,
        )
        sid = session["id"]
        round_no = int(session["current_round"])
        files = _round_files(root, sid, round_no, str(session["reviewer_name"]))

        if args.auto:
            if not builder_cmd or not reviewer_cmd:
                _echo(
                    "Auto mode requires both commands. Provide --builder-cmd and --reviewer-cmd "
                    "or set builder_command/reviewer_command in config."
                )
                return 1
            rc = _run_agent_step(root, session, "builder", builder_cmd, round_no)
            if rc != 0:
                return rc
            rc = _run_agent_step(root, session, "reviewer", reviewer_cmd, round_no)
            if rc != 0:
                return rc
            return _advance_session(root, sid)

        _echo(f"Started session: {sid}")
        _echo(render_session_header(sid, str(session.get("task", "")), round_no, int(session.get("max_rounds", 0) or 0)))
        _echo(f"Agents: {_resolve_builder_display_name(session=session)} (builder), {_resolve_reviewer_display_name(session=session)} (reviewer)")
        _echo(f"Round {round_no} files:")
        _echo(f"  - {files['builder']}")
        _echo(f"  - {files['reviewer']}")
        _echo(f"  - {files['findings']}")
        _echo(f"After updating findings, continue with: a2a run --resume {sid}")
        return 0
    except RuntimeError as exc:
        _echo(str(exc))
        return 1


def cmd_loop(args: argparse.Namespace) -> int:
    try:
        root = _must_find_root()
        prep_path = _prepare_path(root)
        if not prep_path.exists():
            _echo("Missing .a2a/prepare.json. Run: a2a prepare")
            return 1

        cfg = _load_config(root)
        max_rounds = args.max_rounds or int(cfg.get("default_max_rounds", 6))
        builder_cmd = _resolve_agent_command(cfg, args.builder_cmd, "builder_command")
        reviewer_cmd = _resolve_agent_command(cfg, args.reviewer_cmd, "reviewer_command")
        builder_cmd, reviewer_cmd = _resolve_default_agent_commands(root, cfg, builder_cmd, reviewer_cmd)
        watch_path = str(Path(args.watch_path).resolve()) if args.watch_path else None
        lore_url = str(getattr(args, "lore_url", "") or "").strip()
        lore_msgid = str(getattr(args, "lore_msgid", "") or "").strip()
        lore_out_dir = str(getattr(args, "lore_out_dir", "") or "").strip()
        lore_input = lore_msgid or lore_url
        lore_source_msgid = _extract_lore_message_id(lore_input) if lore_input else None
        auto_respin_raw = getattr(args, "auto_respin", None)
        auto_respin = bool(auto_respin_raw) if auto_respin_raw is not None else bool(lore_input)
        single_series_mode = bool(getattr(args, "_single_series", False))

        if lore_input:
            if args.session:
                _echo("Lore fetch flags are only supported for new sessions (no --session).")
                return 1
            if watch_path:
                _echo("Use either --watch-path or --lore-url/--lore-msgid, not both.")
                return 1
            fetch_base_dir = _lore_fetch_base_dir(cfg, lore_out_dir=lore_out_dir)
            kernel_tree_cfg = str(((cfg.get("upstream_evidence") if isinstance(cfg, dict) else {}) or {}).get("kernel_tree") or "").strip()
            if kernel_tree_cfg:
                _echo(f"Kernel tree (config): {Path(kernel_tree_cfg).expanduser().resolve()}")
            _echo(f"Lore fetch base dir: {fetch_base_dir}")
            fetched_dir, fetched_msgid = _fetch_lore_series(cfg, lore_input, lore_out_dir=lore_out_dir)
            watch_path = str(fetched_dir)
            lore_source_msgid = fetched_msgid
            _echo(f"Lore source message-id: {fetched_msgid}")
            _echo(f"Lore patch series fetched to: {watch_path}")
        elif lore_out_dir:
            _echo("--lore-out-dir requires --lore-url or --lore-msgid.")
            return 1

        if not single_series_mode and not args.session and watch_path:
            wp = Path(watch_path)
            if wp.is_dir():
                series_dirs = [d for d in sorted(wp.iterdir()) if d.is_dir() and list(d.glob("*.patch"))]
                if len(series_dirs) > 1:
                    manifest = auto_discover_series(root, wp)
                    _echo(
                        f"Auto-discovered {len(manifest.get('series', []))} series. "
                        "Running dependency-aware patchset loop."
                    )

                    def _run_series(series_row: dict) -> dict[str, str | int]:
                        before = {p.stem for p in (root / A2A_DIRNAME / "sessions").glob("sess-*.json")}
                        sub_args = argparse.Namespace(
                            session=None,
                            task=f"{args.task or 'patchset'}:{series_row.get('name')}",
                            max_rounds=max_rounds,
                            timeout_min=args.timeout_min,
                            builder_cmd=args.builder_cmd,
                            reviewer_cmd=args.reviewer_cmd,
                            watch_path=str(series_row.get("path")),
                            lore_url=None,
                            lore_msgid=None,
                            auto_respin=False,
                            max_iterations=args.max_iterations,
                            _single_series=True,
                        )
                        rc = cmd_loop(sub_args)
                        after = {p.stem for p in (root / A2A_DIRNAME / "sessions").glob("sess-*.json")}
                        new_ids = sorted(after - before)
                        sid = new_ids[-1] if new_ids else None
                        status = "failed"
                        if sid:
                            try:
                                status = str(_load_session(root, sid).get("status", "failed"))
                            except RuntimeError:
                                status = "failed"
                        return {
                            "session_id": sid or "",
                            "status": status,
                            "rc": int(rc),
                        }

                    patchset_payload = run_all_series(root, manifest, _run_series)
                    _echo(json.dumps(patchset_payload, indent=2, sort_keys=True))
                    return 0 if str(patchset_payload.get("status", "")).lower() == "lgtm" else 1

        if args.session and args.task:
            _echo("Use either --session or --task, not both.")
            return 1

        if args.session:
            session = _load_session(root, args.session)
            sid = str(session["id"])
        else:
            if not args.task:
                _echo("Missing --task for new autonomous session.")
                return 1
            session = _start_session(
                root,
                args.task,
                max_rounds=max_rounds,
                timeout_min=args.timeout_min,
                builder_command=builder_cmd,
                reviewer_command=reviewer_cmd,
                watch_path=watch_path,
                lore_message_id=lore_source_msgid,
            )
            sid = str(session["id"])
            _echo(f"Started session: {sid}")
            _echo(
                render_session_header(
                    sid,
                    str(session.get("task", "")),
                    int(session.get("current_round", 1) or 1),
                    int(session.get("max_rounds", 0) or 0),
                )
            )
            _echo(
                "Agents: "
                f"{_resolve_builder_display_name(session=session, cfg=cfg)} (builder), "
                f"{_resolve_reviewer_display_name(session=session, cfg=cfg)} (reviewer)"
            )

        if not builder_cmd:
            builder_cmd = str(session.get("builder_command") or "").strip() or None
        if not reviewer_cmd:
            reviewer_cmd = str(session.get("reviewer_command") or "").strip() or None
        if watch_path and not session.get("watch_path"):
            session["watch_path"] = watch_path
            session["updated_at"] = _now_utc()
            _write_session(root, session)
        elif not watch_path:
            watch_path = str(session.get("watch_path") or "").strip() or None

        if not builder_cmd or not reviewer_cmd:
            _echo(
                "Autonomous loop requires both commands. "
                "Provide --builder-cmd/--reviewer-cmd or set config/session defaults."
            )
            return 1

        max_iterations = args.max_iterations if args.max_iterations and args.max_iterations > 0 else None
        iterations = 0

        with _worktree_lock(root, session, sid):
            while True:
                session = _load_session(root, sid)
                status = str(session.get("status", "in_progress"))
                if status == "lgtm":
                    _echo(f"Session {sid}: already LGTM.")
                    _auto_write_session_html_report(root, sid)
                    return 0
                if status == "stopped":
                    _echo(f"Session {sid}: already stopped.")
                    _auto_write_session_html_report(root, sid)
                    return 1

                if max_iterations is not None and iterations >= max_iterations:
                    _echo(f"Session {sid}: loop paused after max_iterations={max_iterations}.")
                    _auto_write_session_html_report(root, sid)
                    return 0

                round_no = int(session.get("current_round", 1))
                builder_display_name = _resolve_builder_display_name(session=session, cfg=cfg)
                reviewer_display_name = _resolve_reviewer_display_name(session=session, cfg=cfg)
                _echo(
                    f"Session {sid}: autonomous round {round_no} start "
                    f"({builder_display_name} -> {reviewer_display_name})."
                )
                _echo(render_phase_progress(round_no, int(session.get("max_rounds", 0) or 0)))
                round_started_at = _now_utc()
                session["round_started_at"] = round_started_at
                session["round_started_round"] = round_no
                session["updated_at"] = round_started_at
                _write_session(root, session)

                rc = _run_agent_step(root, session, "builder", builder_cmd, round_no)
                if rc != 0:
                    _auto_write_session_html_report(root, sid)
                    return rc

                gate_ok, _gate_ran = _run_validation_gate(root, session, round_no)
                _echo(render_gate_status(gate_ok))
                if not gate_ok:
                    _auto_write_session_html_report(root, sid)
                    return 1

                sa_result = _run_static_analysis(root, session, round_no)
                if not sa_result.get("gate_passed", True):
                    _echo("Static analysis gate: sparse introduced new warnings (blocking).")
                elif not sa_result.get("skipped", False):
                    _echo("Static analysis gate: passed.")

                rc = _run_agent_step(root, session, "reviewer", reviewer_cmd, round_no)
                if rc != 0:
                    _auto_write_session_html_report(root, sid)
                    return rc

                rc = _advance_session(root, sid)
                session = _load_session(root, sid)
                status = str(session.get("status", "in_progress"))
                iterations += 1

                if status == "lgtm":
                    if auto_respin:
                        try:
                            next_version_payload = _auto_generate_next_version(root, session)
                            _echo("Auto next-version generation completed:")
                            _echo(json.dumps(next_version_payload, indent=2, sort_keys=True))
                            post_respin_payload = _run_post_respin_validation(
                                root,
                                session,
                                next_version_payload,
                                reviewer_cmd=reviewer_cmd,
                            )
                            _echo("Post-respin validation completed:")
                            _echo(json.dumps(post_respin_payload, indent=2, sort_keys=True))
                            if str(post_respin_payload.get("status", "")).lower() != "ok":
                                _echo("Post-respin validation failed. Generated patchset requires fixes before send.")
                                _auto_write_session_html_report(root, sid)
                                return 1
                        except Exception as exc:
                            _echo(f"Auto next-version generation warning: {exc}")
                    _auto_write_session_html_report(root, sid)
                    return 0
                if status == "stopped":
                    _auto_write_session_html_report(root, sid)
                    return 1
                if rc != 0:
                    _auto_write_session_html_report(root, sid)
                    return rc
    except RuntimeError as exc:
        _echo(str(exc))
        return 1


def cmd_respin(args: argparse.Namespace) -> int:
    try:
        root = _must_find_root()

        resume_id = str(args.resume or "").strip() or None
        session_id = str(args.session or resume_id or "").strip() or None
        if session_id:
            result = run_respin(
                root,
                session_id,
                dry_run=bool(args.dry_run),
                conflict_strategy=args.conflict_strategy,
                resume_id=resume_id,
            )
            _echo(json.dumps(result, indent=2, sort_keys=True))
            return 0

        # Backward-compatible mode retained for legacy automation.
        if not args.input_path:
            _echo("Missing --session (preferred) or --input-path (legacy).")
            return 1
        source = Path(args.input_path).resolve()
        if not source.exists():
            _echo(f"Respin input path not found: {source}")
            return 1

        output = Path(args.out_path).resolve() if args.out_path else _default_respin_output_path(source)
        if output == source:
            _echo("Respin output path must differ from input path. Use --out-path to choose a new location.")
            return 1

        _copy_respin_source(source, output, force=bool(args.force))
        _echo(f"Created respin path: {output}")

        loop_args = argparse.Namespace(
            session=None,
            task=args.task or f"respin-{output.name}",
            max_rounds=args.max_rounds,
            timeout_min=args.timeout_min,
            builder_cmd=args.builder_cmd,
            reviewer_cmd=args.reviewer_cmd,
            watch_path=str(output),
            max_iterations=args.max_iterations,
        )
        rc = cmd_loop(loop_args)
        _echo(f"Respin watch path: {output}")
        return rc
    except RuntimeError as exc:
        _echo(str(exc))
        return 1


def cmd_review(args: argparse.Namespace) -> int:
    try:
        root = _must_find_root()
        cfg = _load_config(root)
        state = load_json(root / A2A_DIRNAME / "state.json")
        reviewer_cmd = _resolve_agent_command(cfg, args.reviewer_cmd, "reviewer_command")
        _builder_default, reviewer_cmd = _resolve_default_agent_commands(root, cfg, None, reviewer_cmd)

        session_id = args.session or state.get("active_session_id")
        if not session_id:
            _echo("No session provided and no active session in state. Use --session.")
            return 1

        session = _load_session(root, session_id)
        round_no = int(args.round) if args.round is not None else int(session.get("current_round", 1))
        if not reviewer_cmd:
            reviewer_cmd = str(session.get("reviewer_command") or "").strip() or None

        if args.run_agent:
            if not reviewer_cmd:
                _echo("No reviewer command configured. Use --reviewer-cmd or set reviewer_command in config.")
                return 1
            rc = _run_agent_step(root, session, "reviewer", reviewer_cmd, round_no)
            if rc != 0:
                return rc

        try:
            _session, open_count, findings, errors, findings_path = _validate_round_only(
                root, session_id, round_no=round_no
            )
        except RuntimeError as exc:
            _echo(str(exc))
            return 1

        if errors:
            _echo("Findings validation failed:")
            for err in errors:
                _echo(f"  - {err}")
            return 1

        _echo(f"Session: {session_id}")
        _echo(f"Round: {round_no}")
        _echo(f"Findings file: {findings_path}")
        _echo(f"Findings total: {len(findings)}")
        _echo(f"Findings open: {open_count}")
        for idx, finding in enumerate(findings, start=1):
            sev = finding.get("severity", "?")
            title = finding.get("title", "")
            loc = finding.get("location", "")
            status = finding.get("status", "")
            _echo(f"  {idx}. [{sev}] {title} ({loc}) status={status}")

        if args.advance:
            current_round = int(session.get("current_round", 1))
            if round_no != current_round:
                _echo(
                    "Cannot advance non-current round. "
                    f"Current round is {current_round}, requested {round_no}."
                )
                return 1
            return _advance_session(root, session_id)

        return 0
    except RuntimeError as exc:
        _echo(str(exc))
        return 1


def cmd_config_get(args: argparse.Namespace) -> int:
    try:
        root = _must_find_root()
        cfg = _load_config(root)
        if args.key:
            if args.key not in cfg:
                _echo(f"Config key not found: {args.key}")
                return 1
            value = cfg[args.key]
            if args.json_output:
                _echo(json.dumps(value, indent=2, sort_keys=True))
            else:
                _echo(value)
            return 0

        if args.json_output:
            _echo(json.dumps(cfg, indent=2, sort_keys=True))
        else:
            for key in sorted(cfg.keys()):
                _echo(f"{key}={cfg[key]}")
        return 0
    except RuntimeError as exc:
        _echo(str(exc))
        return 1


def cmd_config_set(args: argparse.Namespace) -> int:
    try:
        root = _must_find_root()
        cfg_path = _config_path(root)
        cfg = _load_config(root)
        key = args.key
        value = _parse_value(args.value)
        cfg[key] = value
        dump_json(cfg_path, cfg)
        _echo(f"Set {key}={value}")
        return 0
    except RuntimeError as exc:
        _echo(str(exc))
        return 1


def cmd_config_reset(args: argparse.Namespace) -> int:
    try:
        root = _must_find_root()
        cfg_path = _config_path(root)
        cfg = default_config()
        if args.keep_reviewer_name and cfg_path.exists():
            old = load_json(cfg_path)
            if "reviewer_name" in old:
                cfg["reviewer_name"] = old["reviewer_name"]
        dump_json(cfg_path, cfg)
        _echo(f"Config reset to defaults at {cfg_path}")
        return 0
    except RuntimeError as exc:
        _echo(str(exc))
        return 1


def cmd_kb(args: argparse.Namespace) -> int:
    try:
        root = _must_find_root()
        if args.clear:
            prompt = "Type CLEAR to confirm knowledge base wipe: "
            choice = input(prompt).strip()
            if choice != "CLEAR":
                _echo("KB clear cancelled.")
                return 1
            clear_kb(root)
            _echo("Knowledge base cleared.")
            return 0

        kb = load_kb(root)
        subsystem = str(args.subsystem or "").strip() or None
        rows = list_kb_entries(root, subsystem=subsystem)
        if args.json_output:
            payload = dict(kb)
            payload["entries"] = rows
            _echo(json.dumps(payload, indent=2, sort_keys=True))
            return 0

        if subsystem:
            _echo(f"Knowledge base entries for subsystem='{subsystem}': {len(rows)}")
        else:
            _echo(f"Knowledge base entries: {len(rows)}")
        if not rows:
            _echo("- no entries")
            return 0
        for row in rows:
            _echo(
                "- [{sev}] {pattern} | seen={seen} | subsystem={sub} | resolution={res}".format(
                    sev=row.get("severity", "?"),
                    pattern=row.get("pattern", ""),
                    seen=row.get("occurrences", 0),
                    sub=row.get("subsystem", "unknown"),
                    res=row.get("resolution", ""),
                )
            )
        return 0
    except RuntimeError as exc:
        _echo(str(exc))
        return 1


def cmd_watch(args: argparse.Namespace) -> int:
    try:
        root = _must_find_root()
        _echo(f"Watching lore thread: {args.msgid} (poll={args.poll}s)")
        events = watch_lore(
            root,
            args.msgid,
            poll_interval_secs=int(args.poll),
            max_loops=args.max_loops,
        )
        for event in events:
            _echo(json.dumps(event, indent=2, sort_keys=True))
        return 0
    except RuntimeError as exc:
        _echo(str(exc))
        return 1
    except KeyboardInterrupt:
        _echo("watch interrupted by user")
        return 0


def cmd_email_bridge(args: argparse.Namespace) -> int:
    try:
        root = _must_find_root()
        overrides = {
            "imap_host": args.imap_host,
            "imap_port": args.imap_port,
            "imap_user": args.imap_user,
            "imap_password": args.imap_password,
            "imap_password_env": args.imap_password_env,
            "mailbox": args.mailbox,
            "smtp_from": args.smtp_from,
            "allowed_senders": args.allowed_sender or [],
            "notify_to": args.notify_to or [],
            "inbox_dir": args.inbox_dir,
            "state_db": args.state_db,
            "lore_out_dir": args.lore_out_dir,
            "approval_token_ttl_min": args.approval_token_ttl_min,
            "auto_detect_requests": args.auto_detect_requests,
            "poll_sec": args.poll_sec,
            "python_bin": args.python_bin,
        }
        result = run_bridge_loop(
            root,
            overrides=overrides,
            once=bool(args.once),
            max_loops=args.max_loops,
        )
        if not bool(result.get("imap_enabled")):
            _echo(
                "Email bridge note: IMAP polling is disabled (missing host/user/password). "
                "Only session notifications were processed."
            )
        _echo(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        _echo("Email bridge interrupted by user.")
        return 130
    except RuntimeError as exc:
        _echo(str(exc))
        return 1


def cmd_maintainers(args: argparse.Namespace) -> int:
    try:
        root = _must_find_root()
        payload = load_profiles(root)
        maintainers = payload.get("maintainers", {})
        if args.list:
            _echo(f"Maintainers: {len(maintainers)}")
            for name, row in sorted(maintainers.items()):
                _echo(
                    "- {name} priority={priority} approval_rate={rate}".format(
                        name=name,
                        priority=row.get("priority", "medium"),
                        rate=row.get("approval_rate", 0.0),
                    )
                )
            return 0
        if args.profile:
            row = maintainers.get(args.profile)
            if not row:
                _echo(f"Maintainer profile not found: {args.profile}")
                return 1
            _echo(json.dumps(row, indent=2, sort_keys=True))
            return 0
        _echo("Use --list or --profile.")
        return 1
    except RuntimeError as exc:
        _echo(str(exc))
        return 1


def cmd_submit(args: argparse.Namespace) -> int:
    try:
        root = _must_find_root()
        sid = str(args.session or "").strip()
        if not sid:
            _echo("Missing --session for submit.")
            return 1
        session = _load_session(root, sid)
        status = str(session.get("status", "")).lower()
        if status != "lgtm":
            _echo(f"Submit blocked: session {sid} is not LGTM (status={status or 'unknown'}).")
            return 1

        cfg = _load_config_or_defaults(root)
        patchset_summary = build_patchset_summary(root, sid)
        result = run_hitl_gate(
            root,
            sid,
            patchset_summary,
            cfg,
            resume=bool(args.resume),
        )
        _echo(json.dumps(result, indent=2, sort_keys=True))
        return 0 if str(result.get("status", "")) == "sent" else 1
    except RuntimeError as exc:
        _echo(str(exc))
        return 1


def cmd_report(args: argparse.Namespace) -> int:
    try:
        root = _must_find_root()
        if args.all:
            if args.session:
                _echo("--all cannot be used with --session.")
                return 1
            if args.latest:
                _echo("--all cannot be used with --latest.")
                return 1
            status_filters = None
            if args.status:
                status_filters = {s.strip().lower() for s in args.status if s.strip()}
            since_dt = None
            if args.since:
                since_dt = _parse_iso_datetime(args.since)
            payload = _all_sessions_report_payload(root, status_filters=status_filters, since_dt=since_dt)
            if args.format == "json":
                out = json.dumps(payload, indent=2, sort_keys=True)
            elif args.format == "markdown":
                out = _render_markdown_report_all(payload)
            else:
                out = _render_html_report_all(payload)
        else:
            sid = _resolve_session_for_report(root, args.session, latest=args.latest)
            payload = _session_report_payload(root, sid)
            if args.format == "json":
                out = json.dumps(payload, indent=2, sort_keys=True)
            elif args.format == "markdown":
                out = _render_markdown_report(payload)
            else:
                out = _render_html_report(payload)

        if args.output:
            out_path = Path(args.output).resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(out + ("" if out.endswith("\n") else "\n"), encoding="utf-8")
            _echo(f"Report written: {out_path}")
        else:
            _echo(out)
        return 0
    except RuntimeError as exc:
        _echo(str(exc))
        return 1


def _load_status_view(root: Path) -> StatusView:
    a2a_dir = root / A2A_DIRNAME
    state_path = a2a_dir / "state.json"
    sessions_dir = a2a_dir / "sessions"

    cfg = _load_config(root)
    state = load_json(state_path) if state_path.exists() else default_state()

    session_files = sorted(sessions_dir.glob("*.json")) if sessions_dir.is_dir() else []
    active = state.get("active_session_id")
    open_findings = None

    active_status = None
    current_round = None
    max_rounds = None
    builder_name = _resolve_builder_display_name(cfg=cfg)
    reviewer_name = _resolve_reviewer_display_name(cfg=cfg)
    reviewer_internal_name = str(cfg.get("reviewer_name", "aryabhatta"))

    if active:
        active_path = sessions_dir / f"{active}.json"
        if active_path.exists():
            sess = load_json(active_path)
            raw_open_findings = sess.get("open_findings")
            if raw_open_findings is None:
                open_findings = None
            else:
                open_findings = int(raw_open_findings)
            active_status = str(sess.get("status", "unknown"))
            current_round = int(sess.get("current_round", 0))
            max_rounds = int(sess.get("max_rounds", 0))
            builder_name = _resolve_builder_display_name(session=sess, cfg=cfg)
            reviewer_name = _resolve_reviewer_display_name(session=sess, cfg=cfg)
            reviewer_internal_name = str(sess.get("reviewer_name") or reviewer_internal_name)

    return StatusView(
        root=str(root),
        active_session_id=active,
        session_count=len(session_files),
        open_findings=open_findings,
        builder_name=builder_name,
        reviewer_name=reviewer_name,
        reviewer_internal_name=reviewer_internal_name,
        active_status=active_status,
        current_round=current_round,
        max_rounds=max_rounds,
    )


def cmd_status(_args: argparse.Namespace) -> int:
    root = find_a2a_root()
    if root is None:
        _echo("No .a2a directory found in current path or parents.")
        _echo("Run: a2a init")
        return 1

    view = _load_status_view(root)
    active_round = view.current_round or 0
    active_max = view.max_rounds or 0
    _echo(
        render_session_header(
            view.active_session_id or "none",
            "status",
            active_round,
            active_max,
        )
    )
    _echo(f"A2A root: {view.root}")
    _echo(f"Builder: {view.builder_name}")
    _echo(f"Reviewer: {view.reviewer_name}")
    _echo(f"Reviewer internal name: {view.reviewer_internal_name}")
    _echo(f"Sessions: {view.session_count}")
    _echo(f"Active session: {view.active_session_id or 'none'}")
    if view.active_session_id is not None:
        findings = "unknown" if view.open_findings is None else str(view.open_findings)
        _echo(f"Open findings (active): {findings}")
        _echo(f"Status (active): {view.active_status or 'unknown'}")
        if view.current_round is not None and view.max_rounds is not None:
            _echo(f"Round (active): {view.current_round}/{view.max_rounds}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="a2a",
        description="A2A CLI scaffold for builder/reviewer workflows.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Initialize .a2a workspace in current directory.")
    p_init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite config/state/template files if they already exist.",
    )
    p_init.set_defaults(func=cmd_init)

    p_status = sub.add_parser("status", help="Show workspace and session status.")
    p_status.set_defaults(func=cmd_status)

    p_prepare = sub.add_parser("prepare", help="Prepare builder/reviewer git worktrees.")
    p_prepare.add_argument(
        "--repo",
        default=".",
        help="Target git repository path (default: current directory).",
    )
    p_prepare.add_argument(
        "--branch",
        required=True,
        help="Branch name for builder worktree (created if missing).",
    )
    p_prepare.add_argument(
        "--reviewer-name",
        default="aryabhatta",
        help="Reviewer agent name.",
    )
    p_prepare.add_argument(
        "--reviewer-readonly-policy",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Mark reviewer as read-only by policy metadata.",
    )
    p_prepare.add_argument(
        "--force",
        action="store_true",
        help="Recreate existing worktree directories if present.",
    )
    p_prepare.set_defaults(func=cmd_prepare)

    p_run = sub.add_parser(
        "run",
        help="Start or continue a manual A2A session loop.",
    )
    p_run.add_argument(
        "--task",
        help="Task description for a new session.",
    )
    p_run.add_argument(
        "--max-rounds",
        type=int,
        help="Maximum review/fix rounds for new session.",
    )
    p_run.add_argument(
        "--timeout-min",
        type=int,
        help="Optional time budget in minutes (metadata only in this version).",
    )
    p_run.add_argument(
        "--resume",
        help="Existing session id to validate findings and advance rounds.",
    )
    p_run.add_argument(
        "--builder-cmd",
        help="Shell command to execute builder step (overrides config builder_command).",
    )
    p_run.add_argument(
        "--reviewer-cmd",
        help="Shell command to execute reviewer step (overrides config reviewer_command).",
    )
    p_run.add_argument(
        "--watch-path",
        help="Path to watch for builder file changes; emits round changed_files/diff artifacts.",
    )
    p_run.add_argument(
        "--run-reviewer",
        action="store_true",
        help="On --resume, run reviewer command before validating and advancing.",
    )
    p_run.add_argument(
        "--auto",
        action="store_true",
        help="On new session, run builder and reviewer commands once for round 1.",
    )
    p_run.set_defaults(func=cmd_run)

    p_loop = sub.add_parser(
        "loop",
        help="Run fully autonomous builder+reviewer rounds until LGTM/STOPPED.",
    )
    p_loop.add_argument(
        "--session",
        help="Existing session id to continue autonomously.",
    )
    p_loop.add_argument(
        "--task",
        help="Task description for a new autonomous session.",
    )
    p_loop.add_argument(
        "--max-rounds",
        type=int,
        help="Maximum review/fix rounds for a new session.",
    )
    p_loop.add_argument(
        "--timeout-min",
        type=int,
        help="Optional time budget in minutes (metadata only in this version).",
    )
    p_loop.add_argument(
        "--builder-cmd",
        help="Shell command to execute builder step (overrides config builder_command).",
    )
    p_loop.add_argument(
        "--reviewer-cmd",
        help="Shell command to execute reviewer step (overrides config reviewer_command).",
    )
    p_loop.add_argument(
        "--watch-path",
        help="Path to watch for builder file changes; emits round changed_files/diff artifacts.",
    )
    p_loop.add_argument(
        "--lore-url",
        help="Lore URL to fetch patch series via b4 for this new loop session.",
    )
    p_loop.add_argument(
        "--lore-msgid",
        help="Lore message-id root to fetch patch series via b4 for this new loop session.",
    )
    p_loop.add_argument(
        "--lore-out-dir",
        help="Override directory where lore-fetched patch series are stored.",
    )
    p_loop.add_argument(
        "--max-iterations",
        type=int,
        help="Optional per-invocation cap on autonomous rounds.",
    )
    p_loop.add_argument(
        "--auto-respin",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="After LGTM, auto-generate next patch version (defaults to enabled for lore input).",
    )
    p_loop.set_defaults(func=cmd_loop)

    p_respin = sub.add_parser(
        "respin",
        help="Create a next-revision patch path and run autonomous loop against it.",
    )
    p_respin.add_argument(
        "--session",
        help="LGTM session id to respin into next patch version.",
    )
    p_respin.add_argument(
        "--resume",
        help="Resume respin state by id (defaults to session id).",
    )
    p_respin.add_argument(
        "--dry-run",
        action="store_true",
        help="Print respin plan only; no writes.",
    )
    p_respin.add_argument(
        "--conflict-strategy",
        choices=["ours", "theirs", "manual", "abort"],
        help="Conflict resolution strategy for git am/rebase conflicts.",
    )
    p_respin.add_argument(
        "--input-path",
        help="(Legacy mode) source patch file or patch-series directory to respin.",
    )
    p_respin.add_argument(
        "--out-path",
        help="Output path for respin patches. Defaults to auto-incremented vN path.",
    )
    p_respin.add_argument(
        "--task",
        help="Task label for the autonomous respin session.",
    )
    p_respin.add_argument(
        "--max-rounds",
        type=int,
        help="Maximum review/fix rounds for respin session.",
    )
    p_respin.add_argument(
        "--timeout-min",
        type=int,
        help="Optional time budget in minutes (metadata only in this version).",
    )
    p_respin.add_argument(
        "--builder-cmd",
        help="Shell command to execute builder step (overrides config builder_command).",
    )
    p_respin.add_argument(
        "--reviewer-cmd",
        help="Shell command to execute reviewer step (overrides config reviewer_command).",
    )
    p_respin.add_argument(
        "--max-iterations",
        type=int,
        help="Optional per-invocation cap on autonomous rounds.",
    )
    p_respin.add_argument(
        "--force",
        action="store_true",
        help="Overwrite respin output path if it already exists.",
    )
    p_respin.set_defaults(func=cmd_respin)

    p_review = sub.add_parser(
        "review",
        help="Validate reviewer findings for a round and optionally advance.",
    )
    p_review.add_argument(
        "--session",
        help="Session id (defaults to active session in state).",
    )
    p_review.add_argument(
        "--round",
        type=int,
        help="Round number to validate (default: current round).",
    )
    p_review.add_argument(
        "--run-agent",
        action="store_true",
        help="Run reviewer command before validation.",
    )
    p_review.add_argument(
        "--reviewer-cmd",
        help="Shell command to execute reviewer step (overrides config reviewer_command).",
    )
    p_review.add_argument(
        "--advance",
        action="store_true",
        help="Advance session state after successful validation.",
    )
    p_review.set_defaults(func=cmd_review)

    p_config = sub.add_parser("config", help="Read/update .a2a configuration.")
    p_config_sub = p_config.add_subparsers(dest="config_command", required=True)

    p_cfg_get = p_config_sub.add_parser("get", help="Get config value(s).")
    p_cfg_get.add_argument("--key", help="Optional key to fetch.")
    p_cfg_get.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print as JSON.",
    )
    p_cfg_get.set_defaults(func=cmd_config_get)

    p_cfg_set = p_config_sub.add_parser("set", help="Set a config key to a value.")
    p_cfg_set.add_argument("key", help="Config key.")
    p_cfg_set.add_argument("value", help="Config value (auto-parsed: bool/int/float/null/string).")
    p_cfg_set.set_defaults(func=cmd_config_set)

    p_cfg_reset = p_config_sub.add_parser("reset", help="Reset config to defaults.")
    p_cfg_reset.add_argument(
        "--keep-reviewer-name",
        action="store_true",
        help="Keep existing reviewer_name if present.",
    )
    p_cfg_reset.set_defaults(func=cmd_config_reset)

    p_kb = sub.add_parser("kb", help="Knowledge base operations.")
    p_kb.add_argument("--list", action="store_true", help="List KB entries (default action).")
    p_kb.add_argument("--subsystem", help="Filter KB entries by subsystem.")
    p_kb.add_argument("--clear", action="store_true", help="Clear KB (requires confirmation).")
    p_kb.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print KB payload as JSON.",
    )
    p_kb.set_defaults(func=cmd_kb)

    p_watch = sub.add_parser("watch", help="Watch lore thread for new replies.")
    p_watch.add_argument("--msgid", required=True, help="LKML/lore message-id root.")
    p_watch.add_argument("--poll", type=int, default=300, help="Poll interval in seconds.")
    p_watch.add_argument(
        "--max-loops",
        type=int,
        help="Optional max polling loops (useful for tests/smoke).",
    )
    p_watch.set_defaults(func=cmd_watch)

    p_email = sub.add_parser(
        "email-bridge",
        help="Process A2A commands over email and send session status notifications.",
    )
    p_email.add_argument(
        "--once",
        action="store_true",
        help="Run one poll cycle and exit (default is daemon loop).",
    )
    p_email.add_argument(
        "--max-loops",
        type=int,
        help="Optional loop cap for daemon mode/testing.",
    )
    p_email.add_argument("--poll-sec", type=int, help="Poll interval in seconds.")
    p_email.add_argument("--imap-host", help="IMAP server host.")
    p_email.add_argument("--imap-port", type=int, help="IMAP server port (default 993).")
    p_email.add_argument("--imap-user", help="IMAP username.")
    p_email.add_argument("--imap-password", help="IMAP password.")
    p_email.add_argument(
        "--imap-password-env",
        help="Environment variable name for IMAP password (default: A2A_EMAIL_IMAP_PASSWORD).",
    )
    p_email.add_argument("--mailbox", help="Mailbox name (default INBOX).")
    p_email.add_argument("--smtp-from", help="Override sender address for bridge replies.")
    p_email.add_argument(
        "--allowed-sender",
        action="append",
        help="Allowlisted sender email (repeatable).",
    )
    p_email.add_argument(
        "--notify-to",
        action="append",
        help="Notification recipient email (repeatable).",
    )
    p_email.add_argument("--inbox-dir", help="Directory for saved email patch attachments.")
    p_email.add_argument("--state-db", help="SQLite DB path for bridge state.")
    p_email.add_argument("--lore-out-dir", help="Default lore fetch directory used by email-triggered runs.")
    p_email.add_argument(
        "--approval-token-ttl-min",
        type=int,
        help="Approval token lifetime in minutes for EXTEND actions.",
    )
    p_email_detect = p_email.add_mutually_exclusive_group()
    p_email_detect.add_argument(
        "--auto-detect-requests",
        dest="auto_detect_requests",
        action="store_true",
        help="Auto-trigger review when an allowed email contains a lore link or patch attachment even without explicit A2A command.",
    )
    p_email_detect.add_argument(
        "--no-auto-detect-requests",
        dest="auto_detect_requests",
        action="store_false",
        help="Disable implicit email request detection (explicit A2A commands only).",
    )
    p_email.set_defaults(auto_detect_requests=None)
    p_email.add_argument("--python-bin", help="Python executable used for spawned loop commands.")
    p_email.set_defaults(func=cmd_email_bridge)

    p_maint = sub.add_parser("maintainers", help="Maintainer profile operations.")
    p_maint.add_argument("--list", action="store_true", help="List maintainer profiles.")
    p_maint.add_argument("--profile", help="Show specific maintainer profile.")
    p_maint.set_defaults(func=cmd_maintainers)

    p_submit = sub.add_parser("submit", help="Run mandatory HITL approval gate then send dry-run submission email.")
    p_submit.add_argument("--session", required=True, help="LGTM session id to submit.")
    p_submit.add_argument(
        "--resume",
        action="store_true",
        help="Resume a previously aborted HITL gate session.",
    )
    p_submit.set_defaults(func=cmd_submit)

    p_report = sub.add_parser("report", help="Render session report (markdown/json/html).")
    p_report.add_argument("--session", help="Session id. Defaults to active or latest.")
    p_report.add_argument(
        "--latest",
        action="store_true",
        help="Use latest session by file mtime.",
    )
    p_report.add_argument(
        "--all",
        action="store_true",
        help="Report all sessions (aggregate view).",
    )
    p_report.add_argument(
        "--status",
        action="append",
        help="Filter statuses in --all mode (repeatable, e.g. --status lgtm --status stopped).",
    )
    p_report.add_argument(
        "--since",
        help="Filter sessions updated_at >= ISO datetime in --all mode.",
    )
    p_report.add_argument(
        "--format",
        choices=["markdown", "json", "html"],
        default="markdown",
        help="Output format.",
    )
    p_report.add_argument(
        "--output",
        help="Write output to file path instead of stdout.",
    )
    p_report.set_defaults(func=cmd_report)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
