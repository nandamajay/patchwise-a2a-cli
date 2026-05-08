import json
import tempfile
import unittest
from pathlib import Path

from a2a_cli.main import (
    _build_round_runtime_summary,
    _render_round_runtime_summary_markdown,
    _round_files,
)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class RoundSummaryTests(unittest.TestCase):
    def test_round_runtime_summary_has_deltas_and_prior_totals(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            session_id = "sess-x"
            reviewer = "aryabhatta"
            report_dir = root / ".a2a" / "reports" / session_id
            log_dir = root / ".a2a" / "logs" / session_id
            report_dir.mkdir(parents=True, exist_ok=True)
            log_dir.mkdir(parents=True, exist_ok=True)

            prior_comments = {
                "comments": [
                    {"id": "prior-msg:a", "from": "a@x", "subject": "A"},
                    {"id": "prior-msg:b", "from": "b@x", "subject": "B"},
                ]
            }
            _write_json(report_dir / "prior_comments.json", prior_comments)

            # Previous round: one prior closed, one prior open.
            prev_findings = {
                "findings": [
                    {
                        "severity": "high",
                        "title": "prior A",
                        "location": "a.patch:1",
                        "evidence": ["ok"],
                        "required_action": "none",
                        "status": "closed",
                        "source_comment_id": "prior-msg:a",
                    },
                    {
                        "severity": "high",
                        "title": "prior B",
                        "location": "b.patch:1",
                        "evidence": ["open"],
                        "required_action": "fix",
                        "status": "open",
                        "source_comment_id": "prior-msg:b",
                    },
                ]
            }
            _write_json(report_dir / "round-01-findings.json", prev_findings)

            _write_json(report_dir / "round-02-findings.json", {"findings": []})
            _write_json(report_dir / "round-02-gate.json", {"passed": True, "failures": 0})

            session = {
                "id": session_id,
                "task": "t",
                "builder_display_name": "chanakya",
                "reviewer_display_name": "aryabhatta",
                "reviewer_name": reviewer,
                "watch_path": "/tmp/watch",
                "prior_review": {
                    "comments_file": str(report_dir / "prior_comments.json"),
                },
                "rounds": [
                    {
                        "round": 1,
                        "findings_file": str(report_dir / "round-01-findings.json"),
                        "findings_open": 1,
                        "findings_total": 2,
                    }
                ],
            }

            current_findings = [
                {
                    "severity": "medium",
                    "title": "new issue",
                    "location": "c.patch:7",
                    "evidence": ["x"],
                    "required_action": "fix",
                    "status": "open",
                    "source_comment_id": "issue-123",
                },
                {
                    "severity": "high",
                    "title": "prior B fixed",
                    "location": "b.patch:9",
                    "evidence": ["fixed"],
                    "required_action": "none",
                    "status": "closed",
                    "source_comment_id": "prior-msg:b",
                },
            ]

            files = _round_files(root, session_id, 2, reviewer)
            _write_json(files["findings"], {"findings": current_findings})

            summary = _build_round_runtime_summary(
                root=root,
                session=session,
                round_no=2,
                findings=current_findings,
                open_count=1,
                change_stats={"changed_files": 1, "diff_lines": 10, "diff_hunks": 2},
                builder_patch_gauge=42,
                builder_confidence=60,
                reviewer_confidence=70,
                round_started_at="2026-05-04T06:00:00+00:00",
                round_elapsed_seconds=95,
            )

            self.assertEqual(summary["findings"]["total"], 2)
            self.assertEqual(summary["findings"]["open"], 1)
            self.assertEqual(summary["findings"]["new_since_prev"], 1)
            self.assertEqual(summary["findings"]["resolved_since_prev"], 1)
            self.assertEqual(summary["prior_comments"]["totals"]["received_total"], 2)
            self.assertEqual(summary["prior_comments"]["totals"]["fixed_by_a2a"], 1)
            self.assertEqual(summary["timing"]["started_at"], "2026-05-04T06:00:00+00:00")
            self.assertEqual(summary["timing"]["elapsed_seconds"], 95)
            self.assertEqual(summary["timing"]["elapsed_hms"], "00:01:35")

            md = _render_round_runtime_summary_markdown(summary)
            self.assertIn("Round 2 Summary", md)
            self.assertIn("- builder: chanakya", md)
            self.assertIn("- reviewer: aryabhatta", md)
            self.assertIn("## Round Timing", md)
            self.assertIn("- elapsed: 00:01:35", md)
            self.assertIn("Top Open Findings", md)


if __name__ == "__main__":
    unittest.main()
