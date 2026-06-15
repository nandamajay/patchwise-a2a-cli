import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from a2a_cli.main import cmd_watch


def _watch_args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "msgid": "20260508-demo@example.com",
        "poll": 1,
        "max_loops": 1,
        "auto_followup": True,
        "session": None,
        "task": None,
        "max_rounds": None,
        "timeout_min": None,
        "builder_cmd": None,
        "reviewer_cmd": None,
        "max_iterations": 1,
        "lore_out_dir": None,
        "focus_issue": None,
        "notify_email": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class WatchFollowupTests(unittest.TestCase):
    def test_auto_followup_uses_active_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a2a = root / ".a2a"
            a2a.mkdir(parents=True, exist_ok=True)
            (a2a / "state.json").write_text(
                json.dumps({"version": 1, "active_session_id": "sess-active", "last_updated": ""}),
                encoding="utf-8",
            )

            def _fake_watch(*_args, **kwargs):
                on_event = kwargs.get("on_event")
                if on_event:
                    on_event({"msg_id": "reply-1@example.com", "author": "reviewer", "priority": "high"})
                return []

            with mock.patch("a2a_cli.main._must_find_root", return_value=root):
                with mock.patch("a2a_cli.main.watch_lore", side_effect=_fake_watch):
                    with mock.patch("a2a_cli.main.cmd_loop", return_value=0) as loop_mock:
                        with mock.patch("a2a_cli.main.send_notification_email", return_value={"sent": True}):
                            rc = cmd_watch(_watch_args())

            self.assertEqual(rc, 0)
            self.assertEqual(loop_mock.call_count, 1)
            loop_args = loop_mock.call_args.args[0]
            self.assertEqual(loop_args.session, "sess-active")
            self.assertIsNone(loop_args.lore_msgid)
            self.assertIsNone(loop_args.task)

    def test_auto_followup_starts_new_session_without_active(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a2a = root / ".a2a"
            a2a.mkdir(parents=True, exist_ok=True)
            (a2a / "state.json").write_text(
                json.dumps({"version": 1, "active_session_id": None, "last_updated": ""}),
                encoding="utf-8",
            )

            def _fake_watch(*_args, **kwargs):
                on_event = kwargs.get("on_event")
                if on_event:
                    on_event({"msg_id": "reply-2@example.com", "author": "reviewer", "priority": "medium"})
                return []

            with mock.patch("a2a_cli.main._must_find_root", return_value=root):
                with mock.patch("a2a_cli.main.watch_lore", side_effect=_fake_watch):
                    with mock.patch("a2a_cli.main.cmd_loop", return_value=0) as loop_mock:
                        with mock.patch("a2a_cli.main.send_notification_email", return_value={"sent": True}):
                            rc = cmd_watch(_watch_args(task="auto-lore-followup"))

            self.assertEqual(rc, 0)
            self.assertEqual(loop_mock.call_count, 1)
            loop_args = loop_mock.call_args.args[0]
            self.assertIsNone(loop_args.session)
            self.assertEqual(loop_args.task, "auto-lore-followup")
            self.assertEqual(loop_args.lore_msgid, "20260508-demo@example.com")

    def test_auto_followup_passes_focus_issue(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a2a = root / ".a2a"
            a2a.mkdir(parents=True, exist_ok=True)
            (a2a / "state.json").write_text(
                json.dumps({"version": 1, "active_session_id": None, "last_updated": ""}),
                encoding="utf-8",
            )

            def _fake_watch(*_args, **kwargs):
                on_event = kwargs.get("on_event")
                if on_event:
                    on_event({"msg_id": "reply-focus@example.com", "author": "reviewer", "priority": "high"})
                return []

            with mock.patch("a2a_cli.main._must_find_root", return_value=root):
                with mock.patch("a2a_cli.main.watch_lore", side_effect=_fake_watch):
                    with mock.patch("a2a_cli.main.cmd_loop", return_value=0) as loop_mock:
                        with mock.patch("a2a_cli.main.send_notification_email", return_value={"sent": True}):
                            rc = cmd_watch(
                                _watch_args(
                                    task="focus-followup",
                                    focus_issue=["swr init timeout -110"],
                                )
                            )

            self.assertEqual(rc, 0)
            self.assertEqual(loop_mock.call_count, 1)
            loop_args = loop_mock.call_args.args[0]
            self.assertEqual(loop_args.focus_issue, ["swr init timeout -110"])

    def test_auto_followup_requires_task_without_active_or_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a2a = root / ".a2a"
            a2a.mkdir(parents=True, exist_ok=True)
            (a2a / "state.json").write_text(
                json.dumps({"version": 1, "active_session_id": None, "last_updated": ""}),
                encoding="utf-8",
            )

            with mock.patch("a2a_cli.main._must_find_root", return_value=root):
                with mock.patch("a2a_cli.main.watch_lore") as watch_mock:
                    rc = cmd_watch(_watch_args())

            self.assertEqual(rc, 1)
            watch_mock.assert_not_called()

    def test_network_warning_does_not_trigger_followup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a2a = root / ".a2a"
            a2a.mkdir(parents=True, exist_ok=True)
            (a2a / "state.json").write_text(
                json.dumps({"version": 1, "active_session_id": "sess-active", "last_updated": ""}),
                encoding="utf-8",
            )

            def _fake_watch(*_args, **kwargs):
                on_event = kwargs.get("on_event")
                if on_event:
                    on_event({"type": "network_warning", "error": "down"})
                return []

            with mock.patch("a2a_cli.main._must_find_root", return_value=root):
                with mock.patch("a2a_cli.main.watch_lore", side_effect=_fake_watch):
                    with mock.patch("a2a_cli.main.cmd_loop", return_value=0) as loop_mock:
                        with mock.patch("a2a_cli.main.send_notification_email", return_value={"sent": True}):
                            rc = cmd_watch(_watch_args())

            self.assertEqual(rc, 0)
            loop_mock.assert_not_called()

    def test_reply_sends_observation_email_default_recipient(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a2a = root / ".a2a"
            a2a.mkdir(parents=True, exist_ok=True)
            (a2a / "state.json").write_text(
                json.dumps({"version": 1, "active_session_id": "sess-active", "last_updated": ""}),
                encoding="utf-8",
            )
            (a2a / "config.json").write_text(
                json.dumps({"version": 1, "email_bridge": {"notify_to": []}}),
                encoding="utf-8",
            )

            def _fake_watch(*_args, **kwargs):
                on_event = kwargs.get("on_event")
                if on_event:
                    on_event(
                        {
                            "msg_id": "reply-3@example.com",
                            "author": "Mark Brown <broonie@kernel.org>",
                            "priority": "high",
                            "excerpt": "Please fix runtime PM __must_check handling",
                        }
                    )
                return []

            with mock.patch("a2a_cli.main._must_find_root", return_value=root):
                with mock.patch("a2a_cli.main.watch_lore", side_effect=_fake_watch):
                    with mock.patch("a2a_cli.main.cmd_loop", return_value=0):
                        with mock.patch("a2a_cli.main.send_notification_email", return_value={"sent": True}) as mail_mock:
                            rc = cmd_watch(_watch_args())

            self.assertEqual(rc, 0)
            self.assertEqual(mail_mock.call_count, 1)
            kwargs = mail_mock.call_args.kwargs
            self.assertEqual(kwargs["to_addrs"], ["nandam@qti.qualcomm.com"])
            self.assertIn("why this was likely missed", str(kwargs["body"]).lower())

    def test_reply_sends_observation_email_custom_recipient(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a2a = root / ".a2a"
            a2a.mkdir(parents=True, exist_ok=True)
            (a2a / "state.json").write_text(
                json.dumps({"version": 1, "active_session_id": "sess-active", "last_updated": ""}),
                encoding="utf-8",
            )

            def _fake_watch(*_args, **kwargs):
                on_event = kwargs.get("on_event")
                if on_event:
                    on_event({"msg_id": "reply-4@example.com", "author": "reviewer", "priority": "medium"})
                return []

            with mock.patch("a2a_cli.main._must_find_root", return_value=root):
                with mock.patch("a2a_cli.main.watch_lore", side_effect=_fake_watch):
                    with mock.patch("a2a_cli.main.cmd_loop", return_value=0):
                        with mock.patch("a2a_cli.main.send_notification_email", return_value={"sent": True}) as mail_mock:
                            rc = cmd_watch(_watch_args(notify_email=["one@example.com", "ONE@example.com"]))

            self.assertEqual(rc, 0)
            kwargs = mail_mock.call_args.kwargs
            self.assertEqual(kwargs["to_addrs"], ["one@example.com"])

    def test_reply_blocks_mailing_list_recipient_and_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a2a = root / ".a2a"
            a2a.mkdir(parents=True, exist_ok=True)
            (a2a / "state.json").write_text(
                json.dumps({"version": 1, "active_session_id": "sess-active", "last_updated": ""}),
                encoding="utf-8",
            )

            def _fake_watch(*_args, **kwargs):
                on_event = kwargs.get("on_event")
                if on_event:
                    on_event({"msg_id": "reply-5@example.com", "author": "reviewer", "priority": "medium"})
                return []

            with mock.patch("a2a_cli.main._must_find_root", return_value=root):
                with mock.patch("a2a_cli.main.watch_lore", side_effect=_fake_watch):
                    with mock.patch("a2a_cli.main.cmd_loop", return_value=0):
                        with mock.patch("a2a_cli.main.send_notification_email", return_value={"sent": True}) as mail_mock:
                            rc = cmd_watch(_watch_args(notify_email=["linux-kernel@vger.kernel.org"]))

            self.assertEqual(rc, 0)
            kwargs = mail_mock.call_args.kwargs
            self.assertEqual(kwargs["to_addrs"], ["nandam@qti.qualcomm.com"])

    def test_reply_blocks_config_mailing_list_recipient_and_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a2a = root / ".a2a"
            a2a.mkdir(parents=True, exist_ok=True)
            (a2a / "state.json").write_text(
                json.dumps({"version": 1, "active_session_id": "sess-active", "last_updated": ""}),
                encoding="utf-8",
            )
            (a2a / "config.json").write_text(
                json.dumps({"version": 1, "email_bridge": {"notify_to": ["linux-arm-msm@vger.kernel.org"]}}),
                encoding="utf-8",
            )

            def _fake_watch(*_args, **kwargs):
                on_event = kwargs.get("on_event")
                if on_event:
                    on_event({"msg_id": "reply-6@example.com", "author": "reviewer", "priority": "medium"})
                return []

            with mock.patch("a2a_cli.main._must_find_root", return_value=root):
                with mock.patch("a2a_cli.main.watch_lore", side_effect=_fake_watch):
                    with mock.patch("a2a_cli.main.cmd_loop", return_value=0):
                        with mock.patch("a2a_cli.main.send_notification_email", return_value={"sent": True}) as mail_mock:
                            rc = cmd_watch(_watch_args(notify_email=None))

            self.assertEqual(rc, 0)
            kwargs = mail_mock.call_args.kwargs
            self.assertEqual(kwargs["to_addrs"], ["nandam@qti.qualcomm.com"])

    def test_email_send_failure_does_not_abort_followup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a2a = root / ".a2a"
            a2a.mkdir(parents=True, exist_ok=True)
            (a2a / "state.json").write_text(
                json.dumps({"version": 1, "active_session_id": "sess-active", "last_updated": ""}),
                encoding="utf-8",
            )

            def _fake_watch(*_args, **kwargs):
                on_event = kwargs.get("on_event")
                if on_event:
                    on_event({"msg_id": "reply-err@example.com", "author": "reviewer", "priority": "medium"})
                return []

            with mock.patch("a2a_cli.main._must_find_root", return_value=root):
                with mock.patch("a2a_cli.main.watch_lore", side_effect=_fake_watch):
                    with mock.patch("a2a_cli.main.cmd_loop", return_value=0) as loop_mock:
                        with mock.patch(
                            "a2a_cli.main.send_notification_email",
                            side_effect=RuntimeError("smtp down"),
                        ):
                            rc = cmd_watch(_watch_args())

            self.assertEqual(rc, 0)
            self.assertEqual(loop_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
