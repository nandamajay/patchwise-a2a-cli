import unittest

from a2a_cli.main import (
    _compute_builder_confidence,
    _compute_builder_patch_gauge,
    _compute_reviewer_confidence,
)


class ConfidenceScoringTests(unittest.TestCase):
    def test_builder_patch_gauge_increases_with_change_size(self) -> None:
        small = _compute_builder_patch_gauge({"changed_files": 0, "diff_lines": 0, "diff_hunks": 0})
        medium = _compute_builder_patch_gauge({"changed_files": 1, "diff_lines": 40, "diff_hunks": 3})
        large = _compute_builder_patch_gauge({"changed_files": 3, "diff_lines": 300, "diff_hunks": 10})
        self.assertLess(small, medium)
        self.assertLess(medium, large)

    def test_builder_confidence_penalizes_no_progress(self) -> None:
        no_progress = _compute_builder_confidence(2, 2, {"changed_files": 0, "diff_lines": 0, "diff_hunks": 0})
        improved = _compute_builder_confidence(2, 0, {"changed_files": 1, "diff_lines": 80, "diff_hunks": 4})
        self.assertLess(no_progress, improved)

    def test_reviewer_confidence_high_quality(self) -> None:
        findings = [
            {
                "severity": "high",
                "title": "Issue A",
                "location": "a.patch:10",
                "evidence": ["a", "b"],
                "required_action": "fix",
                "status": "open",
                "source_comment_id": "prior-msg:1@example.com",
            },
            {
                "severity": "medium",
                "title": "Issue B",
                "location": "b.patch:20",
                "evidence": ["a", "b"],
                "required_action": "fix",
                "status": "closed",
                "source_comment_id": "prior-msg:2@example.com",
            },
        ]
        score = _compute_reviewer_confidence(findings)
        self.assertGreaterEqual(score, 75)
        self.assertLessEqual(score, 95)

    def test_reviewer_confidence_low_quality(self) -> None:
        findings = [
            {
                "severity": "high",
                "title": "Issue A",
                "location": "a.patch",
                "evidence": "",
                "required_action": "fix",
                "status": "open",
                "source_comment_id": "",
            },
            {
                "severity": "high",
                "title": "Issue B",
                "location": "",
                "evidence": [],
                "required_action": "fix",
                "status": "open",
                "source_comment_id": "",
            },
        ]
        score = _compute_reviewer_confidence(findings)
        self.assertLessEqual(score, 40)

    def test_reviewer_confidence_penalizes_volatile_ids(self) -> None:
        stable = [
            {
                "severity": "low",
                "title": "Issue A",
                "location": "a.patch:11",
                "evidence": ["proof"],
                "required_action": "fix",
                "status": "open",
                "source_comment_id": "issue-abc",
            }
        ]
        volatile = [dict(stable[0], source_comment_id="round2-new-1")]
        stable_score = _compute_reviewer_confidence(stable)
        volatile_score = _compute_reviewer_confidence(volatile)
        self.assertLess(volatile_score, stable_score)


if __name__ == "__main__":
    unittest.main()
