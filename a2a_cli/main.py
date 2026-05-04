import argparse
import builtins
import difflib
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
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

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


def _next_session_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    return f"sess-{stamp}"


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
    prefix_match = _REV_PREFIX_RE.match(stem)
    if prefix_match:
        num = int(prefix_match.group("num"))
        return f"v{num + 1}-{prefix_match.group('rest')}"

    trailing_match = _REV_TRAILING_RE.match(stem)
    if trailing_match:
        num = int(trailing_match.group("num"))
        return (
            f"{trailing_match.group('prefix')}"
            f"{trailing_match.group('sep')}v{num + 1}"
        )

    return f"v2-{stem}"


def _default_respin_output_path(source: Path) -> Path:
    if source.is_dir():
        name_match = _REV_TRAILING_RE.match(source.name)
        if name_match:
            num = int(name_match.group("num"))
            next_name = (
                f"{name_match.group('prefix')}"
                f"{name_match.group('sep')}v{num + 1}"
            )
        else:
            next_name = f"{source.name}_v2"
        return source.parent / next_name

    if source.is_file():
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


def _bump_patch_subject_versions(patch_files: list[Path]) -> int:
    detected_max = 1
    for patch in patch_files:
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
            break
    target = detected_max + 1

    for patch in patch_files:
        try:
            text = patch.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        replaced = False
        out_lines: list[str] = []
        for line in text.splitlines():
            if replaced:
                out_lines.append(line)
                continue
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
            replaced = True

        if replaced:
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


def _generate_lore_next_version(root: Path, session: dict) -> dict:
    session_id = str(session.get("id") or "")
    watch_raw = str(session.get("watch_path") or "").strip()
    if not watch_raw:
        raise RuntimeError("Cannot generate next version: session watch_path is empty.")
    source = Path(watch_raw).resolve()
    if not source.exists():
        raise RuntimeError(f"Cannot generate next version: watch_path not found: {source}")

    output = _default_respin_output_path(source)
    _copy_respin_source(source, output, force=False)

    patch_files: list[Path] = []
    if output.is_file() and output.suffix == ".patch":
        patch_files = [output]
    elif output.is_dir():
        patch_files = sorted(p for p in output.rglob("*.patch") if p.is_file())
    if not patch_files:
        raise RuntimeError(f"No patch files found in generated output path: {output}")

    next_version = _bump_patch_subject_versions(patch_files)
    payload = {
        "status": "ok",
        "kind": "lore_copy",
        "session_id": session_id,
        "source_watch_path": str(source),
        "output_path": str(output),
        "patch_count": len(patch_files),
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
        f"- elapsed_seconds: {timing.get('elapsed_seconds')}",
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
        },
        "totals": totals,
        "rounds": rounds,
        "prior_comment_summary": prior_comment_summary,
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
            lines.append(
                "- round {round}: findings_total={total}, findings_open={open}, "
                "builder_patch_gauge={gauge}, builder_confidence={bconf}, reviewer_confidence={rconf}, "
                "gate_passed={gate_passed}, gate_failures={gate_failures}, "
                "summary_json={summary_json}, validated_at={ts}".format(
                    round=r.get("round"),
                    total=r.get("findings_total"),
                    open=r.get("findings_open"),
                    gauge=r.get("builder_patch_gauge"),
                    bconf=r.get("builder_confidence"),
                    rconf=r.get("reviewer_confidence"),
                    gate_passed=r.get("gate_passed"),
                    gate_failures=r.get("gate_failures"),
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

    session_id = _next_session_id()
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

        while True:
            session = _load_session(root, sid)
            status = str(session.get("status", "in_progress"))
            if status == "lgtm":
                _echo(f"Session {sid}: already LGTM.")
                return 0
            if status == "stopped":
                _echo(f"Session {sid}: already stopped.")
                return 1

            if max_iterations is not None and iterations >= max_iterations:
                _echo(f"Session {sid}: loop paused after max_iterations={max_iterations}.")
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
                return rc

            gate_ok, _gate_ran = _run_validation_gate(root, session, round_no)
            _echo(render_gate_status(gate_ok))
            if not gate_ok:
                return 1

            sa_result = _run_static_analysis(root, session, round_no)
            if not sa_result.get("gate_passed", True):
                _echo("Static analysis gate: sparse introduced new warnings (blocking).")
            elif not sa_result.get("skipped", False):
                _echo("Static analysis gate: passed.")

            rc = _run_agent_step(root, session, "reviewer", reviewer_cmd, round_no)
            if rc != 0:
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
                    except Exception as exc:
                        _echo(f"Auto next-version generation warning: {exc}")
                return 0
            if status == "stopped":
                return 1
            if rc != 0:
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
            else:
                out = _render_markdown_report_all(payload)
        else:
            sid = _resolve_session_for_report(root, args.session, latest=args.latest)
            payload = _session_report_payload(root, sid)
            if args.format == "json":
                out = json.dumps(payload, indent=2, sort_keys=True)
            else:
                out = _render_markdown_report(payload)

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

    p_report = sub.add_parser("report", help="Render session report (markdown/json).")
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
        choices=["markdown", "json"],
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
