from __future__ import annotations

import json
import tempfile
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from a2a_cli.main import _build_suggested_replies_markdown, _generate_lore_next_version
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
