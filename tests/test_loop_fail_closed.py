import argparse
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from a2a_cli.config import default_config, default_state, dump_json
from a2a_cli.main import cmd_loop


@contextmanager
def _noop_worktree_lock(_root: Path, _session: dict, _session_id: str):
    yield


def _loop_args(session_id: str) -> argparse.Namespace:
    return argparse.Namespace(
        session=session_id,
        task=None,
        max_rounds=None,
        extend_rounds=0,
        timeout_min=None,
        builder_cmd=None,
        reviewer_cmd=None,
        watch_path=None,
        lore_url=None,
        lore_msgid=None,
        source_msgid=None,
        lore_out_dir=None,
        fetch_out_dir=None,
        github_pr=None,
        gerrit_change=None,
        gerrit_base_url=None,
        max_iterations=1,
        auto_respin=False,
        focus_issue=None,
        _single_series=True,
    )


class LoopFailClosedTests(unittest.TestCase):
    def _write_workspace(self, root: Path, sid: str) -> None:
        a2a = root / ".a2a"
        (a2a / "sessions").mkdir(parents=True, exist_ok=True)
        (a2a / "reports" / sid).mkdir(parents=True, exist_ok=True)
        (a2a / "logs" / sid).mkdir(parents=True, exist_ok=True)
        dump_json(a2a / "config.json", default_config())
        dump_json(a2a / "state.json", default_state() | {"active_session_id": sid})
        dump_json(
            a2a / "prepare.json",
            {
                "repo_path": str(root),
                "branch": "master",
                "reviewer_name": "aryabhatta",
                "worktrees": {
                    "builder": str(root),
                    "aryabhatta": str(root),
                },
            },
        )
        dump_json(
            a2a / "sessions" / f"{sid}.json",
            {
                "version": 1,
                "id": sid,
                "task": "unit-test-loop-fail-closed",
                "status": "in_progress",
                "created_at": "2026-06-15T00:00:00+00:00",
                "updated_at": "2026-06-15T00:00:00+00:00",
                "max_rounds": 6,
                "timeout_min": None,
                "current_round": 1,
                "open_findings": None,
                "builder_display_name": "chanakya",
                "reviewer_display_name": "aryabhatta",
                "reviewer_name": "aryabhatta",
                "repo_path": str(root),
                "branch": "master",
                "worktrees": {
                    "builder": str(root),
                    "aryabhatta": str(root),
                },
                "rounds": [],
                "builder_command": "echo builder",
                "reviewer_command": "echo reviewer",
                "watch_path": str(root),
            },
        )

    def test_loop_marks_session_stopped_when_static_analysis_gate_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sid = "sess-loop-static-analysis-stop"
            self._write_workspace(root, sid)
            args = _loop_args(sid)

            with mock.patch("a2a_cli.main._must_find_root", return_value=root):
                with mock.patch("a2a_cli.main._worktree_lock", _noop_worktree_lock):
                    with mock.patch("a2a_cli.main._refresh_prior_review_context", return_value=(None, False)):
                        with mock.patch("a2a_cli.main._run_agent_step", return_value=0):
                            with mock.patch("a2a_cli.main._run_validation_gate", return_value=(True, True)):
                                with mock.patch(
                                    "a2a_cli.main._run_static_analysis",
                                    return_value={
                                        "gate_passed": False,
                                        "skipped": False,
                                        "missing_required_tools": ["sparse", "coccinelle"],
                                    },
                                ):
                                    with mock.patch("a2a_cli.main._auto_write_session_html_report"):
                                        rc = cmd_loop(args)

            self.assertEqual(rc, 1)
            session = (root / ".a2a" / "sessions" / f"{sid}.json").read_text(encoding="utf-8")
            self.assertIn('"status": "stopped"', session)
            self.assertIn("static analysis gate failed", session)
            self.assertIn("missing required tools", session)

    def test_loop_marks_session_stopped_on_unexpected_round_exception(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sid = "sess-loop-unexpected-stop"
            self._write_workspace(root, sid)
            args = _loop_args(sid)

            with mock.patch("a2a_cli.main._must_find_root", return_value=root):
                with mock.patch("a2a_cli.main._worktree_lock", _noop_worktree_lock):
                    with mock.patch("a2a_cli.main._refresh_prior_review_context", return_value=(None, False)):
                        with mock.patch("a2a_cli.main._run_agent_step", return_value=0):
                            with mock.patch(
                                "a2a_cli.main._run_validation_gate",
                                side_effect=ValueError("boom"),
                            ):
                                with mock.patch("a2a_cli.main._auto_write_session_html_report"):
                                    rc = cmd_loop(args)

            self.assertEqual(rc, 1)
            session = (root / ".a2a" / "sessions" / f"{sid}.json").read_text(encoding="utf-8")
            self.assertIn('"status": "stopped"', session)
            self.assertIn("unexpected loop error in round 1", session)
            self.assertIn("ValueError: boom", session)


if __name__ == "__main__":
    unittest.main()
