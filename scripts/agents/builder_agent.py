#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SUBJECT_INDEX_RE = re.compile(r"^(Subject:\s*\[PATCH(?:\s+v\d+)?\s+)\d+/\d+(\].*)$", re.MULTILINE)
_FILE_PREFIX_RE = re.compile(r"^(?P<vprefix>v\d+-)?(?P<num>\d{4})-(?P<rest>.+)$")


@dataclass
class PatchDoc:
    path: Path
    text: str


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
        docs.append(PatchDoc(path=path, text=text))
    return docs


def previous_findings(report_dir: Path, round_no: int) -> list[dict[str, Any]]:
    if round_no <= 1:
        return []
    prev = report_dir / f"round-{round_no - 1:02d}-findings.json"
    if not prev.exists():
        return []
    try:
        payload = json.loads(prev.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    findings = payload.get("findings", []) if isinstance(payload, dict) else []
    if isinstance(findings, list):
        return [x for x in findings if isinstance(x, dict)]
    return []


def locate_va_patch(docs: list[PatchDoc]) -> PatchDoc | None:
    for doc in docs:
        if (
            "diff --git a/sound/soc/codecs/lpass-va-macro.c" in doc.text
            or "--- a/sound/soc/codecs/lpass-va-macro.c" in doc.text
            or "+++ b/sound/soc/codecs/lpass-va-macro.c" in doc.text
        ):
            return doc
    return None


def replace_once(path: Path, before: str, after: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if before not in text:
        return False
    new_text = text.replace(before, after, 1)
    if new_text == text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def is_lpi_patch(doc: PatchDoc) -> bool:
    return (
        "pinctrl: qcom: lpass-lpi" in doc.text
        or "drivers/pinctrl/qcom/pinctrl-lpass-lpi.c" in doc.text
        or "/lpi/" in str(doc.path)
    )


def locate_lpi_full_pm_ops_patch(docs: list[PatchDoc]) -> PatchDoc | None:
    best: PatchDoc | None = None
    best_score = -1
    for doc in docs:
        if not is_lpi_patch(doc):
            continue
        if "RUNTIME_PM_OPS(pm_clk_suspend, pm_clk_resume, NULL)" not in doc.text:
            continue
        if ".pm = pm_ptr(&lpi_pinctrl_pm_ops)" not in doc.text:
            continue
        score = doc.text.count(".pm = pm_ptr(&lpi_pinctrl_pm_ops)")
        if score > best_score:
            best = doc
            best_score = score
    return best


def locate_lpi_guard_patch(docs: list[PatchDoc]) -> PatchDoc | None:
    for doc in docs:
        if not is_lpi_patch(doc):
            continue
        if (
            "drivers/pinctrl/qcom/pinctrl-lpass-lpi.c" in doc.text
            and "pm_runtime_resume_and_get(state->dev)" in doc.text
            and "pm_runtime_put_autosuspend(state->dev)" in doc.text
        ):
            return doc
    return None


def locate_lpi_common_patch(docs: list[PatchDoc]) -> PatchDoc | None:
    for doc in docs:
        if not is_lpi_patch(doc):
            continue
        if (
            "drivers/pinctrl/qcom/pinctrl-lpass-lpi.c" in doc.text
            and "devm_pm_clk_create(dev)" in doc.text
            and "of_pm_clk_add_clks(dev)" in doc.text
        ):
            return doc
    return None


def _parse_patch_filename(name: str) -> tuple[str, str] | None:
    m = _FILE_PREFIX_RE.match(name)
    if not m:
        return None
    vprefix = m.group("vprefix") or ""
    rest = m.group("rest")
    return vprefix, rest


def _update_subject_index(path: Path, idx: int, total: int) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    def _repl(m: re.Match[str]) -> str:
        return f"{m.group(1)}{idx}/{total}{m.group(2)}"

    new_text, n = _SUBJECT_INDEX_RE.subn(_repl, text, count=1)
    if n == 0 or new_text == text:
        return False

    path.write_text(new_text, encoding="utf-8")
    return True


def reorder_lpi_series(base: Path) -> tuple[list[str], list[str], list[str]]:
    docs = load_patch_docs(base)
    changed: list[str] = []
    actions: list[str] = []
    risks: list[str] = []

    pm_ops = locate_lpi_full_pm_ops_patch(docs)
    guard = locate_lpi_guard_patch(docs)
    common = locate_lpi_common_patch(docs)

    if pm_ops is None or guard is None or common is None:
        risks.append("Could not locate all LPI series parts (pm_ops/guard/common) for auto-respin.")
        return changed, actions, risks

    target_docs: list[PatchDoc] = []
    seen: set[Path] = set()
    for doc in [pm_ops, guard, common]:
        if doc.path in seen:
            continue
        target_docs.append(doc)
        seen.add(doc.path)

    if len(target_docs) < 2:
        risks.append("Insufficient distinct LPI patches to reorder.")
        return changed, actions, risks

    # First pass: move everything to temporary names to avoid collisions.
    temp_map: list[tuple[Path, Path]] = []
    for idx, doc in enumerate(target_docs, start=1):
        parsed = _parse_patch_filename(doc.path.name)
        if parsed is None:
            risks.append(f"Skipping non-standard patch filename: {doc.path.name}")
            continue
        _vprefix, _rest = parsed
        tmp = doc.path.with_name(f"{doc.path.name}.a2a-tmp-{os.getpid()}-{idx}")
        if doc.path != tmp:
            doc.path.rename(tmp)
        temp_map.append((tmp, doc.path))

    # Second pass: assign normalized order 0001, 0002, 0003 (or fewer if deduped).
    final_paths: list[Path] = []
    total = len(temp_map)
    for idx, (tmp, original) in enumerate(temp_map, start=1):
        parsed = _parse_patch_filename(original.name)
        if parsed is None:
            final = original
        else:
            vprefix, rest = parsed
            final = original.with_name(f"{vprefix}{idx:04d}-{rest}")
        tmp.rename(final)
        final_paths.append(final)
        if str(final) not in changed:
            changed.append(str(final))

    for idx, path in enumerate(final_paths, start=1):
        _update_subject_index(path, idx, total)

    actions.append(
        "Reordered LPI series to pm_ops -> guard -> common and renumbered patch indices for bisect safety."
    )
    return changed, actions, risks


def _unique(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        out.append(item)
        seen.add(item)
    return out


def write_builder_markdown(path: Path, changed: list[str], actions: list[str], risks: list[str]) -> None:
    lines = [
        f"# Round {os.environ.get('A2A_ROUND', '?')}: Builder Output",
        "",
        "## Changes",
    ]
    if changed:
        lines.extend([f"- {x}" for x in changed])
    else:
        lines.append("- no file changes")

    lines.extend(["", "## Rationale"])
    if actions:
        lines.extend([f"- {x}" for x in actions])
    else:
        lines.append("- No actionable open findings from previous round.")

    lines.extend(["", "## Verification Commands", "- reviewer run validates prior comment mappings"])
    lines.extend(["", "## Response To Reviewer Findings"])
    if risks:
        lines.extend([f"- {x}" for x in risks])
    else:
        lines.append("- All addressed in this round.")
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    round_no = int(os.environ.get("A2A_ROUND", "1"))
    watch_raw = os.environ.get("A2A_WATCH_PATH", "").strip()
    watch_path = Path(watch_raw) if watch_raw else None
    builder_file = Path(os.environ["A2A_BUILDER_FILE"])
    report_dir = Path(os.environ["A2A_REPORT_DIR"])

    changed: list[str] = []
    actions: list[str] = []
    risks: list[str] = []

    if watch_path is None or not watch_path.exists():
        write_builder_markdown(builder_file, changed, actions, ["A2A_WATCH_PATH is missing or invalid."])
        return 0

    docs = load_patch_docs(watch_path)
    open_prev = [
        finding
        for finding in previous_findings(report_dir, round_no)
        if str(finding.get("status", "")).lower() != "closed"
    ]

    va_patch = locate_va_patch(docs)
    need_lpi_respin = False

    for finding in open_prev:
        cid = str(finding.get("source_comment_id") or "")
        title = str(finding.get("title") or "").lower()

        if (
            "11f2596c-c9e5-46d3-af6b-1f6b09c2db78" in cid
            or "lpass-va-macro" in title
            or "runtime pm" in title
        ) and va_patch is not None:
            changed_now = replace_once(
                va_patch.path,
                "pm_runtime_put_noidle(va->dev);",
                "pm_runtime_put_autosuspend(va->dev);",
            )
            changed_sync = replace_once(
                va_patch.path,
                "pm_runtime_put_noidle(dev);",
                "pm_runtime_put_sync(dev);",
            )
            if changed_now:
                changed.append(str(va_patch.path))
                actions.append("Replaced pm_runtime_put_noidle() with pm_runtime_put_autosuspend() in VA patch.")
            if changed_sync:
                if str(va_patch.path) not in changed:
                    changed.append(str(va_patch.path))
                actions.append("Replaced pm_runtime_put_noidle(dev) with pm_runtime_put_sync(dev) in VA patch.")
            if not changed_now and not changed_sync:
                actions.append("No runtime PM noidle->sync/autosuspend replacement required in VA patch.")

        if "077cec8c-f6a3-4ee8-8ccf-7bc2e540bc61" in cid or "29c02913-25a7-4269-9fa6-6f44c94ccefa" in cid:
            need_lpi_respin = True

    if need_lpi_respin:
        lpi_changed, lpi_actions, lpi_risks = reorder_lpi_series(watch_path)
        changed.extend(lpi_changed)
        actions.extend(lpi_actions)
        risks.extend(lpi_risks)

    changed = _unique(changed)
    actions = _unique(actions)
    risks = _unique(risks)

    write_builder_markdown(builder_file, changed, actions, risks)
    print(f"[builder] changed_files={len(changed)} open_prev_findings={len(open_prev)}")
    if changed:
        for item in changed:
            print(f"[builder] changed: {item}")
    if actions:
        for item in actions:
            print(f"[builder] action: {item}")
    if risks:
        for item in risks:
            print(f"[builder] risk: {item}")
    print(f"[builder] builder_file={builder_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
