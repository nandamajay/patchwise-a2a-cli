from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SessionStartRequest(BaseModel):
    task: str
    watch_path: str
    max_rounds: int = 3
    open_screen: bool = True
    open_web_terminals: bool = True


class SessionStatus(BaseModel):
    session_id: str
    status: str
    current_round: int
    max_rounds: int
    final_status: str | None
    screen_session_name: str | None
    ws_ports: dict[str, int] = Field(default_factory=dict)


class RoundSummary(BaseModel):
    round: int
    gate: str
    scores: dict[str, Any] = Field(default_factory=dict)
    findings: dict[str, Any] = Field(default_factory=dict)
    prior_comments: dict[str, Any] = Field(default_factory=dict)
    top_open: list[dict[str, Any]] = Field(default_factory=list)


class SessionReport(BaseModel):
    session_id: str
    task: str
    watch_path: str
    max_rounds: int
    final_status: str
    rounds: list[RoundSummary] = Field(default_factory=list)
    prior_comments: list[dict[str, Any]] = Field(default_factory=list)
    lgtm_checklist: dict[str, Any] = Field(default_factory=dict)
