import tempfile
import unittest
from pathlib import Path
from unittest import mock

from a2a_cli.static_analysis import run_coccinelle, run_gate, run_sparse


class StaticAnalysisTests(unittest.TestCase):
    def _setup_paths(self) -> tuple[Path, Path]:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        repo = root / "linux-next"
        (repo / ".git").mkdir(parents=True, exist_ok=True)
        patch = root / "x.patch"
        patch.write_text("+++ b/drivers/foo.c\n@@\n+int x;\n", encoding="utf-8")
        return repo, patch

    def test_sparse_new_warning_detected(self) -> None:
        repo, patch = self._setup_paths()
        git_calls: list[tuple[str, ...]] = []

        def fake_git(_repo: Path, *args: str, check: bool = True):
            git_calls.append(args)
            if args[:2] == ("rev-parse", "--abbrev-ref"):
                return mock.Mock(stdout="main\n", returncode=0, stderr="")
            return mock.Mock(stdout="", returncode=0, stderr="")

        with mock.patch("a2a_cli.static_analysis._tool_available", side_effect=lambda x: True):
            with mock.patch("a2a_cli.static_analysis._clean_tree", return_value=True):
                with mock.patch("a2a_cli.static_analysis._git", side_effect=fake_git):
                    with mock.patch("a2a_cli.static_analysis.subprocess.run", return_value=mock.Mock(stdout="foo.c:1: warning: boom", stderr="", returncode=0)):
                        result = run_sparse(str(patch), str(repo), {"block_on_sparse": True, "baseline_warnings": []})
        self.assertTrue(result["blocking"])
        self.assertEqual(len(result["new_warnings"]), 1)

    def test_sparse_pre_existing_warning_ignored(self) -> None:
        repo, patch = self._setup_paths()
        with mock.patch("a2a_cli.static_analysis._tool_available", side_effect=lambda x: True):
            with mock.patch("a2a_cli.static_analysis._clean_tree", return_value=True):
                with mock.patch("a2a_cli.static_analysis._git", return_value=mock.Mock(stdout="main\n", returncode=0, stderr="")):
                    with mock.patch("a2a_cli.static_analysis.subprocess.run", return_value=mock.Mock(stdout="foo.c:1: warning: boom", stderr="", returncode=0)):
                        result = run_sparse(
                            str(patch),
                            str(repo),
                            {"block_on_sparse": True, "baseline_warnings": ["foo.c:1: warning: boom"]},
                        )
        self.assertFalse(result["blocking"])
        self.assertEqual(result["new_warnings"], [])

    def test_coccinelle_match_advisory_not_blocking(self) -> None:
        repo, patch = self._setup_paths()
        with mock.patch("a2a_cli.static_analysis._tool_available", return_value=True):
            with mock.patch("a2a_cli.static_analysis.subprocess.run", return_value=mock.Mock(stdout="match line", stderr="", returncode=0)):
                result = run_coccinelle(str(patch), str(repo))
        self.assertFalse(result["blocking"])

    def test_gate_fails_on_new_sparse_error(self) -> None:
        with mock.patch("a2a_cli.static_analysis.run_sparse", return_value={"blocking": True, "new_warnings": ["x"], "total_warnings": 1}):
            with mock.patch("a2a_cli.static_analysis.run_coccinelle", return_value={"matches": [], "blocking": False}):
                result = run_gate("a.patch", "/repo", {"sparse": True, "coccinelle": True})
        self.assertFalse(result["gate_passed"])

    def test_gate_passes_on_no_new_errors(self) -> None:
        with mock.patch("a2a_cli.static_analysis.run_sparse", return_value={"blocking": False, "new_warnings": [], "total_warnings": 0}):
            with mock.patch("a2a_cli.static_analysis.run_coccinelle", return_value={"matches": ["x"], "blocking": False}):
                result = run_gate("a.patch", "/repo", {"sparse": True, "coccinelle": True})
        self.assertTrue(result["gate_passed"])

    def test_kernel_tree_restored_after_analysis(self) -> None:
        repo, patch = self._setup_paths()
        calls: list[tuple[str, ...]] = []

        def fake_git(_repo: Path, *args: str, check: bool = True):
            calls.append(args)
            if args[:2] == ("rev-parse", "--abbrev-ref"):
                return mock.Mock(stdout="main\n", returncode=0, stderr="")
            return mock.Mock(stdout="", returncode=0, stderr="")

        with mock.patch("a2a_cli.static_analysis._tool_available", side_effect=lambda x: True):
            with mock.patch("a2a_cli.static_analysis._clean_tree", return_value=True):
                with mock.patch("a2a_cli.static_analysis._git", side_effect=fake_git):
                    with mock.patch("a2a_cli.static_analysis.subprocess.run", return_value=mock.Mock(stdout="", stderr="", returncode=0)):
                        run_sparse(str(patch), str(repo), {"block_on_sparse": True})
        self.assertIn(("checkout", "main"), calls)
        self.assertTrue(any(c[:2] == ("branch", "-D") for c in calls))

    def test_sparse_not_installed_graceful_skip(self) -> None:
        repo, patch = self._setup_paths()
        with mock.patch("a2a_cli.static_analysis._tool_available", side_effect=lambda x: x != "sparse"):
            result = run_sparse(str(patch), str(repo), {})
        self.assertTrue(result["skipped"])

    def test_coccinelle_not_installed_graceful_skip(self) -> None:
        repo, patch = self._setup_paths()
        with mock.patch("a2a_cli.static_analysis._tool_available", return_value=False):
            result = run_coccinelle(str(patch), str(repo))
        self.assertTrue(result["skipped"])


if __name__ == "__main__":
    unittest.main()
