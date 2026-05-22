from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clamp_score(value: Any) -> int:
    try:
        score = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, score))


def _normalize_score(value: Any, label: str, messages: list[str]) -> int:
    if value is None:
        messages.append(f"{label} missing from JSON; treated as 0")
        return 0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        messages.append(f"{label} invalid; treated as 0")
        return 0
    if parsed > 100:
        messages.append(f"{label} > 100; clamped to 100")
    if parsed < 0:
        messages.append(f"{label} < 0; clamped to 0")
    return clamp_score(parsed)


@dataclass
class ScoreThresholds:
    low_builder_confidence: int = 40
    low_reviewer_confidence: int = 60
    high_confidence_lgtm: int = 90
    volatility_swing: int = 30
    zero_patch_gauge: int = 0

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "ScoreThresholds":
        payload = cfg.get("score_thresholds", {}) if isinstance(cfg, dict) else {}
        return cls(
            low_builder_confidence=int(payload.get("low_builder_confidence", 40)),
            low_reviewer_confidence=int(payload.get("low_reviewer_confidence", 60)),
            high_confidence_lgtm=int(payload.get("high_confidence_lgtm", 90)),
            volatility_swing=int(payload.get("volatility_swing", 30)),
            zero_patch_gauge=int(payload.get("zero_patch_gauge", 0)),
        )


def evaluate_round_scores(
    *,
    round_no: int,
    open_findings: int,
    builder_confidence: int,
    reviewer_confidence: int,
    patch_gauge: int,
    previous_builder_confidence: int | None,
    previous_reviewer_confidence: int | None,
    thresholds: ScoreThresholds,
) -> dict[str, Any]:
    pre_messages: list[str] = []
    bconf = _normalize_score(builder_confidence, "builder_confidence", pre_messages)
    rconf = _normalize_score(reviewer_confidence, "reviewer_confidence", pre_messages)
    gauge = _normalize_score(patch_gauge, "patch_gauge", pre_messages)

    decisions: dict[str, Any] = {
        "round": round_no,
        "builder_confidence": bconf,
        "reviewer_confidence": rconf,
        "patch_gauge": gauge,
        "force_extra_round": False,
        "block_lgtm": False,
        "abort_session": False,
        "abort_reason": "",
        "low_quality_reviewer": False,
        "allow_early_lgtm": False,
        "volatility_warning": False,
        "extra_scrutiny_next_round": False,
        "messages": pre_messages,
    }

    if bconf < thresholds.low_builder_confidence:
        if open_findings > 0:
            decisions["force_extra_round"] = True
            decisions["messages"].append("builder confidence low — extra round forced")
        else:
            decisions["messages"].append("builder confidence low (no open findings) — warning only")

    if rconf < thresholds.low_reviewer_confidence:
        decisions["low_quality_reviewer"] = True
        decisions["block_lgtm"] = True
        decisions["extra_scrutiny_next_round"] = True
        decisions["messages"].append("reviewer confidence low — LOW_QUALITY verdict flagged")

    if gauge <= thresholds.zero_patch_gauge:
        decisions["messages"].append("No code changes detected in this round")
        if round_no > 1 and open_findings > 0:
            decisions["abort_session"] = True
            decisions["abort_reason"] = "Chanakya produced no patch changes — aborting"

    if bconf >= thresholds.high_confidence_lgtm and rconf >= thresholds.high_confidence_lgtm and open_findings == 0:
        decisions["allow_early_lgtm"] = True
        decisions["messages"].append(f"High confidence early LGTM at round {round_no}")

    swings: list[int] = []
    if previous_builder_confidence is not None:
        swings.append(abs(bconf - clamp_score(previous_builder_confidence)))
    if previous_reviewer_confidence is not None:
        swings.append(abs(rconf - clamp_score(previous_reviewer_confidence)))
    if swings and max(swings) > thresholds.volatility_swing:
        decisions["volatility_warning"] = True
        if open_findings > 0:
            decisions["force_extra_round"] = True
            decisions["extra_scrutiny_next_round"] = True
            decisions["messages"].append("score instability detected (volatility swing > threshold) — extra round forced")
        else:
            decisions["messages"].append("score instability detected (volatility swing > threshold) — warning only")

    if open_findings > 0:
        decisions["allow_early_lgtm"] = False
        decisions["block_lgtm"] = True
        decisions["messages"].append("open findings remain — LGTM blocked by findings gate")

    if bconf == 0 and rconf == 0 and gauge == 0:
        decisions["abort_session"] = True
        decisions["abort_reason"] = "All scores are zero (empty/invalid agent output) — aborting"

    return decisions


def mark_findings_low_quality(findings_payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(findings_payload)
    payload["verdict_quality"] = "LOW_QUALITY"
    payload["reviewer_action"] = "re-examine_with_explicit_evidence_next_round"
    return payload


def append_score_decision(path: Path, decision: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                rows = loaded
        except json.JSONDecodeError:
            rows = []
    row = dict(decision)
    row["recorded_at"] = _utc_now()
    rows.append(row)
    path.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8")
