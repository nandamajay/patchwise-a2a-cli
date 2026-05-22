import json
import tempfile
import unittest
from pathlib import Path

from a2a_cli.email_bridge import (
    BridgeStore,
    _infer_auto_run_request,
    extend_stopped_session_once,
    handle_command,
    load_bridge_config,
    parse_a2a_command,
    run_bridge_once,
)


def _write_session(root: Path, session_id: str, payload: dict) -> None:
    sessions_dir = root / ".a2a" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"{session_id}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class EmailBridgeTests(unittest.TestCase):
    def test_auto_infer_lore_request_without_explicit_command(self) -> None:
        inferred = _infer_auto_run_request(
            "Review request",
            "Please review: https://lore.kernel.org/all/20260413121824.375473-1-ajay.nandam@oss.qualcomm.com/.",
            [],
        )
        self.assertIsNotNone(inferred)
        self.assertEqual(inferred["command"], "run")
        self.assertEqual(inferred["mode"], "lore")
        self.assertIn("URL", inferred["params"])
        self.assertEqual(
            inferred["params"]["URL"],
            "https://lore.kernel.org/all/20260413121824.375473-1-ajay.nandam@oss.qualcomm.com/",
        )

    def test_auto_infer_attachment_precedence_over_lore(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            patch = Path(td) / "0001-demo.patch"
            patch.write_text("Subject: [PATCH] demo\n", encoding="utf-8")
            inferred = _infer_auto_run_request(
                "Review request",
                "Link: https://lore.kernel.org/r/20260413.1@foo",
                [patch],
            )
        self.assertIsNotNone(inferred)
        self.assertEqual(inferred["command"], "run")
        self.assertEqual(inferred["mode"], "attachment")

    def test_auto_infer_github_request_without_explicit_command(self) -> None:
        inferred = _infer_auto_run_request(
            "Review request",
            "Please review: https://github.com/openai/sample/pull/42",
            [],
        )
        self.assertIsNotNone(inferred)
        self.assertEqual(inferred["command"], "run")
        self.assertEqual(inferred["mode"], "github")
        self.assertEqual(inferred["params"]["PR"], "https://github.com/openai/sample/pull/42")

    def test_auto_infer_gerrit_request_without_explicit_command(self) -> None:
        inferred = _infer_auto_run_request(
            "Review request",
            "Please review: https://review.example.com/c/project/+/12345/7",
            [],
        )
        self.assertIsNotNone(inferred)
        self.assertEqual(inferred["command"], "run")
        self.assertEqual(inferred["mode"], "gerrit")
        self.assertIn("CHANGE", inferred["params"])

    def test_parse_command_from_subject_with_body_params(self) -> None:
        parsed = parse_a2a_command(
            "A2A RUN LORE URL=https://lore.kernel.org/all/123@example.com/",
            "TASK=lore-review\nMAX_ROUNDS=4\n",
        )
        self.assertEqual(parsed["command"], "run")
        self.assertEqual(parsed["mode"], "lore")
        self.assertEqual(parsed["params"]["TASK"], "lore-review")
        self.assertEqual(parsed["params"]["MAX_ROUNDS"], "4")

    def test_approval_token_is_one_time(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = BridgeStore(Path(td) / "bridge.db")
            try:
                token = store.create_approval_token(
                    session_id="sess-1",
                    action="extend",
                    issued_to="user@example.com",
                    ttl_min=60,
                )
                ok, reason = store.consume_approval_token(
                    token=token,
                    session_id="sess-1",
                    action="extend",
                    sender="user@example.com",
                )
                self.assertTrue(ok)
                self.assertEqual(reason, "")
                ok2, reason2 = store.consume_approval_token(
                    token=token,
                    session_id="sess-1",
                    action="extend",
                    sender="user@example.com",
                )
                self.assertFalse(ok2)
                self.assertIn("used", reason2)
            finally:
                store.close()

    def test_extend_stopped_session_once_updates_round(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sid = "sess-test"
            _write_session(
                root,
                sid,
                {
                    "id": sid,
                    "status": "stopped",
                    "current_round": 3,
                    "max_rounds": 3,
                },
            )
            updated = extend_stopped_session_once(root, sid)
            self.assertEqual(updated["status"], "in_progress")
            self.assertEqual(updated["current_round"], 4)
            self.assertEqual(updated["max_rounds"], 4)

    def test_handle_extend_no_auto_run(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sid = "sess-test"
            _write_session(
                root,
                sid,
                {
                    "id": sid,
                    "status": "stopped",
                    "current_round": 2,
                    "max_rounds": 2,
                },
            )
            cfg = load_bridge_config(
                root,
                {
                    "state_db": str(root / ".a2a" / "email_bridge" / "bridge.db"),
                    "inbox_dir": str(root / ".a2a" / "email_bridge" / "inbox"),
                },
            )
            store = BridgeStore(cfg.state_db)
            try:
                token = store.create_approval_token(
                    session_id=sid,
                    action="extend",
                    issued_to="",
                    ttl_min=60,
                )
                status, response = handle_command(
                    cfg,
                    store,
                    "user@example.com",
                    {
                        "command": "extend",
                        "mode": "",
                        "params": {
                            "SESSION": sid,
                            "TOKEN": token,
                            "AUTO_RUN": "no",
                        },
                    },
                    [],
                )
                self.assertEqual(status, "ok")
                self.assertIn("Session extended.", response)
                self.assertIn("Auto run skipped", response)
            finally:
                store.close()

    def test_handle_run_attachment_requires_patch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = load_bridge_config(
                root,
                {
                    "state_db": str(root / ".a2a" / "email_bridge" / "bridge.db"),
                    "inbox_dir": str(root / ".a2a" / "email_bridge" / "inbox"),
                },
            )
            store = BridgeStore(cfg.state_db)
            try:
                status, response = handle_command(
                    cfg,
                    store,
                    "user@example.com",
                    {"command": "run", "mode": "attachment", "params": {"TASK": "t1"}},
                    [],
                )
                self.assertEqual(status, "error")
                self.assertIn("No .patch/.diff attachments", response)
            finally:
                store.close()

    def test_handle_run_github_schedules_loop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = load_bridge_config(
                root,
                {
                    "state_db": str(root / ".a2a" / "email_bridge" / "bridge.db"),
                    "inbox_dir": str(root / ".a2a" / "email_bridge" / "inbox"),
                },
            )
            store = BridgeStore(cfg.state_db)
            try:
                with unittest.mock.patch(
                    "a2a_cli.email_bridge._spawn_loop_and_track",
                    return_value=(111, "sess-github", root / "run.log"),
                ) as spawn_mock:
                    status, response = handle_command(
                        cfg,
                        store,
                        "user@example.com",
                        {
                            "command": "run",
                            "mode": "github",
                            "params": {"PR": "https://github.com/openai/sample/pull/42"},
                        },
                        [],
                    )
                self.assertEqual(status, "ok")
                self.assertIn("GitHub PR review scheduled", response)
                cmd = spawn_mock.call_args.args[3]
                self.assertIn("--github-pr", cmd)
            finally:
                store.close()

    def test_handle_run_gerrit_schedules_loop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = load_bridge_config(
                root,
                {
                    "state_db": str(root / ".a2a" / "email_bridge" / "bridge.db"),
                    "inbox_dir": str(root / ".a2a" / "email_bridge" / "inbox"),
                },
            )
            store = BridgeStore(cfg.state_db)
            try:
                with unittest.mock.patch(
                    "a2a_cli.email_bridge._spawn_loop_and_track",
                    return_value=(112, "sess-gerrit", root / "run.log"),
                ) as spawn_mock:
                    status, response = handle_command(
                        cfg,
                        store,
                        "user@example.com",
                        {
                            "command": "run",
                            "mode": "gerrit",
                            "params": {
                                "CHANGE": "12345",
                                "GERRIT_BASE_URL": "https://review.example.com",
                            },
                        },
                        [],
                    )
                self.assertEqual(status, "ok")
                self.assertIn("Gerrit change review scheduled", response)
                cmd = spawn_mock.call_args.args[3]
                self.assertIn("--gerrit-change", cmd)
                self.assertIn("--gerrit-base-url", cmd)
            finally:
                store.close()

    def test_run_bridge_once_without_imap_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = run_bridge_once(
                root,
                {
                    "state_db": str(root / ".a2a" / "email_bridge" / "bridge.db"),
                    "inbox_dir": str(root / ".a2a" / "email_bridge" / "inbox"),
                    "imap_host": "",
                    "imap_user": "",
                    "imap_password": "",
                    "auto_detect_requests": True,
                },
            )
            self.assertFalse(result["imap_enabled"])
            self.assertTrue(result["auto_detect_requests"])
            self.assertIn("incoming_processed", result)
            self.assertIn("notifications_sent", result)


if __name__ == "__main__":
    unittest.main()
