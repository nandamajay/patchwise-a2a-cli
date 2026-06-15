# Risk vs Effort Matrix (Blockers #1-#6)

Scoring model:
- Risk reduction score: 1-10 (10 = highest production risk removed).
- Effort: engineer-hours for minimum safe implementation.
- Efficiency index = risk reduction / effort hours.

| Blocker | Risk Removed (1-10) | Min Effort (hrs) | Efficiency (risk/hr) | Notes |
|---|---:|---:|---:|---|
| B1 Runtime safety controls/modules | 10 | 18 | 0.56 | Hard safety boundary; prevents invalid import and unsafe influence. |
| B2 Subsystem scope mismatch | 8 | 5 | 1.60 | Prevents leakage/misapplication; critical correctness guard. |
| B3 Config evolution risk | 7 | 3 | 2.33 | Highest leverage quick win; enables deterministic behavior. |
| B4 Score-engine regression risk | 9 | 6 | 1.50 | Protects LGTM/abort semantics; prevents silent decision drift. |
| B5 Freshness/confidence misuse | 8 | 5 | 1.60 | Removes stale/low-evidence score pollution risk. |
| B6 Telemetry/causality gaps (minimum subset) | 6 | 5 | 1.20 | Required for auditability, rollback confidence, 30-day success tracking. |

## Ranking by risk-reduction efficiency
1. B3 (2.33)
2. B2 (1.60)
3. B5 (1.60)
4. B4 (1.50)
5. B6 (1.20)
6. B1 (0.56)

Note: B1 has the lowest efficiency but remains mandatory because it is a hard dependency for safe integration.

## Highest risk reduction per hour invested
**Winner: B3 (Config evolution risk)**
- Smallest implementation footprint.
- Immediate prevention of latent misconfiguration failures.
- Unblocks deterministic behavior for all other blockers.

## Weekend-constrained strategy (single engineer)
If total budget is ~16-20 hours, maximize merge probability with:
1. B3 (full)
2. B1 core subset: registry + safety fallback path in main (partial B1)
3. B4 minimal overlay with strict cap and monotonic guard
4. B2 scope mapping guard
5. B5 confidence/evidence floor (freshness optional only if time remains)

This ordering minimizes catastrophic failure modes before adding broader telemetry/report polish.
