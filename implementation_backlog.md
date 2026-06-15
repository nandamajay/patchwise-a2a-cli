# Implementation Backlog (Blockers #1-#6)

Assumptions for estimates:
- 1 engineer, familiar with A2A_CLI codebase.
- Includes coding + unit tests + local verification.
- LOC = net added/modified code + tests.

## Blocker #1 — Missing runtime safety controls/modules
Priority: MUST_HAVE

### B1-T1: Export registry loader + schema validation
- Files:
  - `a2a_cli/aura_export_registry.py` (new)
  - `a2a_cli/config.py` (read defaults)
- Tasks:
  - Load bundle path and `export_manifest.json`.
  - Validate declared artifact presence.
  - Validate required record fields.
  - Return structured payload (`manifest`, `artifacts`, `errors`, `warnings`).
- Estimated LOC: 220-320
- Time: 5-7 hours
- Dependencies: none
- Acceptance tests:
  - `tests/test_aura_export_registry.py` (new)

### B1-T2: Safety guard engine
- Files:
  - `a2a_cli/aura_safety.py` (new)
- Tasks:
  - Enforce exclusion list (`reviewer_behavior_model`, `reviewer_profiles`, `discovered_rule_catalog`).
  - Enforce scope and schema guard outcomes.
  - Emit guard-trip events for telemetry.
- Estimated LOC: 160-240
- Time: 4-6 hours
- Dependencies: B1-T1
- Acceptance tests:
  - `tests/test_aura_safety.py` (new)

### B1-T3: Rule engine skeleton and matching surface
- Files:
  - `a2a_cli/aura_rule_engine.py` (new)
- Tasks:
  - Build deterministic matcher interface for patterns/rules/risk signals/playbooks.
  - Emit `risk_vector` + `advisory_context`.
- Estimated LOC: 180-260
- Time: 4-6 hours
- Dependencies: B1-T1
- Acceptance tests:
  - `tests/test_aura_rule_engine.py` (new)

### B1-T4: Main-loop integration with fail-safe fallback
- Files:
  - `a2a_cli/main.py`
- Tasks:
  - Load AURA export once per session.
  - Disable AURA on any load/safety failure and continue baseline flow.
  - Inject advisory context in `_agent_env(...)` only when safe.
- Estimated LOC: 180-260
- Time: 5-7 hours
- Dependencies: B1-T1, B1-T2, B1-T3
- Acceptance tests:
  - `tests/test_aura_fallback_behavior.py` (new)
  - Update `tests/test_prompt_runtime_loading.py`

---

## Blocker #2 — Subsystem scope mismatch
Priority: MUST_HAVE

### B2-T1: Canonical subsystem mapping
- Files:
  - `a2a_cli/aura_rule_engine.py`
  - `a2a_cli/config.py`
- Tasks:
  - Add map from runtime subsystem labels -> export taxonomy.
  - Add `cross_subsystem` handling policy.
- Estimated LOC: 90-140
- Time: 2-3 hours
- Dependencies: B1-T3
- Acceptance tests:
  - `tests/test_aura_scope_mapping.py` (new)

### B2-T2: Scope enforcement in safety layer
- Files:
  - `a2a_cli/aura_safety.py`
- Tasks:
  - Filter non-matching records out of score-influence path.
  - Preserve advisory-only visibility where allowed.
- Estimated LOC: 70-120
- Time: 2-3 hours
- Dependencies: B1-T2, B2-T1
- Acceptance tests:
  - Extend `tests/test_aura_safety.py`

---

## Blocker #3 — Config evolution risk
Priority: MUST_HAVE

### B3-T1: Add AURA config defaults
- Files:
  - `a2a_cli/config.py`
- Tasks:
  - Add nested `aura_export` defaults:
    - `enabled`, `path`, `scope_allowlist`, `subsystem_map`, `max_score_influence`, `maintainer_alignment_mode`, `confidence_floor`, `freshness_days`.
- Estimated LOC: 40-70
- Time: 1-1.5 hours
- Dependencies: none
- Acceptance tests:
  - `tests/test_config_aura_defaults.py` (new)

### B3-T2: Recursive config defaulting
- Files:
  - `a2a_cli/main.py`
- Tasks:
  - Replace top-level-only merge in `_load_config(...)` with recursive deep-merge for missing keys.
- Estimated LOC: 40-80
- Time: 1.5-2.5 hours
- Dependencies: none
- Acceptance tests:
  - Extend `tests/test_prompt_runtime_loading.py` or new `tests/test_config_aura_defaults.py`

---

## Blocker #4 — Score-engine regression risk
Priority: MUST_HAVE

### B4-T1: Add capped, monotonic AURA overlay API
- Files:
  - `a2a_cli/score_engine.py`
- Tasks:
  - Implement `apply_aura_risk_overlay(decision, risk_vector, cap)`.
  - Ensure no baseline safety relaxation.
  - Attach overlay telemetry fields.
- Estimated LOC: 110-170
- Time: 3-4 hours
- Dependencies: B1-T3, B3-T1
- Acceptance tests:
  - Extend `tests/test_score_engine.py`

### B4-T2: Wire overlay into round decision flow
- Files:
  - `a2a_cli/main.py`
- Tasks:
  - Apply overlay after `evaluate_round_scores(...)` and before persistence.
  - Pass capped influence from config.
- Estimated LOC: 50-90
- Time: 1.5-2.5 hours
- Dependencies: B4-T1, B1-T4
- Acceptance tests:
  - Extend `tests/test_lgtm_decision.py`

---

## Blocker #5 — Freshness/confidence misuse
Priority: MUST_HAVE

### B5-T1: Freshness gate
- Files:
  - `a2a_cli/aura_safety.py`
- Tasks:
  - Parse `last_seen` and reject stale score-influence records by policy window.
- Estimated LOC: 60-100
- Time: 1.5-2.5 hours
- Dependencies: B1-T2, B3-T1
- Acceptance tests:
  - `tests/test_aura_freshness_confidence.py` (new)

### B5-T2: Confidence/evidence floor
- Files:
  - `a2a_cli/aura_safety.py`
  - `a2a_cli/aura_rule_engine.py`
- Tasks:
  - Enforce confidence floor for scoring.
  - Force `review_risk_signals` with `evidence_count<2` to advisory-only.
- Estimated LOC: 70-120
- Time: 2-3 hours
- Dependencies: B1-T2, B1-T3, B3-T1
- Acceptance tests:
  - Extend `tests/test_aura_freshness_confidence.py`

---

## Blocker #6 — Telemetry/causality gaps
Priority: SHOULD_HAVE (minimum subset for merge: MUST)

### B6-T1: Round summary telemetry
- Files:
  - `a2a_cli/main.py`
  - `tests/test_round_summary.py`
- Tasks:
  - Add `aura` section to round summary JSON/MD with load, matching, guard, overlay fields.
- Estimated LOC: 90-140
- Time: 2.5-3.5 hours
- Dependencies: B1-T4, B4-T2, B5-T2
- Acceptance tests:
  - Extend `tests/test_round_summary.py`

### B6-T2: Score decision telemetry and report surfacing
- Files:
  - `a2a_cli/main.py`
  - `a2a_cli/score_engine.py`
  - `tests/test_report_payload.py`
- Tasks:
  - Persist overlay and guard outcome fields in `score_decisions.json`.
  - Show AURA health/version in report/status payload.
- Estimated LOC: 80-130
- Time: 2.5-3.5 hours
- Dependencies: B6-T1
- Acceptance tests:
  - Extend `tests/test_report_payload.py`

---

## Independent tasks that can run in parallel

Parallel lane A (Config + Loader foundations):
- B3-T1, B3-T2, B1-T1

Parallel lane B (Safety + mapping):
- B1-T2, B2-T1, B2-T2, B5-T1

Parallel lane C (Rule + scoring):
- B1-T3, B4-T1, B5-T2

Parallel lane D (Main integration + telemetry):
- B1-T4, B4-T2, B6-T1, B6-T2

Recommended split for 2 engineers:
- Engineer A: lanes A+B
- Engineer B: lanes C+D

---

## Highest risk reduction per hour

Highest risk-reduction/hour: **Blocker #3 (Config evolution risk)**.
- Why: very low effort, removes a class of silent misconfiguration failures that would invalidate all other blocker fixes.
- Estimated impact: medium-high risk reduction with <4 hours total.
