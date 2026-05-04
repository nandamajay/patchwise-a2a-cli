import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from a2a_cli.main import _extract_lore_message_id, _fetch_lore_series, _lore_fetch_base_dir


class LoreLoopInputTests(unittest.TestCase):
    def test_extract_lore_message_id_from_url(self) -> None:
        mid = _extract_lore_message_id(
            "https://lore.kernel.org/all/20260504-foo-bar-1@example.com/T/"
        )
        self.assertEqual(mid, "20260504-foo-bar-1@example.com")

    def test_extract_lore_message_id_from_raw(self) -> None:
        mid = _extract_lore_message_id("<20260504-abc@example.com>")
        self.assertEqual(mid, "20260504-abc@example.com")

    def test_lore_fetch_base_dir_prefers_configured_kernel_tree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            kernel_tree = Path(td) / "linux-next"
            (kernel_tree / "scripts").mkdir(parents=True, exist_ok=True)
            (kernel_tree / "scripts" / "checkpatch.pl").write_text("", encoding="utf-8")
            base = _lore_fetch_base_dir({"upstream_evidence": {"kernel_tree": str(kernel_tree)}})
            self.assertEqual(base, kernel_tree / ".a2a" / "lore_series")

    def test_fetch_lore_series_requires_b4(self) -> None:
        with mock.patch("a2a_cli.main.shutil.which", return_value=None):
            with self.assertRaises(RuntimeError):
                _fetch_lore_series({}, "20260504-abc@example.com")

    def test_fetch_lore_series_success_creates_patch_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            kernel_tree = Path(td) / "linux-next"
            (kernel_tree / "scripts").mkdir(parents=True, exist_ok=True)
            (kernel_tree / "scripts" / "checkpatch.pl").write_text("", encoding="utf-8")
            cfg = {"upstream_evidence": {"kernel_tree": str(kernel_tree)}}

            def _fake_run(cmd: list[str], text: bool, capture_output: bool) -> SimpleNamespace:
                out_idx = cmd.index("-o") + 1
                out_dir = Path(cmd[out_idx])
                out_dir.mkdir(parents=True, exist_ok=True)
                quilt_dir = out_dir / "thread.patches"
                quilt_dir.mkdir(parents=True, exist_ok=True)
                (quilt_dir / "0001-test.patch").write_text("From test\n", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="ok", stderr="")

            with mock.patch("a2a_cli.main.shutil.which", return_value="/usr/bin/b4"):
                with mock.patch("a2a_cli.main.subprocess.run", side_effect=_fake_run):
                    out_dir, mid = _fetch_lore_series(cfg, "https://lore.kernel.org/r/20260504-xyz@example.com")
            self.assertTrue(out_dir.exists())
            self.assertTrue((out_dir / "thread.patches" / "0001-test.patch").exists())
            self.assertEqual(mid, "20260504-xyz@example.com")


if __name__ == "__main__":
    unittest.main()
