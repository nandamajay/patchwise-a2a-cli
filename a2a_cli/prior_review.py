from __future__ import annotations

import os
import gzip
import json
import mailbox
import re
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import Message
from email.utils import parseaddr
from pathlib import Path


_USER_AGENT = "A2A-CLI/0.1"
_SASHIKO_BASE_URL = "https://sashiko.dev"
_PATCH_SUBJECT_RE = re.compile(
    r"\[PATCH(?:\s+v(?P<version>\d+))?(?:\s+\d+/\d+)?\]\s*(?P<title>.*)", re.IGNORECASE
)
_LORE_LINK_RE = re.compile(
    r"(?:https?://)?lore\.kernel\.org/(?:r|all)/(?P<msgid>[^/\s>]+)(?:/[^\s>]*)?",
    re.IGNORECASE,
)
_GENERIC_URL_RE = re.compile(r"https?://[^\s>]+")
_RESULT_LINK_RE = re.compile(r"\d+\.\s+<b><a\s*\n?href=\"([^\"]+)/\"", re.MULTILINE)
_GIT_KERNEL_COMMIT_RE = re.compile(r"https?://git\.kernel\.org/[^\s]*/c/[0-9a-f]{7,40}", re.IGNORECASE)
_GITHUB_PR_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<number>\d+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
_APPLY_NOTICE_TOKENS = (
    "applied to",
    "applied in",
    "merged into",
    "queued in",
    "queued for",
    "picked up",
    "integrated into the linux-next tree",
    "sent to linus",
)


def classify_prior_comment(comment: dict) -> dict[str, str | bool]:
    comment_id = str(comment.get("id") or "")
    subject = str(comment.get("subject") or "")
    excerpt = str(comment.get("excerpt") or "")
    source = str(comment.get("source") or "")
    body = " ".join([subject, excerpt, source]).lower()
    if comment_id.startswith("prior-meta:"):
        return {
            "comment_type": "meta",
            "external_resolved": False,
            "external_reference": "",
            "classification_reason": "system-generated prior-thread metadata",
        }

    has_apply_token = any(token in body for token in _APPLY_NOTICE_TOKENS)
    commit_match = _GIT_KERNEL_COMMIT_RE.search(" ".join([subject, excerpt, source]))
    has_kernel_tree_ref = "git.kernel.org" in body and "/c/" in body
    if has_apply_token and (commit_match is not None or has_kernel_tree_ref):
        external_ref = commit_match.group(0) if commit_match else source
        return {
            "comment_type": "maintainer_apply_notice",
            "external_resolved": True,
            "external_reference": external_ref,
            "classification_reason": "maintainer apply/merge notification",
        }

    return {
        "comment_type": "actionable_review",
        "external_resolved": False,
        "external_reference": "",
        "classification_reason": "review comment requires mapping/closure evidence",
    }


def _annotate_prior_comment(comment: dict) -> dict:
    out = dict(comment)
    meta = classify_prior_comment(out)
    out.update(meta)
    return out


def ingest_prior_review_context(
    watch_path: Path,
    report_dir: Path,
    *,
    search_if_missing: bool,
    max_comments: int,
    seed_message_ids: list[str] | None = None,
    source_context: dict | None = None,
) -> dict | None:
    patch_files = _collect_patch_files(watch_path)
    if not patch_files:
        return None

    metadata = _series_metadata(patch_files)
    links = _extract_links_from_patches(patch_files)

    lore_sources: list[dict] = []
    search_attempted = False
    for seed in seed_message_ids or []:
        msgid = str(seed or "").strip()
        if not msgid:
            continue
        lore_sources.append(
            {
                "kind": "seed",
                "message_id": msgid,
                "source": f"seed:{msgid}",
                "fetch_url": _thread_mbox_url(msgid),
            }
        )

    for link in links:
        msgid = _message_id_from_link(link)
        if msgid:
            lore_sources.append(
                {
                    "kind": "link",
                    "message_id": msgid,
                    "source": link,
                    "fetch_url": _thread_mbox_url(msgid),
                }
            )

    if not lore_sources and search_if_missing:
        candidates = metadata.get("series") or []
        for series in candidates:
            series_version = series.get("version")
            series_subject = str(series.get("subject_core") or "")
            series_author = str(series.get("author_email") or "")
            if series_version is None or int(series_version) <= 1 or not series_subject:
                continue

            search_attempted = True
            target_version = int(series_version) - 1
            query_ids = _search_lore_message_ids(series_author, series_subject, target_version)
            for msgid in query_ids:
                lore_sources.append(
                    {
                        "kind": "search",
                        "message_id": msgid,
                        "source": f"search:v{target_version}:{series_subject}",
                        "fetch_url": _thread_mbox_url(msgid),
                    }
                )

    seen_source_msgids: set[str] = set()
    deduped_lore_sources: list[dict] = []
    for src in lore_sources:
        msgid = str(src.get("message_id") or "")
        if not msgid or msgid in seen_source_msgids:
            continue
        deduped_lore_sources.append(src)
        seen_source_msgids.add(msgid)

    comments: list[dict] = []
    seen_comment_ids: set[str] = set()
    for src in deduped_lore_sources:
        messages = _load_thread_messages(str(src["message_id"]))
        for msg in messages:
            comment = _comment_from_message(msg, author_email=str(metadata.get("author_email") or ""), source=src)
            if not comment:
                continue
            cid = str(comment["id"])
            if cid in seen_comment_ids:
                continue
            comments.append(comment)
            seen_comment_ids.add(cid)
            if len(comments) >= max_comments:
                break
        if len(comments) >= max_comments:
            break

    remaining_slots = max(0, max_comments - len(comments))
    external_sources, external_comments = _collect_external_prior_comments(
        source_context,
        max_comments=remaining_slots,
    )
    for comment in external_comments:
        cid = str(comment.get("id") or "").strip()
        if not cid or cid in seen_comment_ids:
            continue
        comments.append(comment)
        seen_comment_ids.add(cid)
        if len(comments) >= max_comments:
            break

    all_sources = deduped_lore_sources + external_sources

    requires_prior = False
    for series in metadata.get("series", []):
        series_version = series.get("version")
        if series_version is not None and int(series_version) > 1:
            requires_prior = True
            break
    if not requires_prior:
        version = metadata.get("version")
        if version is not None and int(version) > 1:
            requires_prior = True

    if requires_prior:
        if not all_sources:
            comments.append(
                {
                    "id": "prior-meta:missing-thread-sources",
                    "message_id": "",
                    "from": "a2a-system",
                    "subject": "Missing prior-thread sources",
                    "date": "",
                    "excerpt": (
                        "Patch revision is v2+ but no prior-version link/source could be resolved. "
                        "Add a Link:/vN lore reference or confirm via manual review."
                    ),
                    "source": "",
                    "source_kind": "system",
                }
            )
        elif not comments:
            comments.append(
                {
                    "id": "prior-meta:no-reviewer-comments",
                    "message_id": "",
                    "from": "a2a-system",
                    "subject": "No reviewer comments parsed from prior threads",
                    "date": "",
                    "excerpt": (
                        "Prior thread source(s) were found but no reviewer comments were extracted. "
                        "Confirm manually that no actionable review comments were missed."
                    ),
                    "source": "",
                    "source_kind": "system",
                }
            )

    comments = [_annotate_prior_comment(comment) for comment in comments]

    report_dir.mkdir(parents=True, exist_ok=True)
    comments_path = report_dir / "prior_comments.json"
    matrix_path = report_dir / "prior_comment_matrix.md"

    type_totals = {
        "actionable_review": 0,
        "maintainer_apply_notice": 0,
        "meta": 0,
    }
    for comment in comments:
        ctype = str(comment.get("comment_type") or "actionable_review")
        if ctype in type_totals:
            type_totals[ctype] += 1

    context = {
        "version": 1,
        "generated_at": _utc_now(),
        "watch_path": str(watch_path),
        "detected": metadata,
        "sources": all_sources,
        "comments_total": len(comments),
        "comment_type_totals": type_totals,
        "comments": comments,
    }
    comments_path.write_text(json.dumps(context, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    matrix = render_prior_comment_matrix(comments, [])
    matrix_path.write_text(matrix, encoding="utf-8")

    return {
        "enabled": True,
        "comments_file": str(comments_path),
        "matrix_file": str(matrix_path),
        "comments_total": len(comments),
        "comment_type_totals": type_totals,
        "source_total": len(all_sources),
        "search_used": bool(search_attempted),
        "detected_version": metadata.get("version"),
        "detected_subject": metadata.get("subject_core"),
        "detected_author": metadata.get("author_email"),
    }


def _collect_external_prior_comments(source_context: dict | None, *, max_comments: int) -> tuple[list[dict], list[dict]]:
    if not isinstance(source_context, dict) or max_comments <= 0:
        return [], []

    kind = str(source_context.get("kind") or "").strip().lower()
    if kind == "github_pr":
        return _load_github_prior_comments(source_context, max_comments=max_comments)
    if kind == "gerrit_change":
        return _load_gerrit_prior_comments(source_context, max_comments=max_comments)
    if kind in ("lore", "sashiko"):
        return _load_sashiko_prior_comments(source_context, max_comments=max_comments)
    return [], []


def _load_sashiko_prior_comments(source_context: dict, *, max_comments: int) -> tuple[list[dict], list[dict]]:
    message_id = str(source_context.get("message_id") or "").strip()
    if not message_id:
        url = str(source_context.get("url") or "").strip()
        if url:
            message_id = _message_id_from_link(url) or ""
    if not message_id:
        return [], []

    if source_context.get("sashiko_ingest") is False:
        return [], []

    base_url = str(source_context.get("sashiko_base_url") or "").strip() or _SASHIKO_BASE_URL
    base_url = base_url.rstrip("/")

    patchset_url = f"{base_url}/api/patchset?id={urllib.parse.quote(message_id, safe='@._+-')}"
    try:
        patchset = _fetch_json(patchset_url)
    except OSError:
        return [], []
    if not isinstance(patchset, dict):
        return [], []

    patches = patchset.get("patches") or []
    patch_by_id: dict[int, dict] = {}
    for p in patches:
        if isinstance(p, dict) and isinstance(p.get("id"), int):
            patch_by_id[p["id"]] = p

    sources = [
        {
            "kind": "sashiko",
            "message_id": message_id,
            "source": f"{base_url}/#/patchset/{urllib.parse.quote(message_id)}",
            "fetch_url": patchset_url,
        }
    ]

    comments: list[dict] = []
    seen_ids: set[str] = set()
    reviews = patchset.get("reviews") or []
    for review in reviews:
        if not isinstance(review, dict):
            continue
        review_id = review.get("id")
        if not isinstance(review_id, int):
            continue

        review_url = f"{base_url}/api/review?id={review_id}"
        try:
            review_payload = _fetch_json(review_url)
        except OSError:
            continue
        if not isinstance(review_payload, dict):
            continue

        output = review_payload.get("output")
        if not isinstance(output, str) or not output.strip():
            continue

        try:
            output_json = json.loads(output)
        except json.JSONDecodeError:
            continue

        findings = output_json.get("findings") or []
        if not isinstance(findings, list):
            continue

        patch_id = review.get("patch_id")
        patch_meta = patch_by_id.get(patch_id, {}) if isinstance(patch_id, int) else {}
        part_index = patch_meta.get("part_index")
        part_subject = str(patch_meta.get("subject") or "").strip()
        part_msgid = str(patch_meta.get("message_id") or "").strip()
        part_suffix = f" part {part_index}" if isinstance(part_index, int) else ""
        source_link = f"{base_url}/#/patchset/{urllib.parse.quote(message_id)}"
        if isinstance(part_index, int):
            source_link = f"{source_link}?part={part_index}"

        for idx, finding in enumerate(findings, 1):
            if not isinstance(finding, dict):
                continue
            severity = str(finding.get("severity") or "Unknown").strip()
            desc = str(
                finding.get("description")
                or finding.get("title")
                or finding.get("id")
                or "Sashiko finding"
            ).strip()
            locs = finding.get("locations") or []
            loc_lines: list[str] = []
            reason_lines: list[str] = []
            if isinstance(locs, list):
                for loc in locs[:3]:
                    if not isinstance(loc, dict):
                        continue
                    loc_file = str(loc.get("file") or "").strip()
                    loc_line = loc.get("line")
                    loc_label = loc_file
                    if isinstance(loc_line, int) and loc_line > 0:
                        loc_label = f"{loc_label}:{loc_line}" if loc_label else f"line {loc_line}"
                    if loc_label:
                        loc_lines.append(loc_label)
                    why = str(loc.get("why_this_location_matters") or "").strip()
                    if why:
                        reason_lines.append(why)

            excerpt_parts = [desc]
            if loc_lines:
                excerpt_parts.append("Locations: " + ", ".join(loc_lines))
            if reason_lines:
                excerpt_parts.append("Reason: " + " ".join(reason_lines))
            excerpt = _truncate_comment_excerpt(" ".join(excerpt_parts))

            comment_id = f"sashiko:{review_id}:{idx}"
            if comment_id in seen_ids:
                continue
            seen_ids.add(comment_id)

            subject = f"Sashiko {severity} finding{part_suffix}"
            if part_subject:
                subject = f"{subject} - {part_subject}"

            comments.append(
                {
                    "id": comment_id,
                    "message_id": part_msgid or message_id,
                    "from": "sashiko-bot",
                    "subject": subject,
                    "date": str(review_payload.get("created_at") or "").strip(),
                    "excerpt": excerpt,
                    "source": source_link,
                    "source_kind": "sashiko_finding",
                }
            )
            if len(comments) >= max_comments:
                return sources, comments

    return sources, comments


def _load_github_prior_comments(source_context: dict, *, max_comments: int) -> tuple[list[dict], list[dict]]:
    repo = str(source_context.get("repo") or "").strip()
    pr_number = int(source_context.get("pr_number") or 0)
    pr_url = str(source_context.get("url") or "").strip()

    if (not repo or pr_number <= 0) and pr_url:
        match = _GITHUB_PR_URL_RE.match(pr_url)
        if match:
            repo = f"{match.group('owner')}/{match.group('repo')}"
            pr_number = int(match.group("number"))

    if not repo or pr_number <= 0:
        return [], []

    if not pr_url:
        pr_url = f"https://github.com/{repo}/pull/{pr_number}"

    headers = {"User-Agent": _USER_AGENT}
    token = str(os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    sources = [
        {
            "kind": "github_pr",
            "message_id": "",
            "source": pr_url,
            "fetch_url": pr_url,
        }
    ]
    comments: list[dict] = []
    seen_ids: set[str] = set()

    endpoints = [
        ("issue", f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"),
        ("review_comment", f"https://api.github.com/repos/{repo}/pulls/{pr_number}/comments"),
        ("review", f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"),
    ]

    for endpoint_kind, endpoint in endpoints:
        try:
            rows = _fetch_json(endpoint, headers=headers)
        except OSError:
            continue
        if not isinstance(rows, list):
            continue

        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_id = str(row.get("id") or "").strip()
            if not raw_id:
                continue

            user = row.get("user")
            author = ""
            if isinstance(user, dict):
                author = str(user.get("login") or user.get("name") or "").strip()
            if not author:
                author = "github-user"

            body = str(row.get("body") or "").strip()
            state = str(row.get("state") or "").strip().lower()
            if endpoint_kind == "review" and not body and state:
                body = f"review_state={state}"
            if not body:
                continue

            comment_id = f"github-{endpoint_kind}:{raw_id}"
            if comment_id in seen_ids:
                continue
            seen_ids.add(comment_id)

            subject = f"GitHub PR comment by {author}"
            if endpoint_kind == "review":
                label = state.upper() if state else "REVIEW"
                subject = f"GitHub PR review ({label}) by {author}"
            elif endpoint_kind == "review_comment":
                path = str(row.get("path") or "").strip()
                line = row.get("line") or row.get("original_line")
                loc = path
                if isinstance(line, int) and line > 0:
                    loc = f"{loc}:{line}" if loc else f"line {line}"
                if loc:
                    subject = f"GitHub inline review comment on {loc}"

            comments.append(
                {
                    "id": comment_id,
                    "message_id": comment_id,
                    "from": author,
                    "subject": subject,
                    "date": str(row.get("created_at") or row.get("submitted_at") or "").strip(),
                    "excerpt": _truncate_comment_excerpt(body),
                    "source": str(row.get("html_url") or pr_url),
                    "source_kind": f"github_pr_{endpoint_kind}",
                }
            )
            if len(comments) >= max_comments:
                return sources, comments

    return sources, comments


def _load_gerrit_prior_comments(source_context: dict, *, max_comments: int) -> tuple[list[dict], list[dict]]:
    base_url = str(source_context.get("base_url") or "").strip().rstrip("/")
    change_id = str(source_context.get("change_id") or "").strip()
    change_url = str(source_context.get("url") or "").strip()

    if not base_url and change_url.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(change_url)
        if parsed.scheme and parsed.netloc:
            base_url = f"{parsed.scheme}://{parsed.netloc}"
    if not change_id and change_url:
        match = re.search(r"/\+/(?P<change>\d+)", change_url)
        if match:
            change_id = str(match.group("change") or "").strip()

    if not base_url or not change_id:
        return [], []
    if not change_url:
        change_url = f"{base_url}/q/{urllib.parse.quote(change_id, safe='~')}"

    encoded_change = urllib.parse.quote(change_id, safe="~")
    sources = [
        {
            "kind": "gerrit_change",
            "message_id": "",
            "source": change_url,
            "fetch_url": change_url,
        }
    ]
    comments: list[dict] = []
    seen_ids: set[str] = set()

    detail_url = f"{base_url}/changes/{encoded_change}/detail?o=MESSAGES"
    try:
        detail = _fetch_json(detail_url, headers={"User-Agent": _USER_AGENT}, strip_xssi=True)
    except OSError:
        detail = {}
    if isinstance(detail, dict):
        for row in detail.get("messages", []):
            if not isinstance(row, dict):
                continue
            text = str(row.get("message") or "").strip()
            if not text:
                continue
            raw_id = str(row.get("id") or "").strip() or str(row.get("date") or "").strip()
            if not raw_id:
                continue
            comment_id = f"gerrit-message:{raw_id}"
            if comment_id in seen_ids:
                continue
            seen_ids.add(comment_id)
            author = _author_from_actor(row.get("author"))
            subject = _truncate_comment_excerpt(text.splitlines()[0], max_len=120)
            comments.append(
                {
                    "id": comment_id,
                    "message_id": comment_id,
                    "from": author,
                    "subject": subject or "Gerrit change message",
                    "date": str(row.get("date") or "").strip(),
                    "excerpt": _truncate_comment_excerpt(text),
                    "source": change_url,
                    "source_kind": "gerrit_change_message",
                }
            )
            if len(comments) >= max_comments:
                return sources, comments

    inline_url = f"{base_url}/changes/{encoded_change}/revisions/current/comments"
    try:
        inline_payload = _fetch_json(inline_url, headers={"User-Agent": _USER_AGENT}, strip_xssi=True)
    except OSError:
        inline_payload = {}
    if isinstance(inline_payload, dict):
        for path, rows in inline_payload.items():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                text = str(row.get("message") or "").strip()
                if not text:
                    continue
                raw_id = str(row.get("id") or "").strip()
                if not raw_id:
                    updated = str(row.get("updated") or "").strip()
                    raw_id = f"{path}:{updated}:{len(comments)}"
                comment_id = f"gerrit-inline:{raw_id}"
                if comment_id in seen_ids:
                    continue
                seen_ids.add(comment_id)
                author = _author_from_actor(row.get("author"))
                line_no = row.get("line")
                loc = str(path)
                if isinstance(line_no, int) and line_no > 0:
                    loc = f"{loc}:{line_no}"
                comments.append(
                    {
                        "id": comment_id,
                        "message_id": comment_id,
                        "from": author,
                        "subject": f"Gerrit inline comment on {loc}",
                        "date": str(row.get("updated") or "").strip(),
                        "excerpt": _truncate_comment_excerpt(text),
                        "source": change_url,
                        "source_kind": "gerrit_inline_comment",
                    }
                )
                if len(comments) >= max_comments:
                    return sources, comments

    return sources, comments


def _fetch_json(url: str, *, headers: dict[str, str] | None = None, strip_xssi: bool = False) -> object:
    req_headers = {"User-Agent": _USER_AGENT}
    if isinstance(headers, dict):
        req_headers.update({str(k): str(v) for k, v in headers.items() if str(k)})
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    if strip_xssi and text.startswith(")]}'"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
    return json.loads(text)


def _author_from_actor(actor: object) -> str:
    if not isinstance(actor, dict):
        return "unknown"
    for key in ("name", "email", "username", "_account_id"):
        value = str(actor.get(key) or "").strip()
        if value:
            return value
    return "unknown"


def _truncate_comment_excerpt(text: str, *, max_len: int = 600) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= max_len:
        return clean
    return clean[:max_len].rstrip() + "..."


def load_prior_comments(comments_file: Path) -> list[dict]:
    payload = json.loads(comments_file.read_text(encoding="utf-8"))
    comments = payload.get("comments", [])
    if not isinstance(comments, list):
        return []
    out: list[dict] = []
    for entry in comments:
        if isinstance(entry, dict) and entry.get("id"):
            out.append(entry)
    return out


def augment_findings_with_prior_comments(findings: list[dict], prior_comments: list[dict], comments_file: Path) -> list[dict]:
    out = list(findings)
    index: dict[str, list[dict]] = {}
    for finding in out:
        if not isinstance(finding, dict):
            continue
        ref = str(finding.get("source_comment_id") or "").strip()
        if not ref:
            continue
        index.setdefault(ref, []).append(finding)

    comment_loc = f"{comments_file.name}:1"
    for comment in prior_comments:
        comment_id = str(comment.get("id") or "").strip()
        if not comment_id:
            continue

        linked = index.get(comment_id, [])
        if linked:
            # Reviewer already emitted a mapped finding for this prior comment.
            # Do not duplicate it with an additional synthetic entry.
            continue
        class_meta = classify_prior_comment(comment)
        if bool(comment.get("external_resolved", class_meta.get("external_resolved", False))) or str(
            comment.get("comment_type") or class_meta.get("comment_type") or ""
        ) == "maintainer_apply_notice":
            # Maintainer apply/merge notifications are externally resolved.
            continue

        subject = str(comment.get("subject") or "prior review comment").strip()
        excerpt = str(comment.get("excerpt") or "").strip()
        source = str(comment.get("source") or "").strip()

        synthetic = {
            "severity": "high",
            "title": f"Unresolved prior review comment: {subject}",
            "location": comment_loc,
            "evidence": [
                f"source_comment_id={comment_id}",
                excerpt if excerpt else "No excerpt extracted from thread message.",
                source if source else "No source URL available.",
            ],
            "required_action": (
                "Address this prior-thread comment or add a finding with "
                "source_comment_id set to this id and status=closed with concrete evidence."
            ),
            "status": "open",
            "source_comment_id": comment_id,
        }
        out.append(synthetic)

    return out


def render_prior_comment_matrix(prior_comments: list[dict], findings: list[dict]) -> str:
    lines = [
        "# Prior Comment Matrix",
        "",
        "| source_comment_id | from | subject | status | evidence |",
        "|---|---|---|---|---|",
    ]

    by_source: dict[str, list[dict]] = {}
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        source_comment_id = str(finding.get("source_comment_id") or "").strip()
        if not source_comment_id:
            continue
        by_source.setdefault(source_comment_id, []).append(finding)

    for comment in prior_comments:
        comment_id = str(comment.get("id") or "").strip()
        if not comment_id:
            continue

        linked = by_source.get(comment_id, [])
        closed = [f for f in linked if str(f.get("status", "")).lower() == "closed"]
        class_meta = classify_prior_comment(comment)
        external_resolved = bool(comment.get("external_resolved", class_meta.get("external_resolved", False))) or str(
            comment.get("comment_type") or class_meta.get("comment_type") or ""
        ) == "maintainer_apply_notice"
        status = "closed" if closed else ("external_resolved" if external_resolved else "open")

        evidence_text = ""
        if closed:
            best = closed[0]
            location = str(best.get("location") or "")
            evidence = best.get("evidence")
            if isinstance(evidence, list):
                evidence_text = " ; ".join(str(x) for x in evidence[:2])
            else:
                evidence_text = str(evidence or "")
            if location:
                evidence_text = f"{location} | {evidence_text}".strip()
        elif external_resolved:
            external_ref = str(comment.get("external_reference") or class_meta.get("external_reference") or "").strip()
            excerpt = str(comment.get("excerpt") or "").strip()
            evidence_text = (
                f"upstream_apply_notice={external_ref}"
                if external_ref
                else "upstream_apply_notice present"
            )
            if excerpt:
                evidence_text = f"{evidence_text} | {excerpt[:240]}"

        lines.append(
            "| {id} | {from_} | {subject} | {status} | {evidence} |".format(
                id=_md_escape(comment_id),
                from_=_md_escape(str(comment.get("from") or "?")),
                subject=_md_escape(str(comment.get("subject") or "?")),
                status=status,
                evidence=_md_escape(evidence_text),
            )
        )

    if len(lines) == 4:
        lines.append("| none | - | - | - | - |")

    lines.append("")
    return "\n".join(lines)


def _collect_patch_files(path: Path) -> list[Path]:
    if path.is_file() and path.suffix == ".patch":
        return [path]
    if path.is_dir():
        return sorted(p for p in path.rglob("*.patch") if p.is_file())
    return []


def _series_metadata(patch_files: list[Path]) -> dict:
    version = None
    author_email = ""
    subject_core = ""
    series: list[dict] = []
    seen_series: set[tuple[int | None, str, str]] = set()

    cover_files = [p for p in patch_files if p.name.endswith("0000-cover-letter.patch")]
    ordered = cover_files if cover_files else patch_files

    for patch in ordered:
        headers = _read_headers(patch)
        subject = headers.get("Subject", "")
        if not subject:
            continue

        parsed_version = None
        title = ""
        match = _PATCH_SUBJECT_RE.search(subject)
        if match:
            version_raw = match.group("version")
            title = str(match.group("title") or "").strip()
            if version_raw:
                try:
                    parsed_version = int(version_raw)
                except ValueError:
                    parsed_version = None
        else:
            title = subject.strip()

        _name, addr = parseaddr(headers.get("From", ""))
        row = (parsed_version, addr, title)
        if row not in seen_series and title:
            series.append(
                {
                    "version": parsed_version,
                    "author_email": addr,
                    "subject_core": title,
                }
            )
            seen_series.add(row)

        if version is None and parsed_version is not None:
            version = parsed_version
        if not subject_core and title:
            subject_core = title
        if not author_email and addr:
            author_email = addr

    return {
        "version": version,
        "author_email": author_email,
        "subject_core": subject_core,
        "series": series,
    }


def _read_headers(path: Path) -> dict[str, str]:
    headers: dict[str, str] = {}
    current_key = ""

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return headers

    for line in lines:
        if line == "":
            break
        if line.startswith((" ", "\t")) and current_key:
            headers[current_key] = headers[current_key] + " " + line.strip()
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        current_key = key.strip()
        headers[current_key] = value.strip()

    return headers


def _extract_links_from_patches(patch_files: list[Path]) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()

    for path in patch_files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            link = ""
            lower = line.lower()
            if lower.startswith("link:"):
                value = line.split(":", 1)[1].strip()
                match = _GENERIC_URL_RE.search(value)
                if match:
                    link = match.group(0)
            elif re.match(r"^v\d+\s*:\s*", lower):
                value = line.split(":", 1)[1].strip()
                match = _GENERIC_URL_RE.search(value)
                if match:
                    link = match.group(0)
                elif value.startswith("lore.kernel.org/"):
                    link = "https://" + value

            if not link:
                lore_match = _LORE_LINK_RE.search(line)
                if lore_match:
                    raw = lore_match.group(0)
                    link = raw if raw.startswith("http") else f"https://{raw}"

            if link and link not in seen:
                links.append(link)
                seen.add(link)

    return links


def _message_id_from_link(link: str) -> str | None:
    match = _LORE_LINK_RE.search(link)
    if not match:
        return None
    msgid = str(match.group("msgid") or "").strip().strip("/>")
    if not msgid:
        return None
    return urllib.parse.unquote(msgid)


def _thread_mbox_url(message_id: str) -> str:
    quoted = urllib.parse.quote(message_id, safe="@._+-")
    return f"https://lore.kernel.org/r/{quoted}/t.mbox.gz"


def _search_lore_message_ids(author_email: str, subject_core: str, target_version: int) -> list[str]:
    query_terms = []
    if author_email:
        query_terms.append(author_email)
    query_terms.append(f"[PATCH v{target_version}")
    if subject_core:
        query_terms.append(subject_core)

    query = " ".join(query_terms).strip()
    if not query:
        return []

    url = "https://lore.kernel.org/all/?q=" + urllib.parse.quote(query)
    try:
        html = _fetch_text(url)
    except OSError:
        return []

    ids: list[str] = []
    seen: set[str] = set()
    for match in _RESULT_LINK_RE.finditer(html):
        value = match.group(1).strip()
        value = urllib.parse.unquote(value)
        if not value or value in seen:
            continue
        ids.append(value)
        seen.add(value)
        if len(ids) >= 4:
            break

    return ids


def _load_thread_messages(message_id: str) -> list[Message]:
    local = _load_local_thread_messages(message_id)
    if local:
        return local

    try:
        raw = _fetch_bytes(_thread_mbox_url(message_id))
    except OSError:
        return []

    mbox_bytes = raw
    if raw[:2] == b"\x1f\x8b":
        try:
            mbox_bytes = gzip.decompress(raw)
        except OSError:
            return []

    return _parse_mbox_bytes(mbox_bytes)


def _load_local_thread_messages(message_id: str) -> list[Message]:
    cache_dir = Path("/tmp/b4_v2_threads")
    if not cache_dir.is_dir():
        return []

    for candidate in sorted(cache_dir.glob("*.mbx")):
        try:
            text = candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if message_id not in text:
            continue
        try:
            return _parse_mbox_file(candidate)
        except OSError:
            continue

    return []


def _parse_mbox_bytes(payload: bytes) -> list[Message]:
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(payload)
        tmp_path = Path(tmp.name)
    try:
        return _parse_mbox_file(tmp_path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _parse_mbox_file(path: Path) -> list[Message]:
    box = mailbox.mbox(str(path), create=False)
    return [msg for msg in box]


def _comment_from_message(msg: Message, *, author_email: str, source: dict) -> dict | None:
    _name, from_email = parseaddr(msg.get("From", ""))
    if not from_email:
        return None
    if author_email and from_email.lower() == author_email.lower():
        return None

    subject = str(msg.get("Subject", "")).strip()
    if not subject:
        return None

    excerpt = _review_excerpt(msg)
    if not excerpt:
        return None

    message_id = str(msg.get("Message-ID", "")).strip().strip("<>")
    if not message_id:
        message_id = f"generated-{abs(hash((from_email, subject, excerpt)))}"

    source_url = str(source.get("fetch_url") or "")
    return {
        "id": f"prior-msg:{message_id}",
        "message_id": message_id,
        "from": from_email,
        "subject": subject,
        "date": str(msg.get("Date", "")).strip(),
        "excerpt": excerpt,
        "source": source_url,
        "source_kind": str(source.get("kind") or ""),
    }


def _review_excerpt(msg: Message) -> str:
    body = _extract_text_body(msg)
    if not body:
        return ""

    keep: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(">"):
            continue
        if line.startswith("-- "):
            break
        if line.lower().startswith("on ") and " wrote:" in line.lower():
            continue
        if line.lower().startswith("from:"):
            continue
        if line.lower().startswith("subject:"):
            continue
        if line.lower().startswith("date:"):
            continue
        keep.append(line)
        if len(keep) >= 8:
            break

    joined = " ".join(keep).strip()
    if len(joined) > 600:
        joined = joined[:600] + "..."
    return joined


def _extract_text_body(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", "")).lower()
            if ctype != "text/plain" or "attachment" in disp:
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                return payload.decode(charset, errors="replace")
            except LookupError:
                return payload.decode("utf-8", errors="replace")
        return ""

    payload = msg.get_payload(decode=True)
    if payload is None:
        raw = msg.get_payload()
        return raw if isinstance(raw, str) else ""

    charset = msg.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _fetch_text(url: str) -> str:
    return _fetch_bytes(url).decode("utf-8", errors="ignore")


def _fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _md_escape(value: str) -> str:
    return value.replace("\n", " ").replace("\r", " ").replace("|", "\\|")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
