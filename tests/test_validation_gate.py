import tempfile
import unittest
from pathlib import Path
from unittest import mock

from a2a_cli.main import (
    _detect_kernel_repo_root,
    _resolve_gate_patch_scope,
    _resolve_gate_patch_targets,
    _run_post_respin_checkpatch,
)


class ValidationGateTests(unittest.TestCase):
    def test_detect_kernel_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            scripts = root / "scripts"
            scripts.mkdir(parents=True, exist_ok=True)
            (scripts / "checkpatch.pl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            watch = root / "patches" / "series"
            watch.mkdir(parents=True, exist_ok=True)
            self.assertEqual(_detect_kernel_repo_root(watch), root)

    def test_resolve_gate_patch_targets_round1_uses_all_when_no_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            watch = Path(td)
            p1 = watch / "a.patch"
            p2 = watch / "sub" / "b.patch"
            p2.parent.mkdir(parents=True, exist_ok=True)
            p1.write_text("x", encoding="utf-8")
            p2.write_text("y", encoding="utf-8")
            targets = _resolve_gate_patch_targets(watch, [], round_no=1)
            self.assertEqual(sorted(targets), sorted([p1, p2]))

    def test_resolve_gate_patch_targets_round2_uses_changed_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            watch = Path(td)
            p1 = watch / "a.patch"
            p2 = watch / "b.patch"
            p1.write_text("x", encoding="utf-8")
            p2.write_text("y", encoding="utf-8")
            targets = _resolve_gate_patch_targets(watch, ["b.patch"], round_no=2)
            self.assertEqual(targets, [p2])

    def test_resolve_gate_patch_targets_round2_no_changes_uses_all(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            watch = Path(td)
            p1 = watch / "a.patch"
            p2 = watch / "nested" / "b.patch"
            p2.parent.mkdir(parents=True, exist_ok=True)
            p1.write_text("x", encoding="utf-8")
            p2.write_text("y", encoding="utf-8")
            targets = _resolve_gate_patch_targets(watch, [], round_no=2)
            self.assertEqual(sorted(targets), sorted([p1, p2]))

    def test_resolve_gate_patch_scope_changed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            watch = Path(td)
            patch = watch / "x.patch"
            patch.write_text("x", encoding="utf-8")
            scope = _resolve_gate_patch_scope(watch, ["x.patch"])
            self.assertEqual(scope, "changed")

    def test_resolve_gate_patch_scope_full_when_no_patch_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            watch = Path(td)
            patch = watch / "x.patch"
            patch.write_text("x", encoding="utf-8")
            scope = _resolve_gate_patch_scope(watch, ["notes.txt"])
            self.assertEqual(scope, "full")

    def test_post_respin_checkpatch_runs_all_non_cover_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "patches"
            output.mkdir(parents=True, exist_ok=True)
            (output / "0000-cover-letter.patch").write_text("cover", encoding="utf-8")
            (output / "0001-a.patch").write_text("a", encoding="utf-8")
            (output / "0002-b.patch").write_text("b", encoding="utf-8")
            kernel = root / "kernel"
            scripts = kernel / "scripts"
            scripts.mkdir(parents=True, exist_ok=True)
            (scripts / "checkpatch.pl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

            with mock.patch(
                "a2a_cli.main.run_shell_command",
                return_value={"returncode": 0, "stdout": "", "stderr": ""},
            ) as runner:
                payload = _run_post_respin_checkpatch(output, kernel)

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["scope"], "full")
            self.assertEqual(payload["files_checked"], 2)
            self.assertEqual(runner.call_count, 2)

    def test_post_respin_checkpatch_missing_kernel_root_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "patches"
            output.mkdir(parents=True, exist_ok=True)
            (output / "0001-a.patch").write_text("a", encoding="utf-8")
            payload = _run_post_respin_checkpatch(output, None)
            self.assertFalse(payload["ok"])
            self.assertIn("kernel tree not found", " ".join(payload["issues"]))


if __name__ == "__main__":
    unittest.main()
