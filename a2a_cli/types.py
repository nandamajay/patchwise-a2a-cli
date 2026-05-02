from dataclasses import dataclass


@dataclass
class StatusView:
    root: str
    active_session_id: str | None
    session_count: int
    open_findings: int | None
    reviewer_name: str
    active_status: str | None
    current_round: int | None
    max_rounds: int | None
