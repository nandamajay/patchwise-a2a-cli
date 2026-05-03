from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import SETTINGS
from .log_streamer import stream_file
from .models import SessionReport, SessionStartRequest, SessionStatus
from .report_reader import list_sessions, load_session_report
from .screen_bridge import get_pane_stream
from .session_manager import get_session_status, start_session, stop_session


ROUND_START_RE = re.compile(r"autonomous round\s+(?P<round>\d+)\s+start", re.IGNORECASE)
GATE_RE = re.compile(r"validation gate\s+(?P<result>passed|failed)", re.IGNORECASE)
SCORES_RE = re.compile(
    r"builder_patch_gauge=(?P<gauge>\d+),\s*builder_confidence=(?P<builder>\d+),\s*reviewer_confidence=(?P<reviewer>\d+)"
)
FINDING_RE = re.compile(r"\s*-\s*\[(?P<severity>[^\]]+)\]\s*(?P<title>.*?)\s*\((?P<location>[^)]*)\)\s*id=(?P<id>\S+)")
SUMMARY_JSON_RE = re.compile(r"Round summary json:\s*(?P<path>\S+)")


BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
STATIC_DIR = FRONTEND_DIR / "static"

app = FastAPI(title="PatchWise A2A Web")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/xterm", StaticFiles(directory=str(STATIC_DIR / "xterm")), name="xterm")


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_from_line(line: str) -> dict[str, Any] | None:
    text = line.strip()

    m = ROUND_START_RE.search(text)
    if m:
        return {"type": "round_start", "round": int(m.group("round"))}

    m = GATE_RE.search(text)
    if m:
        result = "pass" if m.group("result").lower() == "passed" else "fail"
        return {"type": "gate_result", "result": result}

    m = SCORES_RE.search(text)
    if m:
        return {
            "type": "scores",
            "builder_patch_gauge": int(m.group("gauge")),
            "builder_confidence": int(m.group("builder")),
            "reviewer_confidence": int(m.group("reviewer")),
        }

    m = FINDING_RE.search(line)
    if m:
        return {
            "type": "finding",
            "severity": m.group("severity"),
            "desc": m.group("title"),
            "location": m.group("location"),
            "id": m.group("id"),
        }

    m = SUMMARY_JSON_RE.search(text)
    if m:
        path = Path(m.group("path"))
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            return {
                "type": "round_summary",
                "summary": payload,
            }

    if "LGTM (all findings closed)" in text or text.endswith(": LGTM"):
        return {"type": "lgtm"}

    return None


@app.get("/")
async def serve_index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.post("/api/session/start", response_model=SessionStatus)
async def api_start_session(req: SessionStartRequest) -> SessionStatus:
    try:
        return await start_session(req)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/session/{session_id}/status", response_model=SessionStatus)
async def api_session_status(session_id: str) -> SessionStatus:
    try:
        return await get_session_status(session_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/session/{session_id}/report", response_model=SessionReport)
async def api_session_report(session_id: str) -> SessionReport:
    try:
        return load_session_report(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/sessions")
async def api_sessions() -> list[dict[str, Any]]:
    return list_sessions()


@app.post("/api/session/{session_id}/stop")
async def api_stop_session(session_id: str) -> dict[str, Any]:
    await stop_session(session_id)
    return {"ok": True, "session_id": session_id}


async def _ws_stream_logs(websocket: WebSocket, session_id: str, pane: str) -> None:
    await websocket.accept()
    try:
        async for line in get_pane_stream(session_id, pane):
            await websocket.send_json({"type": "log", "line": line, "ts": _ts()})
    except WebSocketDisconnect:
        return


@app.websocket("/ws/{session_id}/builder")
async def ws_builder(websocket: WebSocket, session_id: str) -> None:
    await _ws_stream_logs(websocket, session_id, "builder")


@app.websocket("/ws/{session_id}/reviewer")
async def ws_reviewer(websocket: WebSocket, session_id: str) -> None:
    await _ws_stream_logs(websocket, session_id, "reviewer")


@app.websocket("/ws/{session_id}/orchestrator")
async def ws_orchestrator(websocket: WebSocket, session_id: str) -> None:
    await _ws_stream_logs(websocket, session_id, "orchestrator")


@app.websocket("/ws/{session_id}/events")
async def ws_events(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    log_path = SETTINGS.logs_dir / session_id / "orchestrator.log"

    try:
        async for line in stream_file(log_path, start_at_end=False):
            event = _event_from_line(line)
            if event is None:
                continue
            event["ts"] = _ts()
            if "session_id" not in event:
                event["session_id"] = session_id
            await websocket.send_json(event)
            await asyncio.sleep(0)
    except WebSocketDisconnect:
        return
