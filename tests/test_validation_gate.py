import tempfile
import unittest
from pathlib import Path

from a2a_cli.main import _detect_kernel_repo_root, _resolve_gate_patch_targets


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

    def test_resolve_gate_patch_targets_round2_no_changes_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            watch = Path(td)
            (watch / "a.patch").write_text("x", encoding="utf-8")
            targets = _resolve_gate_patch_targets(watch, [], round_no=2)
            self.assertEqual(targets, [])


if __name__ == "__main__":
    unittest.main()
