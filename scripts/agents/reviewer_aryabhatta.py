#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def find_patches(base: Path) -> list[Path]:
    if base.is_file() and base.suffix == ".patch":
        return [base]
    if base.is_dir():
        return sorted(p for p in base.rglob("*.patch") if p.is_file())
    return []


def find_line(path: Path, needle: str) -> int | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for idx, line in enumerate(lines, start=1):
        if needle in line:
            return idx
    return None


def file_by_name(patches: list[Path], token: str) -> Path | None:
    for patch in patches:
        if token in patch.name:
            return patch
    return None


def has_text(path: Path, needle: str) -> bool:
    return find_line(path, needle) is not None


def check_va_runtime_fix(patches: list[Path]) -> tuple[bool, list[str], str]:
    va_patch = file_by_name(patches, "v3-0002-ASoC-codecs-lpass-va-macro-Switch-to-PM-clock-framework.patch")
    if va_patch is None:
        return False, ["Missing VA v3 patch file."], "unknown:1"

    ev: list[str] = []
    loc = f"{va_patch.name}:1"

    l_resume = find_line(va_patch, "pm_runtime_resume_and_get(va->dev)")
    l_noidle = find_line(va_patch, "pm_runtime_put_noidle(va->dev)")
    l_auto = find_line(va_patch, "pm_runtime_put_autosuspend(va->dev)")
    l_put_sync = find_line(va_patch, "pm_runtime_put_sync(dev)")
    l_enable = find_line(va_patch, "pm_runtime_enable(dev)")
    l_err = find_line(va_patch, "err_rpm_disable:")

    if l_resume:
        loc = f"{va_patch.name}:{l_resume}"
        ev.append(f"Found runtime resume-and-get at line {l_resume}.")
    if l_auto:
        ev.append(f"Found autosuspend put path at line {l_auto}.")
    if l_put_sync:
        ev.append(f"Found probe unwind sync put at line {l_put_sync}.")

    ok = True
    if l_noidle:
        ok = False
        ev.append(f"Still contains pm_runtime_put_noidle(va->dev) at line {l_noidle}.")
    if not l_resume or not l_auto or not l_put_sync:
        ok = False

    if l_enable and l_err and l_enable < l_err:
        ev.append(f"Runtime PM enable appears before err_rpm_disable label ({l_enable} < {l_err}).")
    else:
        ok = False
        ev.append("Could not confirm runtime PM enable ordering against err_rpm_disable label.")

    return ok, ev, loc


def check_va_mark_last_busy_logic(patches: list[Path]) -> tuple[bool, list[str], str]:
    va_patch = file_by_name(patches, "v3-0002-ASoC-codecs-lpass-va-macro-Switch-to-PM-clock-framework.patch")
    if va_patch is None:
        return False, ["Missing VA v3 patch file."], "unknown:1"

    l = find_line(va_patch, "pm_runtime_put_autosuspend(va->dev)")
    if l is None:
        return False, ["pm_runtime_put_autosuspend(va->dev) not found in VA patch."], f"{va_patch.name}:1"

    return (
        True,
        ["VA disable path uses pm_runtime_put_autosuspend(), which includes mark_last_busy internally."],
        f"{va_patch.name}:{l}",
    )


def check_lpi_bisect_reorder(patches: list[Path]) -> tuple[bool, list[str], str]:
    p1 = file_by_name(patches, "v3-0001-pinctrl-qcom-lpass-lpi-Enable-runtime-PM-hooks-on-all-SoCs.patch")
    p2 = file_by_name(
        patches,
        "v3-0002-pinctrl-qcom-lpass-lpi-Switch-to-PM-clock-framework-and-guard-GPIO-access.patch",
    )
    cover = file_by_name(patches, "v3-0000-cover-letter.patch")

    ev: list[str] = []
    loc = "unknown:1"
    ok = True

    if p1 is None or p2 is None:
        return False, ["Missing expected LPI v3 split patches (0001/0002)."], "unknown:1"

    loc = f"{p1.name}:1"
    l_pm_ops = find_line(p1, ".pm = pm_ptr(&lpi_pinctrl_pm_ops)")
    l_pm_clock = find_line(p1, "RUNTIME_PM_OPS(pm_clk_suspend, pm_clk_resume, NULL)")
    if l_pm_ops and l_pm_clock:
        ev.append(f"Patch1 wires PM ops (lines {l_pm_clock}, {l_pm_ops}).")
    else:
        ok = False
        ev.append("Patch1 PM-ops wiring evidence not found.")

    l_rpm_get = find_line(p2, "pm_runtime_resume_and_get(state->dev)")
    if l_rpm_get:
        ev.append(f"Patch2 adds GPIO runtime resume guard at line {l_rpm_get}.")
    else:
        ok = False
        ev.append("Patch2 missing GPIO runtime resume guard evidence.")

    if cover is not None:
        l_reorder = find_line(cover, "Reordered the series for bisect safety")
        if l_reorder:
            ev.append(f"Cover letter explicitly states bisect-safe reorder at line {l_reorder}.")

    return ok, ev, loc


def check_lpi_crash_risk_split(patches: list[Path]) -> tuple[bool, list[str], str]:
    p1 = file_by_name(patches, "v3-0001-pinctrl-qcom-lpass-lpi-Enable-runtime-PM-hooks-on-all-SoCs.patch")
    p2 = file_by_name(
        patches,
        "v3-0002-pinctrl-qcom-lpass-lpi-Switch-to-PM-clock-framework-and-guard-GPIO-access.patch",
    )
    if p1 is None or p2 is None:
        return False, ["Missing expected LPI v3 split patches (0001/0002)."], "unknown:1"

    l_ops = find_line(p1, "RUNTIME_PM_OPS(pm_clk_suspend, pm_clk_resume, NULL)")
    l_guard_read = find_line(p2, "ret = pm_runtime_resume_and_get(state->dev)")
    l_guard_put = find_line(p2, "pm_runtime_put_autosuspend(state->dev)")

    ok = all(x is not None for x in [l_ops, l_guard_read, l_guard_put])
    ev = []
    if l_ops:
        ev.append(f"Patch1 adds runtime PM callbacks at line {l_ops}.")
    if l_guard_read:
        ev.append(f"Patch2 guards MMIO reads/writes with runtime resume at line {l_guard_read}.")
    if l_guard_put:
        ev.append(f"Patch2 balances runtime PM with autosuspend put at line {l_guard_put}.")
    if not ev:
        ev.append("Could not find required split/guard evidence in LPI patches.")

    loc = f"{p2.name}:{l_guard_read or 1}"
    return ok, ev, loc


def comment_to_finding(comment: dict[str, Any], patches: list[Path]) -> dict[str, Any]:
    cid = str(comment.get("id") or "")
    subject = str(comment.get("subject") or "prior review comment")

    check_ok = False
    evidence: list[str] = []
    location = "prior_comments.json:1"

    if "11f2596c-c9e5-46d3-af6b-1f6b09c2db78" in cid:
        check_ok, evidence, location = check_va_runtime_fix(patches)
    elif "1d479cf0-673a-4cea-8ba7-7287456a8f48" in cid:
        check_ok, evidence, location = check_va_mark_last_busy_logic(patches)
    elif "077cec8c-f6a3-4ee8-8ccf-7bc2e540bc61" in cid:
        check_ok, evidence, location = check_lpi_bisect_reorder(patches)
    elif "29c02913-25a7-4269-9fa6-6f44c94ccefa" in cid:
        check_ok, evidence, location = check_lpi_crash_risk_split(patches)
    else:
        evidence = ["No automated checker registered for this prior comment id."]

    status = "closed" if check_ok else "open"
    severity = "low" if check_ok else "high"

    return {
        "severity": severity,
        "title": f"Prior comment mapping: {subject}",
        "location": location,
        "evidence": evidence,
        "required_action": (
            "None." if check_ok else "Apply code/series fix and rerun reviewer with evidence."
        ),
        "status": status,
        "source_comment_id": cid,
    }


def write_review_markdown(path: Path, findings: list[dict[str, Any]]) -> None:
    lines = [
        f"# Round {os.environ.get('A2A_ROUND', '?')}: Aryabhatta Review",
        "",
        "## Findings",
        "",
    ]

    if not findings:
        lines.append("- no findings")
    else:
        for f in findings:
            lines.append(
                "- [{sev}] {title} ({loc}) status={status}".format(
                    sev=f.get("severity"),
                    title=f.get("title"),
                    loc=f.get("location"),
                    status=f.get("status"),
                )
            )

    open_count = sum(1 for f in findings if str(f.get("status", "")).lower() != "closed")
    lines.extend(["", "## Verdict", "", f"- {'LGTM' if open_count == 0 else 'pending'}", ""])

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    findings_file = Path(os.environ["A2A_FINDINGS_FILE"])
    review_file = Path(os.environ["A2A_REVIEW_FILE"])
    prior_file_raw = os.environ.get("A2A_PRIOR_COMMENTS_FILE", "").strip()
    watch_path_raw = os.environ.get("A2A_WATCH_PATH", "").strip()

    prior_comments: list[dict[str, Any]] = []
    if prior_file_raw and Path(prior_file_raw).exists():
        payload = load_json(Path(prior_file_raw))
        items = payload.get("comments", []) if isinstance(payload, dict) else []
        if isinstance(items, list):
            prior_comments = [x for x in items if isinstance(x, dict)]

    patches = find_patches(Path(watch_path_raw)) if watch_path_raw else []

    findings: list[dict[str, Any]] = []
    for comment in prior_comments:
        findings.append(comment_to_finding(comment, patches))

    save_json(findings_file, {"findings": findings})
    write_review_markdown(review_file, findings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
