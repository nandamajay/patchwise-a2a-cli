import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from a2a_cli.main import (
    _advance_session,
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
        if not path.exists():
            self.skipTest(f"missing reproduction artifact: {path}")
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

    def test_consistency_guard_blocks_lgtm_when_findings_all_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a2a_dir = root / ".a2a"
            a2a_dir.mkdir(parents=True, exist_ok=True)
            _write_json(
                a2a_dir / "state.json",
                {"version": 1, "active_session_id": None, "last_updated": "2026-05-11T00:00:00+00:00"},
            )

            session_id = "sess-guard"
            findings_path = a2a_dir / "reports" / session_id / "round-01-findings.json"
            closed_finding = {
                "severity": "low",
                "title": "prior resolved",
                "location": "x.patch:1",
                "evidence": ["fixed"],
                "required_action": "none",
                "status": "closed",
                "source_comment_id": "prior-msg:abc",
            }
            _write_json(findings_path, {"open": 0, "new": 0, "findings": [closed_finding]})

            session = {
                "id": session_id,
                "task": "guard-test",
                "status": "in_progress",
                "current_round": 1,
                "max_rounds": 1,
                "reviewer_name": "aryabhatta",
                "rounds": [],
                "watch_path": str(root),
                "repo_path": str(root),
            }

            verdict_calls: list[str] = []
            status_updates: list[str] = []

            with ExitStack() as stack:
                stack.enter_context(mock.patch("a2a_cli.main._echo", return_value=None))
                stack.enter_context(
                    mock.patch("a2a_cli.main._load_config_or_defaults", return_value={"reviewer_consistency_guard": True})
                )
                stack.enter_context(mock.patch("a2a_cli.main._load_session", return_value=dict(session)))
                stack.enter_context(mock.patch("a2a_cli.main._refresh_prior_review_context", return_value=None))
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._validate_round_only",
                        return_value=(dict(session), 0, [closed_finding], [], findings_path),
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._builder_change_stats",
                        return_value={"changed_files": 0, "diff_lines": 0, "diff_hunks": 0},
                    )
                )
                stack.enter_context(mock.patch("a2a_cli.main._compute_builder_patch_gauge", return_value=100))
                stack.enter_context(mock.patch("a2a_cli.main._compute_builder_confidence", return_value=95))
                stack.enter_context(mock.patch("a2a_cli.main._compute_reviewer_confidence", return_value=95))
                stack.enter_context(mock.patch("a2a_cli.main.ScoreThresholds.from_config", return_value=mock.Mock()))
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main.evaluate_round_scores",
                        return_value={
                            "messages": [],
                            "abort_session": False,
                            "block_lgtm": False,
                            "force_extra_round": False,
                            "extra_scrutiny_next_round": False,
                        },
                    )
                )
                stack.enter_context(mock.patch("a2a_cli.main.append_score_decision", return_value=None))
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._build_round_runtime_summary",
                        return_value={
                            "findings": {"open_items": []},
                            "prior_comments": {"totals": {}},
                            "timing": {},
                        },
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._write_round_runtime_summary",
                        return_value={
                            "json": a2a_dir / "reports" / session_id / "round-01-summary.json",
                            "md": a2a_dir / "reports" / session_id / "round-01-summary.md",
                        },
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._write_round_suggested_replies",
                        return_value=a2a_dir / "reports" / session_id / "round-01-suggested-replies.md",
                    )
                )
                stack.enter_context(mock.patch("a2a_cli.main._append_summary_round", return_value=None))
                stack.enter_context(mock.patch("a2a_cli.main.render_round_table", return_value=""))
                stack.enter_context(mock.patch("a2a_cli.main.render_scores", return_value=""))
                stack.enter_context(mock.patch("a2a_cli.main.render_prior_comment_status", return_value=""))
                stack.enter_context(mock.patch("a2a_cli.main.extract_advertised_findings", return_value=[]))
                stack.enter_context(mock.patch("a2a_cli.main.render_advertised_findings_text", return_value=""))
                stack.enter_context(mock.patch("a2a_cli.main._round_files", return_value={"findings": findings_path}))
                stack.enter_context(mock.patch("a2a_cli.main._reviewer_verdict_for_round", return_value="LGTM"))
                stack.enter_context(mock.patch("a2a_cli.main.should_issue_lgtm", return_value=(True, "LGTM")))
                risk_guard = stack.enter_context(
                    mock.patch(
                        "a2a_cli.main.reviewer_log_has_unresolved_risk",
                        return_value=(True, "unresolved concern"),
                    )
                )
                stack.enter_context(mock.patch("a2a_cli.main.requires_full_subsystem_review", return_value=False))
                stack.enter_context(mock.patch("a2a_cli.main._prompt_extend_after_max_rounds", return_value=False))
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._set_summary_status",
                        side_effect=lambda _root, _sid, status: status_updates.append(status),
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._append_summary_verdict",
                        side_effect=lambda _root, _sid, verdict: verdict_calls.append(verdict),
                    )
                )
                stack.enter_context(mock.patch("a2a_cli.main._write_session", return_value=None))
                rc = _advance_session(root, session_id)

            self.assertEqual(rc, 1)
            self.assertTrue(risk_guard.called, "consistency guard should run even when findings are all closed")
            self.assertIn("stopped", [s.lower() for s in status_updates])
            self.assertTrue(any(v.startswith("STOPPED") for v in verdict_calls))
            self.assertFalse(any(v.upper() == "LGTM" for v in verdict_calls))

    def test_full_series_checkpatch_gate_blocks_lgtm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a2a_dir = root / ".a2a"
            a2a_dir.mkdir(parents=True, exist_ok=True)
            _write_json(
                a2a_dir / "state.json",
                {"version": 1, "active_session_id": None, "last_updated": "2026-05-11T00:00:00+00:00"},
            )

            session_id = "sess-checkpatch-gate"
            findings_path = a2a_dir / "reports" / session_id / "round-01-findings.json"
            closed_finding = {
                "severity": "low",
                "title": "resolved",
                "location": "x.patch:1",
                "evidence": ["fixed"],
                "required_action": "none",
                "status": "closed",
            }
            _write_json(findings_path, {"open": 0, "new": 0, "findings": [closed_finding]})

            session = {
                "id": session_id,
                "task": "checkpatch-gate-test",
                "status": "in_progress",
                "current_round": 1,
                "max_rounds": 1,
                "reviewer_name": "aryabhatta",
                "rounds": [],
                "watch_path": str(root),
                "repo_path": str(root),
            }

            verdict_calls: list[str] = []
            status_updates: list[str] = []

            with ExitStack() as stack:
                stack.enter_context(mock.patch("a2a_cli.main._echo", return_value=None))
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._load_config_or_defaults",
                        return_value={"reviewer_consistency_guard": True, "lgtm_full_series_checkpatch": True},
                    )
                )
                stack.enter_context(mock.patch("a2a_cli.main._load_session", return_value=dict(session)))
                stack.enter_context(mock.patch("a2a_cli.main._refresh_prior_review_context", return_value=None))
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._validate_round_only",
                        return_value=(dict(session), 0, [closed_finding], [], findings_path),
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._builder_change_stats",
                        return_value={"changed_files": 0, "diff_lines": 0, "diff_hunks": 0},
                    )
                )
                stack.enter_context(mock.patch("a2a_cli.main._compute_builder_patch_gauge", return_value=100))
                stack.enter_context(mock.patch("a2a_cli.main._compute_builder_confidence", return_value=95))
                stack.enter_context(mock.patch("a2a_cli.main._compute_reviewer_confidence", return_value=95))
                stack.enter_context(mock.patch("a2a_cli.main.ScoreThresholds.from_config", return_value=mock.Mock()))
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main.evaluate_round_scores",
                        return_value={
                            "messages": [],
                            "abort_session": False,
                            "block_lgtm": False,
                            "force_extra_round": False,
                            "extra_scrutiny_next_round": False,
                        },
                    )
                )
                stack.enter_context(mock.patch("a2a_cli.main.append_score_decision", return_value=None))
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._build_round_runtime_summary",
                        return_value={
                            "findings": {"open_items": []},
                            "prior_comments": {"totals": {}},
                            "timing": {},
                        },
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._write_round_runtime_summary",
                        return_value={
                            "json": a2a_dir / "reports" / session_id / "round-01-summary.json",
                            "md": a2a_dir / "reports" / session_id / "round-01-summary.md",
                        },
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._write_round_suggested_replies",
                        return_value=a2a_dir / "reports" / session_id / "round-01-suggested-replies.md",
                    )
                )
                stack.enter_context(mock.patch("a2a_cli.main._append_summary_round", return_value=None))
                stack.enter_context(mock.patch("a2a_cli.main.render_round_table", return_value=""))
                stack.enter_context(mock.patch("a2a_cli.main.render_scores", return_value=""))
                stack.enter_context(mock.patch("a2a_cli.main.render_prior_comment_status", return_value=""))
                stack.enter_context(mock.patch("a2a_cli.main.extract_advertised_findings", return_value=[]))
                stack.enter_context(mock.patch("a2a_cli.main.render_advertised_findings_text", return_value=""))
                stack.enter_context(mock.patch("a2a_cli.main._round_files", return_value={"findings": findings_path}))
                stack.enter_context(mock.patch("a2a_cli.main._reviewer_verdict_for_round", return_value="LGTM"))
                stack.enter_context(mock.patch("a2a_cli.main.should_issue_lgtm", return_value=(True, "LGTM")))
                stack.enter_context(mock.patch("a2a_cli.main.reviewer_log_has_unresolved_risk", return_value=(False, "")))
                stack.enter_context(mock.patch("a2a_cli.main.requires_full_subsystem_review", return_value=False))
                lgtm_gate = stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._run_lgtm_full_series_checkpatch",
                        return_value={
                            "ran": True,
                            "ok": False,
                            "issues": ["checkpatch failed for 0001-a.patch (rc=1)"],
                            "report": str(a2a_dir / "reports" / session_id / "round-01-lgtm-checkpatch.json"),
                        },
                    )
                )
                stack.enter_context(mock.patch("a2a_cli.main._prompt_extend_after_max_rounds", return_value=False))
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._set_summary_status",
                        side_effect=lambda _root, _sid, status: status_updates.append(status),
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._append_summary_verdict",
                        side_effect=lambda _root, _sid, verdict: verdict_calls.append(verdict),
                    )
                )
                stack.enter_context(mock.patch("a2a_cli.main._write_session", return_value=None))
                rc = _advance_session(root, session_id)

            self.assertEqual(rc, 1)
            self.assertTrue(lgtm_gate.called, "full-series checkpatch gate should run before LGTM")
            self.assertIn("stopped", [s.lower() for s in status_updates])
            self.assertTrue(any(v.startswith("STOPPED") for v in verdict_calls))
            self.assertFalse(any(v.upper() == "LGTM" for v in verdict_calls))

    def test_quality_gate_requires_explicit_reviewer_lgtm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a2a_dir = root / ".a2a"
            a2a_dir.mkdir(parents=True, exist_ok=True)
            _write_json(
                a2a_dir / "state.json",
                {"version": 1, "active_session_id": None, "last_updated": "2026-05-11T00:00:00+00:00"},
            )

            session_id = "sess-quality-verdict"
            findings_path = a2a_dir / "reports" / session_id / "round-01-findings.json"
            closed_finding = {
                "severity": "low",
                "title": "resolved",
                "location": "x.patch:1",
                "evidence": ["fixed"],
                "required_action": "none",
                "status": "closed",
                "source_comment_id": "issue-1",
            }
            _write_json(findings_path, {"open": 0, "new": 0, "findings": [closed_finding]})

            session = {
                "id": session_id,
                "task": "quality-verdict-test",
                "status": "in_progress",
                "current_round": 1,
                "max_rounds": 1,
                "reviewer_name": "aryabhatta",
                "rounds": [],
                "watch_path": str(root),
                "repo_path": str(root),
            }

            verdict_calls: list[str] = []
            status_updates: list[str] = []

            with ExitStack() as stack:
                stack.enter_context(mock.patch("a2a_cli.main._echo", return_value=None))
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._load_config_or_defaults",
                        return_value={"reviewer_consistency_guard": False, "lgtm_full_series_checkpatch": False},
                    )
                )
                stack.enter_context(mock.patch("a2a_cli.main._load_session", return_value=dict(session)))
                stack.enter_context(mock.patch("a2a_cli.main._refresh_prior_review_context", return_value=None))
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._validate_round_only",
                        return_value=(dict(session), 0, [closed_finding], [], findings_path),
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._builder_change_stats",
                        return_value={"changed_files": 0, "diff_lines": 0, "diff_hunks": 0},
                    )
                )
                stack.enter_context(mock.patch("a2a_cli.main._compute_builder_patch_gauge", return_value=100))
                stack.enter_context(mock.patch("a2a_cli.main._compute_builder_confidence", return_value=95))
                stack.enter_context(mock.patch("a2a_cli.main._compute_reviewer_confidence", return_value=95))
                stack.enter_context(mock.patch("a2a_cli.main.ScoreThresholds.from_config", return_value=mock.Mock()))
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main.evaluate_round_scores",
                        return_value={
                            "messages": [],
                            "abort_session": False,
                            "block_lgtm": False,
                            "force_extra_round": False,
                            "extra_scrutiny_next_round": False,
                        },
                    )
                )
                stack.enter_context(mock.patch("a2a_cli.main.append_score_decision", return_value=None))
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._build_round_runtime_summary",
                        return_value={
                            "findings": {"open_items": []},
                            "prior_comments": {"totals": {}},
                            "timing": {},
                        },
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._write_round_runtime_summary",
                        return_value={
                            "json": a2a_dir / "reports" / session_id / "round-01-summary.json",
                            "md": a2a_dir / "reports" / session_id / "round-01-summary.md",
                        },
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._write_round_suggested_replies",
                        return_value=a2a_dir / "reports" / session_id / "round-01-suggested-replies.md",
                    )
                )
                stack.enter_context(mock.patch("a2a_cli.main._append_summary_round", return_value=None))
                stack.enter_context(mock.patch("a2a_cli.main.render_round_table", return_value=""))
                stack.enter_context(mock.patch("a2a_cli.main.render_scores", return_value=""))
                stack.enter_context(mock.patch("a2a_cli.main.render_prior_comment_status", return_value=""))
                stack.enter_context(mock.patch("a2a_cli.main.extract_advertised_findings", return_value=[]))
                stack.enter_context(mock.patch("a2a_cli.main.render_advertised_findings_text", return_value=""))
                stack.enter_context(mock.patch("a2a_cli.main._round_files", return_value={"findings": findings_path}))
                stack.enter_context(mock.patch("a2a_cli.main._reviewer_verdict_for_round", return_value=""))
                stack.enter_context(mock.patch("a2a_cli.main.should_issue_lgtm", return_value=(True, "LGTM")))
                stack.enter_context(mock.patch("a2a_cli.main.reviewer_log_has_unresolved_risk", return_value=(False, "")))
                stack.enter_context(mock.patch("a2a_cli.main.requires_full_subsystem_review", return_value=False))
                stack.enter_context(mock.patch("a2a_cli.main._prompt_extend_after_max_rounds_count", return_value=0))
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._set_summary_status",
                        side_effect=lambda _root, _sid, status: status_updates.append(status),
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "a2a_cli.main._append_summary_verdict",
                        side_effect=lambda _root, _sid, verdict: verdict_calls.append(verdict),
                    )
                )
                stack.enter_context(mock.patch("a2a_cli.main._write_session", return_value=None))
                rc = _advance_session(root, session_id)

            self.assertEqual(rc, 1)
            self.assertIn("stopped", [s.lower() for s in status_updates])
            self.assertTrue(any(v.startswith("STOPPED") for v in verdict_calls))
            self.assertFalse(any(v.upper() == "LGTM" for v in verdict_calls))

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
