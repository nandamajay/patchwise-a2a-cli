import json
import tempfile
import unittest
from pathlib import Path

from a2a_cli.dependency_tracker import block_patchset_lgtm_until_reconciled, track_symbol_changes
from a2a_cli.series_manager import auto_discover_series, run_all_series


class SeriesManagerTests(unittest.TestCase):
    def _make_patch(self, path: Path, content: str = "int pm_runtime_enable(void) { return 0; }\n") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_auto_discover_finds_both_series(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wp = root / "patches" / "xo_sd_v3"
            self._make_patch(wp / "lpi" / "0001-a.patch")
            self._make_patch(wp / "codecs" / "0001-b.patch")
            manifest = auto_discover_series(root, wp)
            names = [s["name"] for s in manifest["series"]]
            self.assertIn("lpi", names)
            self.assertIn("codecs", names)

    def test_dependency_order_respected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = {
                "series": [
                    {"name": "codecs", "path": "/x/codecs", "depends_on": ["lpi"], "shared_symbols": []},
                    {"name": "lpi", "path": "/x/lpi", "depends_on": [], "shared_symbols": []},
                ]
            }
            order: list[str] = []

            def runner(row: dict) -> dict:
                order.append(str(row["name"]))
                return {"status": "lgtm"}

            run_all_series(root, manifest, runner)
            self.assertEqual(order, ["lpi", "codecs"])

    def test_cross_series_symbol_impact_detected(self) -> None:
        impact = track_symbol_changes(
            {"name": "lpi", "shared_symbols": ["pm_runtime_enable", "x"]},
            {"name": "codecs", "shared_symbols": ["pm_runtime_enable", "y"]},
        )
        self.assertIn("pm_runtime_enable", impact["changed_symbols"])
        self.assertTrue(impact["unresolved"])

    def test_patchset_lgtm_blocked_until_reconciled(self) -> None:
        blocked = block_patchset_lgtm_until_reconciled(
            [{"name": "lpi", "status": "lgtm"}, {"name": "codecs", "status": "failed"}],
            [{"source_series": "lpi", "affected_series": "codecs", "changed_symbols": ["pm_runtime_enable"]}],
        )
        self.assertTrue(blocked)

    def test_independent_series_run_correctly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = {
                "series": [
                    {"name": "a", "path": "/x/a", "depends_on": [], "shared_symbols": []},
                    {"name": "b", "path": "/x/b", "depends_on": [], "shared_symbols": []},
                ]
            }
            seen: set[str] = set()

            def runner(row: dict) -> dict:
                seen.add(str(row["name"]))
                return {"status": "lgtm"}

            payload = run_all_series(root, manifest, runner)
            self.assertEqual(seen, {"a", "b"})
            self.assertEqual(payload["status"], "lgtm")

    def test_manifest_generated_from_cover_letter(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wp = root / "patches" / "xo_sd_v3"
            self._make_patch(wp / "lpi" / "0001-a.patch")
            self._make_patch(wp / "codecs" / "0001-b.patch")
            (wp / "codecs" / "0000-cover.patch").write_text("Depends-on: lpi\n", encoding="utf-8")
            manifest = auto_discover_series(root, wp)
            codecs = [row for row in manifest["series"] if row["name"] == "codecs"][0]
            self.assertIn("lpi", codecs.get("depends_on", []))

    def test_partial_lgtm_reported_correctly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = {
                "series": [
                    {"name": "a", "path": "/x/a", "depends_on": [], "shared_symbols": []},
                    {"name": "b", "path": "/x/b", "depends_on": [], "shared_symbols": []},
                ]
            }

            def runner(row: dict) -> dict:
                return {"status": "lgtm" if row["name"] == "a" else "failed"}

            payload = run_all_series(root, manifest, runner)
            self.assertEqual(payload["status"], "partial")
            summary = json.loads((root / ".a2a" / "reports" / "patchset_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "partial")


if __name__ == "__main__":
    unittest.main()
