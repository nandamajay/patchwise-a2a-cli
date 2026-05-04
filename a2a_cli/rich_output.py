from __future__ import annotations

import os
import shutil
from typing import Any


def _terminal_width(default: int = 100) -> int:
    return shutil.get_terminal_size((default, 24)).columns


def _supports_unicode() -> bool:
    if os.getenv("A2A_ASCII_ONLY", "").strip() in {"1", "true", "yes"}:
        return False
    encoding = (getattr(getattr(os, "device_encoding", None), "__call__", None) and os.device_encoding(1)) or ""
    if not encoding:
        encoding = os.getenv("PYTHONIOENCODING", "") or os.getenv("LANG", "")
    return "ascii" not in encoding.lower()


def _clamp_percent(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, parsed))


def _bar(value: Any, width: int = 10, full: str = "█", empty: str = "░", ascii_only: bool = False) -> str:
    percent = _clamp_percent(value)
    if ascii_only:
        full, empty = "#", "-"
    if percent is None:
        return f"[{empty * width}] N/A"
    filled = int(round((percent / 100.0) * width))
    return f"[{full * filled}{empty * (width - filled)}] {percent}%"


def _pad(text: str, width: int) -> str:
    if len(text) > width:
        return text[: max(0, width - 1)] + ("…" if width > 0 else "")
    return text + (" " * (width - len(text)))


def _line(content: str, width: int, left: str, right: str) -> str:
    inner = max(0, width - 2)
    return f"{left}{_pad(content, inner)}{right}"


def _box_chars(ascii_only: bool) -> dict[str, str]:
    if ascii_only:
        return {
            "h": "-",
            "v": "|",
            "tl": "+",
            "tr": "+",
            "bl": "+",
            "br": "+",
            "tsep": "+",
            "bsep": "+",
            "lsep": "+",
            "rsep": "+",
            "cross": "+",
        }
    return {
        "h": "─",
        "v": "│",
        "tl": "┌",
        "tr": "┐",
        "bl": "└",
        "br": "┘",
        "tsep": "┬",
        "bsep": "┴",
        "lsep": "├",
        "rsep": "┤",
        "cross": "┼",
    }


def render_session_header(
    session_id: str,
    task: str,
    round_num: int,
    max_rounds: int,
    *,
    width: int | None = None,
    ascii_only: bool | None = None,
) -> str:
    ascii_flag = (not _supports_unicode()) if ascii_only is None else ascii_only
    box = _box_chars(ascii_flag)
    w = max(60, min(width or _terminal_width(), 140))
    top = f"{box['tl']}{box['h'] * (w - 2)}{box['tr']}"
    mid1 = _line(f" Session: {session_id}", w, box["v"], box["v"])
    mid2 = _line(f" Task: {task}", w, box["v"], box["v"])
    mid3 = _line(f" Round: {round_num}/{max_rounds}", w, box["v"], box["v"])
    bot = f"{box['bl']}{box['h'] * (w - 2)}{box['br']}"
    return "\n".join([top, mid1, mid2, mid3, bot])


def render_round_table(
    round_data: dict[str, Any],
    *,
    width: int | None = None,
    ascii_only: bool | None = None,
) -> str:
    ascii_flag = (not _supports_unicode()) if ascii_only is None else ascii_only
    box = _box_chars(ascii_flag)
    w = max(80, min(width or _terminal_width(), 140))
    mid = max(10, (w - 3) // 2)
    right = w - 3 - mid

    round_no = int(round_data.get("round", 0) or 0)
    max_rounds = int(round_data.get("max_rounds", 0) or 0)
    gate = "PASSED" if bool(round_data.get("gate_passed", False)) else "FAILED"
    gate_icon = "✅" if bool(round_data.get("gate_passed", False)) else "❌"
    if ascii_flag:
        gate_icon = "OK" if bool(round_data.get("gate_passed", False)) else "NO"

    findings = round_data.get("findings", {}) or {}
    prior = round_data.get("prior_comments", {}) or {}

    left_lines = [
        " CHANAKYA",
        f" Confidence: {_bar(round_data.get('builder_confidence'), ascii_only=ascii_flag)}",
        f" Patch Gauge: {round_data.get('builder_patch_gauge', 'N/A')}",
    ]
    right_lines = [
        " ARYABHATTA",
        f" Confidence: {_bar(round_data.get('reviewer_confidence'), ascii_only=ascii_flag)}",
        f" Verdict: {round_data.get('verdict', 'PENDING')}",
    ]

    out = []
    out.append(f"{box['tl']}{box['h'] * (w - 2)}{box['tr']}")
    out.append(_line(f" Round {round_no}/{max_rounds}  ·  Gate: {gate_icon} {gate}", w, box["v"], box["v"]))
    out.append(f"{box['lsep']}{box['h'] * mid}{box['cross']}{box['h'] * right}{box['rsep']}")

    for idx in range(max(len(left_lines), len(right_lines))):
        ltxt = left_lines[idx] if idx < len(left_lines) else ""
        rtxt = right_lines[idx] if idx < len(right_lines) else ""
        line = (
            f"{box['v']}{_pad(ltxt, mid)}"
            f"{box['v']}{_pad(rtxt, right)}{box['v']}"
        )
        out.append(line)

    out.append(f"{box['lsep']}{box['h'] * mid}{box['bsep']}{box['h'] * right}{box['rsep']}")
    out.append(
        _line(
            " Findings: total={t} open={o} closed={c} new={n} resolved={r}".format(
                t=findings.get("total", 0),
                o=findings.get("open", 0),
                c=findings.get("closed", 0),
                n=findings.get("new_since_prev", findings.get("new", 0)),
                r=findings.get("resolved_since_prev", findings.get("resolved", 0)),
            ),
            w,
            box["v"],
            box["v"],
        )
    )
    prior_totals = prior.get("totals", prior) if isinstance(prior, dict) else {}
    external_resolved = int(prior_totals.get("external_resolved", prior_totals.get("closed_by_upstream", 0)) or 0)
    upstream_chunk = f" (upstream={external_resolved})" if external_resolved > 0 else ""
    out.append(
        _line(
            " Prior Comments: received={rcv} open={op} closed={cl}{upstream}".format(
                rcv=prior_totals.get("received_total", prior_totals.get("received", 0)),
                op=prior_totals.get("open", 0),
                cl=prior_totals.get("closed", 0),
                upstream=upstream_chunk,
            ),
            w,
            box["v"],
            box["v"],
        )
    )
    elapsed_seconds = round_data.get("round_elapsed_seconds")
    try:
        elapsed_value = int(elapsed_seconds) if elapsed_seconds is not None else None
    except (TypeError, ValueError):
        elapsed_value = None
    if elapsed_value is not None and elapsed_value >= 0:
        out.append(_line(f" Elapsed: {elapsed_value}s", w, box["v"], box["v"]))
    out.append(f"{box['bl']}{box['h'] * (w - 2)}{box['br']}")
    return "\n".join(out)


def render_finding_card(
    finding: dict[str, Any],
    *,
    width: int | None = None,
    ascii_only: bool | None = None,
) -> str:
    ascii_flag = (not _supports_unicode()) if ascii_only is None else ascii_only
    w = max(70, min(width or _terminal_width(), 140))

    severity = str(finding.get("severity", "low")).lower()
    severity_token = {
        "critical": "🔴 critical",
        "high": "🟠 high",
        "medium": "🟡 medium",
        "low": "🔵 low",
    }.get(severity, "🔵 low")
    if ascii_flag:
        severity_token = f"[{severity.upper()}]"

    lines = [
        f"{severity_token}  {finding.get('title', '')}",
        f"ID: {finding.get('id', '')}",
        f"Location: {finding.get('location', '')}",
        f"Description: {finding.get('description') or finding.get('required_action', '')}",
    ]
    out = []
    sep = "-" * min(w, 120)
    out.append(sep)
    for line in lines:
        out.append(line[:w])
    out.append(sep)
    return "\n".join(out)


def render_scores(
    builder_conf: Any,
    reviewer_conf: Any,
    patch_gauge: Any,
    *,
    ascii_only: bool | None = None,
) -> str:
    ascii_flag = (not _supports_unicode()) if ascii_only is None else ascii_only
    return "\n".join(
        [
            f"Chanakya  confidence  {_bar(builder_conf, ascii_only=ascii_flag)}",
            f"Aryabhata confidence  {_bar(reviewer_conf, ascii_only=ascii_flag)}",
            f"Patch gauge           {_bar(patch_gauge, ascii_only=ascii_flag)}",
        ]
    )


def render_prior_comment_status(comments: dict[str, Any] | list[dict[str, Any]]) -> str:
    if isinstance(comments, dict):
        totals = comments.get("totals", comments)
        received = int(totals.get("received_total", totals.get("received", 0)) or 0)
        open_count = int(totals.get("open", 0) or 0)
        closed = int(totals.get("closed", 0) or 0)
        external = int(totals.get("external_resolved", totals.get("closed_by_upstream", 0)) or 0)
    else:
        received = len(comments)
        open_count = 0
        closed = 0
        external = 0
        for row in comments:
            status = str((row or {}).get("status", "")).lower()
            if status in {"closed", "external_resolved"}:
                closed += 1
                if status == "external_resolved":
                    external += 1
            else:
                open_count += 1
    out = f"Prior Comments: received={received} open={open_count} closed={closed}"
    if external > 0:
        out += f" (upstream={external})"
    return out


def render_prior_comment_table(
    comments: dict[str, Any] | list[dict[str, Any]],
    *,
    width: int | None = None,
    ascii_only: bool | None = None,
) -> str:
    ascii_flag = (not _supports_unicode()) if ascii_only is None else ascii_only
    w = max(90, min(width or _terminal_width(), 160))
    rows = comments.get("tracked", []) if isinstance(comments, dict) else comments
    totals = comments.get("totals", comments) if isinstance(comments, dict) else {}
    received_total = int(totals.get("received_total", totals.get("received", 0)) or 0)
    open_total = int(totals.get("open", 0) or 0)
    closed_total = int(totals.get("closed", 0) or 0)
    external_total = int(totals.get("external_resolved", totals.get("closed_by_upstream", 0)) or 0)
    if not isinstance(rows, list):
        rows = []

    header = "Prior Comments Table"
    if ascii_flag:
        sep = "+" + ("-" * (w - 2)) + "+"
        out = [sep, _line(f" {header}", w, "|", "|"), sep]
    else:
        sep = "┌" + ("─" * (w - 2)) + "┐"
        mid = "├" + ("─" * (w - 2)) + "┤"
        out = [sep, _line(f" {header}", w, "│", "│"), mid]

    out.append(
        _line(
            (
                f" Totals: received={received_total} open={open_total} closed={closed_total}"
                + (f" upstream={external_total}" if external_total > 0 else "")
            ),
            w,
            "│" if not ascii_flag else "|",
            "│" if not ascii_flag else "|",
        )
    )
    if not rows:
        out.append(
            _line(
                " no tracked comments",
                w,
                "│" if not ascii_flag else "|",
                "│" if not ascii_flag else "|",
            )
        )

    for idx, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        source_id = str(row.get("source_comment_id") or "")
        subject = str(row.get("subject") or "")
        status = str(row.get("current_status") or row.get("status") or "open")
        fixed = "yes" if bool(row.get("fixed_by_a2a")) else "no"
        origin = str(row.get("resolution_origin") or "").strip()
        closed_round = row.get("closed_round")
        closed_text = str(closed_round) if closed_round is not None else "-"
        evidence = str(row.get("latest_evidence") or "").strip()
        location = str(row.get("latest_location") or "").strip()
        status_norm = status.lower()
        if status_norm == "external_resolved":
            needs_eye = "yes" if not evidence and not location else "no"
        else:
            needs_eye = "yes" if status_norm != "closed" or not evidence or not location else "no"
        origin_chunk = f" origin={origin}" if origin else ""
        out.append(_line(f" {idx}. {source_id} [{status}] fixed_by_a2a={fixed}{origin_chunk} needs_eye={needs_eye}", w, "│" if not ascii_flag else "|", "│" if not ascii_flag else "|"))
        out.append(_line(f"    closed_round={closed_text}  subject={subject}", w, "│" if not ascii_flag else "|", "│" if not ascii_flag else "|"))
    out.append("└" + ("─" * (w - 2)) + "┘" if not ascii_flag else "+" + ("-" * (w - 2)) + "+")
    return "\n".join(out)


def render_gate_status(passed: bool) -> str:
    if passed:
        return "Gate: ✅ PASSED"
    return "Gate: ❌ FAILED"


def render_lgtm_banner(
    session_id: str,
    *,
    rounds: int | None = None,
    total_findings: int | None = None,
    prior_closed: int | None = None,
    prior_received: int | None = None,
    static_analysis_status: str | None = None,
    kb_updates: int | None = None,
    ascii_only: bool | None = None,
) -> str:
    ascii_flag = (not _supports_unicode()) if ascii_only is None else ascii_only
    check = "[LGTM]" if ascii_flag else "✅"
    body_lines = [
        f"   {check}  LGTM — All findings closed",
        f"   Session: {session_id}",
    ]
    if rounds is not None or total_findings is not None:
        body_lines.append(
            f"   Rounds: {rounds if rounds is not None else 'N/A'}  ·  Total findings: "
            f"{total_findings if total_findings is not None else 'N/A'}"
        )
    if prior_closed is not None and prior_received is not None:
        body_lines.append(f"   Prior comments: {prior_closed}/{prior_received} closed")
    if static_analysis_status:
        body_lines.append(f"   Static analysis: {static_analysis_status}")
    if kb_updates is not None:
        body_lines.append(
            f"   Knowledge base: {kb_updates} new pattern{'s' if kb_updates != 1 else ''}"
        )

    inner_width = max(len(line) for line in body_lines) if body_lines else 40
    if ascii_flag:
        top = "+" + ("=" * inner_width) + "+"
        bot = "+" + ("=" * inner_width) + "+"
        left = right = "|"
    else:
        top = "╔" + ("═" * inner_width) + "╗"
        bot = "╚" + ("═" * inner_width) + "╝"
        left = right = "║"

    out = [top]
    for line in body_lines:
        out.append(f"{left}{_pad(line, inner_width)}{right}")
    out.append(bot)
    return "\n".join(out)


def render_phase_progress(phase: int, total_phases: int, *, ascii_only: bool | None = None) -> str:
    ascii_flag = (not _supports_unicode()) if ascii_only is None else ascii_only
    pct = 0
    if total_phases > 0:
        pct = int((phase / total_phases) * 100)
    bar = _bar(pct, ascii_only=ascii_flag)
    return f"Phase Progress: {phase}/{total_phases} {bar}"
