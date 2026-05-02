#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class PatchDoc:
    path: Path
    text: str
    lines: list[str]


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def find_patch_files(base: Path) -> list[Path]:
    if base.is_file() and base.suffix == ".patch":
        return [base]
    if base.is_dir():
        return sorted(p for p in base.rglob("*.patch") if p.is_file())
    return []


def load_patch_docs(base: Path) -> list[PatchDoc]:
    docs: list[PatchDoc] = []
    for path in find_patch_files(base):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        docs.append(PatchDoc(path=path, text=text, lines=text.splitlines()))
    return docs


def find_line(doc: PatchDoc, needle: str) -> int | None:
    for idx, line in enumerate(doc.lines, start=1):
        if needle in line:
            return idx
    return None


def first_doc_with_all(docs: list[PatchDoc], needles: list[str]) -> PatchDoc | None:
    for doc in docs:
        if all(n in doc.text for n in needles):
            return doc
    return None


def first_doc_with_any(docs: list[PatchDoc], needles: list[str]) -> PatchDoc | None:
    for doc in docs:
        if any(n in doc.text for n in needles):
            return doc
    return None


def patch_order(docs: list[PatchDoc], target: PatchDoc | None) -> int | None:
    if target is None:
        return None
    for idx, doc in enumerate(docs):
        if doc.path == target.path:
            return idx
    return None


def locate_va_patch(docs: list[PatchDoc]) -> PatchDoc | None:
    return first_doc_with_any(
        docs,
        [
            "diff --git a/sound/soc/codecs/lpass-va-macro.c",
            "--- a/sound/soc/codecs/lpass-va-macro.c",
            "+++ b/sound/soc/codecs/lpass-va-macro.c",
        ],
    )


def locate_lpi_pm_ops_patch(docs: list[PatchDoc]) -> PatchDoc | None:
    return first_doc_with_all(
        docs,
        [
            "RUNTIME_PM_OPS(pm_clk_suspend, pm_clk_resume, NULL)",
            ".pm = pm_ptr(&lpi_pinctrl_pm_ops)",
        ],
    )


def locate_lpi_common_patch(docs: list[PatchDoc]) -> PatchDoc | None:
    return first_doc_with_all(
        docs,
        [
            "drivers/pinctrl/qcom/pinctrl-lpass-lpi.c",
            "devm_pm_clk_create(dev)",
            "of_pm_clk_add_clks(dev)",
        ],
    )


def locate_lpi_guard_patch(docs: list[PatchDoc]) -> PatchDoc | None:
    return first_doc_with_all(
        docs,
        [
            "drivers/pinctrl/qcom/pinctrl-lpass-lpi.c",
            "pm_runtime_resume_and_get(state->dev)",
            "pm_runtime_put_autosuspend(state->dev)",
        ],
    )


def locate_lpi_full_pm_ops_patch(docs: list[PatchDoc]) -> PatchDoc | None:
    # Heuristic for the patch that wires PM ops on multiple SoC drivers.
    best: PatchDoc | None = None
    best_score = -1
    for doc in docs:
        if "RUNTIME_PM_OPS(pm_clk_suspend, pm_clk_resume, NULL)" not in doc.text:
            continue
        if ".pm = pm_ptr(&lpi_pinctrl_pm_ops)" not in doc.text:
            continue
        score = doc.text.count(".pm = pm_ptr(&lpi_pinctrl_pm_ops)")
        if score > best_score:
            best = doc
            best_score = score
    return best


def check_va_runtime_fix(docs: list[PatchDoc]) -> tuple[bool, list[str], str]:
    va_patch = locate_va_patch(docs)
    if va_patch is None:
        return False, ["Could not locate VA macro patch content."], "unknown:1"

    ev: list[str] = []
    loc = f"{va_patch.path.name}:1"

    l_resume = find_line(va_patch, "pm_runtime_resume_and_get(va->dev)")
    l_noidle = find_line(va_patch, "pm_runtime_put_noidle(va->dev)")
    l_auto = find_line(va_patch, "pm_runtime_put_autosuspend(va->dev)")
    l_put_sync = find_line(va_patch, "pm_runtime_put_sync(dev)")
    if l_put_sync is None:
        l_put_sync = find_line(va_patch, "pm_runtime_put_sync(va->dev)")
    l_enable = find_line(va_patch, "pm_runtime_enable(dev)")
    if l_enable is None:
        l_enable = find_line(va_patch, "pm_runtime_enable(va->dev)")
    l_err = find_line(va_patch, "err_rpm_disable:")

    if l_resume:
        loc = f"{va_patch.path.name}:{l_resume}"
        ev.append(f"Found runtime resume-and-get at line {l_resume}.")
    if l_auto:
        ev.append(f"Found autosuspend put path at line {l_auto}.")
    if l_put_sync:
        ev.append(f"Found probe unwind sync put at line {l_put_sync}.")
    else:
        ev.append("Missing probe unwind sync put path (pm_runtime_put_sync).")

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


def check_va_mark_last_busy_logic(docs: list[PatchDoc]) -> tuple[bool, list[str], str]:
    va_patch = locate_va_patch(docs)
    if va_patch is None:
        return False, ["Could not locate VA macro patch content."], "unknown:1"

    l = find_line(va_patch, "pm_runtime_put_autosuspend(va->dev)")
    if l is None:
        return False, ["pm_runtime_put_autosuspend(va->dev) not found in VA patch."], f"{va_patch.path.name}:1"

    return (
        True,
        ["VA disable path uses pm_runtime_put_autosuspend(), which includes mark_last_busy internally."],
        f"{va_patch.path.name}:{l}",
    )


def check_lpi_bisect_reorder(docs: list[PatchDoc]) -> tuple[bool, list[str], str]:
    pm_ops_patch = locate_lpi_pm_ops_patch(docs)
    full_pm_ops_patch = locate_lpi_full_pm_ops_patch(docs)
    common_patch = locate_lpi_common_patch(docs)
    guard_patch = locate_lpi_guard_patch(docs)

    if pm_ops_patch is None:
        return False, ["Could not locate patch adding LPASS LPI runtime PM ops."], "unknown:1"
    if full_pm_ops_patch is None:
        return False, ["Could not locate patch wiring PM ops across all LPI SoC drivers."], "unknown:1"
    if common_patch is None:
        return False, ["Could not locate common LPASS LPI runtime PM conversion patch."], "unknown:1"

    ev: list[str] = []
    loc = f"{full_pm_ops_patch.path.name}:1"
    ok = True

    l_pm_ops = find_line(pm_ops_patch, ".pm = pm_ptr(&lpi_pinctrl_pm_ops)")
    l_pm_clock = find_line(pm_ops_patch, "RUNTIME_PM_OPS(pm_clk_suspend, pm_clk_resume, NULL)")
    if l_pm_ops and l_pm_clock:
        ev.append(f"PM-ops wiring present in {pm_ops_patch.path.name} (lines {l_pm_clock}, {l_pm_ops}).")
    else:
        ok = False
        ev.append("Could not verify PM-ops wiring lines in PM-ops patch.")

    full_ops_count = full_pm_ops_patch.text.count(".pm = pm_ptr(&lpi_pinctrl_pm_ops)")
    ev.append(f"Full PM-ops patch {full_pm_ops_patch.path.name} updates {full_ops_count} driver entries.")

    ord_pm = patch_order(docs, full_pm_ops_patch)
    ord_common = patch_order(docs, common_patch)
    if ord_pm is not None and ord_common is not None and ord_pm <= ord_common:
        ev.append(f"Patch order is bisect-safe for PM ops vs common conversion ({ord_pm} <= {ord_common}).")
    else:
        ok = False
        ev.append("Patch order is not bisect-safe: PM-ops patch appears after common conversion patch.")

    if guard_patch is not None:
        l_guard = find_line(guard_patch, "pm_runtime_resume_and_get(state->dev)")
        if l_guard:
            ev.append(f"GPIO runtime guard exists in {guard_patch.path.name}:{l_guard}.")

    return ok, ev, loc


def check_lpi_crash_risk_split(docs: list[PatchDoc]) -> tuple[bool, list[str], str]:
    pm_ops_patch = locate_lpi_full_pm_ops_patch(docs)
    common_patch = locate_lpi_common_patch(docs)
    guard_patch = locate_lpi_guard_patch(docs)

    if pm_ops_patch is None or common_patch is None or guard_patch is None:
        return False, ["Could not locate PM-ops/common/guard LPASS LPI patch set pieces."], "unknown:1"

    l_ops = find_line(pm_ops_patch, "RUNTIME_PM_OPS(pm_clk_suspend, pm_clk_resume, NULL)")
    l_guard_read = find_line(guard_patch, "pm_runtime_resume_and_get(state->dev)")
    l_guard_put = find_line(guard_patch, "pm_runtime_put_autosuspend(state->dev)")

    ok = all(x is not None for x in [l_ops, l_guard_read, l_guard_put])
    ev: list[str] = []
    if l_ops:
        ev.append(f"PM callbacks present in {pm_ops_patch.path.name}:{l_ops}.")
    if l_guard_read:
        ev.append(f"GPIO runtime guard present in {guard_patch.path.name}:{l_guard_read}.")
    if l_guard_put:
        ev.append(f"GPIO autosuspend balance present in {guard_patch.path.name}:{l_guard_put}.")

    ord_common = patch_order(docs, common_patch)
    ord_guard = patch_order(docs, guard_patch)
    if ord_common is not None and ord_guard is not None:
        if common_patch.path == guard_patch.path or ord_guard <= ord_common:
            ev.append("Common conversion and guard are ordered crash-safe.")
        else:
            ok = False
            ev.append("Common conversion appears before guard patch, leaving bisect crash risk.")

    loc = f"{guard_patch.path.name}:{l_guard_read or 1}"
    return ok, ev, loc


def comment_to_finding(comment: dict[str, Any], docs: list[PatchDoc]) -> dict[str, Any]:
    cid = str(comment.get("id") or "")
    subject = str(comment.get("subject") or "prior review comment")
    subject_l = subject.lower()

    check_ok = False
    evidence: list[str] = []
    location = "prior_comments.json:1"

    if "1d479cf0-673a-4cea-8ba7-7287456a8f48" in cid:
        check_ok, evidence, location = check_va_mark_last_busy_logic(docs)
    elif "11f2596c-c9e5-46d3-af6b-1f6b09c2db78" in cid:
        check_ok, evidence, location = check_va_runtime_fix(docs)
    elif "lpass-va-macro" in subject_l:
        check_ok, evidence, location = check_va_runtime_fix(docs)
    elif "077cec8c-f6a3-4ee8-8ccf-7bc2e540bc61" in cid or "0/3] pinctrl: qcom: lpass-lpi" in subject_l:
        check_ok, evidence, location = check_lpi_bisect_reorder(docs)
    elif "29c02913-25a7-4269-9fa6-6f44c94ccefa" in cid or "resume clocks for gpio access" in subject_l:
        check_ok, evidence, location = check_lpi_crash_risk_split(docs)
    else:
        evidence = ["No automated checker registered for this prior comment id/subject."]

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
        for finding in findings:
            lines.append(
                "- [{sev}] {title} ({loc}) status={status}".format(
                    sev=finding.get("severity"),
                    title=finding.get("title"),
                    loc=finding.get("location"),
                    status=finding.get("status"),
                )
            )

    open_count = sum(1 for finding in findings if str(finding.get("status", "")).lower() != "closed")
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
            prior_comments = [item for item in items if isinstance(item, dict)]

    docs = load_patch_docs(Path(watch_path_raw)) if watch_path_raw else []

    findings = [comment_to_finding(comment, docs) for comment in prior_comments]

    save_json(findings_file, {"findings": findings})
    write_review_markdown(review_file, findings)

    open_count = sum(1 for finding in findings if str(finding.get("status", "")).lower() != "closed")
    closed_count = len(findings) - open_count
    print(f"[aryabhatta] findings_total={len(findings)} closed={closed_count} open={open_count}")
    print(f"[aryabhatta] review_file={review_file}")
    print(f"[aryabhatta] findings_file={findings_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
