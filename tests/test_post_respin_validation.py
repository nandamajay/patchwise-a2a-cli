import tempfile
import unittest
from unittest import mock
from pathlib import Path

from a2a_cli.main import (
    _collect_post_respin_repair_findings,
    _extract_findings_from_agent_output,
    _is_infra_applyability_failure,
    _reviewer_verdict_from_text,
    _run_post_respin_auto_repair,
    _validate_cover_changelog_quality,
    _validate_patchset_artifact_coherence,
    _validate_respin_delta,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class PostRespinValidationTests(unittest.TestCase):
    def test_extract_findings_from_noisy_output(self) -> None:
        noisy = (
            "progress line\n"
            '{"findings":[{"severity":"low","title":"x","location":"a.patch:1","evidence":["e"],'
            '"required_action":"none","status":"closed","source_comment_id":"id-1"}]}\n'
            "tail"
        )
        findings = _extract_findings_from_agent_output(noisy)
        self.assertIsInstance(findings, list)
        self.assertEqual(len(findings or []), 1)

    def test_patchset_artifact_coherence_detects_subject_total_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            patch_dir = out / "demo.patches"
            _write(
                patch_dir / "0000-cover-letter.patch",
                "Subject: [PATCH v3 0/2] demo\n\n---\n",
            )
            _write(
                patch_dir / "0001-a.patch",
                "Subject: [PATCH v3 1/2] a\n\n---\ndiff --git a/a.c b/a.c\n",
            )
            _write(
                patch_dir / "0002-b.patch",
                "Subject: [PATCH v3 2/3] b\n\n---\ndiff --git a/b.c b/b.c\n",
            )
            _write(
                patch_dir / "series",
                "0000-cover-letter.patch\n0001-a.patch\n0002-b.patch\n",
            )
            _write(out / "demo.cover", "Subject: [PATCH v3 0/2] demo\n")
            _write(
                out / "demo.mbx",
                "Subject: [PATCH v3 1/2] a\n\n---\n\nSubject: [PATCH v3 2/2] b\n",
            )

            issues = _validate_patchset_artifact_coherence(out)
            self.assertTrue(any("0002-b.patch" in issue for issue in issues))

    def test_patchset_artifact_coherence_accepts_mbx_with_cover_row(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            patch_dir = out / "demo.patches"
            _write(
                patch_dir / "0000-cover-letter.patch",
                "Subject: [PATCH v3 0/2] demo\n\n---\n",
            )
            _write(
                patch_dir / "0001-a.patch",
                "Subject: [PATCH v3 1/2] a\n\n---\ndiff --git a/a.c b/a.c\n",
            )
            _write(
                patch_dir / "0002-b.patch",
                "Subject: [PATCH v3 2/2] b\n\n---\ndiff --git a/b.c b/b.c\n",
            )
            _write(
                patch_dir / "series",
                "0000-cover-letter.patch\n0001-a.patch\n0002-b.patch\n",
            )
            _write(out / "demo.cover", "Subject: [PATCH v3 0/2] demo\n")
            _write(
                out / "demo.mbx",
                "\n".join(
                    [
                        "Subject: [PATCH v3 0/2] demo",
                        "",
                        "Subject: [PATCH v3 1/2] a",
                        "",
                        "Subject: [PATCH v3 2/2] b",
                        "",
                    ]
                ),
            )

            issues = _validate_patchset_artifact_coherence(out)
            self.assertEqual([], issues)

    def test_patchset_artifact_coherence_rejects_mixed_versioned_families(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            v5_dir = out / "v5_demo.patches"
            v6_dir = out / "v6_demo.patches"
            _write(v5_dir / "0001-a.patch", "Subject: [PATCH v5 1/1] a\n\n---\n")
            _write(v5_dir / "series", "0001-a.patch\n")
            _write(v6_dir / "0001-b.patch", "Subject: [PATCH v6 1/1] b\n\n---\n")
            _write(v6_dir / "series", "0001-b.patch\n")
            _write(out / "v5_demo.cover", "Subject: [PATCH v5 0/1] v5\n")
            _write(out / "v6_demo.cover", "Subject: [PATCH v6 0/1] v6\n")

            issues = _validate_patchset_artifact_coherence(out)
            self.assertTrue(any("multiple versioned patchset families detected" in issue for issue in issues))

    def test_cover_changelog_quality_detects_tool_meta_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            _write(
                out / "0000-cover-letter.patch",
                "\n".join(
                    [
                        "Subject: [PATCH v3 0/1] demo",
                        "",
                        "---",
                        "Changes since v2:",
                        "- Automated respin update generated by A2A.",
                        "",
                    ]
                )
                + "\n",
            )
            issues = _validate_cover_changelog_quality(out)
            self.assertTrue(any("non-technical/tool-meta" in issue for issue in issues))

    def test_respin_delta_detects_touched_file_drift(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "src"
            out = root / "out"
            _write(
                src / "0001-a.patch",
                "Subject: [PATCH v2 1/1] demo\n\n---\ndiff --git a/a.c b/a.c\n",
            )
            _write(
                out / "0001-a.patch",
                "Subject: [PATCH v3 1/1] demo\n\n---\ndiff --git a/b.c b/b.c\n",
            )
            issues = _validate_respin_delta(src, out)
            self.assertTrue(any("touched-file drift" in issue for issue in issues))

    def test_respin_delta_uses_requested_patchset_family(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "src"
            out = root / "out"

            _write(src / "v5_demo.patches" / "0001-old.patch", "Subject: [PATCH v5 1/1] old\n\n---\n")
            _write(src / "v5_demo.patches" / "series", "0001-old.patch\n")
            _write(src / "v6_demo.patches" / "0001-new.patch", "Subject: [PATCH v6 1/1] demo\n\n---\ndiff --git a/a.c b/a.c\n")
            _write(src / "v6_demo.patches" / "series", "0001-new.patch\n")

            _write(out / "v5_demo.patches" / "0001-old.patch", "Subject: [PATCH v7 1/1] old\n\n---\n")
            _write(out / "v5_demo.patches" / "series", "0001-old.patch\n")
            _write(out / "v6_demo.patches" / "0001-new.patch", "Subject: [PATCH v7 1/1] demo\n\n---\ndiff --git a/a.c b/a.c\n")
            _write(out / "v6_demo.patches" / "series", "0001-new.patch\n")

            issues = _validate_respin_delta(src, out, source_patchset_name="v6_demo")
            self.assertEqual([], issues)

    def test_collect_post_respin_repair_findings_rebuilds_post_respin_check_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            report_dir = Path(td)
            _write(
                report_dir / "post-respin-findings.json",
                (
                    '{"findings":['
                    '{"severity":"medium","title":"old applyability open","location":"applyability:1",'
                    '"evidence":["old"],"required_action":"old","status":"open",'
                    '"source_comment_id":"post-respin:applyability"},'
                    '{"severity":"low","title":"unrelated finding","location":"demo.patch:1",'
                    '"evidence":["x"],"required_action":"fix","status":"open",'
                    '"source_comment_id":"subsys-scan:demo"}'
                    "]}\n"
                ),
            )

            validation_payload = {
                "checks": {
                    "reviewer_validation": {"ok": True, "issues": []},
                    "applyability": {"ran": True, "ok": True, "issues": []},
                    "delta_guard": {"ok": False, "issues": ["subject drift"]},
                }
            }

            findings = _collect_post_respin_repair_findings(report_dir, validation_payload)
            by_id = {
                str(row.get("source_comment_id")): str(row.get("status"))
                for row in findings
                if isinstance(row, dict)
            }

            self.assertEqual(by_id.get("subsys-scan:demo"), "open")
            self.assertEqual(by_id.get("post-respin:applyability"), "closed")
            self.assertEqual(by_id.get("post-respin:delta_guard"), "open")
            self.assertEqual(
                len([row for row in findings if str(row.get("source_comment_id")) == "post-respin:applyability"]),
                1,
            )

    def test_reviewer_verdict_parser_uses_explicit_verdict_section(self) -> None:
        text = (
            "# Round 2: Aryabhatta Review\n\n"
            "## Findings\n"
            "- This line says LGTM in prose but is not the verdict.\n\n"
            "## Verdict\n"
            "- pending\n"
        )
        self.assertEqual(_reviewer_verdict_from_text(text), "REJECT")

    def test_is_infra_applyability_failure_detects_worktree_write_failure(self) -> None:
        payload = {
            "checks": {
                "applyability": {
                    "ok": False,
                    "issues": [
                        "unable to create temporary apply-check worktree: "
                        "error: unable to write file drivers/gpu/drm/amd/demo.h"
                    ],
                    "results": [{"stage": "worktree_add", "ok": False, "returncode": 128}],
                }
            }
        }
        self.assertTrue(_is_infra_applyability_failure(payload))

    def test_is_infra_applyability_failure_rejects_patch_content_failure(self) -> None:
        payload = {
            "checks": {
                "applyability": {
                    "ok": False,
                    "issues": [
                        "generated patch series does not apply cleanly with git am on baseline HEAD: "
                        "error: patch does not apply"
                    ],
                    "results": [{"stage": "git_am", "ok": False, "returncode": 1}],
                }
            }
        }
        self.assertFalse(_is_infra_applyability_failure(payload))

    def test_post_respin_auto_repair_short_circuits_on_infra_applyability_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sid = "sess-infra-applyability"
            output = root / "generated" / "v2"
            output.mkdir(parents=True, exist_ok=True)

            session = {"id": sid, "reviewer_name": "aryabhatta"}
            next_version_payload = {"output_path": str(output)}
            initial_validation = {
                "status": "failed",
                "checks": {
                    "applyability": {
                        "ok": False,
                        "issues": [
                            "unable to create temporary apply-check worktree: "
                            "error: unable to write file drivers/gpu/drm/amd/demo.h"
                        ],
                        "results": [{"stage": "worktree_add", "ok": False, "returncode": 128}],
                    }
                },
                "issues": ["applyability: failed"],
            }

            with mock.patch("a2a_cli.main._run_agent_step", side_effect=AssertionError("must not run")):
                payload = _run_post_respin_auto_repair(
                    root,
                    session,
                    next_version_payload,
                    builder_cmd="builder",
                    reviewer_cmd="reviewer",
                    initial_validation_payload=initial_validation,
                )

            self.assertEqual(payload.get("status"), "failed")
            auto = payload.get("auto_repair", {})
            self.assertEqual(auto.get("result"), "infra_blocked")
            self.assertEqual(auto.get("attempts"), 0)
            self.assertEqual(auto.get("blocked_check"), "applyability")
            self.assertTrue(any("infrastructure-level" in str(issue) for issue in payload.get("issues", [])))


if __name__ == "__main__":
    unittest.main()
