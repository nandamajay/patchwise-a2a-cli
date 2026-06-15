import json
import multiprocessing as mp
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from a2a_cli.main import _ensure_session_worktrees, _worktree_lock, _worktree_lock_path


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

    def test_ensure_session_worktrees_recovers_missing_builder(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=True)
            builder_path = root / ".a2a" / "worktrees" / "builder"
            reviewer_path = root / ".a2a" / "worktrees" / "aryabhatta"
            reviewer_path.mkdir(parents=True, exist_ok=True)
            (reviewer_path / ".git").write_text("gitdir: /tmp/reviewer\n", encoding="utf-8")

            session = {
                "id": "sess-test",
                "repo_path": str(repo),
                "reviewer_name": "aryabhatta",
                "branch": "a2a/test-branch",
                "worktrees": {
                    "builder": str(builder_path),
                    "aryabhatta": str(reviewer_path),
                },
            }

            with mock.patch("a2a_cli.main._git_ok", return_value=True):
                with mock.patch("a2a_cli.main._git", return_value="") as git_mock:
                    updated = _ensure_session_worktrees(root, session)

            self.assertEqual(updated["worktrees"]["builder"], str(builder_path.resolve()))
            self.assertEqual(git_mock.call_count, 1)
            git_mock.assert_called_once_with(
                repo.resolve(),
                "worktree",
                "add",
                "--force",
                str(builder_path.resolve()),
                "a2a/test-branch",
            )

    def test_ensure_session_worktrees_initializes_defaults_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=True)
            session = {
                "id": "sess-test",
                "repo_path": str(repo),
                "reviewer_name": "aryabhatta",
                "branch": "a2a/test-branch",
            }

            with mock.patch("a2a_cli.main._git_ok", return_value=True):
                with mock.patch("a2a_cli.main._git", return_value="") as git_mock:
                    with mock.patch("a2a_cli.main._write_session") as write_mock:
                        updated = _ensure_session_worktrees(root, session)

            builder_path = root / ".a2a" / "worktrees" / "builder"
            reviewer_path = root / ".a2a" / "worktrees" / "aryabhatta"
            self.assertEqual(updated["worktrees"]["builder"], str(builder_path.resolve()))
            self.assertEqual(updated["worktrees"]["aryabhatta"], str(reviewer_path.resolve()))
            self.assertIn("updated_at", updated)
            self.assertEqual(git_mock.call_count, 2)
            git_mock.assert_has_calls(
                [
                    mock.call(
                        repo.resolve(),
                        "worktree",
                        "add",
                        "--force",
                        str(builder_path.resolve()),
                        "a2a/test-branch",
                    ),
                    mock.call(
                        repo.resolve(),
                        "worktree",
                        "add",
                        "--force",
                        "--detach",
                        str(reviewer_path.resolve()),
                        "a2a/test-branch",
                    ),
                ]
            )
            write_mock.assert_called_once_with(root, updated)


if __name__ == "__main__":
    unittest.main()
