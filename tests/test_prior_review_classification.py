import tempfile
from pathlib import Path

from a2a_cli.prior_review import (
    augment_findings_with_prior_comments,
    classify_prior_comment,
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
