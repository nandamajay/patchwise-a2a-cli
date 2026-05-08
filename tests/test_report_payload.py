import json
import tempfile
import unittest
from pathlib import Path

from a2a_cli.main import _render_html_report, _render_markdown_report, _session_report_payload, _write_session_html_report


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class ReportPayloadTests(unittest.TestCase):
    def test_write_session_html_report_generates_html_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a2a = root / ".a2a"
            sessions = a2a / "sessions"
            reports = a2a / "reports" / "sess-html"
            patches_root = a2a / "patches" / "sess-html"
            sessions.mkdir(parents=True, exist_ok=True)
            reports.mkdir(parents=True, exist_ok=True)
            (patches_root / "v3").mkdir(parents=True, exist_ok=True)

            _write_json(
                reports / "lore_next_version.json",
                {
                    "kind": "lore_copy",
                    "next_version": 3,
                    "output_path": str((patches_root / "v3").resolve()),
                    "source_watch_path": "/tmp/input-series",
                    "generated_at": "2026-01-01T00:02:00+00:00",
                },
            )

            _write_json(
                reports / "round-01-findings.json",
                {
                    "findings": [
                        {
                            "severity": "high",
                            "title": "<unsafe-title>",
                            "location": "x.patch:10",
                            "evidence": ["test"],
                            "required_action": "fix",
                            "status": "open",
                            "source_comment_id": "source-1",
                        }
                    ]
                },
            )
            _write_json(
                reports / "round-01-gate.json",
                {"ran": True, "passed": True, "failures": 0},
            )

            session = {
                "id": "sess-html",
                "task": "html-report-test",
                "status": "in_progress",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:01:00+00:00",
                "max_rounds": 3,
                "current_round": 2,
                "open_findings": 1,
                "builder_display_name": "chanakya",
                "reviewer_display_name": "aryabhatta",
                "reviewer_name": "aryabhatta",
                "repo_path": "/tmp/repo",
                "branch": "a2a/test",
                "builder_command": "builder",
                "reviewer_command": "reviewer",
                "watch_path": "/tmp/input-series",
                "lore": {
                    "message_id": "20260101000000.1-ajay.nandam@oss.qualcomm.com",
                },
                "rounds": [
                    {
                        "round": 1,
                        "validated_at": "2026-01-01T00:01:00+00:00",
                        "findings_total": 1,
                        "findings_open": 1,
                        "findings_file": str(reports / "round-01-findings.json"),
                    }
                ],
            }
            _write_json(sessions / "sess-html.json", session)

            out = _write_session_html_report(root, "sess-html")
            self.assertEqual(out, reports / "session-report.html")
            self.assertTrue(out.exists())

            html = out.read_text(encoding="utf-8")
            self.assertIn("<html", html)
            self.assertIn("PatchWise A2A — Agent Performance Report", html)
            self.assertIn("Round 1", html)
            self.assertIn("&lt;unsafe-title&gt;", html)
            self.assertIn("Session I/O Details", html)
            self.assertIn(str((patches_root / "v3").resolve()), html)
            self.assertIn("https://lore.kernel.org/r/20260101000000.1-ajay.nandam@oss.qualcomm.com", html)

    def test_render_html_report_allows_in_memory_render(self) -> None:
        payload = {
            "session": {
                "id": "sess-inline",
                "task": "inline-html",
                "status": "lgtm",
                "builder_display_name": "chanakya",
                "reviewer_display_name": "aryabhatta",
                "reviewer_name": "aryabhatta",
                "branch": "a2a/test",
                "repo_path": "/tmp/repo",
                "updated_at": "2026-01-01T00:01:00+00:00",
            },
            "totals": {
                "rounds_validated": 0,
                "findings_total": 0,
                "findings_open_last": 0,
                "gate_failures_total": 0,
                "gate_failed_rounds": 0,
            },
            "rounds": [],
            "prior_comment_summary": [],
        }
        html = _render_html_report(payload)
        self.assertIn("sess-inline", html)
        self.assertIn("No validated rounds yet.", html)

    def test_markdown_report_includes_absolute_io_details(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a2a = root / ".a2a"
            sessions = a2a / "sessions"
            reports = a2a / "reports" / "sess-md"
            patches_root = a2a / "patches" / "sess-md"
            sessions.mkdir(parents=True, exist_ok=True)
            reports.mkdir(parents=True, exist_ok=True)
            (patches_root / "v2").mkdir(parents=True, exist_ok=True)

            _write_json(
                reports / "lore_next_version.json",
                {
                    "kind": "lore_copy",
                    "next_version": 2,
                    "output_path": str((patches_root / "v2").resolve()),
                    "source_watch_path": "/tmp/md-watch",
                },
            )

            session = {
                "id": "sess-md",
                "task": "markdown-details-test",
                "status": "lgtm",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:01:00+00:00",
                "max_rounds": 1,
                "current_round": 1,
                "open_findings": 0,
                "builder_display_name": "chanakya",
                "reviewer_display_name": "aryabhatta",
                "reviewer_name": "aryabhatta",
                "repo_path": "/tmp/repo",
                "branch": "a2a/test",
                "builder_command": "builder",
                "reviewer_command": "reviewer",
                "watch_path": "/tmp/md-watch",
                "lore": {"message_id": "20260101001010.2-ajay.nandam@oss.qualcomm.com"},
                "rounds": [],
            }
            _write_json(sessions / "sess-md.json", session)

            payload = _session_report_payload(root, "sess-md")
            md = _render_markdown_report(payload)
            self.assertIn("## Session I/O Details", md)
            self.assertIn(f"- input_watch_path: {str(Path('/tmp/md-watch').resolve())}", md)
            self.assertIn("- input_lore_link: https://lore.kernel.org/r/20260101001010.2-ajay.nandam@oss.qualcomm.com", md)
            self.assertIn(f"- latest_output_patches_path: {str((patches_root / 'v2').resolve())}", md)

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
                "builder_display_name": "chanakya",
                "reviewer_display_name": "aryabhatta",
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
            self.assertEqual(payload["session"]["builder_display_name"], "chanakya")
            self.assertEqual(payload["session"]["reviewer_display_name"], "aryabhatta")

            summary = {row["source_comment_id"]: row for row in payload["prior_comment_summary"]}
            self.assertEqual(summary["prior-msg:c1"]["fixed_by_a2a"], False)
            self.assertEqual(summary["prior-msg:c2"]["fixed_by_a2a"], True)
            self.assertEqual(summary["prior-msg:c2"]["closed_round"], 2)

    def test_prior_comment_summary_marks_upstream_apply_notice(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a2a = root / ".a2a"
            sessions = a2a / "sessions"
            reports = a2a / "reports" / "sess-upstream"
            sessions.mkdir(parents=True, exist_ok=True)
            reports.mkdir(parents=True, exist_ok=True)

            prior_comments = {
                "comments": [
                    {
                        "id": "prior-msg:apply@example.com",
                        "from": "broonie@kernel.org",
                        "subject": "Re: [PATCH] test",
                        "excerpt": (
                            "Applied to https://git.kernel.org/pub/scm/linux/kernel/git/broonie/sound.git "
                            "for-7.1 Thanks! https://git.kernel.org/broonie/sound/c/74c876bfd71b"
                        ),
                        "source": "https://lore.kernel.org/r/example/t.mbox.gz",
                    }
                ]
            }
            _write_json(reports / "prior_comments.json", prior_comments)
            _write_json(reports / "round-01-findings.json", {"findings": []})
            _write_json(reports / "round-01-gate.json", {"ran": True, "passed": True, "failures": 0})

            session = {
                "id": "sess-upstream",
                "task": "report-payload-upstream",
                "status": "lgtm",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:01:00+00:00",
                "max_rounds": 1,
                "current_round": 1,
                "open_findings": 0,
                "builder_display_name": "chanakya",
                "reviewer_display_name": "aryabhatta",
                "reviewer_name": "aryabhatta",
                "repo_path": "/tmp/repo",
                "branch": "a2a/test",
                "builder_command": "builder",
                "reviewer_command": "reviewer",
                "prior_review": {
                    "enabled": True,
                    "comments_file": str(reports / "prior_comments.json"),
                    "comments_total": 1,
                    "source_total": 1,
                    "search_used": False,
                },
                "rounds": [
                    {
                        "round": 1,
                        "validated_at": "2026-01-01T00:01:00+00:00",
                        "findings_total": 0,
                        "findings_open": 0,
                        "findings_file": str(reports / "round-01-findings.json"),
                    }
                ],
            }
            _write_json(sessions / "sess-upstream.json", session)

            payload = _session_report_payload(root, "sess-upstream")
            self.assertEqual(
                payload["session"]["prior_review"]["comment_status_totals"]["comments_external_resolved"],
                1,
            )
            summary = {row["source_comment_id"]: row for row in payload["prior_comment_summary"]}
            self.assertEqual(summary["prior-msg:apply@example.com"]["current_status"], "external_resolved")
            self.assertEqual(summary["prior-msg:apply@example.com"]["resolution_origin"], "upstream")


if __name__ == "__main__":
    unittest.main()
