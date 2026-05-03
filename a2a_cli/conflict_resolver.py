from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class ConflictError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ConflictResolver:
    repo_root: Path
    report_dir: Path
    strategy: str = "abort"

    STRATEGIES = ["ours", "theirs", "manual", "abort"]

    def __post_init__(self) -> None:
        if self.strategy not in self.STRATEGIES:
            raise ValueError(f"Unsupported conflict strategy: {self.strategy}")

    @property
    def _conflict_log_path(self) -> Path:
        return self.report_dir / "conflict_log.json"

    @property
    def _conflict_report_path(self) -> Path:
        return self.report_dir / "conflict_report.json"

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            text=True,
            capture_output=True,
        )

    def _load_log(self) -> list[dict]:
        path = self._conflict_log_path
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        if isinstance(payload, list):
            return payload
        return []

    def _save_log(self, rows: list[dict]) -> None:
        self._conflict_log_path.parent.mkdir(parents=True, exist_ok=True)
        self._conflict_log_path.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def resolve(self, patch_file: Path, conflict_info: dict) -> dict:
        conflicted_files = [str(p) for p in conflict_info.get("conflicted_files", [])]
        entry = {
            "timestamp": _utc_now(),
            "patch_file": str(patch_file),
            "strategy": self.strategy,
            "conflicted_files": conflicted_files,
            "details": conflict_info,
            "result": "pending",
        }
        rows = self._load_log()
        rows.append(entry)
        self._save_log(rows)

        if self.strategy == "abort":
            self._git("am", "--abort")
            entry["result"] = "aborted"
            self._save_log(rows)
            self._conflict_report_path.parent.mkdir(parents=True, exist_ok=True)
            self._conflict_report_path.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            raise ConflictError(
                "Patch apply conflict. Strategy=abort. "
                f"See {self._conflict_report_path} for details."
            )

        if self.strategy in {"ours", "theirs"}:
            checkout_mode = f"--{self.strategy}"
            if conflicted_files:
                checkout = self._git("checkout", checkout_mode, "--", *conflicted_files)
                if checkout.returncode != 0:
                    raise ConflictError(checkout.stderr.strip() or checkout.stdout.strip())
                add = self._git("add", "--", *conflicted_files)
                if add.returncode != 0:
                    raise ConflictError(add.stderr.strip() or add.stdout.strip())
            cont = self._git("am", "--continue")
            if cont.returncode != 0:
                raise ConflictError(cont.stderr.strip() or cont.stdout.strip())
            entry["result"] = "resolved"
            self._save_log(rows)
            return entry

        # manual
        print("Manual conflict resolution required.")
        print(f"Patch: {patch_file}")
        if conflicted_files:
            print("Conflicted files:")
            for rel in conflicted_files:
                print(f"  - {rel}")
        print("Resolve conflicts in repo, stage files, then type 'continue'. Type 'abort' to stop.")
        while True:
            choice = input("conflict> ").strip().lower()
            if choice == "continue":
                cont = self._git("am", "--continue")
                if cont.returncode == 0:
                    entry["result"] = "resolved-manual"
                    self._save_log(rows)
                    return entry
                print(cont.stderr.strip() or cont.stdout.strip())
                continue
            if choice == "abort":
                self._git("am", "--abort")
                entry["result"] = "aborted-manual"
                self._save_log(rows)
                raise ConflictError("Manual conflict resolution aborted by operator.")
            print("Type 'continue' or 'abort'.")
