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
        "validation_gate_fail_on_warnings": False,
        "validation_gate_fail_on_checks": False,
        "validation_gate_timeout_sec": 300,
        "validation_gate_max_checkpatch_files": 50,
        "validation_gate_command": None,
        "lgtm_full_series_checkpatch": True,
        "lgtm_checkpatch_fail_on_warnings": False,
        "lgtm_checkpatch_fail_on_checks": False,
        "findings_gate": {
            "mode": "quality",  # quality|strict
            "always_block_prior_comments": True,
            "persistence_rounds": {
                "critical": 1,
                "high": 2,
                "medium": 2,
                "low": 3,
            },
        },
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
            "fail_on_missing_tools": True,
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
        "post_respin_run_reviewer": True,
        "post_respin_applyability": True,
        "post_respin_trailer_hygiene": True,
        "post_respin_suggested_by_max_delta_lines": 120,
        "post_respin_checkpatch": True,
        "post_respin_upstream_compat": True,
        "post_respin_max_checkpatch_files": 100,
        "post_respin_auto_repair": True,
        "post_respin_repair_max_rounds": 5,
        "post_respin_repair_max_total_rounds": 15,
        "post_respin_repair_max_no_progress": 2,
        "post_respin_repair_max_rate_limit": 2,
        "prior_review_gate": True,
        "prior_review_search": True,
        "prior_review_refresh_each_round": True,
        "prior_review_max_comments": 120,
        "sashiko_ingest": True,
        "sashiko_base_url": "https://sashiko.dev",
        "surgical_review_parity_mode": True,
        "surgical_scan_max_findings": 80,
        "reviewer_consistency_guard": True,
        "full_subsystem_review_required": True,
        "lore_fetch_dir": "",
        "email_bridge": {
            "poll_sec": 60,
            "imap_host": "",
            "imap_port": 993,
            "imap_user": "",
            "imap_password_env": "A2A_EMAIL_IMAP_PASSWORD",
            "mailbox": "INBOX",
            "smtp_from": "",
            "allowed_senders": [],
            "notify_to": [],
            "inbox_dir": "",
            "state_db": "",
            "approval_token_ttl_min": 720,
            "auto_detect_requests": False,
            "lore_fetch_dir": "",
        },
        "aura_export": {
            "enabled": False,
            "path": "",
            "scope_allowlist": [],
            "subsystem_map": {},
            "max_score_influence": 0.15,
            "maintainer_alignment_mode": "advisory",
            "confidence_floor": "MEDIUM",
            "freshness_days": 30,
        },
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
