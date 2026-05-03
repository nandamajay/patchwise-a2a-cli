import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from a2a_cli.upstream_evidence import (
    build_evidence_package,
    enrich_findings_with_evidence,
    search_elixir,
    search_kernel_git,
    search_lore,
)


class UpstreamEvidenceTests(unittest.TestCase):
    def test_elixir_url_generated_for_symbol(self) -> None:
        url = search_elixir("pm_runtime_resume_and_get", "pinctrl")
        self.assertIn("elixir.bootlin.com", url)
        self.assertIn("pm_runtime_resume_and_get", url)

    def test_lore_url_generated_for_pattern(self) -> None:
        url = search_lore("pm_runtime")
        self.assertIn("lore.kernel.org", url)
        self.assertIn("pm_runtime", url)

    def test_git_commits_found_in_kernel_tree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True, text=True)
            (repo / "x.txt").write_text("x\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "x.txt"], check=True, capture_output=True, text=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "-c",
                    "user.email=test@example.com",
                    "-c",
                    "user.name=test",
                    "commit",
                    "-m",
                    "pm_runtime fix",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            rows = search_kernel_git("pm_runtime", str(repo))
            self.assertTrue(rows)

    def test_evidence_package_built_for_finding(self) -> None:
        pkg = build_evidence_package(
            {"title": "pm_runtime leak", "subsystem": "pinctrl"},
            kernel_tree="",
            kb_entries=[],
        )
        self.assertIn("elixir_url", pkg)
        self.assertIn("lore_url", pkg)
        self.assertIn("symbol", pkg)

    def test_strict_mode_blocks_empty_evidence(self) -> None:
        findings = [{"id": "F1", "title": "x", "status": "open"}]
        with mock.patch("a2a_cli.upstream_evidence.build_evidence_package", return_value={}):
            _rows, violations = enrich_findings_with_evidence(
                findings,
                kernel_tree="",
                strict_mode=True,
                block_on_no_evidence=True,
                kb_entries=[],
            )
        self.assertTrue(violations)

    def test_network_unreachable_graceful_fallback(self) -> None:
        findings = [{"id": "F1", "title": "x", "status": "open"}]
        rows, violations = enrich_findings_with_evidence(
            findings,
            kernel_tree="/nonexistent",
            strict_mode=True,
            block_on_no_evidence=False,
            kb_entries=[],
        )
        self.assertEqual(len(violations), 0)
        self.assertIn("upstream_evidence", rows[0])

    def test_no_upstream_match_noted_not_blocking(self) -> None:
        findings = [{"id": "F1", "title": "unknown_symbol_issue", "status": "open"}]
        rows, violations = enrich_findings_with_evidence(
            findings,
            kernel_tree="/nonexistent",
            strict_mode=True,
            block_on_no_evidence=True,
            kb_entries=[],
        )
        self.assertEqual(violations, [])
        self.assertEqual(rows[0]["upstream_evidence"]["git_commits"], [])


if __name__ == "__main__":
    unittest.main()
