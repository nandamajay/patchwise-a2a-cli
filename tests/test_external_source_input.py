import base64
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from a2a_cli.main import (
    _fetch_gerrit_change_series,
    _fetch_github_pr_series,
    _parse_gerrit_change_ref,
    _parse_github_pr_ref,
)


class ExternalSourceInputTests(unittest.TestCase):
    def test_parse_github_pr_url(self) -> None:
        repo, number, pr_url = _parse_github_pr_ref("https://github.com/openai/sample/pull/42")
        self.assertEqual(repo, "openai/sample")
        self.assertEqual(number, 42)
        self.assertEqual(pr_url, "https://github.com/openai/sample/pull/42")

    def test_parse_github_pr_short(self) -> None:
        repo, number, pr_url = _parse_github_pr_ref("openai/sample#42")
        self.assertEqual(repo, "openai/sample")
        self.assertEqual(number, 42)
        self.assertEqual(pr_url, "https://github.com/openai/sample/pull/42")

    def test_parse_gerrit_change_url(self) -> None:
        base_url, change_id, canonical = _parse_gerrit_change_ref(
            "https://review.example.com/c/project/+/12345/7"
        )
        self.assertEqual(base_url, "https://review.example.com")
        self.assertEqual(change_id, "12345")
        self.assertEqual(canonical, "https://review.example.com/c/project/+/12345/7")

    def test_parse_gerrit_change_number_with_base(self) -> None:
        base_url, change_id, canonical = _parse_gerrit_change_ref(
            "12345",
            base_url="https://review.example.com",
        )
        self.assertEqual(base_url, "https://review.example.com")
        self.assertEqual(change_id, "12345")
        self.assertEqual(canonical, "https://review.example.com/q/12345")

    def test_fetch_github_pr_series_writes_patch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            payload = b"From abcdef Mon Sep 17 00:00:00 2001\nSubject: [PATCH] demo\n"
            with mock.patch("a2a_cli.main._fetch_url_bytes", return_value=payload):
                out_dir, source = _fetch_github_pr_series({}, "openai/sample#9", fetch_out_dir=td)

            self.assertTrue(out_dir.exists())
            patches = sorted(out_dir.glob("*.patch"))
            self.assertEqual(len(patches), 1)
            self.assertIn("github_pr", str(source.get("kind")))
            self.assertEqual(source.get("pr_number"), 9)

    def test_fetch_gerrit_change_series_decodes_base64_patch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            patch_text = "From abcdef Mon Sep 17 00:00:00 2001\nSubject: [PATCH] gerrit demo\n"
            encoded = base64.b64encode(patch_text.encode("utf-8"))
            with mock.patch("a2a_cli.main._fetch_url_bytes", return_value=encoded):
                out_dir, source = _fetch_gerrit_change_series(
                    {},
                    "12345",
                    base_url="https://review.example.com",
                    fetch_out_dir=td,
                )

            self.assertTrue(out_dir.exists())
            patches = sorted(out_dir.glob("*.patch"))
            self.assertEqual(len(patches), 1)
            text = patches[0].read_text(encoding="utf-8")
            self.assertIn("Subject: [PATCH] gerrit demo", text)
            self.assertEqual(source.get("kind"), "gerrit_change")


if __name__ == "__main__":
    unittest.main()
