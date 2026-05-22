import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from a2a_cli.conflict_resolver import ConflictError, ConflictResolver
from a2a_cli.respin import (
    _collect_patch_series,
    _generate_cover_letter_template,
    _refresh_cover_letter_headers,
    detect_version_number,
    next_version_path,
    respin,
)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class RespinTests(unittest.TestCase):
    def _bootstrap_workspace(self, td: str, session_status: str = "lgtm") -> tuple[Path, str, Path]:
        root = Path(td)
        a2a = root / ".a2a"
        (a2a / "sessions").mkdir(parents=True, exist_ok=True)
        (a2a / "reports").mkdir(parents=True, exist_ok=True)
        _write_json(
            a2a / "config.json",
            {
                "respin": {
                    "conflict_strategy": "abort",
                    "keep_temp_branch": False,
                    "auto_increment_version": True,
                }
            },
        )

        repo = root / "kernel"
        (repo / ".git").mkdir(parents=True, exist_ok=True)
        patches = repo / "patches" / "xo_sd_v2"
        patches.mkdir(parents=True, exist_ok=True)
        (patches / "0001-a.patch").write_text("dummy\n", encoding="utf-8")
        (patches / "0002-b.patch").write_text("dummy\n", encoding="utf-8")

        sid = "sess-test"
        _write_json(
            a2a / "sessions" / f"{sid}.json",
            {
                "id": sid,
                "status": session_status,
                "watch_path": str(patches),
            },
        )
        return root, sid, patches

    def test_version_detection_v2_gives_v3(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "xo_sd_v2"
            path.mkdir(parents=True, exist_ok=True)
            out, src, nxt = next_version_path(path)
            self.assertEqual(src, 2)
            self.assertEqual(nxt, 3)
            self.assertEqual(out.name, "xo_sd_v3")

    def test_version_detection_v1_gives_v2(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "xo_sd_v1"
            path.mkdir(parents=True, exist_ok=True)
            out, src, nxt = next_version_path(path)
            self.assertEqual(src, 1)
            self.assertEqual(nxt, 2)
            self.assertEqual(out.name, "xo_sd_v2")

    def test_dry_run_no_writes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root, sid, _patches = self._bootstrap_workspace(td)
            result = respin(root, sid, dry_run=True)
            self.assertEqual(result["status"], "dry_run")
            self.assertFalse(Path(result["output_dir"]).exists())
            self.assertFalse(Path(result["output_copy_dir"]).exists())

    def test_cover_letter_changelog_entries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "out"
            out_dir.mkdir(parents=True, exist_ok=True)
            cover = _generate_cover_letter_template(
                out_dir,
                3,
                ["Fix runtime pm (a.patch:10)", "Fix error path (b.patch:44)"],
                previous_cover=None,
            )
            text = cover.read_text(encoding="utf-8")
            self.assertIn("Changes since v2:", text)
            self.assertIn("Fix runtime pm", text)
            self.assertNotIn("F-1", text)

    def test_refresh_cover_letter_headers_replaces_stale_thread_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cover = Path(td) / "0000-cover-letter.patch"
            cover.write_text(
                "\n".join(
                    [
                        "Subject: [PATCH v3 0/1] demo",
                        "From: Author <author@example.com>",
                        "Date: Fri, 10 May 2026 11:00:00 +0000",
                        "Message-Id: <old-v3@example.com>",
                        "In-Reply-To: <old-thread@example.com>",
                        "References: <old-thread@example.com>",
                        "",
                        "body",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            changed = _refresh_cover_letter_headers(cover)
            text = cover.read_text(encoding="utf-8")

            self.assertTrue(changed)
            self.assertNotIn("Message-Id: <old-v3@example.com>", text)
            self.assertNotIn("In-Reply-To:", text)
            self.assertNotIn("References:", text)
            self.assertRegex(text, r"(?im)^Date:\s+.+$")
            self.assertRegex(text, r"(?im)^Message-Id:\s*<[^>]+>$")

    def test_collect_patch_series_prefers_nested_series_and_skips_cover(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "watch"
            series_dir = root / "thread.patches"
            series_dir.mkdir(parents=True, exist_ok=True)
            (series_dir / "0000-cover-letter.patch").write_text("cover\n", encoding="utf-8")
            (series_dir / "0001-a.patch").write_text("a\n", encoding="utf-8")
            (series_dir / "0002-b.patch").write_text("b\n", encoding="utf-8")
            (series_dir / "obsolete_0003-c.patch").write_text("c\n", encoding="utf-8")
            (series_dir / "series").write_text(
                "\n".join(["0000-cover-letter.patch", "0001-a.patch", "0002-b.patch"]) + "\n",
                encoding="utf-8",
            )
            patches = _collect_patch_series(root)
            names = [p.name for p in patches]
            self.assertEqual(names, ["0001-a.patch", "0002-b.patch"])

    def test_patch_applied_in_series_order(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root, sid, patches = self._bootstrap_workspace(td)
            calls: list[tuple[str, ...]] = []

            def fake_git(_repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
                calls.append(args)
                if args[:3] == ("rev-parse", "--verify", "origin/main"):
                    return subprocess.CompletedProcess(args, 1, "", "no origin")
                if args[:2] == ("rev-parse", "--abbrev-ref"):
                    return subprocess.CompletedProcess(args, 0, "main\n", "")
                if args[:2] == ("status", "--porcelain"):
                    return subprocess.CompletedProcess(args, 0, "", "")
                return subprocess.CompletedProcess(args, 0, "", "")

            with mock.patch("a2a_cli.respin._run_git", side_effect=fake_git):
                respin(root, sid, dry_run=False)

            am_calls = [args for args in calls if args and args[0] == "am" and len(args) == 2]
            self.assertEqual(am_calls[0][1], str(patches / "0001-a.patch"))
            self.assertEqual(am_calls[1][1], str(patches / "0002-b.patch"))

    def test_conflict_strategy_abort(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            report_dir = Path(td) / "reports"
            resolver = ConflictResolver(Path(td), report_dir, strategy="abort")
            with mock.patch.object(
                resolver, "_git", return_value=subprocess.CompletedProcess(["git"], 0, "", "")
            ):
                with self.assertRaises(ConflictError):
                    resolver.resolve(Path("0001.patch"), {"conflicted_files": ["a.c"]})
            self.assertTrue((report_dir / "conflict_report.json").exists())

    def test_conflict_strategy_ours(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            report_dir = Path(td) / "reports"
            resolver = ConflictResolver(Path(td), report_dir, strategy="ours")
            calls: list[tuple[str, ...]] = []

            def fake_git(*args: str) -> subprocess.CompletedProcess:
                calls.append(args)
                return subprocess.CompletedProcess(args, 0, "", "")

            with mock.patch.object(resolver, "_git", side_effect=fake_git):
                entry = resolver.resolve(Path("0001.patch"), {"conflicted_files": ["a.c"]})
            self.assertEqual(entry["result"], "resolved")
            self.assertIn(("checkout", "--ours", "--", "a.c"), calls)

    def test_conflict_strategy_manual_pauses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            report_dir = Path(td) / "reports"
            resolver = ConflictResolver(Path(td), report_dir, strategy="manual")
            with mock.patch("builtins.input", side_effect=["continue"]):
                with mock.patch.object(
                    resolver, "_git", return_value=subprocess.CompletedProcess(["git"], 0, "", "")
                ):
                    entry = resolver.resolve(Path("0001.patch"), {"conflicted_files": ["a.c"]})
            self.assertEqual(entry["result"], "resolved-manual")

    def test_output_copied_to_a2a_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root, sid, _patches = self._bootstrap_workspace(td)

            def fake_git(_repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
                if args[:3] == ("rev-parse", "--verify", "origin/main"):
                    return subprocess.CompletedProcess(args, 1, "", "no origin")
                if args[:2] == ("rev-parse", "--abbrev-ref"):
                    return subprocess.CompletedProcess(args, 0, "main\n", "")
                if args[:2] == ("status", "--porcelain"):
                    return subprocess.CompletedProcess(args, 0, "", "")
                return subprocess.CompletedProcess(args, 0, "", "")

            with mock.patch("a2a_cli.respin._run_git", side_effect=fake_git):
                result = respin(root, sid, dry_run=False)

            self.assertTrue(Path(result["output_copy_dir"]).exists())

    def test_respin_blocked_if_session_not_lgtm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root, sid, _patches = self._bootstrap_workspace(td, session_status="in_progress")
            with self.assertRaises(RuntimeError):
                respin(root, sid, dry_run=True)


if __name__ == "__main__":
    unittest.main()
