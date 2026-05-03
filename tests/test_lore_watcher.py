import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from a2a_cli.lore_watcher import (
    load_known_message_ids,
    process_new_reply,
    save_known_message_ids,
    watch,
)
from a2a_cli.maintainer_tracker import get_priority, update_profile


class LoreWatcherTests(unittest.TestCase):
    def test_new_reply_detected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def fetch_ids(_msgid: str) -> set[str]:
                return {"msg-1"}

            def fetch_msg(_msgid: str) -> str:
                return "From: Mark Brown <broonie@kernel.org>\n\nAcked-by: x"

            events = watch(root, "id-1", poll_interval_secs=1, max_loops=1, fetch_ids_fn=fetch_ids, fetch_msg_fn=fetch_msg)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["msg_id"], "msg-1")

    def test_known_replies_not_re_processed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            save_known_message_ids(root, "id-1", {"msg-1"})

            def fetch_ids(_msgid: str) -> set[str]:
                return {"msg-1"}

            events = watch(root, "id-1", poll_interval_secs=1, max_loops=1, fetch_ids_fn=fetch_ids, fetch_msg_fn=lambda _: "")
            self.assertEqual(events, [])

    def test_non_reviewer_replies_handled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ev = process_new_reply(root, "m1", "Random User <x@y>", "looks good")
            self.assertIn("priority", ev)
            self.assertFalse(ev["trigger_session"])

    def test_maintainer_profile_updated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def fetch_ids(_msgid: str) -> set[str]:
                return {"msg-1"}

            def fetch_msg(_msgid: str) -> str:
                return "From: Dev One <dev@example.com>\n\nnit: pm_runtime"

            watch(root, "id-1", poll_interval_secs=1, max_loops=1, fetch_ids_fn=fetch_ids, fetch_msg_fn=fetch_msg)
            priority = get_priority(root, "Dev One <dev@example.com>")
            self.assertIn(priority, {"low", "medium", "high"})

    def test_priority_assigned_correctly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for _ in range(5):
                update_profile(root, "Mark Brown <broonie@kernel.org>", ["pm_runtime"], "lgtm")
            self.assertEqual(get_priority(root, "Mark Brown <broonie@kernel.org>"), "high")

    def test_auto_session_trigger_if_configured(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for _ in range(5):
                update_profile(root, "Mark Brown <broonie@kernel.org>", ["pm_runtime"], "lgtm")
            ev = process_new_reply(
                root,
                "m1",
                "Mark Brown <broonie@kernel.org>",
                "please fix",
                config={"auto_trigger_session": True},
            )
            self.assertTrue(ev["trigger_session"])

    def test_poll_interval_respected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def fetch_ids(_msgid: str) -> set[str]:
                return set()

            with mock.patch("a2a_cli.lore_watcher.time.sleep") as sleep_mock:
                watch(root, "id-1", poll_interval_secs=7, max_loops=1, fetch_ids_fn=fetch_ids, fetch_msg_fn=lambda _: "")
            sleep_mock.assert_not_called()

    def test_network_unreachable_graceful(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def fetch_ids(_msgid: str) -> set[str]:
                raise urllib.error.URLError("down")

            with mock.patch("a2a_cli.lore_watcher.time.sleep"):
                events = watch(root, "id-1", poll_interval_secs=1, max_loops=1, fetch_ids_fn=fetch_ids, fetch_msg_fn=lambda _: "")
            self.assertEqual(events[0]["type"], "network_warning")
            self.assertEqual(load_known_message_ids(root, "id-1"), set())


if __name__ == "__main__":
    unittest.main()
