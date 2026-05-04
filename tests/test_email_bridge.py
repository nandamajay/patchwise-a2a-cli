import json
import tempfile
import unittest
from pathlib import Path

from a2a_cli.email_bridge import (
    BridgeStore,
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
                },
            )
            self.assertFalse(result["imap_enabled"])
            self.assertIn("incoming_processed", result)
            self.assertIn("notifications_sent", result)


if __name__ == "__main__":
    unittest.main()
