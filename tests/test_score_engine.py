import json
import tempfile
import unittest
from pathlib import Path

from a2a_cli.score_engine import (
    ScoreThresholds,
    append_score_decision,
    evaluate_round_scores,
)


class ScoreEngineTests(unittest.TestCase):
    def test_low_builder_confidence_forces_extra_round_when_findings_open(self) -> None:
        thresholds = ScoreThresholds()
        decision = evaluate_round_scores(
            round_no=1,
            open_findings=1,
            builder_confidence=30,
            reviewer_confidence=95,
            patch_gauge=50,
            previous_builder_confidence=None,
            previous_reviewer_confidence=None,
            thresholds=thresholds,
        )
        self.assertTrue(decision["force_extra_round"])

    def test_low_builder_confidence_is_warning_only_when_findings_closed(self) -> None:
        thresholds = ScoreThresholds()
        decision = evaluate_round_scores(
            round_no=1,
            open_findings=0,
            builder_confidence=30,
            reviewer_confidence=95,
            patch_gauge=50,
            previous_builder_confidence=None,
            previous_reviewer_confidence=None,
            thresholds=thresholds,
        )
        self.assertFalse(decision["force_extra_round"])
        self.assertIn("warning only", " ".join(decision["messages"]))

    def test_low_reviewer_forces_re_examine(self) -> None:
        thresholds = ScoreThresholds()
        decision = evaluate_round_scores(
            round_no=1,
            open_findings=0,
            builder_confidence=90,
            reviewer_confidence=55,
            patch_gauge=20,
            previous_builder_confidence=None,
            previous_reviewer_confidence=None,
            thresholds=thresholds,
        )
        self.assertTrue(decision["low_quality_reviewer"])
        self.assertTrue(decision["block_lgtm"])
        self.assertTrue(decision["extra_scrutiny_next_round"])

    def test_zero_patch_gauge_aborts_after_round_1(self) -> None:
        thresholds = ScoreThresholds()
        decision = evaluate_round_scores(
            round_no=2,
            open_findings=1,
            builder_confidence=70,
            reviewer_confidence=75,
            patch_gauge=0,
            previous_builder_confidence=80,
            previous_reviewer_confidence=80,
            thresholds=thresholds,
        )
        self.assertTrue(decision["abort_session"])

    def test_high_confidence_early_lgtm(self) -> None:
        thresholds = ScoreThresholds()
        decision = evaluate_round_scores(
            round_no=2,
            open_findings=0,
            builder_confidence=95,
            reviewer_confidence=96,
            patch_gauge=44,
            previous_builder_confidence=90,
            previous_reviewer_confidence=90,
            thresholds=thresholds,
        )
        self.assertTrue(decision["allow_early_lgtm"])

    def test_volatility_detected_over_30_swing(self) -> None:
        thresholds = ScoreThresholds(volatility_swing=30)
        decision = evaluate_round_scores(
            round_no=3,
            open_findings=1,
            builder_confidence=20,
            reviewer_confidence=90,
            patch_gauge=40,
            previous_builder_confidence=70,
            previous_reviewer_confidence=50,
            thresholds=thresholds,
        )
        self.assertTrue(decision["volatility_warning"])
        self.assertTrue(decision["force_extra_round"])

    def test_volatility_is_warning_only_when_findings_closed(self) -> None:
        thresholds = ScoreThresholds(volatility_swing=30)
        decision = evaluate_round_scores(
            round_no=3,
            open_findings=0,
            builder_confidence=20,
            reviewer_confidence=90,
            patch_gauge=40,
            previous_builder_confidence=70,
            previous_reviewer_confidence=50,
            thresholds=thresholds,
        )
        self.assertTrue(decision["volatility_warning"])
        self.assertFalse(decision["force_extra_round"])
        self.assertIn("warning only", " ".join(decision["messages"]))

    def test_open_findings_always_block_lgtm(self) -> None:
        thresholds = ScoreThresholds()
        decision = evaluate_round_scores(
            round_no=1,
            open_findings=2,
            builder_confidence=98,
            reviewer_confidence=97,
            patch_gauge=80,
            previous_builder_confidence=None,
            previous_reviewer_confidence=None,
            thresholds=thresholds,
        )
        self.assertTrue(decision["block_lgtm"])
        self.assertFalse(decision["allow_early_lgtm"])
        self.assertIn("open findings remain — LGTM blocked by findings gate", " ".join(decision["messages"]))

    def test_score_decisions_written_to_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "score_decisions.json"
            append_score_decision(path, {"round": 1, "force_extra_round": True})
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload), 1)
            self.assertTrue(payload[0]["force_extra_round"])

    def test_all_thresholds_configurable(self) -> None:
        thresholds = ScoreThresholds.from_config(
            {
                "score_thresholds": {
                    "low_builder_confidence": 10,
                    "low_reviewer_confidence": 20,
                    "high_confidence_lgtm": 80,
                    "volatility_swing": 5,
                    "zero_patch_gauge": 2,
                }
            }
        )
        self.assertEqual(thresholds.low_builder_confidence, 10)
        self.assertEqual(thresholds.low_reviewer_confidence, 20)
        self.assertEqual(thresholds.high_confidence_lgtm, 80)
        self.assertEqual(thresholds.volatility_swing, 5)
        self.assertEqual(thresholds.zero_patch_gauge, 2)

    def test_missing_and_overflow_scores_are_normalized(self) -> None:
        thresholds = ScoreThresholds()
        decision = evaluate_round_scores(
            round_no=3,
            open_findings=0,
            builder_confidence=None,
            reviewer_confidence=120,
            patch_gauge=None,
            previous_builder_confidence=None,
            previous_reviewer_confidence=None,
            thresholds=thresholds,
        )
        self.assertEqual(decision["builder_confidence"], 0)
        self.assertEqual(decision["reviewer_confidence"], 100)
        self.assertEqual(decision["patch_gauge"], 0)
        text = " ".join(decision["messages"])
        self.assertIn("builder_confidence missing", text)
        self.assertIn("reviewer_confidence > 100", text)


if __name__ == "__main__":
    unittest.main()
