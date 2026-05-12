import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from a2a_cli.main import _run_auto_respin_if_requested


class AutoRespinResumeTests(unittest.TestCase):
    def test_skips_regeneration_when_existing_artifacts_present(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            session_id = "sess-existing"
            report_dir = root / ".a2a" / "reports" / session_id
            report_dir.mkdir(parents=True, exist_ok=True)
            output_path = root / ".a2a" / "patches" / session_id / "v4"
            output_path.mkdir(parents=True, exist_ok=True)
            (report_dir / "lore_next_version.json").write_text(
                json.dumps({"status": "ok", "output_path": str(output_path)}),
                encoding="utf-8",
            )

            session = {"id": session_id}
            with mock.patch("a2a_cli.main._auto_generate_next_version") as generator:
                rc = _run_auto_respin_if_requested(
                    root,
                    session,
                    auto_respin=True,
                    builder_cmd="builder",
                    reviewer_cmd="reviewer",
                    skip_if_existing=True,
                )

            self.assertEqual(rc, 0)
            self.assertFalse(generator.called)

    def test_runs_pipeline_and_returns_success_on_clean_validation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            session = {"id": "sess-ok"}
            next_payload = {"output_path": str(root / "out" / "v4"), "status": "ok"}

            with mock.patch("a2a_cli.main._auto_generate_next_version", return_value=next_payload) as generator:
                with mock.patch("a2a_cli.main._run_post_respin_validation", return_value={"status": "ok"}) as validate:
                    with mock.patch("a2a_cli.main._run_post_respin_auto_repair") as repair:
                        rc = _run_auto_respin_if_requested(
                            root,
                            session,
                            auto_respin=True,
                            builder_cmd="builder",
                            reviewer_cmd="reviewer",
                            skip_if_existing=False,
                        )

            self.assertEqual(rc, 0)
            self.assertTrue(generator.called)
            self.assertTrue(validate.called)
            self.assertFalse(repair.called)

    def test_returns_failure_when_validation_and_auto_repair_fail(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            session = {"id": "sess-fail"}
            next_payload = {"output_path": str(root / "out" / "v4"), "status": "ok"}

            with mock.patch("a2a_cli.main._auto_generate_next_version", return_value=next_payload):
                with mock.patch("a2a_cli.main._run_post_respin_validation", return_value={"status": "failed"}):
                    with mock.patch(
                        "a2a_cli.main._run_post_respin_auto_repair",
                        return_value={"status": "failed"},
                    ):
                        rc = _run_auto_respin_if_requested(
                            root,
                            session,
                            auto_respin=True,
                            builder_cmd="builder",
                            reviewer_cmd="reviewer",
                            skip_if_existing=False,
                        )

            self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
