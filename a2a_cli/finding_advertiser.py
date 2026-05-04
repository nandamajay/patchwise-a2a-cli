from __future__ import annotations

from dataclasses import dataclass
from typing import Any


HARDWARE_RISK_KEYWORDS = [
    "hardware",
    "shared rail",
    "refcount",
    "power down",
    "dropout",
    "glitch",
    "race",
    "corrupt",
    "register",
    "concurrent",
    "sequencing",
    "timing",
    "voltage",
    "current",
    "buck",
    "flyback",
    "clsh",
]


@dataclass
class AdvertisedFinding:
    source_comment_id: str
    severity: str
    title: str
    location: str
    required_action: str
    evidence: list[str]
    reason: str
    round_number: int
    agent: str


def _clamp_width(width: int | None) -> int:
    value = width or 100
    return max(80, min(value, 160))


def _has_evidence(finding: dict[str, Any]) -> bool:
    upstream = finding.get("upstream_evidence")
    if isinstance(upstream, dict) and upstream:
        return True
    evidence = finding.get("evidence")
    if isinstance(evidence, list):
        return any(str(item).strip() for item in evidence)
    return bool(str(evidence or "").strip())


def _norm_evidence(finding: dict[str, Any]) -> list[str]:
    evidence = finding.get("evidence")
    if isinstance(evidence, list):
        return [str(item).strip() for item in evidence if str(item).strip()]
    text = str(evidence or "").strip()
    return [text] if text else []


def _severity_rank(severity: str) -> int:
    norm = severity.lower()
    if norm == "critical":
        return 0
    if norm == "high":
        return 1
    if norm == "medium":
        return 2
    if norm == "low":
        return 3
    return 4


def _contains_hardware_risk_text(text: str) -> tuple[bool, str]:
    haystack = text.lower()
    for keyword in HARDWARE_RISK_KEYWORDS:
        if keyword in haystack:
            return True, keyword
    return False, ""


def should_advertise(
    finding: dict[str, Any],
    round_number: int,
    *,
    is_new_since_prev: bool = False,
) -> tuple[bool, str]:
    severity = str(finding.get("severity") or "low").lower()
    status = str(finding.get("status") or "open").lower()
    title = str(finding.get("title") or "")
    required_action = str(finding.get("required_action") or "")
    evidence_text = " ".join(_norm_evidence(finding))
    risk, keyword = _contains_hardware_risk_text(
        " ".join([title, required_action, evidence_text])
    )

    if severity in {"critical", "high"}:
        return True, f"{severity.upper()} severity finding"
    if severity == "medium" and status == "open":
        return True, "MEDIUM severity open finding"
    if round_number > 1 and is_new_since_prev:
        return True, f"NEW finding raised in round {round_number}"
    if _has_evidence(finding):
        return True, "upstream evidence attached"
    if risk:
        return True, f"hardware risk detected ({keyword})"
    return False, ""


def extract_advertised_findings(
    findings_payload: dict[str, Any],
    round_summary: dict[str, Any],
    round_number: int,
    agent: str = "aryabhatta",
) -> list[AdvertisedFinding]:
    findings_list = findings_payload.get("findings", [])
    if not isinstance(findings_list, list):
        return []

    fsum = round_summary.get("findings", {}) if isinstance(round_summary, dict) else {}
    raw_new_ids = fsum.get("new_ids", []) if isinstance(fsum, dict) else []
    new_ids: set[str] = set()
    if isinstance(raw_new_ids, list):
        new_ids = {str(item).strip() for item in raw_new_ids if str(item).strip()}

    out: list[AdvertisedFinding] = []
    for row in findings_list:
        if not isinstance(row, dict):
            continue
        source_id = str(row.get("source_comment_id") or "").strip()
        is_new = source_id in new_ids if source_id else False
        advertise, reason = should_advertise(row, round_number, is_new_since_prev=is_new)
        if not advertise:
            continue
        out.append(
            AdvertisedFinding(
                source_comment_id=source_id or "untracked",
                severity=str(row.get("severity") or "low").lower(),
                title=str(row.get("title") or ""),
                location=str(row.get("location") or ""),
                required_action=str(row.get("required_action") or ""),
                evidence=_norm_evidence(row),
                reason=reason,
                round_number=round_number,
                agent=agent,
            )
        )
    out.sort(key=lambda item: _severity_rank(item.severity))
    return out


def _severity_token(severity: str, ascii_only: bool) -> str:
    norm = severity.lower()
    if ascii_only:
        return f"[{norm.upper()}]"
    if norm == "critical":
        return "🔴 critical"
    if norm == "high":
        return "🟠 high"
    if norm == "medium":
        return "🟡 medium"
    return "🔵 low"


def _has_hardware_risk(findings: list[AdvertisedFinding]) -> tuple[bool, str]:
    for item in findings:
        text = " ".join([item.title, item.required_action, " ".join(item.evidence)])
        risk, _kw = _contains_hardware_risk_text(text)
        if risk:
            return True, item.source_comment_id
    return False, ""


def _pad(text: str, width: int) -> str:
    if len(text) >= width:
        return text[: max(0, width - 1)] + ("…" if width > 0 else "")
    return text + (" " * (width - len(text)))


def _line(content: str, width: int, left: str, right: str) -> str:
    inner = max(0, width - 2)
    return f"{left}{_pad(content, inner)}{right}"


def render_advertised_findings_text(
    findings: list[AdvertisedFinding],
    *,
    round_number: int,
    agent: str = "aryabhatta",
    width: int | None = None,
    ascii_only: bool = False,
) -> str:
    if not findings:
        return ""

    w = _clamp_width(width)
    if ascii_only:
        tl, tr, bl, br, h, v = "+", "+", "+", "+", "-", "|"
    else:
        tl, tr, bl, br, h, v = "┌", "┐", "└", "┘", "─", "│"

    lines: list[str] = []
    risk, source_id = _has_hardware_risk(findings)
    if risk:
        lines.append(f"{tl}{h * (w - 2)}{tr}")
        lines.append(
            _line(
                " ⚠ HARDWARE RISK DETECTED - Review carefully before LGTM",
                w,
                v,
                v,
            )
        )
        lines.append(_line(f" See finding: {source_id}", w, v, v))
        lines.append(f"{bl}{h * (w - 2)}{br}")

    label = "ARYABHATTA" if agent == "aryabhatta" else "CHANAKYA"
    lines.append(f"Key findings from {label} (Round {round_number}):")
    for finding in findings:
        token = _severity_token(finding.severity, ascii_only=ascii_only)
        lines.append("-" * min(120, w))
        lines.append(f"{token}  {finding.title}")
        lines.append(f"ID: {finding.source_comment_id}")
        lines.append(f"Location: {finding.location}")
        lines.append(f"Advertised because: {finding.reason}")
        if finding.required_action:
            lines.append(f"Action: {finding.required_action[: max(10, w - 8)]}")
        if finding.evidence:
            lines.append(f"Evidence: {finding.evidence[0][: max(10, w - 10)]}")
    lines.append("-" * min(120, w))
    return "\n".join(lines)
