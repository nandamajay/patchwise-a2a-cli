import json
import tempfile
import unittest
from pathlib import Path

from a2a_cli.knowledge_base import (
    build_aryabhata_context,
    build_chanakya_context,
    clear_kb,
    list_kb_entries,
    load_kb,
    save_kb,
    update_kb_after_lgtm,
)


class KnowledgeBaseTests(unittest.TestCase):
    def test_entry_created_after_lgtm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            update_kb_after_lgtm(
                root,
                session_id="sess-1",
                watch_path="/tmp/linux-next/sound",
                resolved_findings=[{"title": "pm_runtime fix", "severity": "medium", "required_action": "use resume_and_get"}],
            )
            rows = list_kb_entries(root)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["occurrences"], 1)

    def test_occurrence_incremented_on_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            kwargs = {
                "watch_path": "/tmp/linux-next/pinctrl",
                "resolved_findings": [{"title": "pm_runtime fix", "severity": "medium", "required_action": "use helper"}],
            }
            update_kb_after_lgtm(root, session_id="sess-1", **kwargs)
            update_kb_after_lgtm(root, session_id="sess-2", **kwargs)
            rows = list_kb_entries(root)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["occurrences"], 2)
            self.assertEqual(rows[0]["last_seen"], "sess-2")

    def test_max_entries_respected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            kb = load_kb(root)
            kb["max_entries"] = 2
            save_kb(root, kb)
            update_kb_after_lgtm(
                root,
                session_id="sess-1",
                watch_path="/tmp/linux-next/sound",
                resolved_findings=[
                    {"title": "A", "required_action": "x"},
                    {"title": "B", "required_action": "x"},
                    {"title": "C", "required_action": "x"},
                ],
            )
            self.assertLessEqual(len(list_kb_entries(root)), 2)

    def test_chanakya_prompt_gets_kb_context(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            update_kb_after_lgtm(
                root,
                session_id="sess-1",
                watch_path="/tmp/linux-next/pinctrl",
                resolved_findings=[{"title": "pm_runtime resume", "required_action": "use resume_and_get"}],
            )
            ctx = build_chanakya_context(root, "pinctrl")
            self.assertIn("Known recurring issues", ctx)
            self.assertIn("pm_runtime resume", ctx)

    def test_aryabhata_gets_kb_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            update_kb_after_lgtm(
                root,
                session_id="sess-1",
                watch_path="/tmp/linux-next/sound",
                resolved_findings=[
                    {"title": "runtime pm leak", "required_action": "add unwind", "evidence_url": "lore.kernel.org/abc"}
                ],
            )
            ctx = build_aryabhata_context(root, [{"title": "runtime pm leak in error path", "status": "open"}])
            self.assertIn("Knowledge base evidence", ctx)
            self.assertIn("Resolved by", ctx)

    def test_kb_survives_session_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            update_kb_after_lgtm(
                root,
                session_id="sess-1",
                watch_path="/tmp/linux-next/sound",
                resolved_findings=[{"title": "x", "required_action": "y"}],
            )
            session_dir = root / ".a2a" / "sessions"
            session_dir.mkdir(parents=True, exist_ok=True)
            for p in session_dir.glob("*"):
                p.unlink()
            self.assertTrue((root / ".a2a" / "knowledge_base.json").exists())
            self.assertGreaterEqual(len(list_kb_entries(root)), 1)

    def test_kb_corruption_handled_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            kb_path = root / ".a2a" / "knowledge_base.json"
            kb_path.parent.mkdir(parents=True, exist_ok=True)
            kb_path.write_text("{not-json", encoding="utf-8")
            kb = load_kb(root)
            self.assertEqual(kb.get("version"), 1)
            self.assertIsInstance(kb.get("entries"), list)

    def test_subsystem_filter_works(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            update_kb_after_lgtm(
                root,
                session_id="sess-1",
                watch_path="/tmp/linux-next/pinctrl",
                resolved_findings=[{"title": "a", "required_action": "x"}],
            )
            update_kb_after_lgtm(
                root,
                session_id="sess-2",
                watch_path="/tmp/linux-next/sound",
                resolved_findings=[{"title": "b", "required_action": "x"}],
            )
            self.assertEqual(len(list_kb_entries(root, subsystem="pinctrl")), 1)

    def test_empty_kb_session_works_normally(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            clear_kb(root)
            self.assertEqual(build_chanakya_context(root, "unknown"), "")
            self.assertEqual(build_aryabhata_context(root, [{"title": "x"}]), "")
            self.assertEqual(len(list_kb_entries(root)), 0)


if __name__ == "__main__":
    unittest.main()
