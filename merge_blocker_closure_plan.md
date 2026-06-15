# Merge Blocker Closure Plan (AURA Export_V1 -> A2A_CLI)

## Inputs
- `/local/mnt/workspace/A2A_CLI/production_readiness_review.md`
- `/local/mnt/workspace/patchwise-a2a-cli/aura_integration_audit/integration_plan.md`
- `/local/mnt/workspace/patchwise-a2a-cli/aura_integration_audit/export_v1_bundle/*`
- Current runtime code in `/local/mnt/workspace/A2A_CLI/a2a_cli`

## Blocker Matrix

| ID | Blocking Item | Priority | Effort | Dependencies |
|---|---|---|---|---|
| B1 | Missing runtime safety controls/modules | MUST_HAVE | 1.5-2.0 days | none |
| B2 | Subsystem scope mismatch | MUST_HAVE | 0.5 day | B1 (registry/rule engine hooks) |
| B3 | Config evolution risk (no nested merge) | MUST_HAVE | 0.25 day | none |
| B4 | Score-engine regression risk (overlay impact) | MUST_HAVE | 0.75 day | B1 |
| B5 | Freshness/confidence policy not enforced | MUST_HAVE | 0.5-0.75 day | B1 |
| B6 | AURA telemetry/causality gaps | SHOULD_HAVE (minimum subset is MUST) | 0.5 day | B1, B4 |

## B1 — Runtime safety controls

### Exact code changes required
1. Add new module: `a2a_cli/aura_export_registry.py`
- Load `export_manifest.json` from configured bundle path.
- Validate presence of required artifacts listed in manifest.
- Validate required record fields (`source`, `confidence`, `subsystem`, `evidence_count`, `last_seen`, `version`).
- Return typed in-memory structure: `manifest`, `records_by_artifact`, `load_errors`.

2. Add new module: `a2a_cli/aura_safety.py`
- Hard safety checks:
  - Reject excluded artifacts if present (`reviewer_behavior_model`, `reviewer_profiles`, `discovered_rule_catalog`).
  - Validate scope allowlist intersection.
  - Validate staleness and confidence policy (see B5).
- Emit structured guard events: `scope_leakage`, `schema_invalid`, `stale_data`, `excluded_artifact_detected`.

3. Add new module: `a2a_cli/aura_rule_engine.py`
- Deterministic matchers over accepted/rejected patterns, subsystem rules, risk signals, maintainer playbooks.
- Produce `risk_vector` and `advisory_context` outputs.

4. Integrate into `a2a_cli/main.py`
- On session/round execution path, attempt bundle load once.
- If load/validation fails: disable AURA for session and continue baseline.
- In `_agent_env(...)`, inject advisory context only when enabled and safe.
- In round flow before LGTM decision, call rule engine to get risk vector.

### Acceptance tests
- `tests/test_aura_export_registry.py`
  - valid bundle loads
  - missing artifact fails safely
  - missing required fields rejected
- `tests/test_aura_safety.py`
  - excluded artifact triggers disable
  - invalid schema triggers session-level fallback
- `tests/test_aura_fallback_behavior.py`
  - absent bundle does not change baseline run behavior

---

## B2 — Subsystem scope mismatch

### Exact code changes required
1. Add canonical mapping function in `a2a_cli/aura_rule_engine.py` (or `aura_safety.py`):
- Runtime inferred values (`pinctrl|codec|codecs|lpass|asoc|sound|unknown`) -> export taxonomy:
  - `sound|asoc|codec|codecs|lpass` -> `ASoC`, `ALSA`, `SoundWire`, `runtime_pm`, `DT-audio` (policy-configured mapping table)
  - `cross_subsystem` records always admissible as advisory

2. Add config mapping/allowlist in `a2a_cli/config.py` defaults:
- `aura_export.scope_allowlist`
- `aura_export.subsystem_map`

3. Add enforcement in `aura_safety.py`:
- drop non-matching records from scoring path
- keep audit counts for scope misses

### Acceptance tests
- `tests/test_aura_scope_mapping.py`
  - each runtime subsystem maps deterministically
  - unknown subsystem yields advisory-only/no-score influence
  - `cross_subsystem` stays advisory unless explicitly allowed

---

## B3 — Config evolution risk

### Exact code changes required
1. Update `a2a_cli/config.py`
- Extend `default_config()` with nested `aura_export` block:
  - `enabled`
  - `path`
  - `scope_allowlist`
  - `max_score_influence`
  - `maintainer_alignment_mode`
  - `confidence_floor`
  - `freshness_days`

2. Update `a2a_cli/main.py::_load_config(...)`
- Replace shallow top-level backfill with recursive/deep merge defaulting.
- Preserve existing user keys while adding only missing nested keys.

### Acceptance tests
- `tests/test_config_aura_defaults.py`
  - partial nested config receives missing subkeys
  - existing subkeys remain unchanged

---

## B4 — Score-engine regression risk

### Exact code changes required
1. Add API in `a2a_cli/score_engine.py`:
- `apply_aura_risk_overlay(decision, risk_vector, cap=0.15)`

2. Overlay safety semantics:
- Monotonic behavior: overlay may increase scrutiny/blocking, never relax existing baseline block conditions.
- Enforce capped influence (`max_score_influence`).
- Persist telemetry in decision object:
  - `aura_overlay_applied`
  - `aura_overlay_delta`
  - `aura_overlay_cap_hit`

3. Hook call site in `a2a_cli/main.py` after `evaluate_round_scores(...)` and before write-out.

### Acceptance tests
- Extend `tests/test_score_engine.py`
  - cap respected under extreme risk vectors
  - open-findings baseline block cannot be overridden
  - overlay cannot force LGTM when baseline blocks
  - cap-hit flag emitted

---

## B5 — Freshness/confidence policy enforcement

### Exact code changes required
1. In `aura_safety.py` / registry loader:
- Parse `last_seen` and enforce `freshness_days` threshold (configurable).
- Enforce confidence floor for score influence:
  - `HIGH`, `MEDIUM_HIGH`, `MEDIUM` allowed for weighted scoring
  - lower confidence advisory-only
- Additional guard for low-evidence risk signals:
  - For `review_risk_signals`, `evidence_count < 2` => advisory-only (no score delta)

2. In `main.py` runtime summary:
- Record filtered counts by reason: stale/confidence/evidence_count.

### Acceptance tests
- `tests/test_aura_freshness_confidence.py`
  - stale records filtered
  - low-confidence records advisory-only
  - low-evidence risk signal does not affect score

---

## B6 — Telemetry/causality gaps

### Exact code changes required
1. Extend round summary in `a2a_cli/main.py::_build_round_runtime_summary(...)`:
- Add `aura` section:
  - `enabled`
  - `bundle_version`
  - `load_success`
  - `matched_records`
  - `scope_miss_count`
  - `guard_trips`
  - `overlay_delta`
  - `overlay_cap_hit`

2. Extend `score_decisions.json` rows with AURA fields from overlay.

3. Add report/status surfacing:
- `cmd_report` and `cmd_status` include integration health line and artifact version.

### Acceptance tests
- Extend `tests/test_round_summary.py`
  - `aura` section present when enabled
  - correct telemetry values serialized
- Add `tests/test_report_payload.py` case
  - HTML/JSON report includes aura integration health

---

## Dependencies and execution order

1. B3 (config deep merge + defaults)
2. B1 (registry/safety/rule-engine skeleton + fallback)
3. B2 (scope mapping + scope guard)
4. B5 (freshness/confidence/evidence policy)
5. B4 (score overlay with cap + monotonic safety)
6. B6 (telemetry/reporting)

## Phase plan

## Phase 1 (minimum work required to reach MERGE_APPROVED)
- Complete B1, B2, B3, B4, B5.
- Complete minimum B6 subset:
  - write aura telemetry into `round-*-summary.json` and `score_decisions.json`.
  - no dashboard polish required.
- Must pass new tests and existing test suite.

## Phase 2 (recommended hardening)
- Full B6 report/status UX surfacing.
- Add bundle hash pinning and immutable runtime cache metadata.
- Add shadow-mode switch and cohort tags for A/B comparisons.
- Add explicit incident counters in `.a2a/reports/<session>/summary.md`.

## Phase 3 (future enhancements)
- Confidence calibration loop using 30-day production outcomes.
- Dynamic risk-weight tuning per subsystem.
- Optional promotion of selected deterministic rules to hard gates after evidence.

## Smallest set of code changes required to move from MERGE_BLOCKED to MERGE_APPROVED

1. Add `aura_export` config + recursive config merge (B3).
2. Implement registry/safety/rule-engine load path with strict fallback-to-baseline (B1).
3. Implement subsystem mapping + scope guard to prevent leakage (B2).
4. Implement capped, monotonic score overlay API and hook it into round decisions (B4).
5. Enforce freshness/confidence/evidence_count policy for score influence (B5).
6. Add minimal AURA telemetry serialization in round summary + score decisions for auditability (minimum B6).

If these six changes are implemented with passing tests, merge can move to `MERGE_APPROVED` with constrained rollout (shadow mode first, then bounded scoring).
