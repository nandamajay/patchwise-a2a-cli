from __future__ import annotations

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
_PATCH_SUBJECT_RE = re.compile(
    r"\[PATCH(?:\s+v(?P<version>\d+))?(?:\s+\d+/\d+)?\]\s*(?P<title>.*)", re.IGNORECASE
)
_LORE_LINK_RE = re.compile(r"(?:https?://)?lore\.kernel\.org/r/([^\s>]+)", re.IGNORECASE)
_GENERIC_URL_RE = re.compile(r"https?://[^\s>]+")
_RESULT_LINK_RE = re.compile(r"\d+\.\s+<b><a\s*\n?href=\"([^\"]+)/\"", re.MULTILINE)


def ingest_prior_review_context(
    watch_path: Path,
    report_dir: Path,
    *,
    search_if_missing: bool,
    max_comments: int,
) -> dict | None:
    patch_files = _collect_patch_files(watch_path)
    if not patch_files:
        return None

    metadata = _series_metadata(patch_files)
    links = _extract_links_from_patches(patch_files)

    sources: list[dict] = []
    search_attempted = False
    for link in links:
        msgid = _message_id_from_link(link)
        if msgid:
            sources.append(
                {
                    "kind": "link",
                    "message_id": msgid,
                    "source": link,
                    "fetch_url": _thread_mbox_url(msgid),
                }
            )

    if not sources and search_if_missing:
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
                sources.append(
                    {
                        "kind": "search",
                        "message_id": msgid,
                        "source": f"search:v{target_version}:{series_subject}",
                        "fetch_url": _thread_mbox_url(msgid),
                    }
                )

    seen_source_msgids: set[str] = set()
    deduped_sources: list[dict] = []
    for src in sources:
        msgid = str(src.get("message_id") or "")
        if not msgid or msgid in seen_source_msgids:
            continue
        deduped_sources.append(src)
        seen_source_msgids.add(msgid)

    comments: list[dict] = []
    seen_comment_ids: set[str] = set()
    for src in deduped_sources:
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
        if not deduped_sources:
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

    report_dir.mkdir(parents=True, exist_ok=True)
    comments_path = report_dir / "prior_comments.json"
    matrix_path = report_dir / "prior_comment_matrix.md"

    context = {
        "version": 1,
        "generated_at": _utc_now(),
        "watch_path": str(watch_path),
        "detected": metadata,
        "sources": deduped_sources,
        "comments_total": len(comments),
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
        "source_total": len(deduped_sources),
        "search_used": bool(search_attempted),
        "detected_version": metadata.get("version"),
        "detected_subject": metadata.get("subject_core"),
        "detected_author": metadata.get("author_email"),
    }


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
        closed = any(str(item.get("status", "")).lower() == "closed" for item in linked)
        if closed:
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
        status = "closed" if closed else "open"

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
    msgid = match.group(1).strip().strip("/>")
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
