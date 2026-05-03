from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    a2a_root: Path
    patches_root: Path
    port: int
    log_level: str

    @property
    def a2a_dir(self) -> Path:
        return self.a2a_root / ".a2a"

    @property
    def sessions_dir(self) -> Path:
        return self.a2a_dir / "sessions"

    @property
    def logs_dir(self) -> Path:
        return self.a2a_dir / "logs"

    @property
    def reports_dir(self) -> Path:
        return self.a2a_dir / "reports"


def load_settings() -> Settings:
    a2a_root = Path(os.getenv("A2A_ROOT", "/workspace/A2A_CLI")).resolve()
    patches_root = Path(os.getenv("PATCHES_ROOT", "/workspace/patches")).resolve()
    port = int(os.getenv("PORT", "7788"))
    log_level = os.getenv("LOG_LEVEL", "info")
    return Settings(
        a2a_root=a2a_root,
        patches_root=patches_root,
        port=port,
        log_level=log_level,
    )


SETTINGS = load_settings()
