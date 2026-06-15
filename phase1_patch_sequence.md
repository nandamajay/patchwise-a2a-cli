# Phase 1 Patch Sequence (Minimum Path to MERGE_APPROVED)

Goal: close blockers #1-#6 minimum subset required by `production_readiness_review.md`.

## Patch order

### Patch 01 — Config defaults and deep merge (B3)
- Files:
  - `a2a_cli/config.py`
  - `a2a_cli/main.py`
  - `tests/test_config_aura_defaults.py` (new)
- Content:
  - Add `aura_export` defaults.
  - Implement recursive merge for `_load_config(...)`.
- Rationale:
  - Unblocks all downstream features that depend on stable nested config.

### Patch 02 — Export registry loader (B1 core)
- Files:
  - `a2a_cli/aura_export_registry.py` (new)
  - `tests/test_aura_export_registry.py` (new)
- Content:
  - Manifest loading, artifact presence validation, record field validation.
- Rationale:
  - Establishes read-only trusted import path.

### Patch 03 — Safety guards + scope map (B1+B2+B5 core)
- Files:
  - `a2a_cli/aura_safety.py` (new)
  - `a2a_cli/aura_rule_engine.py` (new, mapping/util surface)
  - `tests/test_aura_safety.py` (new)
  - `tests/test_aura_scope_mapping.py` (new)
  - `tests/test_aura_freshness_confidence.py` (new)
- Content:
  - Exclusion enforcement.
  - Scope filtering via taxonomy mapping.
  - Freshness + confidence + evidence_count policy.
- Rationale:
  - Prevents unsafe/stale/mis-scoped influence.

### Patch 04 — Rule engine matching + advisory context (B1)
- Files:
  - `a2a_cli/aura_rule_engine.py`
  - `tests/test_aura_rule_engine.py` (new)
- Content:
  - Deterministic matching for patterns/rules/risk signals/playbooks.
  - Emit `risk_vector` and `advisory_context`.
- Rationale:
  - Provides deterministic signal generation before score overlay.

### Patch 05 — Capped monotonic score overlay (B4)
- Files:
  - `a2a_cli/score_engine.py`
  - `tests/test_score_engine.py`
- Content:
  - Add `apply_aura_risk_overlay(...)` with cap + monotonic safety semantics.
- Rationale:
  - Introduces bounded decision influence with regression protection.

### Patch 06 — Main-loop integration + fail-safe fallback (B1+B4)
- Files:
  - `a2a_cli/main.py`
  - `tests/test_aura_fallback_behavior.py` (new)
  - `tests/test_lgtm_decision.py` (extend)
  - `tests/test_prompt_runtime_loading.py` (extend)
- Content:
  - Session-level registry/safety/rule-engine load.
  - Safe disable-on-failure fallback.
  - Apply overlay in round decision path.
  - Advisory context injection in `_agent_env(...)`.
- Rationale:
  - Wires entire integration path while preserving baseline behavior.

### Patch 07 — Minimum telemetry for auditability (B6 minimum)
- Files:
  - `a2a_cli/main.py`
  - `tests/test_round_summary.py`
  - `tests/test_report_payload.py`
- Content:
  - Add `aura` telemetry block to round summary.
  - Persist overlay/safety fields in `score_decisions.json`.
- Rationale:
  - Required to prove bounded impact and support rollback decisions.

---

## Parallelization plan

Can be developed in parallel after Patch 01:
- Stream A: Patch 02 -> Patch 03
- Stream B: Patch 04 -> Patch 05
- Stream C: Patch 06 -> Patch 07 (starts once 02/03/05 are ready)

Critical path:
1. Patch 01
2. Patch 02 + Patch 03
3. Patch 04 + Patch 05
4. Patch 06
5. Patch 07

---

## Estimated Phase 1 effort
- Coding + tests total: 34-49 engineer-hours.
- With 1 engineer: ~1.5 weekends.
- With 2 engineers in parallel: ~1 focused weekend.
