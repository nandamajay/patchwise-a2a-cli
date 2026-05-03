import json
from datetime import datetime, timezone
from pathlib import Path


A2A_DIRNAME = ".a2a"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_config() -> dict:
    return {
        "version": 1,
        "builder_display_name": "chanakya",
        "reviewer_display_name": "aryabhatta",
        "reviewer_name": "aryabhatta",
        "strict_evidence": True,
        "llm_native_default": True,
        "llm_native_strict": True,
        "llm_native_fallback": False,
        "llm_native_timeout_sec": 900,
        "validation_gate_enabled": True,
        "validation_gate_strict": False,
        "validation_gate_checkpatch": True,
        "validation_gate_timeout_sec": 300,
        "validation_gate_max_checkpatch_files": 50,
        "validation_gate_command": None,
        "respin": {
            "conflict_strategy": "abort",
            "keep_temp_branch": False,
            "auto_increment_version": True,
        },
        "prior_review_gate": True,
        "prior_review_search": True,
        "prior_review_max_comments": 120,
        "builder_command": None,
        "reviewer_command": None,
        "default_max_rounds": 6,
        "created_at": utc_now(),
    }


def default_state() -> dict:
    return {
        "version": 1,
        "active_session_id": None,
        "last_updated": utc_now(),
    }


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
