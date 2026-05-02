import json
import tempfile
import unittest
from pathlib import Path

from a2a_cli.main import _session_report_payload


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class ReportPayloadTests(unittest.TestCase):
    def test_prior_comment_summary_tracks_fixed_by_a2a(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a2a = root / ".a2a"
            sessions = a2a / "sessions"
            reports = a2a / "reports" / "sess-1"
            sessions.mkdir(parents=True, exist_ok=True)
            reports.mkdir(parents=True, exist_ok=True)

            prior_comments = {
                "comments": [
                    {"id": "prior-msg:c1", "from": "a@example.com", "subject": "comment 1"},
                    {"id": "prior-msg:c2", "from": "b@example.com", "subject": "comment 2"},
                ]
            }
            _write_json(reports / "prior_comments.json", prior_comments)

            r1_findings = {
                "findings": [
                    {
                        "severity": "high",
                        "title": "c1 fixed",
                        "location": "x.patch:10",
                        "evidence": ["ok"],
                        "required_action": "none",
                        "status": "closed",
                        "source_comment_id": "prior-msg:c1",
                    },
                    {
                        "severity": "high",
                        "title": "c2 open",
                        "location": "x.patch:11",
                        "evidence": ["open"],
                        "required_action": "fix",
                        "status": "open",
                        "source_comment_id": "prior-msg:c2",
                    },
                ]
            }
            r2_findings = {
                "findings": [
                    {
                        "severity": "high",
                        "title": "c1 fixed",
                        "location": "x.patch:20",
                        "evidence": ["ok"],
                        "required_action": "none",
                        "status": "closed",
                        "source_comment_id": "prior-msg:c1",
                    },
                    {
                        "severity": "high",
                        "title": "c2 fixed",
                        "location": "x.patch:21",
                        "evidence": ["ok"],
                        "required_action": "none",
                        "status": "closed",
                        "source_comment_id": "prior-msg:c2",
                    },
                ]
            }
            _write_json(reports / "round-01-findings.json", r1_findings)
            _write_json(reports / "round-02-findings.json", r2_findings)

            _write_json(
                reports / "round-01-gate.json",
                {"ran": True, "passed": False, "failures": 2},
            )
            _write_json(
                reports / "round-02-gate.json",
                {"ran": True, "passed": True, "failures": 0},
            )

            session = {
                "id": "sess-1",
                "task": "report-payload-test",
                "status": "lgtm",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:10:00+00:00",
                "max_rounds": 2,
                "current_round": 2,
                "open_findings": 0,
                "reviewer_name": "aryabhatta",
                "repo_path": "/tmp/repo",
                "branch": "a2a/test",
                "builder_command": "builder",
                "reviewer_command": "reviewer",
                "prior_review": {
                    "enabled": True,
                    "comments_file": str(reports / "prior_comments.json"),
                    "comments_total": 2,
                    "source_total": 1,
                    "search_used": False,
                },
                "rounds": [
                    {
                        "round": 1,
                        "validated_at": "2026-01-01T00:01:00+00:00",
                        "findings_total": 2,
                        "findings_open": 1,
                        "findings_file": str(reports / "round-01-findings.json"),
                    },
                    {
                        "round": 2,
                        "validated_at": "2026-01-01T00:02:00+00:00",
                        "findings_total": 2,
                        "findings_open": 0,
                        "findings_file": str(reports / "round-02-findings.json"),
                    },
                ],
            }
            _write_json(sessions / "sess-1.json", session)

            payload = _session_report_payload(root, "sess-1")
            self.assertEqual(payload["totals"]["gate_failures_total"], 2)
            self.assertEqual(payload["totals"]["gate_failed_rounds"], 1)

            summary = {row["source_comment_id"]: row for row in payload["prior_comment_summary"]}
            self.assertEqual(summary["prior-msg:c1"]["fixed_by_a2a"], False)
            self.assertEqual(summary["prior-msg:c2"]["fixed_by_a2a"], True)
            self.assertEqual(summary["prior-msg:c2"]["closed_round"], 2)


if __name__ == "__main__":
    unittest.main()
