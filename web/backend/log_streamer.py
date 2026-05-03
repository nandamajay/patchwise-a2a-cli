from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator


async def stream_file(path: Path, *, start_at_end: bool = False, poll_sec: float = 0.5) -> AsyncIterator[str]:
    offset = 0
    if start_at_end and path.exists():
        offset = path.stat().st_size

    while True:
        if not path.exists():
            await asyncio.sleep(poll_sec)
            continue

        try:
            size = path.stat().st_size
        except OSError:
            await asyncio.sleep(poll_sec)
            continue

        if size < offset:
            offset = 0

        if size > offset:
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
                    offset = handle.tell()
            except OSError:
                await asyncio.sleep(poll_sec)
                continue

            for line in chunk.splitlines():
                yield line

        await asyncio.sleep(poll_sec)


async def stream_glob(
    directory: Path,
    pattern: str,
    *,
    poll_sec: float = 0.5,
    include_existing: bool = True,
) -> AsyncIterator[str]:
    offsets: dict[Path, int] = {}

    while True:
        files = sorted(directory.glob(pattern)) if directory.exists() else []
        for path in files:
            if not path.is_file():
                continue

            current_size = path.stat().st_size
            if path not in offsets:
                offsets[path] = 0 if include_existing else current_size

            offset = offsets[path]
            if current_size < offset:
                offset = 0

            if current_size > offset:
                try:
                    with path.open("r", encoding="utf-8", errors="replace") as handle:
                        handle.seek(offset)
                        chunk = handle.read()
                        offsets[path] = handle.tell()
                except OSError:
                    continue

                for line in chunk.splitlines():
                    yield line

        await asyncio.sleep(poll_sec)
