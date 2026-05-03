from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

from .config import SETTINGS
from .log_streamer import stream_file, stream_glob


WS_PORTS = {
    "builder": 7789,
    "reviewer": 7790,
    "orchestrator": 7791,
}


@dataclass
class PaneBridge:
    queue: asyncio.Queue[str]
    tasks: list[asyncio.Task] = field(default_factory=list)


_BRIDGES: dict[str, dict[str, PaneBridge]] = {}


def _queue_push(queue: asyncio.Queue[str], line: str) -> None:
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    try:
        queue.put_nowait(line)
    except asyncio.QueueFull:
        pass


async def _pump_file(path: Path, queue: asyncio.Queue[str]) -> None:
    async for line in stream_file(path, start_at_end=False):
        _queue_push(queue, line)


async def _pump_glob(directory: Path, pattern: str, queue: asyncio.Queue[str]) -> None:
    async for line in stream_glob(directory, pattern, include_existing=True):
        _queue_push(queue, line)


async def start_bridges(session_id: str) -> None:
    if session_id in _BRIDGES:
        return

    logs_dir = SETTINGS.logs_dir / session_id
    logs_dir.mkdir(parents=True, exist_ok=True)

    builder_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=5000)
    reviewer_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=5000)
    orchestrator_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=5000)

    builder = PaneBridge(
        queue=builder_queue,
        tasks=[asyncio.create_task(_pump_glob(logs_dir, "round-*-builder.log", builder_queue))],
    )
    reviewer = PaneBridge(
        queue=reviewer_queue,
        tasks=[
            asyncio.create_task(_pump_glob(logs_dir, "round-*-reviewer.log", reviewer_queue)),
            asyncio.create_task(_pump_glob(logs_dir, "round-*-aryabhatta.log", reviewer_queue)),
        ],
    )
    orchestrator = PaneBridge(
        queue=orchestrator_queue,
        tasks=[asyncio.create_task(_pump_file(logs_dir / "orchestrator.log", orchestrator_queue))],
    )

    _BRIDGES[session_id] = {
        "builder": builder,
        "reviewer": reviewer,
        "orchestrator": orchestrator,
    }


async def stop_bridges(session_id: str) -> None:
    panes = _BRIDGES.pop(session_id, None)
    if not panes:
        return

    for pane in panes.values():
        for task in pane.tasks:
            task.cancel()

    await asyncio.gather(
        *[task for pane in panes.values() for task in pane.tasks],
        return_exceptions=True,
    )


async def get_pane_stream(session_id: str, pane: str) -> AsyncIterator[str]:
    if pane not in WS_PORTS:
        raise RuntimeError(f"Unknown pane: {pane}")

    if session_id not in _BRIDGES:
        await start_bridges(session_id)

    pane_bridge = _BRIDGES[session_id][pane]
    while True:
        line = await pane_bridge.queue.get()
        yield line
