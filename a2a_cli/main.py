import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .adapters.shell_adapter import run_shell_command
from .config import A2A_DIRNAME, default_config, default_state, dump_json, load_json
from .prior_review import (
    augment_findings_with_prior_comments,
    ingest_prior_review_context,
    load_prior_comments,
    render_prior_comment_matrix,
)
from .types import StatusView


_REV_TRAILING_RE = re.compile(r"^(?P<prefix>.*?)(?P<sep>[_-])v(?P<num>\d+)$", re.IGNORECASE)
_REV_PREFIX_RE = re.compile(r"^v(?P<num>\d+)-(?P<rest>.+)$", re.IGNORECASE)


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

    print(f"Initialized A2A workspace in {a2a_dir}")
    if written:
        print("Created/updated files:")
        for entry in written:
            print(f"  - {entry}")
    else:
        print("No files changed (already initialized).")
    return 0


def _as_json(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _state_path(root: Path) -> Path:
    return root / A2A_DIRNAME / "state.json"


def _prepare_path(root: Path) -> Path:
    return root / A2A_DIRNAME / "prepare.json"


def _report_dir(root: Path, session_id: str) -> Path:
    return root / A2A_DIRNAME / "reports" / session_id


def _next_session_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    return f"sess-{stamp}"


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


def _write_round_templates(root: Path, session_id: str, round_no: int, reviewer_name: str) -> None:
    files = _round_files(root, session_id, round_no, reviewer_name)
    report_dir = files["report_dir"]
    builder_file = files["builder"]
    reviewer_file = files["reviewer"]
    findings_file = files["findings"]
    report_dir.mkdir(parents=True, exist_ok=True)

    builder_tpl = (
        f"# Round {round_no}: Builder Output\n\n"
        "## Changes\n- \n\n"
        "## Rationale\n- \n\n"
        "## Verification Commands\n- \n\n"
        "## Response To Reviewer Findings\n- \n"
    )
    reviewer_tpl = (
        f"# Round {round_no}: {reviewer_name} Review\n\n"
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


def _load_session(root: Path, session_id: str) -> dict:
    path = _session_path(root, session_id)
    if not path.exists():
        raise RuntimeError(f"Session not found: {session_id}")
    return load_json(path)


def _write_session(root: Path, session: dict) -> None:
    dump_json(_session_path(root, str(session["id"])), session)


def _agent_env(session: dict, round_no: int, files: dict[str, Path], role: str) -> dict[str, str]:
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
    return _clamp_score(base)


def _compute_reviewer_confidence(findings: list[dict]) -> int:
    if not findings:
        return 82

    total = len(findings)
    with_source_id = 0
    evidence_items_total = 0
    evidence_missing = 0
    with_location = 0
    for finding in findings:
        source_id = str(finding.get("source_comment_id") or "").strip()
        if source_id:
            with_source_id += 1
        loc = str(finding.get("location") or "")
        if ":" in loc:
            with_location += 1

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

    score = 58
    score += int(source_ratio * 18)
    score += int(location_ratio * 12)
    score += int(min(avg_evidence, 4.0) * 4)
    score -= evidence_missing * 6
    return _clamp_score(score)


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
            linked = by_source.get(source_id, [])
            closed = [f for f in linked if str(f.get("status", "")).lower() == "closed"]
            selected = closed[0] if closed else (linked[0] if linked else None)
            status = "closed" if closed else "open"
            history_by_id.setdefault(source_id, []).append(
                {
                    "round": round_no,
                    "status": status,
                    "location": str(selected.get("location") or "") if selected else "",
                    "evidence": selected.get("evidence") if selected else [],
                }
            )

    rows: list[dict] = []
    for comment in prior_comments:
        source_id = str(comment.get("id") or "").strip()
        if not source_id:
            continue
        history = history_by_id.get(source_id, [])
        initial_status = history[0]["status"] if history else "open"
        current_status = history[-1]["status"] if history else "open"

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

        fixed_by_a2a = bool(initial_status == "open" and current_status == "closed")
        rows.append(
            {
                "source_comment_id": source_id,
                "from": str(comment.get("from") or ""),
                "subject": str(comment.get("subject") or ""),
                "initial_status": initial_status,
                "current_status": current_status,
                "closed_round": closed_round,
                "fixed_by_a2a": fixed_by_a2a,
                "already_fixed_before_a2a": bool(initial_status == "closed"),
                "latest_location": latest_location,
                "latest_evidence": latest_evidence,
            }
        )

    return rows


def _run_agent_step(root: Path, session: dict, role: str, command: str, round_no: int) -> int:
    reviewer_name = str(session.get("reviewer_name", "aryabhatta"))
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

    env = _agent_env(session, round_no, files, role)
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
        print(f"{role} command failed (rc={result['returncode']}). See log: {log_path}")
        return int(result["returncode"])

    if role == "builder" and watch_path:
        watch_after = None
        try:
            watch_after = _snapshot_text_files(Path(str(watch_path)))
        except OSError:
            watch_after = None
        _write_builder_change_artifacts(root, session, round_no, watch_before, watch_after)

    print(f"{role} command completed. Log: {log_path}")
    if role == "builder":
        print(f"{role} report: {files['builder']}")
    else:
        print(f"{role} report: {files['reviewer']}")
        print(f"{role} findings: {files['findings']}")
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
    session = _load_session(root, session_id)
    raw_rounds = sorted(session.get("rounds", []), key=lambda r: int(r.get("round", 0)))
    rounds: list[dict] = []
    previous_open: int | None = None
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

        previous_open = findings_open
        rounds.append(row)

    prior_comment_summary = _build_prior_comment_summary(session, rounds)
    prior_totals = {
        "comments_total": len(prior_comment_summary),
        "comments_closed": len([r for r in prior_comment_summary if r.get("current_status") == "closed"]),
        "comments_open": len([r for r in prior_comment_summary if r.get("current_status") != "closed"]),
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
            "comment_status_totals": prior_totals,
        }

    totals = {
        "rounds_validated": len(rounds),
        "findings_total": 0,
        "findings_open_last": session.get("open_findings"),
    }
    for record in rounds:
        totals["findings_total"] += int(record.get("findings_total", 0))

    payload = {
        "session": {
            "id": session.get("id"),
            "task": session.get("task"),
            "status": session.get("status"),
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at"),
            "max_rounds": session.get("max_rounds"),
            "current_round": session.get("current_round"),
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
    lines = [
        f"# A2A Report: {sess['id']}",
        "",
        f"- task: {sess.get('task')}",
        f"- status: {sess.get('status')}",
        f"- reviewer: {sess.get('reviewer_name')}",
        f"- repo: {sess.get('repo_path')}",
        f"- branch: {sess.get('branch')}",
        f"- rounds_validated: {totals.get('rounds_validated')}",
        f"- findings_total: {totals.get('findings_total')}",
        f"- findings_open_last: {totals.get('findings_open_last')}",
    ]
    if isinstance(prior, dict):
        lines.append(f"- prior_comments_total: {prior.get('comments_total')}")
        lines.append(f"- prior_sources_total: {prior.get('source_total')}")
        lines.append(f"- prior_search_used: {prior.get('search_used')}")
        status_totals = prior.get("comment_status_totals")
        if isinstance(status_totals, dict):
            lines.append(f"- prior_comments_closed: {status_totals.get('comments_closed')}")
            lines.append(f"- prior_comments_open: {status_totals.get('comments_open')}")
            lines.append(f"- prior_fixed_by_a2a: {status_totals.get('fixed_by_a2a')}")

    lines.extend(["", "## Rounds", ""])
    if not rounds:
        lines.append("- no validated rounds yet")
    else:
        for r in rounds:
            lines.append(
                "- round {round}: findings_total={total}, findings_open={open}, "
                "builder_patch_gauge={gauge}, builder_confidence={bconf}, reviewer_confidence={rconf}, validated_at={ts}".format(
                    round=r.get("round"),
                    total=r.get("findings_total"),
                    open=r.get("findings_open"),
                    gauge=r.get("builder_patch_gauge"),
                    bconf=r.get("builder_confidence"),
                    rconf=r.get("reviewer_confidence"),
                    ts=r.get("validated_at"),
                )
            )

    lines.extend(["", "## Prior Comment Summary", ""])
    if not prior_comment_summary:
        lines.append("- no prior comments tracked")
    else:
        lines.extend(
            [
                "| source_comment_id | subject | initial | current | fixed_by_a2a | closed_round | latest_location |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for row in prior_comment_summary:
            lines.append(
                "| {id} | {subject} | {initial} | {current} | {fixed} | {closed_round} | {loc} |".format(
                    id=str(row.get("source_comment_id") or "").replace("|", "\\|"),
                    subject=str(row.get("subject") or "").replace("|", "\\|"),
                    initial=str(row.get("initial_status") or ""),
                    current=str(row.get("current_status") or ""),
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

        print(f"Prepared A2A worktrees for branch '{args.branch}'.")
        print(f"Repo: {repo}")
        print(f"Builder worktree: {builder_path}")
        print(f"Reviewer worktree ({reviewer_name}): {reviewer_path}")
        print("Next: a2a run --task \"<your task>\"")
        return 0
    except RuntimeError as exc:
        print(str(exc))
        return 1


def _start_session(
    root: Path,
    task: str,
    max_rounds: int,
    timeout_min: int | None,
    builder_command: str | None = None,
    reviewer_command: str | None = None,
    watch_path: str | None = None,
) -> dict:
    cfg = _load_config(root)
    prep = load_json(_prepare_path(root))
    state_path = root / A2A_DIRNAME / "state.json"
    state = load_json(state_path)

    session_id = _next_session_id()
    reviewer_name = str(prep.get("reviewer_name") or cfg.get("reviewer_name", "aryabhatta"))
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
        "reviewer_name": reviewer_name,
        "repo_path": prep["repo_path"],
        "branch": prep["branch"],
        "worktrees": prep["worktrees"],
        "rounds": [],
        "builder_command": builder_command,
        "reviewer_command": reviewer_command,
        "watch_path": watch_path,
        "llm_native": {
            "default": bool(cfg.get("llm_native_default", True)),
            "strict": bool(cfg.get("llm_native_strict", True)),
            "fallback": bool(cfg.get("llm_native_fallback", False)),
            "timeout_sec": int(cfg.get("llm_native_timeout_sec", 900)),
        },
    }

    prior_gate = bool(cfg.get("prior_review_gate", True))
    search_if_missing = bool(cfg.get("prior_review_search", True))
    max_comments = int(cfg.get("prior_review_max_comments", 120))
    if prior_gate and watch_path:
        report_dir = _report_dir(root, session_id)
        context = ingest_prior_review_context(
            Path(watch_path),
            report_dir,
            search_if_missing=search_if_missing,
            max_comments=max_comments,
        )
        if context:
            session["prior_review"] = context

    dump_json(_session_path(root, session_id), session)

    summary = _report_dir(root, session_id) / "summary.md"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(
        f"# A2A Session {session_id}\n\n"
        f"- task: {task}\n"
        f"- reviewer: {reviewer_name}\n"
        f"- status: in_progress\n"
        f"- max_rounds: {max_rounds}\n\n"
        "## Round History\n\n",
        encoding="utf-8",
    )

    _write_round_templates(root, session_id, 1, reviewer_name)

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
        print(str(exc))
        return 1
    round_no = int(session.get("current_round", 1))
    reviewer_name = str(session.get("reviewer_name", "aryabhatta"))
    if errors:
        print("Findings validation failed:")
        for err in errors:
            print(f"  - {err}")
        return 1

    prev_open = None
    for previous_round in session.get("rounds", []):
        if int(previous_round.get("round", -1)) == round_no - 1:
            prev_open = int(previous_round.get("findings_open", 0))
            break

    change_stats = _builder_change_stats(root, session_id, round_no, reviewer_name)
    builder_patch_gauge = _compute_builder_patch_gauge(change_stats)
    builder_confidence = _compute_builder_confidence(prev_open, open_count, change_stats)
    reviewer_confidence = _compute_reviewer_confidence(findings)

    round_record = {
        "round": round_no,
        "validated_at": _now_utc(),
        "findings_total": len(findings),
        "findings_open": open_count,
        "findings_file": str(findings_path),
        "builder_changed_files": int(change_stats.get("changed_files", 0)),
        "builder_diff_lines": int(change_stats.get("diff_lines", 0)),
        "builder_diff_hunks": int(change_stats.get("diff_hunks", 0)),
        "builder_patch_gauge": builder_patch_gauge,
        "builder_confidence": builder_confidence,
        "reviewer_confidence": reviewer_confidence,
    }
    rounds = [r for r in session.get("rounds", []) if int(r.get("round", -1)) != round_no]
    rounds.append(round_record)
    rounds = sorted(rounds, key=lambda r: int(r["round"]))
    session["rounds"] = rounds
    session["open_findings"] = open_count
    session["updated_at"] = _now_utc()

    _append_summary_round(root, session_id, round_no, len(findings), open_count)
    print(
        "Round scores: "
        f"builder_patch_gauge={builder_patch_gauge}, "
        f"builder_confidence={builder_confidence}, "
        f"reviewer_confidence={reviewer_confidence}"
    )

    max_rounds = int(session.get("max_rounds", 1))
    if open_count == 0:
        session["status"] = "lgtm"
        _append_summary_verdict(root, session_id, "LGTM")
        if state.get("active_session_id") == session_id:
            state["active_session_id"] = None
            state["last_updated"] = _now_utc()
            dump_json(state_path, state)
        _write_session(root, session)
        print(f"Session {session_id}: LGTM (all findings closed).")
        return 0

    if round_no >= max_rounds:
        session["status"] = "stopped"
        _append_summary_verdict(root, session_id, "STOPPED (max rounds reached)")
        if state.get("active_session_id") == session_id:
            state["active_session_id"] = None
            state["last_updated"] = _now_utc()
            dump_json(state_path, state)
        _write_session(root, session)
        print(f"Session {session_id}: stopped at max rounds ({max_rounds}) with open findings={open_count}.")
        return 1

    next_round = round_no + 1
    session["current_round"] = next_round
    session["status"] = "in_progress"
    _write_session(root, session)
    _write_round_templates(root, session_id, next_round, reviewer_name)
    print(
        f"Session {session_id}: round {round_no} validated with open findings={open_count}. "
        f"Prepared round {next_round} templates."
    )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    try:
        root = _must_find_root()
        prep_path = _prepare_path(root)
        if not prep_path.exists():
            print("Missing .a2a/prepare.json. Run: a2a prepare")
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
                    print("No reviewer command configured. Use --reviewer-cmd or set reviewer_command in config.")
                    return 1
                rc = _run_agent_step(root, session, "reviewer", reviewer_cmd, round_no)
                if rc != 0:
                    return rc
            return _advance_session(root, args.resume)

        if not args.task:
            print("Missing --task for new session.")
            return 1

        session = _start_session(
            root,
            args.task,
            max_rounds=max_rounds,
            timeout_min=args.timeout_min,
            builder_command=builder_cmd,
            reviewer_command=reviewer_cmd,
            watch_path=watch_path,
        )
        sid = session["id"]
        round_no = int(session["current_round"])
        files = _round_files(root, sid, round_no, str(session["reviewer_name"]))

        if args.auto:
            if not builder_cmd or not reviewer_cmd:
                print(
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

        print(f"Started session: {sid}")
        print(f"Round {round_no} files:")
        print(f"  - {files['builder']}")
        print(f"  - {files['reviewer']}")
        print(f"  - {files['findings']}")
        print(f"After updating findings, continue with: a2a run --resume {sid}")
        return 0
    except RuntimeError as exc:
        print(str(exc))
        return 1


def cmd_loop(args: argparse.Namespace) -> int:
    try:
        root = _must_find_root()
        prep_path = _prepare_path(root)
        if not prep_path.exists():
            print("Missing .a2a/prepare.json. Run: a2a prepare")
            return 1

        cfg = _load_config(root)
        max_rounds = args.max_rounds or int(cfg.get("default_max_rounds", 6))
        builder_cmd = _resolve_agent_command(cfg, args.builder_cmd, "builder_command")
        reviewer_cmd = _resolve_agent_command(cfg, args.reviewer_cmd, "reviewer_command")
        builder_cmd, reviewer_cmd = _resolve_default_agent_commands(root, cfg, builder_cmd, reviewer_cmd)
        watch_path = str(Path(args.watch_path).resolve()) if args.watch_path else None

        if args.session and args.task:
            print("Use either --session or --task, not both.")
            return 1

        if args.session:
            session = _load_session(root, args.session)
            sid = str(session["id"])
        else:
            if not args.task:
                print("Missing --task for new autonomous session.")
                return 1
            session = _start_session(
                root,
                args.task,
                max_rounds=max_rounds,
                timeout_min=args.timeout_min,
                builder_command=builder_cmd,
                reviewer_command=reviewer_cmd,
                watch_path=watch_path,
            )
            sid = str(session["id"])
            print(f"Started session: {sid}")

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
            print(
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
                print(f"Session {sid}: already LGTM.")
                return 0
            if status == "stopped":
                print(f"Session {sid}: already stopped.")
                return 1

            if max_iterations is not None and iterations >= max_iterations:
                print(f"Session {sid}: loop paused after max_iterations={max_iterations}.")
                return 0

            round_no = int(session.get("current_round", 1))
            print(f"Session {sid}: autonomous round {round_no} start.")

            rc = _run_agent_step(root, session, "builder", builder_cmd, round_no)
            if rc != 0:
                return rc

            rc = _run_agent_step(root, session, "reviewer", reviewer_cmd, round_no)
            if rc != 0:
                return rc

            rc = _advance_session(root, sid)
            session = _load_session(root, sid)
            status = str(session.get("status", "in_progress"))
            iterations += 1

            if status == "lgtm":
                return 0
            if status == "stopped":
                return 1
            if rc != 0:
                return rc
    except RuntimeError as exc:
        print(str(exc))
        return 1


def cmd_respin(args: argparse.Namespace) -> int:
    try:
        _must_find_root()
        source = Path(args.input_path).resolve()
        if not source.exists():
            print(f"Respin input path not found: {source}")
            return 1

        output = Path(args.out_path).resolve() if args.out_path else _default_respin_output_path(source)
        if output == source:
            print("Respin output path must differ from input path. Use --out-path to choose a new location.")
            return 1

        _copy_respin_source(source, output, force=bool(args.force))
        print(f"Created respin path: {output}")

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
        print(f"Respin watch path: {output}")
        return rc
    except RuntimeError as exc:
        print(str(exc))
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
            print("No session provided and no active session in state. Use --session.")
            return 1

        session = _load_session(root, session_id)
        round_no = int(args.round) if args.round is not None else int(session.get("current_round", 1))
        if not reviewer_cmd:
            reviewer_cmd = str(session.get("reviewer_command") or "").strip() or None

        if args.run_agent:
            if not reviewer_cmd:
                print("No reviewer command configured. Use --reviewer-cmd or set reviewer_command in config.")
                return 1
            rc = _run_agent_step(root, session, "reviewer", reviewer_cmd, round_no)
            if rc != 0:
                return rc

        try:
            _session, open_count, findings, errors, findings_path = _validate_round_only(
                root, session_id, round_no=round_no
            )
        except RuntimeError as exc:
            print(str(exc))
            return 1

        if errors:
            print("Findings validation failed:")
            for err in errors:
                print(f"  - {err}")
            return 1

        print(f"Session: {session_id}")
        print(f"Round: {round_no}")
        print(f"Findings file: {findings_path}")
        print(f"Findings total: {len(findings)}")
        print(f"Findings open: {open_count}")
        for idx, finding in enumerate(findings, start=1):
            sev = finding.get("severity", "?")
            title = finding.get("title", "")
            loc = finding.get("location", "")
            status = finding.get("status", "")
            print(f"  {idx}. [{sev}] {title} ({loc}) status={status}")

        if args.advance:
            current_round = int(session.get("current_round", 1))
            if round_no != current_round:
                print(
                    "Cannot advance non-current round. "
                    f"Current round is {current_round}, requested {round_no}."
                )
                return 1
            return _advance_session(root, session_id)

        return 0
    except RuntimeError as exc:
        print(str(exc))
        return 1


def cmd_config_get(args: argparse.Namespace) -> int:
    try:
        root = _must_find_root()
        cfg = _load_config(root)
        if args.key:
            if args.key not in cfg:
                print(f"Config key not found: {args.key}")
                return 1
            value = cfg[args.key]
            if args.json_output:
                print(json.dumps(value, indent=2, sort_keys=True))
            else:
                print(value)
            return 0

        if args.json_output:
            print(json.dumps(cfg, indent=2, sort_keys=True))
        else:
            for key in sorted(cfg.keys()):
                print(f"{key}={cfg[key]}")
        return 0
    except RuntimeError as exc:
        print(str(exc))
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
        print(f"Set {key}={value}")
        return 0
    except RuntimeError as exc:
        print(str(exc))
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
        print(f"Config reset to defaults at {cfg_path}")
        return 0
    except RuntimeError as exc:
        print(str(exc))
        return 1


def cmd_report(args: argparse.Namespace) -> int:
    try:
        root = _must_find_root()
        if args.all:
            if args.session:
                print("--all cannot be used with --session.")
                return 1
            if args.latest:
                print("--all cannot be used with --latest.")
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
            print(f"Report written: {out_path}")
        else:
            print(out)
        return 0
    except RuntimeError as exc:
        print(str(exc))
        return 1


def _load_status_view(root: Path) -> StatusView:
    a2a_dir = root / A2A_DIRNAME
    cfg_path = a2a_dir / "config.json"
    state_path = a2a_dir / "state.json"
    sessions_dir = a2a_dir / "sessions"

    cfg = load_json(cfg_path) if cfg_path.exists() else default_config()
    state = load_json(state_path) if state_path.exists() else default_state()

    session_files = sorted(sessions_dir.glob("*.json")) if sessions_dir.is_dir() else []
    active = state.get("active_session_id")
    open_findings = None

    active_status = None
    current_round = None
    max_rounds = None

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

    return StatusView(
        root=str(root),
        active_session_id=active,
        session_count=len(session_files),
        open_findings=open_findings,
        reviewer_name=str(cfg.get("reviewer_name", "aryabhatta")),
        active_status=active_status,
        current_round=current_round,
        max_rounds=max_rounds,
    )


def cmd_status(_args: argparse.Namespace) -> int:
    root = find_a2a_root()
    if root is None:
        print("No .a2a directory found in current path or parents.")
        print("Run: a2a init")
        return 1

    view = _load_status_view(root)
    print(f"A2A root: {view.root}")
    print(f"Reviewer: {view.reviewer_name}")
    print(f"Sessions: {view.session_count}")
    print(f"Active session: {view.active_session_id or 'none'}")
    if view.active_session_id is not None:
        findings = "unknown" if view.open_findings is None else str(view.open_findings)
        print(f"Open findings (active): {findings}")
        print(f"Status (active): {view.active_status or 'unknown'}")
        if view.current_round is not None and view.max_rounds is not None:
            print(f"Round (active): {view.current_round}/{view.max_rounds}")
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
        "--max-iterations",
        type=int,
        help="Optional per-invocation cap on autonomous rounds.",
    )
    p_loop.set_defaults(func=cmd_loop)

    p_respin = sub.add_parser(
        "respin",
        help="Create a next-revision patch path and run autonomous loop against it.",
    )
    p_respin.add_argument(
        "--input-path",
        required=True,
        help="Source patch file or patch-series directory to respin.",
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
