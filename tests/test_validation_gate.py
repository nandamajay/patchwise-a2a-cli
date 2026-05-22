import tempfile
import unittest
from pathlib import Path
from unittest import mock

from a2a_cli.main import (
    _as_json,
    _detect_kernel_repo_root,
    _run_post_respin_validation,
    _resolve_gate_patch_scope,
    _resolve_gate_patch_targets,
    _run_post_respin_checkpatch,
    _run_post_respin_upstream_compat,
)


class ValidationGateTests(unittest.TestCase):
    def test_detect_kernel_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            scripts = root / "scripts"
            scripts.mkdir(parents=True, exist_ok=True)
            (scripts / "checkpatch.pl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            watch = root / "patches" / "series"
            watch.mkdir(parents=True, exist_ok=True)
            self.assertEqual(_detect_kernel_repo_root(watch), root)

    def test_resolve_gate_patch_targets_round1_uses_all_when_no_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            watch = Path(td)
            p1 = watch / "a.patch"
            p2 = watch / "sub" / "b.patch"
            p2.parent.mkdir(parents=True, exist_ok=True)
            p1.write_text("x", encoding="utf-8")
            p2.write_text("y", encoding="utf-8")
            targets = _resolve_gate_patch_targets(watch, [], round_no=1)
            self.assertEqual(sorted(targets), sorted([p1, p2]))

    def test_resolve_gate_patch_targets_round2_uses_changed_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            watch = Path(td)
            p1 = watch / "a.patch"
            p2 = watch / "b.patch"
            p1.write_text("x", encoding="utf-8")
            p2.write_text("y", encoding="utf-8")
            targets = _resolve_gate_patch_targets(watch, ["b.patch"], round_no=2)
            self.assertEqual(targets, [p2])

    def test_resolve_gate_patch_targets_round2_no_changes_uses_all(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            watch = Path(td)
            p1 = watch / "a.patch"
            p2 = watch / "nested" / "b.patch"
            p2.parent.mkdir(parents=True, exist_ok=True)
            p1.write_text("x", encoding="utf-8")
            p2.write_text("y", encoding="utf-8")
            targets = _resolve_gate_patch_targets(watch, [], round_no=2)
            self.assertEqual(sorted(targets), sorted([p1, p2]))

    def test_resolve_gate_patch_scope_changed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            watch = Path(td)
            patch = watch / "x.patch"
            patch.write_text("x", encoding="utf-8")
            scope = _resolve_gate_patch_scope(watch, ["x.patch"])
            self.assertEqual(scope, "changed")

    def test_resolve_gate_patch_scope_full_when_no_patch_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            watch = Path(td)
            patch = watch / "x.patch"
            patch.write_text("x", encoding="utf-8")
            scope = _resolve_gate_patch_scope(watch, ["notes.txt"])
            self.assertEqual(scope, "full")

    def test_post_respin_checkpatch_runs_all_non_cover_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "patches"
            output.mkdir(parents=True, exist_ok=True)
            (output / "0000-cover-letter.patch").write_text("cover", encoding="utf-8")
            (output / "0001-a.patch").write_text("a", encoding="utf-8")
            (output / "0002-b.patch").write_text("b", encoding="utf-8")
            kernel = root / "kernel"
            scripts = kernel / "scripts"
            scripts.mkdir(parents=True, exist_ok=True)
            (scripts / "checkpatch.pl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

            with mock.patch(
                "a2a_cli.main.run_shell_command",
                return_value={"returncode": 0, "stdout": "", "stderr": ""},
            ) as runner:
                payload = _run_post_respin_checkpatch(output, kernel)

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["scope"], "full")
            self.assertEqual(payload["files_checked"], 2)
            self.assertEqual(runner.call_count, 2)

    def test_post_respin_checkpatch_missing_kernel_root_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "patches"
            output.mkdir(parents=True, exist_ok=True)
            (output / "0001-a.patch").write_text("a", encoding="utf-8")
            payload = _run_post_respin_checkpatch(output, None)
            self.assertFalse(payload["ok"])
            self.assertIn("kernel tree not found", " ".join(payload["issues"]))

    def test_post_respin_upstream_compat_flags_deprecated_din_dout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "patches"
            output.mkdir(parents=True, exist_ok=True)
            patch = output / "0001-a.patch"
            patch.write_text(
                "\n".join(
                    [
                        "diff --git a/arch/arm64/boot/dts/qcom/shikra.dtsi b/arch/arm64/boot/dts/qcom/shikra.dtsi",
                        "--- a/arch/arm64/boot/dts/qcom/shikra.dtsi",
                        "+++ b/arch/arm64/boot/dts/qcom/shikra.dtsi",
                        "@@ -1,1 +1,3 @@",
                        "+\tqcom,din-ports = <3>;",
                        "+\tqcom,dout-ports = <0>;",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            payload = _run_post_respin_upstream_compat(output)
            self.assertFalse(payload["ok"])
            joined = " ".join(payload["issues"])
            self.assertIn("deprecated DT property 'qcom,din-ports'", joined)
            self.assertIn("deprecated DT property 'qcom,dout-ports'", joined)

    def test_post_respin_upstream_compat_flags_downstream_swr_props(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "patches"
            output.mkdir(parents=True, exist_ok=True)
            patch = output / "0001-a.patch"
            patch.write_text(
                "\n".join(
                    [
                        "diff --git a/arch/arm64/boot/dts/qcom/shikra.dtsi b/arch/arm64/boot/dts/qcom/shikra.dtsi",
                        "--- a/arch/arm64/boot/dts/qcom/shikra.dtsi",
                        "+++ b/arch/arm64/boot/dts/qcom/shikra.dtsi",
                        "@@ -1,1 +1,3 @@",
                        '+\tcompatible = "qcom,swr-mstr";',
                        "+\tqcom,swr-port-mapping = <1 2 3>;",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            payload = _run_post_respin_upstream_compat(output)
            self.assertFalse(payload["ok"])
            joined = " ".join(payload["issues"])
            self.assertIn("downstream-only compatible \"qcom,swr-mstr\"", joined)
            self.assertIn("downstream-only DT property 'qcom,swr-port-mapping'", joined)

    def test_post_respin_validation_uses_source_watch_path_for_kernel_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a2a = root / ".a2a"
            (a2a / "reports" / "sess-test").mkdir(parents=True, exist_ok=True)
            (a2a / "config.json").write_text(_as_json({"post_respin_checkpatch": True}), encoding="utf-8")

            kernel_root = root / "kernel"
            scripts = kernel_root / "scripts"
            scripts.mkdir(parents=True, exist_ok=True)
            (scripts / "checkpatch.pl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

            source_watch = kernel_root / ".a2a" / "lore_series" / "demo"
            source_watch.mkdir(parents=True, exist_ok=True)
            output = root / "generated" / "v2"
            output.mkdir(parents=True, exist_ok=True)
            (output / "0001-a.patch").write_text(
                "\n".join(
                    [
                        "From: Tester <tester@example.com>",
                        "Subject: [PATCH] test",
                        "",
                        "---",
                        " a.txt | 1 +",
                        " 1 file changed, 1 insertion(+)",
                        "",
                        "diff --git a/a.txt b/a.txt",
                        "new file mode 100644",
                        "index 000000000000..e69de29bb2d1",
                        "--- /dev/null",
                        "+++ b/a.txt",
                        "@@ -0,0 +1 @@",
                        "+x",
                        "-- ",
                        "2.34.1",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            session = {
                "id": "sess-test",
                "watch_path": str(output),
                "repo_path": str(kernel_root),
                "reviewer_name": "aryabhatta",
            }
            next_version_payload = {
                "output_path": str(output),
                "source_watch_path": str(source_watch),
            }

            with mock.patch("a2a_cli.main._run_post_respin_reviewer_validation", return_value={"ran": False, "ok": True, "issues": []}):
                with mock.patch("a2a_cli.main.run_shell_command", return_value={"returncode": 0, "stdout": "", "stderr": ""}) as runner:
                    payload = _run_post_respin_validation(
                        root,
                        session,
                        next_version_payload,
                        reviewer_cmd="echo reviewer",
                    )

            checkpatch = payload["checks"]["checkpatch"]
            self.assertTrue(checkpatch.get("ok"))
            self.assertEqual(checkpatch.get("kernel_root"), str(kernel_root))
            self.assertEqual(checkpatch.get("files_checked"), 1)
            self.assertEqual(runner.call_count, 1)


if __name__ == "__main__":
    unittest.main()
