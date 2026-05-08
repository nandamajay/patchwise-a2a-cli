from __future__ import annotations

import json
import re
import tempfile
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from a2a_cli.main import _build_suggested_replies_markdown, _generate_lore_next_version, _next_session_id
from a2a_cli.prior_review import ingest_prior_review_context


def test_ingest_prior_review_context_uses_seed_message_ids() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        watch = root / "watch"
        report = root / "report"
        watch.mkdir(parents=True, exist_ok=True)
        (watch / "0001-test.patch").write_text(
            "\n".join(
                [
                    "From: Author <author@example.com>",
                    "Subject: [PATCH] test lore seed",
                    "",
                    "---",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        msg = EmailMessage()
        msg["From"] = "Reviewer <reviewer@example.com>"
        msg["Subject"] = "Re: [PATCH] test lore seed"
        msg["Message-ID"] = "<reply-1@example.com>"
        msg.set_content("Please move cleanup to POST_PMD.")

        with mock.patch("a2a_cli.prior_review._load_thread_messages", return_value=[msg]):
            context = ingest_prior_review_context(
                watch,
                report,
                search_if_missing=False,
                max_comments=50,
                seed_message_ids=["20260504-seed@example.com"],
            )

        assert context is not None
        assert int(context["source_total"]) == 1
        assert int(context["comments_total"]) == 1
        payload = json.loads((report / "prior_comments.json").read_text(encoding="utf-8"))
        assert payload["sources"][0]["kind"] == "seed"
        assert payload["sources"][0]["message_id"] == "20260504-seed@example.com"
        assert payload["comments"][0]["comment_type"] == "actionable_review"


def test_build_suggested_replies_markdown_includes_open_and_closed() -> None:
    summary = {
        "prior_comments": {
            "tracked": [
                {
                    "source_comment_id": "prior-msg:1",
                    "subject": "Fix sequencing",
                    "current_status": "closed",
                    "latest_location": "foo.patch:12",
                    "latest_evidence": "updated ordering",
                },
                {
                    "source_comment_id": "prior-msg:2",
                    "subject": "Check refcount",
                    "current_status": "open",
                },
                {
                    "source_comment_id": "prior-msg:3",
                    "subject": "Applied upstream",
                    "current_status": "external_resolved",
                    "external_reference": "https://git.kernel.org/broonie/sound/c/74c876bfd71b",
                },
            ]
        }
    }
    findings = [
        {
            "source_comment_id": "prior-msg:2",
            "status": "open",
            "required_action": "Add shared-rail reference counting",
        }
    ]
    out = _build_suggested_replies_markdown(summary, findings, round_no=2)
    assert "prior-msg:1" in out
    assert "Addressed in this revision at foo.patch:12" in out
    assert "prior-msg:2" in out
    assert "planned action: Add shared-rail reference counting" in out
    assert "prior-msg:3" in out
    assert "Already applied upstream (https://git.kernel.org/broonie/sound/c/74c876bfd71b)" in out


def test_generate_lore_next_version_copies_and_bumps_subject() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        watch = root / "lore-watch"
        watch.mkdir(parents=True, exist_ok=True)
        source_patch = watch / "0001-test.patch"
        source_patch.write_text(
            "\n".join(
                [
                    "From: Author <author@example.com>",
                    "Subject: [PATCH 1/1] test patch",
                    "",
                    "---",
                    "diff --git a/a b/a",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        session = {
            "id": "sess-lore-next",
            "watch_path": str(watch),
            "lore": {"message_id": "20260504-seed@example.com"},
        }
        payload = _generate_lore_next_version(root, session)
        out_path = Path(payload["output_path"])
        assert out_path.exists()
        bumped_patch = next(out_path.rglob("*.patch"))
        text = bumped_patch.read_text(encoding="utf-8")
        assert "Subject: [PATCH v2 1/1] test patch" in text
        report_path = Path(payload["report"])
        assert report_path.exists()


def test_generate_lore_next_version_respects_cover_version_and_series_count() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        watch = root / "lore-watch"
        patch_dir = watch / "v2_demo.patches"
        patch_dir.mkdir(parents=True, exist_ok=True)

        (watch / "v2_demo.cover").write_text(
            "\n".join(
                [
                    "Subject: [PATCH v2 0/2] Demo series",
                    "From: Author <author@example.com>",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (watch / "v2_demo.mbx").write_text(
            "\n".join(
                [
                    "From git@z Thu Jan  1 00:00:00 1970",
                    "Subject: [PATCH v2 1/3] patch-a",
                    "",
                    "---",
                    "",
                    "From git@z Thu Jan  1 00:00:00 1970",
                    "Subject: [PATCH v2 2/3] patch-b",
                    "",
                    "---",
                    "",
                    "From git@z Thu Jan  1 00:00:00 1970",
                    "Subject: [PATCH v2 3/3] obsolete",
                    "",
                    "---",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (patch_dir / "0001-a.patch").write_text(
            "Subject: [PATCH 1/2] patch-a\n\n---\n",
            encoding="utf-8",
        )
        (patch_dir / "0002-b.patch").write_text(
            "Subject: [PATCH 2/2] patch-b\n\n---\n",
            encoding="utf-8",
        )
        (patch_dir / "obsolete_0003-c.patch").write_text(
            "Subject: [PATCH 3/3] obsolete\n\n---\n",
            encoding="utf-8",
        )
        (patch_dir / "series").write_text("0001-a.patch\n0002-b.patch\n", encoding="utf-8")

        session = {
            "id": "sess-lore-v3",
            "watch_path": str(watch),
            "lore": {"message_id": "20260504-seed@example.com"},
        }
        payload = _generate_lore_next_version(root, session)
        out_path = Path(payload["output_path"])

        assert payload["next_version"] == 3
        assert payload["patch_count"] == 2
        assert out_path == root / ".a2a" / "patches" / "sess-lore-v3" / "v3"
        cover_text = (out_path / "v2_demo.cover").read_text(encoding="utf-8")
        assert "Subject: [PATCH v3 0/2]" in cover_text
        assert "Changes since v2:" in cover_text
        assert "v2: https://lore.kernel.org/r/20260504-seed@example.com" in cover_text
        assert "Automated respin update generated by A2A." not in cover_text

        out_patch_dir = out_path / "v2_demo.patches"
        assert (out_patch_dir / "0000-cover-letter.patch").exists()
        cover_patch_text = (out_patch_dir / "0000-cover-letter.patch").read_text(encoding="utf-8")
        assert "Changes since v2:" in cover_patch_text
        assert "Automated respin update generated by A2A." not in cover_patch_text
        series_lines = (out_patch_dir / "series").read_text(encoding="utf-8").splitlines()
        assert series_lines[0] == "0000-cover-letter.patch"
        assert "Subject: [PATCH v3 1/2]" in (out_patch_dir / "0001-a.patch").read_text(encoding="utf-8")
        assert "Subject: [PATCH v3 2/2]" in (out_patch_dir / "0002-b.patch").read_text(encoding="utf-8")
        out_mbx_text = (out_path / "v2_demo.mbx").read_text(encoding="utf-8")
        assert "Subject: [PATCH v3 1/2]" in out_mbx_text
        assert "Subject: [PATCH v3 2/2]" in out_mbx_text
        assert "Subject: [PATCH v3 3/3]" not in out_mbx_text


def test_generate_lore_next_version_changelog_hides_internal_comment_ids() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        watch = root / "lore-watch"
        patch_dir = watch / "v2_demo.patches"
        patch_dir.mkdir(parents=True, exist_ok=True)
        (watch / "v2_demo.cover").write_text(
            "Subject: [PATCH v2 0/1] demo\nFrom: Author <author@example.com>\n",
            encoding="utf-8",
        )
        (patch_dir / "0001-a.patch").write_text(
            "Subject: [PATCH 1/1] patch-a\n\n---\n",
            encoding="utf-8",
        )
        (patch_dir / "series").write_text("0001-a.patch\n", encoding="utf-8")

        sid = "sess-lore-changelog"
        report_dir = root / ".a2a" / "reports" / sid
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "round-01-findings.json").write_text(
            json.dumps(
                {
                    "findings": [
                        {
                            "status": "closed",
                            "title": "Fix runtime PM unwind ordering",
                            "location": "0001-a.patch:42",
                            "source_comment_id": "prior-msg:abc123@example.com",
                        }
                    ]
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        session = {
            "id": sid,
            "watch_path": str(watch),
            "lore": {"message_id": "20260504-seed@example.com"},
        }
        payload = _generate_lore_next_version(root, session)
        out_path = Path(payload["output_path"])
        cover_patch = next(out_path.rglob("0000-cover-letter.patch"))
        cover_text = cover_patch.read_text(encoding="utf-8")
        assert "Fix runtime PM unwind ordering (0001-a.patch:42)" in cover_text
        assert "prior-msg:abc123@example.com" not in cover_text
        assert "Automated respin update generated by A2A." not in cover_text


def test_next_session_id_includes_task_slug_when_available() -> None:
    sid = _next_session_id("LPI xo_sd review")
    assert re.match(r"^sess-lpi-xo-sd-review-\d{8}-[0-9a-f]{6}$", sid)


def test_next_session_id_without_task_keeps_legacy_shape() -> None:
    sid = _next_session_id()
    assert re.match(r"^sess-\d{8}-[0-9a-f]{6}$", sid)
