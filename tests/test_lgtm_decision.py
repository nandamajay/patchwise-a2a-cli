import json
import tempfile
import unittest
from pathlib import Path

from a2a_cli.main import reviewer_log_has_unresolved_risk, should_issue_lgtm


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class LgtmDecisionTests(unittest.TestCase):
    def test_unit_1_new_findings_block_lgtm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "findings.json"
            _write_json(path, {"open": 0, "new": 1, "closed": 2, "resolved": 2})
            ok, reason = should_issue_lgtm(str(path), "LGTM")
            self.assertFalse(ok)
            self.assertIn("new findings raised this round = 1", reason)

    def test_unit_2_open_findings_block_lgtm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "findings.json"
            _write_json(path, {"open": 1, "new": 0, "closed": 1, "resolved": 1})
            ok, reason = should_issue_lgtm(str(path), "LGTM")
            self.assertFalse(ok)
            self.assertIn("open findings = 1", reason)

    def test_unit_3_verdict_mismatch_blocks_lgtm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "findings.json"
            _write_json(path, {"open": 0, "new": 0, "closed": 2, "resolved": 2})
            ok, reason = should_issue_lgtm(str(path), "REJECT")
            self.assertFalse(ok)
            self.assertIn("Aryabhata verdict = REJECT", reason)

    def test_unit_4_all_conditions_met(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "findings.json"
            _write_json(path, {"open": 0, "new": 0, "closed": 2, "resolved": 2})
            ok, reason = should_issue_lgtm(str(path), "LGTM")
            self.assertTrue(ok)
            self.assertEqual(reason, "LGTM")

    def test_unit_5_exact_failing_session_reproduced(self) -> None:
        path = Path(
            ".a2a/reports/sess-20260504-025535-245021/round-02-findings.json"
        )
        self.assertTrue(path.exists(), f"missing reproduction artifact: {path}")
        ok, reason = should_issue_lgtm(str(path), "LGTM")
        self.assertFalse(ok)
        self.assertIn("new findings raised this round = 1", reason)

    def test_reviewer_guard_detects_uncertain_issue_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "reviewer.log"
            path.write_text(
                "stderr:\n"
                "thinking\n"
                "**Validating patch semantics**\n"
                "I see a potential issue and uncertainty around shared rail ownership.\n"
                "This might be a duplicate #define problem.\n"
                "exec\n",
                encoding="utf-8",
            )
            blocked, snippet = reviewer_log_has_unresolved_risk(path)
            self.assertTrue(blocked)
            self.assertTrue(snippet)
            self.assertIn("shared rail ownership", snippet)

    def test_reviewer_guard_ignores_clean_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "reviewer.log"
            path.write_text(
                "stderr:\n"
                "thinking\n"
                "Validated patch locations and evidence; no unresolved issues remain.\n"
                "exec\n",
                encoding="utf-8",
            )
            blocked, snippet = reviewer_log_has_unresolved_risk(path)
            self.assertFalse(blocked)
            self.assertEqual(snippet, "")


if __name__ == "__main__":
    unittest.main()
