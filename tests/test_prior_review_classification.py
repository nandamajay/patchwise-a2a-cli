import tempfile
from pathlib import Path
from unittest import mock

from a2a_cli.prior_review import (
    _message_id_from_link,
    augment_findings_with_prior_comments,
    classify_prior_comment,
    ingest_prior_review_context,
    render_prior_comment_matrix,
)


def test_classify_prior_comment_detects_apply_notice() -> None:
    comment = {
        "id": "prior-msg:apply@example.com",
        "subject": "Re: [PATCH] test",
        "excerpt": (
            "Applied to https://git.kernel.org/pub/scm/linux/kernel/git/broonie/sound.git "
            "for-7.1 Thanks! https://git.kernel.org/broonie/sound/c/74c876bfd71b"
        ),
        "source": "https://lore.kernel.org/r/example/t.mbox.gz",
    }
    meta = classify_prior_comment(comment)
    assert meta["comment_type"] == "maintainer_apply_notice"
    assert bool(meta["external_resolved"]) is True
    assert "git.kernel.org" in str(meta["external_reference"])


def test_augment_findings_skips_synthetic_open_for_apply_notice() -> None:
    with tempfile.TemporaryDirectory() as td:
        comments_file = Path(td) / "prior_comments.json"
        comments_file.write_text("{}", encoding="utf-8")
        prior_comments = [
            {
                "id": "prior-msg:apply@example.com",
                "subject": "Re: [PATCH] test",
                "excerpt": (
                    "Applied to https://git.kernel.org/pub/scm/linux/kernel/git/broonie/sound.git "
                    "for-7.1 Thanks! https://git.kernel.org/broonie/sound/c/74c876bfd71b"
                ),
                "source": "https://lore.kernel.org/r/example/t.mbox.gz",
            }
        ]

        out = augment_findings_with_prior_comments([], prior_comments, comments_file)
        assert out == []


def test_prior_comment_matrix_marks_external_resolved() -> None:
    prior_comments = [
        {
            "id": "prior-msg:apply@example.com",
            "from": "broonie@kernel.org",
            "subject": "Re: [PATCH] test",
            "excerpt": (
                "Applied to https://git.kernel.org/pub/scm/linux/kernel/git/broonie/sound.git "
                "for-7.1 Thanks! https://git.kernel.org/broonie/sound/c/74c876bfd71b"
            ),
            "source": "https://lore.kernel.org/r/example/t.mbox.gz",
        }
    ]
    matrix = render_prior_comment_matrix(prior_comments, [])
    assert "external_resolved" in matrix
    assert "upstream_apply_notice=" in matrix


def test_message_id_from_all_lore_link() -> None:
    mid = _message_id_from_link(
        "https://lore.kernel.org/all/20260508113636.3561383-1-ajay.nandam@oss.qualcomm.com/"
    )
    assert mid == "20260508113636.3561383-1-ajay.nandam@oss.qualcomm.com"


def test_ingest_prior_context_uses_github_external_source() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        watch = root / "watch"
        report = root / "report"
        watch.mkdir(parents=True, exist_ok=True)
        (watch / "0001-test.patch").write_text(
            "From: Author <author@example.com>\nSubject: [PATCH] demo\n\n---\n",
            encoding="utf-8",
        )

        mock_sources = [
            {
                "kind": "github_pr",
                "message_id": "",
                "source": "https://github.com/openai/sample/pull/1",
                "fetch_url": "https://github.com/openai/sample/pull/1",
            }
        ]
        mock_comments = [
            {
                "id": "github-issue:1",
                "message_id": "github-issue:1",
                "from": "reviewer",
                "subject": "GitHub PR comment",
                "date": "",
                "excerpt": "Please split this change.",
                "source": "https://github.com/openai/sample/pull/1#issuecomment-1",
                "source_kind": "github_pr_issue",
            }
        ]

        with mock.patch(
            "a2a_cli.prior_review._load_github_prior_comments",
            return_value=(mock_sources, mock_comments),
        ):
            ctx = ingest_prior_review_context(
                watch,
                report,
                search_if_missing=False,
                max_comments=20,
                source_context={"kind": "github_pr", "repo": "openai/sample", "pr_number": 1},
            )

        assert ctx is not None
        assert int(ctx["source_total"]) == 1
        assert int(ctx["comments_total"]) == 1


def test_ingest_prior_context_uses_gerrit_external_source() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        watch = root / "watch"
        report = root / "report"
        watch.mkdir(parents=True, exist_ok=True)
        (watch / "0001-test.patch").write_text(
            "From: Author <author@example.com>\nSubject: [PATCH] demo\n\n---\n",
            encoding="utf-8",
        )

        mock_sources = [
            {
                "kind": "gerrit_change",
                "message_id": "",
                "source": "https://review.example.com/c/project/+/12345",
                "fetch_url": "https://review.example.com/c/project/+/12345",
            }
        ]
        mock_comments = [
            {
                "id": "gerrit-message:1",
                "message_id": "gerrit-message:1",
                "from": "gerrit-reviewer",
                "subject": "Gerrit message",
                "date": "",
                "excerpt": "Please address nit comments.",
                "source": "https://review.example.com/c/project/+/12345",
                "source_kind": "gerrit_change_message",
            }
        ]

        with mock.patch(
            "a2a_cli.prior_review._load_gerrit_prior_comments",
            return_value=(mock_sources, mock_comments),
        ):
            ctx = ingest_prior_review_context(
                watch,
                report,
                search_if_missing=False,
                max_comments=20,
                source_context={"kind": "gerrit_change", "change_id": "12345"},
            )

        assert ctx is not None
        assert int(ctx["source_total"]) == 1
        assert int(ctx["comments_total"]) == 1
