from __future__ import annotations

import asyncio
import os
import re
import signal
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import SETTINGS
from .models import SessionStartRequest, SessionStatus
from .screen_bridge import WS_PORTS, start_bridges, stop_bridges


_STARTED_SESSION_RE = re.compile(r"^Started session:\s+(?P<sid>\S+)")
_LLM_BUILDER_CMD = "PATH=/host/bin:$PATH bash /workspace/A2A_CLI/scripts/agents/builder_llm_native.sh"
_LLM_REVIEWER_CMD = "PATH=/host/bin:$PATH bash /workspace/A2A_CLI/scripts/agents/reviewer_llm_native.sh"


@dataclass
class RuntimeSession:
    proc: subprocess.Popen
    provisional_id: str
    started_at_epoch: float
    session_id: str | None = None
    output_thread: threading.Thread | None = None
    discovered: threading.Event = field(default_factory=threading.Event)


_RUNTIME_BY_ID: dict[str, RuntimeSession] = {}
_RUNTIME_LOCK = threading.Lock()


def _next_session_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    return f"sess-{stamp}"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_file(session_id: str) -> Path:
    return SETTINGS.sessions_dir / f"{session_id}.json"


def _normalize_status(raw: str) -> tuple[str, str | None]:
    status = raw.strip().lower()
    if status in {"lgtm", "failed", "stopped"}:
        return status, status
    if status in {"in_progress", "running", ""}:
        return "running", None
    return status, None


def _ensure_log_copy(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    shutil.copy2(src, dst)


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _discover_session_id_from_files(
    req: SessionStartRequest,
    *,
    not_before_epoch: float,
    max_scan: int = 40,
) -> str | None:
    try:
        req_watch = str(Path(req.watch_path).resolve())
    except Exception:
        req_watch = str(req.watch_path)

    candidates = sorted(
        SETTINGS.sessions_dir.glob("sess-*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:max_scan]

    for path in candidates:
        if path.stat().st_mtime < (not_before_epoch - 5):
            continue

        try:
            import json

            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            continue

        created_at = str(payload.get("created_at") or "")
        if created_at:
            try:
                if datetime.fromisoformat(created_at).timestamp() < (not_before_epoch - 5):
                    continue
            except ValueError:
                pass

        payload_task = str(payload.get("task") or "")
        payload_max_rounds = int(payload.get("max_rounds") or 0)
        payload_watch_raw = str(payload.get("watch_path") or "")
        try:
            payload_watch = str(Path(payload_watch_raw).resolve())
        except Exception:
            payload_watch = payload_watch_raw

        if payload_task != req.task:
            continue
        if payload_max_rounds != int(req.max_rounds):
            continue
        if payload_watch != req_watch:
            continue

        return str(payload.get("id") or path.stem)

    return None


def _drain_process_output(runtime: RuntimeSession) -> None:
    provisional_log = SETTINGS.logs_dir / runtime.provisional_id / "orchestrator.log"
    active_log = provisional_log

    if runtime.proc.stdout is None:
        runtime.discovered.set()
        return

    for line in runtime.proc.stdout:
        match = _STARTED_SESSION_RE.match(line.strip())
        if match and runtime.session_id is None:
            sid = match.group("sid")
            runtime.session_id = sid
            with _RUNTIME_LOCK:
                _RUNTIME_BY_ID[sid] = runtime
            actual_log = SETTINGS.logs_dir / sid / "orchestrator.log"
            _ensure_log_copy(provisional_log, actual_log)
            active_log = actual_log
            runtime.discovered.set()

        _append_line(active_log, line)

    if runtime.session_id is None:
        runtime.session_id = runtime.provisional_id
        with _RUNTIME_LOCK:
            _RUNTIME_BY_ID[runtime.session_id] = runtime
        runtime.discovered.set()


def _spawn_loop(req: SessionStartRequest) -> RuntimeSession:
    provisional_id = _next_session_id()
    cmd = [
        "python",
        "-m",
        "a2a_cli.main",
        "loop",
        "--task",
        req.task,
        "--watch-path",
        req.watch_path,
        "--max-rounds",
        str(req.max_rounds),
        "--builder-cmd",
        _LLM_BUILDER_CMD,
        "--reviewer-cmd",
        _LLM_REVIEWER_CMD,
    ]

    proc_env = dict(os.environ)
    proc_env["PATH"] = f"/host/bin:{proc_env.get('PATH', '')}"
    proc_env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        cmd,
        cwd=SETTINGS.a2a_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=proc_env,
        start_new_session=True,
    )

    runtime = RuntimeSession(
        proc=proc,
        provisional_id=provisional_id,
        started_at_epoch=time.time(),
    )
    thread = threading.Thread(target=_drain_process_output, args=(runtime,), daemon=True)
    runtime.output_thread = thread
    thread.start()
    return runtime


async def start_session(req: SessionStartRequest) -> SessionStatus:
    runtime = _spawn_loop(req)

    found = await asyncio.to_thread(runtime.discovered.wait, 45.0)
    if found:
        session_id = str(runtime.session_id or runtime.provisional_id)
    else:
        discovered_sid = _discover_session_id_from_files(
            req,
            not_before_epoch=runtime.started_at_epoch,
        )
        if not discovered_sid:
            raise RuntimeError("Timed out waiting for session creation")
        session_id = discovered_sid
        with _RUNTIME_LOCK:
            _RUNTIME_BY_ID[session_id] = runtime

    if req.open_screen:
        screen_log = SETTINGS.logs_dir / session_id / "screen-launch.log"
        screen_log.parent.mkdir(parents=True, exist_ok=True)
        proc_env = dict(os.environ)
        proc_env["PATH"] = f"/host/bin:{proc_env.get('PATH', '')}"
        with screen_log.open("a", encoding="utf-8") as handle:
            subprocess.Popen(
                ["bash", "scripts/launch_live_screens.sh", "--session", session_id],
                cwd=SETTINGS.a2a_root,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
                env=proc_env,
            )

    if req.open_web_terminals:
        await start_bridges(session_id)

    return await get_session_status(session_id)


async def get_session_status(session_id: str) -> SessionStatus:
    current_round = 0
    max_rounds = 0
    final_status: str | None = None
    status = "running"

    session_path = _session_file(session_id)
    if session_path.exists():
        import json

        with session_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)

        current_round = int(payload.get("current_round") or 0)
        max_rounds = int(payload.get("max_rounds") or 0)
        status, final_status = _normalize_status(str(payload.get("status") or "running"))

    with _RUNTIME_LOCK:
        runtime = _RUNTIME_BY_ID.get(session_id)

    if runtime is not None and runtime.proc.poll() is not None and final_status is None:
        if status == "running":
            status = "failed"
            final_status = "failed"

    return SessionStatus(
        session_id=session_id,
        status=status,
        current_round=current_round,
        max_rounds=max_rounds,
        final_status=final_status,
        screen_session_name=f"patchwise-{session_id}",
        ws_ports=dict(WS_PORTS),
    )


async def stop_session(session_id: str) -> None:
    with _RUNTIME_LOCK:
        runtime = _RUNTIME_BY_ID.pop(session_id, None)

    if runtime is not None and runtime.proc.poll() is None:
        try:
            os.killpg(runtime.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            runtime.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(runtime.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    await stop_bridges(session_id)

    for cmd in [
        ["screen", "-S", f"patchwise-{session_id}", "-X", "quit"],
        ["screen", "-S", "a2a-builder", "-X", "quit"],
        ["screen", "-S", "a2a-reviewer", "-X", "quit"],
        ["screen", "-S", "a2a-logs", "-X", "quit"],
    ]:
        subprocess.run(cmd, cwd=SETTINGS.a2a_root, capture_output=True, text=True)

    session_path = _session_file(session_id)
    if session_path.exists():
        import json

        with session_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["status"] = "stopped"
        payload["updated_at"] = _now_utc()
        with session_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
