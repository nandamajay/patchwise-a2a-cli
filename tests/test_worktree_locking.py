import json
import multiprocessing as mp
import tempfile
import time
import unittest
from pathlib import Path

from a2a_cli.main import _worktree_lock, _worktree_lock_path


def _acquire_lock(root_str: str, session: dict, session_id: str, hold_sec: float, queue: mp.Queue) -> None:
    root = Path(root_str)
    with _worktree_lock(root, session, session_id):
        queue.put(time.monotonic())
        time.sleep(hold_sec)


class WorktreeLockingTests(unittest.TestCase):
    def test_worktree_lock_path_is_stable_for_same_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            session_a = {
                "reviewer_name": "aryabhatta",
                "repo_path": str(root / "repo"),
                "worktrees": {
                    "builder": str(root / "wt" / "builder"),
                    "aryabhatta": str(root / "wt" / "reviewer"),
                },
            }
            session_b = {
                "reviewer_name": "aryabhatta",
                "repo_path": str(root / "repo"),
                "worktrees": {
                    "builder": str(root / "wt" / "builder"),
                    "aryabhatta": str(root / "wt" / "reviewer"),
                },
            }
            lock_a = _worktree_lock_path(root, session_a)
            lock_b = _worktree_lock_path(root, session_b)
            self.assertEqual(lock_a, lock_b)
            self.assertIn(".a2a/locks/worktrees", str(lock_a))
            self.assertEqual(lock_a.suffix, ".lock")

    def test_worktree_lock_path_changes_for_different_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            session_a = {
                "reviewer_name": "aryabhatta",
                "repo_path": str(root / "repo-a"),
                "worktrees": {
                    "builder": str(root / "wt-a" / "builder"),
                    "aryabhatta": str(root / "wt-a" / "reviewer"),
                },
            }
            session_b = {
                "reviewer_name": "aryabhatta",
                "repo_path": str(root / "repo-b"),
                "worktrees": {
                    "builder": str(root / "wt-b" / "builder"),
                    "aryabhatta": str(root / "wt-b" / "reviewer"),
                },
            }
            self.assertNotEqual(_worktree_lock_path(root, session_a), _worktree_lock_path(root, session_b))

    def test_worktree_lock_serializes_parallel_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            session = {
                "reviewer_name": "aryabhatta",
                "repo_path": str(root / "repo"),
                "worktrees": {
                    "builder": str(root / "wt" / "builder"),
                    "aryabhatta": str(root / "wt" / "reviewer"),
                },
            }
            ctx = mp.get_context("fork")
            q1: mp.Queue = ctx.Queue()
            q2: mp.Queue = ctx.Queue()
            p1 = ctx.Process(
                target=_acquire_lock,
                args=(str(root), session, "sess-lock-1", 1.2, q1),
            )
            p2 = ctx.Process(
                target=_acquire_lock,
                args=(str(root), session, "sess-lock-2", 0.1, q2),
            )

            p1.start()
            t1 = q1.get(timeout=3)
            p2.start()
            t2 = q2.get(timeout=5)
            p1.join(timeout=5)
            p2.join(timeout=5)

            self.assertEqual(p1.exitcode, 0)
            self.assertEqual(p2.exitcode, 0)
            self.assertGreaterEqual(t2 - t1, 1.0)

            lock_path = _worktree_lock_path(root, session)
            self.assertTrue(lock_path.exists())
            lock_payload = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(lock_payload.get("session_id"), "sess-lock-2")


if __name__ == "__main__":
    unittest.main()
