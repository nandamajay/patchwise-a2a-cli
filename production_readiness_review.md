# Production Readiness Review: AURA Export_V1 -> PatchWise-A2A

## Scope Reviewed
- Export bundle: `/local/mnt/workspace/patchwise-a2a-cli/aura_integration_audit/export_v1_bundle`
- Plan: `/local/mnt/workspace/patchwise-a2a-cli/aura_integration_audit/integration_plan.md`
- Safety report: `/local/mnt/workspace/patchwise-a2a-cli/aura_integration_audit/export_safety_report.md`
- Runtime target: `/local/mnt/workspace/A2A_CLI`

## 1) What could still break in production?

1. Missing runtime safety controls (hard blocker).
- The plan requires new guard modules (`aura_export_registry.py`, `aura_rule_engine.py`, `aura_safety.py`) and capped overlay behavior, but they do not exist yet in `A2A_CLI`.
- Evidence: `integration_plan.md:37`, `integration_plan.md:42`, `integration_plan.md:44`, `integration_plan.md:26`, `integration_plan.md:27`.
- Evidence (current code): no AURA integration code paths in `a2a_cli/*`.

2. Subsystem scope mismatch can misapply or skip rules.
- Export uses values like `ASoC`, `ALSA`, `SoundWire`, `runtime_pm`, `DT-audio` (manifest) and `dt_binding`, `cross_subsystem` (records), while runtime subsystem inference is only `pinctrl|codec|codecs|lpass|asoc|sound|unknown`.
- Evidence: `export_manifest.json:6`, `export_manifest.json:12`; `knowledge_base.py:63`, `knowledge_base.py:65`, `knowledge_base.py:68`.

3. Config evolution risk (nested keys).
- Current config loader only backfills missing top-level keys; it does not deep-merge nested dicts.
- If `aura_export` is partially configured, missing subkeys may not be auto-populated.
- Evidence: `main.py:713`, `main.py:718`, `main.py:721`; `config.py:13`.

4. Score-engine regression risk.
- Current scoring has strict LGTM/block/abort logic with no concept of external risk overlay; introducing overlay without monotonic caps and audit trail can change stop/LGTM behavior unexpectedly.
- Evidence: `score_engine.py:58`, `score_engine.py:97`, `score_engine.py:103`, `score_engine.py:127`, `score_engine.py:132`.
- Evidence (planned change): `integration_plan.md:25`, `integration_plan.md:27`.

5. Staleness and confidence misuse.
- Safety report explicitly says stale-knowledge handling is conditional on runtime freshness policy; that policy is not implemented yet in target repo.
- Evidence: `export_safety_report.md:22`, `export_safety_report.md:26`, `integration_plan.md:41`, `integration_plan.md:68`.

6. Observability blind spots for post-deploy causality.
- Current round/session artifacts are strong, but there is no existing AURA-specific telemetry keyspace; impact attribution would be ambiguous without explicit deltas.
- Evidence: `main.py:5492`, `main.py:5591`, `main.py:7468`, `main.py:7481`.

## 2) What assumptions are unproven?

1. Transfer estimates are assumed to hold in this runtime.
- Expected gains are transfer-adjusted estimates, not validated on this production loop.
- Evidence: `export_summary.md` Q1/Q2/Q3 text.

2. Freshness policy is assumed but unspecified operationally.
- Safety says PASS only with runtime freshness policy, but no concrete threshold/expiry enforcement is present in current runtime.
- Evidence: `export_safety_report.md:26`; `integration_plan.md:41`, `integration_plan.md:68`.

3. Scope guard correctness is assumed.
- Plan assumes subsystem filtering will prevent leakage; mapping ontology mismatch is unresolved.
- Evidence: `integration_plan.md:14`, `integration_plan.md:46`; `knowledge_base.py:63`.

4. Maintainer guidance quality is assumed sufficient after filtering.
- Only 3 maintainer records remain in bundle; family/reviewer coverage is intentionally narrow.
- Evidence: `export_manifest.json:24`, `export_manifest.json:25`, `export_manifest.json:53`.

5. Replay bias control is assumed via cap.
- Cap is proposed but not implemented/tested in target code yet.
- Evidence: `export_safety_report.md:31`, `export_safety_report.md:33`; `integration_plan.md:26`.

## 3) Least trustworthy exported artifact

`review_risk_signals.json` is least trustworthy for production decision influence.

Why:
- It is directly intended to affect risk/score behavior (highest blast radius if wrong).
- Many entries have `evidence_count=1` (thin support) while still marked actionable.
- Safety report marks replay-bias/staleness controls as conditional on runtime enforcement.

Evidence:
- Bundle stats: `review_risk_signals.json` median `evidence_count=1.0`.
- `export_safety_report.md:28`, `export_safety_report.md:33`.
- `integration_plan.md:25`, `integration_plan.md:27`.

## 4) Telemetry PatchWise should collect after deployment

Collect all metrics with `aura_enabled` and `aura_bundle_version` dimensions.

1. Load/Safety telemetry
- `aura_bundle_load_success`, `manifest_version`, `artifact_hash`.
- `aura_guard_trip_count` by reason (`scope_leakage`, `stale_data`, `schema_invalid`, `excluded_artifact_detected`).

2. Match telemetry
- `aura_records_matched_total` by artifact and confidence tier.
- `aura_scope_miss_count` (no matching subsystem mapping).

3. Decision-impact telemetry
- `score_pre_overlay` vs `score_post_overlay` and `overlay_delta`.
- `overlay_cap_hit_count`.
- `lgtm_block_reason` distribution with/without AURA active.

4. Review-survival telemetry
- `rounds_to_lgtm`, `session_status`, `findings_open_final`, `high_severity_open_final`.
- `elapsed_seconds_per_round` and session latency.

5. Outcome telemetry
- `objection_count`, `new_findings_per_round`, `resolved_since_prev`.
- Existing artifact anchors for extraction: `round-*-summary.json`, `score_decisions.json`, `summary.md`.
- Evidence: `main.py:5599`, `main.py:5603`, `main.py:5627`, `main.py:5632`, `main.py:7481`.

## 5) Rollback mechanism required

1. Immediate kill switch (must-have)
- `aura_export.enabled=false` to disable all AURA reads and overlays at runtime.

2. Degraded mode switch
- keep advisory prompt hints only; disable score overlay (`max_score_influence=0`).

3. Automatic safety rollback
- if manifest/schema/guard fails in-session, auto-disable AURA for that session and annotate report.
- Evidence requirement already present in plan: `integration_plan.md:81`.

4. Version rollback
- Pin bundle path to immutable version directory; rollback by pointer change only.

5. Data rollback safety
- Never mutate `.a2a/reports`, `.a2a/sessions`, `.a2a/logs` historical artifacts.

## 6) 30-day success metrics

Baseline snapshot from current repo artifacts (`.a2a/reports`):
- sessions: 44
- status distribution: `lgtm=23`, `in_progress=17`, `stopped=4`
- sessions with round summaries: 35
- median rounds to latest summary: 4
- p90 rounds: 15

Success criteria after 30 days (A/B or shadow + phased enablement):
1. Acceptance improvement
- `LGTM_rate` +5 percentage points vs baseline cohort.

2. Objection reduction
- `new_findings_per_round` down >=10%.
- `high/critical open findings at final round` down >=15%.

3. Latency reduction
- median `rounds_to_lgtm` down >=15%.
- median wall-clock session latency down >=10%.

4. Safety constraints
- `aura_guard_trip_rate < 1%` of sessions.
- `overlay_cap_hit_rate < 5%` of AURA-enabled rounds.
- no increase in `stopped` due to score regressions.

5. Reliability constraints
- bundle load/validation success >=99%.
- fallback-to-baseline on failures with zero session crash regressions.

## Decision

MERGE_BLOCKED

Rationale:
- Export artifacts are production-candidate, but runtime safety/validation/overlay components in the integration plan are not yet implemented in `A2A_CLI`.
- The safety report itself marks critical controls as conditional on runtime enforcement.
- Deploying before those controls and telemetry exist would make failures hard to attribute and hard to contain.
