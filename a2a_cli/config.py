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
        "score_thresholds": {
            "low_builder_confidence": 40,
            "low_reviewer_confidence": 60,
            "high_confidence_lgtm": 90,
            "volatility_swing": 30,
            "zero_patch_gauge": 0,
        },
        "upstream_evidence": {
            "kernel_tree": "",
            "strict_mode": True,
            "block_on_no_evidence": True,
            "elixir_base": "https://elixir.bootlin.com/linux/latest",
        },
        "static_analysis": {
            "sparse": True,
            "coccinelle": True,
            "block_on_sparse": True,
            "block_on_coccinelle": False,
            "smatch": False,
        },
        "submission": {
            "dry_run": True,
            "dry_run_recipient": "nandam@qti.qualcomm.com",
            "allow_community_send": False,
            "community_to": [],
            "community_cc": [],
            "hitl_timeout_secs": 300,
        },
        "respin": {
            "conflict_strategy": "abort",
            "keep_temp_branch": False,
            "auto_increment_version": True,
        },
        "prior_review_gate": True,
        "prior_review_search": True,
        "prior_review_max_comments": 120,
        "reviewer_consistency_guard": True,
        "full_subsystem_review_required": True,
        "lore_fetch_dir": "",
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
