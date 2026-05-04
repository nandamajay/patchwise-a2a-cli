import json
import tempfile
import unittest
from pathlib import Path

from a2a_cli.main import (
    has_independent_subsystem_findings,
    requires_full_subsystem_review,
    reviewer_log_has_unresolved_risk,
    should_issue_lgtm,
)


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

    def test_dual_track_helper_prior_only_is_not_independent(self) -> None:
        findings = [
            {"source_comment_id": "prior-msg:abc"},
            {"source_comment_id": "prior-meta:xyz"},
            {"source_comment_id": "meta:guard"},
        ]
        self.assertFalse(has_independent_subsystem_findings(findings))

    def test_dual_track_helper_subsys_scan_is_independent(self) -> None:
        findings = [
            {"source_comment_id": "prior-msg:abc"},
            {"source_comment_id": "subsys-scan:pm-runtime"},
        ]
        self.assertTrue(has_independent_subsystem_findings(findings))

    def test_dual_track_helper_non_prior_id_is_independent(self) -> None:
        findings = [
            {"source_comment_id": "prior-msg:abc"},
            {"source_comment_id": "issue-123"},
        ]
        self.assertTrue(has_independent_subsystem_findings(findings))

    def test_requires_full_subsystem_review_enabled_with_prior_comments(self) -> None:
        session = {"prior_review": {"comments_total": 2}}
        cfg = {"full_subsystem_review_required": True}
        self.assertTrue(requires_full_subsystem_review(session, cfg))

    def test_requires_full_subsystem_review_disabled_or_no_prior(self) -> None:
        self.assertFalse(
            requires_full_subsystem_review(
                {"prior_review": {"comments_total": 2}},
                {"full_subsystem_review_required": False},
            )
        )
        self.assertFalse(
            requires_full_subsystem_review(
                {"prior_review": {"comments_total": 0}},
                {"full_subsystem_review_required": True},
            )
        )


if __name__ == "__main__":
    unittest.main()
