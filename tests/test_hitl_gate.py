import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from a2a_cli.hitl_gate import run_hitl_gate
from a2a_cli.submission_mailer import send_dry_run


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _input_iter(values: list[str]):
    queue = list(values)

    def _reader(_prompt: str) -> str:
        if not queue:
            raise AssertionError("input queue exhausted")
        return queue.pop(0)

    return _reader


class HitlGateTests(unittest.TestCase):
    def _bootstrap_workspace(self, td: str, *, watch_as_file: bool = False) -> tuple[Path, str]:
        root = Path(td)
        sid = "sess-hitl"
        a2a = root / ".a2a"
        (a2a / "sessions").mkdir(parents=True, exist_ok=True)
        report_dir = a2a / "reports" / sid
        report_dir.mkdir(parents=True, exist_ok=True)

        patch_dir = root / "patches" / "xo_sd_v3"
        patch_dir.mkdir(parents=True, exist_ok=True)
        cover = patch_dir / "0000-cover-letter.patch"
        cover.write_text("Subject: [PATCH v3 0/1] demo series\n\nBody.\n", encoding="utf-8")
        (patch_dir / "0001-demo.patch").write_text("diff --git a/a.c b/a.c\n", encoding="utf-8")

        watch_path = patch_dir / "0001-demo.patch" if watch_as_file else patch_dir
        _write_json(
            a2a / "sessions" / f"{sid}.json",
            {
                "id": sid,
                "task": "hitl-test",
                "status": "lgtm",
                "watch_path": str(watch_path),
                "rounds": [{"round": 1, "findings_total": 2, "findings_open": 0}],
            },
        )
        (report_dir / "round-01-summary.md").write_text("# Summary\n", encoding="utf-8")
        _write_json(
            report_dir / "round-01-summary.json",
            {
                "findings": {"total": 2, "open": 0},
                "validation_gate": {"failures": 0},
                "prior_comments": {"totals": {"closed": 2, "received_total": 2}},
            },
        )
        return root, sid

    def _cfg(self, **overrides):
        base = {
            "submission": {
                "dry_run": True,
                "dry_run_recipient": "nandam@qti.qualcomm.com",
                "allow_community_send": False,
                "community_to": [],
                "community_cc": [],
                "hitl_timeout_secs": 300,
            }
        }
        base["submission"].update(overrides)
        return base

    def _summary(self) -> dict:
        return {
            "series": [{"name": "lpi", "status": "lgtm", "patch_count": 2, "rounds": 1}],
            "findings_resolved": 2,
            "findings_total": 2,
            "checkpatch_errors": 0,
            "sparse_new_warnings": 0,
            "prior_comments_closed": 2,
            "prior_comments_total": 2,
        }

    def test_approve_requires_double_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root, sid = self._bootstrap_workspace(td)
            send_calls = []

            def fake_send(_root, _sid, _recipient, _cc, _cfg):
                send_calls.append(True)
                return {"sent": True}

            result = run_hitl_gate(
                root,
                sid,
                self._summary(),
                self._cfg(),
                input_fn=_input_iter(["approve", "wrong", "approve", "APPROVE"]),
                send_fn=fake_send,
            )
            self.assertEqual(result["status"], "sent")
            self.assertEqual(len(send_calls), 1)

    def test_abort_saves_state_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root, sid = self._bootstrap_workspace(td)
            result = run_hitl_gate(
                root,
                sid,
                self._summary(),
                self._cfg(),
                input_fn=_input_iter(["abort"]),
            )
            self.assertEqual(result["status"], "aborted")
            state_path = Path(result["state_path"])
            self.assertTrue(state_path.exists())
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["reason"], "user_abort")

    def test_modify_validates_email_format(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root, sid = self._bootstrap_workspace(td)
            result = run_hitl_gate(
                root,
                sid,
                self._summary(),
                self._cfg(),
                input_fn=_input_iter(["modify", "bad-email", "abort"]),
            )
            self.assertEqual(result["status"], "aborted")
            payload = result["state"]
            self.assertEqual(payload["recipient"], "nandam@qti.qualcomm.com")

    def test_review_opens_editor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root, sid = self._bootstrap_workspace(td)
            opened = {"called": False}

            def fake_editor(_path: Path) -> None:
                opened["called"] = True

            run_hitl_gate(
                root,
                sid,
                self._summary(),
                self._cfg(),
                input_fn=_input_iter(["review", "abort"]),
                open_editor_fn=fake_editor,
            )
            self.assertTrue(opened["called"])

    def test_timeout_triggers_abort_not_approve(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root, sid = self._bootstrap_workspace(td)

            def timeout_input(_prompt: str) -> str:
                raise TimeoutError("timed out")

            result = run_hitl_gate(
                root,
                sid,
                self._summary(),
                self._cfg(hitl_timeout_secs=1),
                input_fn=timeout_input,
            )
            self.assertEqual(result["status"], "aborted")
            self.assertEqual(result["reason"], "timeout")

    def test_community_list_never_populated_autonomously(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root, sid = self._bootstrap_workspace(td)
            cfg = self._cfg(community_to=["community@lists.example.org"])
            with mock.patch("a2a_cli.submission_mailer.send_email") as send_mock:
                send_mock.return_value = {"sent": True, "fallback": "", "error": ""}
                out = send_dry_run(root, sid, "nandam@qti.qualcomm.com", [], cfg)
            self.assertTrue(out["sent"])
            kwargs = send_mock.call_args.kwargs
            self.assertEqual(kwargs["to_addrs"], ["nandam@qti.qualcomm.com"])
            self.assertEqual(kwargs["cc_addrs"], [])

    def test_dry_run_default_true_always(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root, sid = self._bootstrap_workspace(td)
            cfg = {"submission": {"dry_run_recipient": "nandam@qti.qualcomm.com"}}
            with mock.patch("a2a_cli.submission_mailer.send_email") as send_mock:
                send_mock.return_value = {"sent": True, "fallback": "", "error": ""}
                out = send_dry_run(root, sid, "nandam@qti.qualcomm.com", [], cfg)
            self.assertTrue(out["sent"])

    def test_safety_assert_blocks_community_send(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root, sid = self._bootstrap_workspace(td)
            cfg = self._cfg(community_to=["community@lists.example.org"])
            with self.assertRaises(RuntimeError):
                send_dry_run(root, sid, "community@lists.example.org", [], cfg)

    def test_resume_after_abort_works(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root, sid = self._bootstrap_workspace(td)
            run_hitl_gate(
                root,
                sid,
                self._summary(),
                self._cfg(),
                input_fn=_input_iter(["modify", "alt@qti.qualcomm.com", "", "abort"]),
            )

            captured = {}

            def fake_send(_root, _sid, recipient, cc_list, _cfg):
                captured["recipient"] = recipient
                captured["cc"] = cc_list
                return {"sent": True}

            result = run_hitl_gate(
                root,
                sid,
                self._summary(),
                self._cfg(),
                resume=True,
                input_fn=_input_iter(["approve", "APPROVE"]),
                send_fn=fake_send,
            )
            self.assertEqual(result["status"], "sent")
            self.assertEqual(captured["recipient"], "alt@qti.qualcomm.com")
            self.assertFalse((root / ".a2a" / "reports" / sid / "hitl_state.json").exists())

    def test_hitl_gate_cannot_be_bypassed_by_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root, sid = self._bootstrap_workspace(td)
            cfg = self._cfg(dry_run=False, allow_community_send=True)
            with self.assertRaises(RuntimeError):
                send_dry_run(root, sid, "nandam@qti.qualcomm.com", [], cfg)


if __name__ == "__main__":
    unittest.main()
