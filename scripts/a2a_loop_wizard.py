#!/usr/bin/env python3
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _prompt(text: str, default: str | None = None, required: bool = False) -> str:
    while True:
        if default is None:
            raw = input(f"{text}: ").strip()
        else:
            raw = input(f"{text} [{default}]: ").strip()
            if not raw:
                raw = default
        if raw or not required:
            return raw
        print("Input is required.")


def _prompt_int(text: str, default: int) -> int:
    while True:
        raw = _prompt(text, default=str(default))
        try:
            return int(raw)
        except ValueError:
            print("Please enter a valid integer.")


def _prompt_yes_no(text: str, default_yes: bool = True) -> bool:
    default = "Y/n" if default_yes else "y/N"
    while True:
        raw = input(f"{text} [{default}]: ").strip().lower()
        if not raw:
            return default_yes
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("Please answer y or n.")


def _run(cmd: list[str]) -> int:
    print()
    print("Running:")
    print("  " + shlex.join(cmd))
    print()
    return subprocess.run(cmd, cwd=ROOT).returncode


def _show_loop_help() -> int:
    return _run([sys.executable, "-m", "a2a_cli.main", "loop", "--help"])


def _sessions_dir() -> Path:
    return ROOT / ".a2a" / "sessions"


def _load_sessions() -> list[dict]:
    sessions_path = _sessions_dir()
    if not sessions_path.exists():
        return []
    out: list[dict] = []
    for path in sorted(sessions_path.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        out.append(payload)
    return out


def _pick_session(filter_status: str | None = None) -> str | None:
    rows = _load_sessions()
    if filter_status:
        rows = [row for row in rows if str(row.get("status", "")).lower() == filter_status.lower()]
    if not rows:
        print("No matching sessions found.")
        return None

    print()
    print("Available sessions:")
    for idx, row in enumerate(rows, start=1):
        sid = str(row.get("id", ""))
        status = str(row.get("status", "unknown"))
        current = int(row.get("current_round", 0) or 0)
        max_rounds = int(row.get("max_rounds", 0) or 0)
        task = str(row.get("task", ""))[:64]
        print(f"{idx}. {sid}  status={status}  round={current}/{max_rounds}  task={task}")

    raw = _prompt("Pick session by number or paste session id", required=True)
    if raw.isdigit():
        pos = int(raw)
        if 1 <= pos <= len(rows):
            return str(rows[pos - 1].get("id") or "")
        print("Invalid selection.")
        return None
    return raw.strip()


def _build_new_loop_command() -> list[str] | None:
    task = _prompt("Task name", required=True)
    print()
    print("Source type:")
    print("1. File/Directory based patch review")
    print("2. Lore URL based review")
    print("3. Lore message-id based review")
    source = _prompt("Choose 1/2/3", default="1")
    if source not in {"1", "2", "3"}:
        print("Invalid source type.")
        return None

    max_rounds = _prompt_int("Max rounds", 3)
    max_iterations_raw = _prompt("Max iterations for this invocation (blank = unlimited)", default="")
    builder_cmd = _prompt("Builder command override (blank = config/default)", default="")
    reviewer_cmd = _prompt("Reviewer command override (blank = config/default)", default="")

    cmd = [sys.executable, "-m", "a2a_cli.main", "loop", "--task", task, "--max-rounds", str(max_rounds)]

    if source == "1":
        watch_path = _prompt("Patch file or series directory path", required=True)
        resolved = str(Path(watch_path).expanduser().resolve())
        cmd.extend(["--watch-path", resolved])
        auto_respin = _prompt_yes_no("Auto respin on LGTM?", default_yes=False)
    else:
        if source == "2":
            lore_url = _prompt("Lore URL", required=True)
            cmd.extend(["--lore-url", lore_url])
        else:
            lore_msgid = _prompt("Lore message-id", required=True)
            cmd.extend(["--lore-msgid", lore_msgid])
        lore_out_dir = _prompt("Lore fetch output directory (blank = config/default)", default="")
        if lore_out_dir:
            cmd.extend(["--lore-out-dir", str(Path(lore_out_dir).expanduser().resolve())])
        auto_respin = _prompt_yes_no("Auto respin on LGTM?", default_yes=True)

    cmd.append("--auto-respin" if auto_respin else "--no-auto-respin")

    if max_iterations_raw.strip():
        try:
            max_iterations = int(max_iterations_raw)
        except ValueError:
            print("Invalid max iterations value.")
            return None
        if max_iterations > 0:
            cmd.extend(["--max-iterations", str(max_iterations)])
    if builder_cmd.strip():
        cmd.extend(["--builder-cmd", builder_cmd.strip()])
    if reviewer_cmd.strip():
        cmd.extend(["--reviewer-cmd", reviewer_cmd.strip()])
    return cmd


def _resume_loop() -> int:
    sid = _pick_session(filter_status=None)
    if not sid:
        return 1
    max_iterations_raw = _prompt("Max iterations for this invocation (blank = unlimited)", default="")
    cmd = [sys.executable, "-m", "a2a_cli.main", "loop", "--session", sid]
    if max_iterations_raw.strip():
        try:
            max_iterations = int(max_iterations_raw)
        except ValueError:
            print("Invalid max iterations value.")
            return 1
        if max_iterations > 0:
            cmd.extend(["--max-iterations", str(max_iterations)])
    return _run(cmd)


def _extend_stopped_and_resume() -> int:
    sid = _pick_session(filter_status="stopped")
    if not sid:
        return 1

    rc = _run([sys.executable, "-m", "a2a_cli.main", "review", "--session", sid, "--advance"])
    if rc != 0:
        return rc
    if not _prompt_yes_no("Start loop now for this session?", default_yes=True):
        return 0
    return _run([sys.executable, "-m", "a2a_cli.main", "loop", "--session", sid])


def main() -> int:
    print("A2A Loop Wizard")
    print(f"Repo root: {ROOT}")
    print()
    print("What do you want to do?")
    print("1. Start a new loop session")
    print("2. Resume an existing session")
    print("3. Extend a stopped session by one round and resume")
    print("4. Show loop command options help")
    print("5. Exit")

    choice = _prompt("Choose 1/2/3/4/5", default="1")
    if choice == "1":
        cmd = _build_new_loop_command()
        if not cmd:
            return 1
        if not _prompt_yes_no("Execute this command now?", default_yes=True):
            print("Cancelled.")
            return 0
        return _run(cmd)
    if choice == "2":
        return _resume_loop()
    if choice == "3":
        return _extend_stopped_and_resume()
    if choice == "4":
        return _show_loop_help()
    print("Exit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
