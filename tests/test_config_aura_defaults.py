import json
import tempfile
import unittest
from pathlib import Path

from a2a_cli.config import default_config
from a2a_cli.main import _load_config


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class AuraConfigDefaultingTests(unittest.TestCase):
    def test_default_config_contains_aura_export_defaults(self) -> None:
        cfg = default_config()
        aura = cfg.get("aura_export")
        self.assertIsInstance(aura, dict)
        self.assertFalse(bool(aura.get("enabled")))

    def test_load_config_deep_fills_missing_aura_nested_keys(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_json(root / ".a2a" / "config.json", {"aura_export": {"enabled": True}})

            loaded = _load_config(root)
            aura = loaded.get("aura_export", {})

            self.assertEqual(aura.get("enabled"), True)
            self.assertEqual(aura.get("path"), "")
            self.assertEqual(aura.get("scope_allowlist"), [])
            self.assertEqual(aura.get("subsystem_map"), {})
            self.assertEqual(aura.get("confidence_floor"), "MEDIUM")

    def test_load_config_does_not_override_existing_values(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_json(
                root / ".a2a" / "config.json",
                {
                    "score_thresholds": {
                        "low_builder_confidence": 25,
                    },
                    "aura_export": {
                        "enabled": True,
                        "confidence_floor": "HIGH",
                    },
                },
            )

            loaded = _load_config(root)
            self.assertEqual(loaded.get("score_thresholds", {}).get("low_builder_confidence"), 25)
            self.assertEqual(loaded.get("aura_export", {}).get("enabled"), True)
            self.assertEqual(loaded.get("aura_export", {}).get("confidence_floor"), "HIGH")

    def test_load_config_keeps_non_dict_user_value(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_json(root / ".a2a" / "config.json", {"aura_export": "off"})

            loaded = _load_config(root)
            self.assertEqual(loaded.get("aura_export"), "off")

    def test_aura_disabled_default_preserves_inactive_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_json(root / ".a2a" / "config.json", {"version": 1})

            loaded = _load_config(root)
            aura = loaded.get("aura_export")

            self.assertIsInstance(aura, dict)
            self.assertEqual(aura.get("enabled"), False)


if __name__ == "__main__":
    unittest.main()
