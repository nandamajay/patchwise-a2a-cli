import json
import tempfile
import unittest
from pathlib import Path

from a2a_cli.driver_gap_analyzer import analyze_driver_gap, write_gap_analysis_reports


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class DriverGapAnalyzerTests(unittest.TestCase):
    def test_analyze_driver_gap_detects_core_differences(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            aura_root = root / "aura"
            downstream = root / "downstream"
            upstream = root / "upstream"

            # Downstream audio driver sample
            _write(
                downstream / "sound" / "soc" / "codecs" / "wsa884x.c",
                "\n".join(
                    [
                        "#include <sound/soc.h>",
                        "static int wsa884x_probe(struct platform_device *pdev)",
                        "{",
                        "\tsnd_soc_codec *codec = NULL;",
                        "\tqcom_vendor_hook_enable();",
                        "\tmissing_downstream_api(codec);",
                        "\tpm_runtime_get_sync(&pdev->dev);",
                        "\treturn 0;",
                        "}",
                    ]
                )
                + "\n",
            )
            _write(
                downstream / "sound" / "soc" / "codecs" / "Kconfig",
                "\n".join(
                    [
                        "config SND_SOC_WSA884X",
                        "\ttristate \"WSA884x codec\"",
                        "\tdepends on SND_SOC",
                    ]
                )
                + "\n",
            )
            _write(
                downstream / "sound" / "soc" / "codecs" / "Makefile",
                "obj-$(CONFIG_SND_SOC_WSA884X) += wsa884x.o\n",
            )
            _write(
                downstream
                / "Documentation"
                / "devicetree"
                / "bindings"
                / "sound"
                / "qcom,wsa884x.yaml",
                "\n".join(
                    [
                        "properties:",
                        "  compatible:",
                        "    const: qcom,wsa884x",
                        "  qcom,swr-port-mapping:",
                        "    $ref: /schemas/types.yaml#/definitions/uint32-array",
                    ]
                )
                + "\n",
            )

            # Upstream sample is missing some downstream-specific content
            _write(
                upstream / "sound" / "soc" / "codecs" / "wsa884x.c",
                "\n".join(
                    [
                        "#include <sound/soc.h>",
                        "static int wsa884x_probe(struct platform_device *pdev)",
                        "{",
                        "\tdevm_snd_soc_register_component(&pdev->dev, NULL, NULL, 0);",
                        "\treturn 0;",
                        "}",
                    ]
                )
                + "\n",
            )
            _write(
                upstream / "sound" / "soc" / "codecs" / "Kconfig",
                "config SND_SOC_WSA883X\n\ttristate \"WSA883x codec\"\n",
            )
            _write(
                upstream / "sound" / "soc" / "codecs" / "Makefile",
                "obj-$(CONFIG_SND_SOC_WSA883X) += wsa883x.o\n",
            )
            _write(
                upstream
                / "Documentation"
                / "devicetree"
                / "bindings"
                / "sound"
                / "qcom,wsa884x.yaml",
                "\n".join(
                    [
                        "properties:",
                        "  compatible:",
                        "    const: qcom,wsa884x",
                    ]
                )
                + "\n",
            )

            result = analyze_driver_gap(
                downstream_root=downstream,
                upstream_root=upstream,
                subsystem="audio",
                aura_root=aura_root,
                driver_name="wsa884x",
            )

            self.assertEqual(result["analyzer"], "Driver_Gap_Analyzer_V1")
            self.assertEqual(result["driver_name"], "wsa884x")
            self.assertGreaterEqual(result["missing_upstream_interfaces"]["count"], 1)
            self.assertGreaterEqual(result["deprecated_downstream_apis"]["count"], 1)
            self.assertGreaterEqual(result["vendor_hook_inventory"]["count"], 1)
            self.assertIn(
                "SND_SOC_WSA884X",
                set(result["kconfig_differences"]["downstream_only_symbols"]),
            )
            self.assertIn(
                "wsa884x.o",
                set(result["makefile_differences"]["downstream_only_objects"]),
            )
            self.assertGreaterEqual(len(result["patch_sequence"]), 2)
            self.assertIn(result["risk_assessment"]["overall"], {"low", "medium", "high"})

    def test_write_gap_analysis_reports_emits_required_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            downstream = root / "downstream"
            upstream = root / "upstream"
            aura_root = root / "aura"
            _write(downstream / "sound" / "soc" / "codecs" / "demo.c", "int demo(void) { return 0; }\n")
            _write(upstream / "sound" / "soc" / "codecs" / "demo.c", "int demo(void) { return 0; }\n")

            result = analyze_driver_gap(
                downstream_root=downstream,
                upstream_root=upstream,
                subsystem="audio",
                aura_root=aura_root,
                driver_name="demo",
            )

            out = root / "reports"
            files = write_gap_analysis_reports(result, out)

            required = {
                "api_gap_report",
                "missing_upstream_interfaces",
                "deprecated_downstream_apis",
                "vendor_hook_inventory",
                "device_tree_differences",
                "kconfig_differences",
                "makefile_differences",
                "architecture_differences",
                "dependency_graph",
                "upstreaming_roadmap",
                "patch_sequence",
                "risk_assessment",
                "architecture_document",
                "implementation_plan",
                "mvp_scope",
                "first_executable_milestone",
                "index",
            }
            self.assertTrue(required.issubset(set(files.keys())))
            for key in required:
                self.assertTrue(Path(files[key]).exists(), msg=f"missing file for {key}")

            payload = json.loads(Path(files["driver_gap_analysis"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["driver_name"], "demo")


if __name__ == "__main__":
    unittest.main()
