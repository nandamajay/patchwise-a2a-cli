from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .maintainer_tracker import get_priority, update_profile


_MSGID_RE = re.compile(r"<([^>]+)>")
_HREF_MSGID_RE = re.compile(
    r"""href=["']?[^"'>\s]*/(?P<msgid>[^/"'>\s]+)/(?:raw)?["']?""",
    re.IGNORECASE,
)
_FROM_RE = re.compile(r"^From:\s*(.+)$", re.MULTILINE)


def _watch_state_path(root: Path, msgid: str) -> Path:
    safe = msgid.replace("/", "_").replace("<", "").replace(">", "")
    return root / ".a2a" / "watch" / f"{safe}.json"


def load_known_message_ids(root: Path, msgid: str) -> set[str]:
    path = _watch_state_path(root, msgid)
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    rows = payload.get("known_ids", []) if isinstance(payload, dict) else []
    return {str(row) for row in rows}


def save_known_message_ids(root: Path, msgid: str, known_ids: set[str]) -> None:
    path = _watch_state_path(root, msgid)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"msgid": msgid, "known_ids": sorted(known_ids)}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def fetch_thread_message_ids(msgid: str) -> set[str]:
    url = f"https://lore.kernel.org/all/{urllib.parse.quote(msgid)}/T/"
    req = urllib.request.Request(url, headers={"User-Agent": "A2A-CLI/0.1"})
    with urllib.request.urlopen(req, timeout=15) as response:
        html = response.read().decode("utf-8", errors="replace")
    message_ids: set[str] = set()
    for match in _HREF_MSGID_RE.finditer(html):
        candidate = urllib.parse.unquote(match.group("msgid")).strip().strip("<>")
        if candidate and "@" in candidate and " " not in candidate:
            message_ids.add(candidate)
    if message_ids:
        return message_ids
    # Fallback for non-standard pages where IDs appear as <msgid>.
    for match in _MSGID_RE.finditer(html):
        candidate = match.group(1).strip().strip("<>")
        if candidate and "@" in candidate and " " not in candidate:
            message_ids.add(candidate)
    return message_ids


def fetch_message(msg_id: str) -> str:
    url = f"https://lore.kernel.org/all/{urllib.parse.quote(msg_id)}/raw"
    req = urllib.request.Request(url, headers={"User-Agent": "A2A-CLI/0.1"})
    with urllib.request.urlopen(req, timeout=15) as response:
        return response.read().decode("utf-8", errors="replace")


def extract_author(content: str) -> str:
    m = _FROM_RE.search(content or "")
    if not m:
        return "unknown"
    return m.group(1).strip()


def process_new_reply(
    root: Path,
    msg_id: str,
    author: str,
    content: str,
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    priority = get_priority(root, author)
    trigger = False
    cfg = config or {}
    if bool(cfg.get("auto_trigger_session")) and priority == "high":
        trigger = True
    return {
        "msg_id": msg_id,
        "author": author,
        "priority": priority,
        "trigger_session": trigger,
        "excerpt": content[:200],
    }


def watch(
    root: Path,
    msgid: str,
    poll_interval_secs: int = 300,
    *,
    max_loops: int | None = None,
    fetch_ids_fn: Callable[[str], set[str]] = fetch_thread_message_ids,
    fetch_msg_fn: Callable[[str], str] = fetch_message,
    process_fn: Callable[[Path, str, str, str], dict[str, Any]] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    known_ids = load_known_message_ids(root, msgid)
    events: list[dict[str, Any]] = []
    loops = 0
    backoff = 1
    while True:
        try:
            current = fetch_ids_fn(msgid)
            new_ids = sorted(current - known_ids)
            for mid in new_ids:
                content = fetch_msg_fn(mid)
                author = extract_author(content)
                if process_fn:
                    event = process_fn(root, mid, author, content)
                else:
                    event = process_new_reply(root, mid, author, content)
                events.append(event)
                if on_event:
                    on_event(event)
                update_profile(root, author, finding_types=["lore_reply"], verdict="pending")
                known_ids.add(mid)
            save_known_message_ids(root, msgid, known_ids)
            backoff = 1
        except urllib.error.URLError as exc:
            warning = {"type": "network_warning", "error": str(exc)}
            events.append(warning)
            if on_event:
                on_event(warning)
            sleep_for = min(poll_interval_secs, backoff * 2)
            time.sleep(max(1, sleep_for))
            backoff = min(backoff * 2, 60)
            loops += 1
            if max_loops is not None and loops >= max_loops:
                break
            continue

        loops += 1
        if max_loops is not None and loops >= max_loops:
            break
        time.sleep(max(1, poll_interval_secs))
    return events
