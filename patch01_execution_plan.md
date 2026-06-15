# Patch-01 Execution Plan

Scope: implement only **Config defaults + deep merge**.
Constraint: **zero behavior changes when AURA is disabled**.

## 1. Exact files to modify

1. `a2a_cli/config.py`
2. `a2a_cli/main.py`
3. `tests/test_config_aura_defaults.py` (new)

No other code paths should change in Patch-01.

## 2. Exact code sections to add

## 2.1 `a2a_cli/config.py`

Current insertion anchor:
- `default_config()` dictionary in [config.py](/local/mnt/workspace/A2A_CLI/a2a_cli/config.py):13

Add new top-level key in `default_config()`:
- `aura_export` block with safe defaults (disabled by default).

Add this section near existing config groups (before `builder_command` is fine):

```python
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
```

Why these defaults:
- `enabled=False` guarantees no runtime behavior change until explicitly enabled.
- Empty `scope_allowlist` and `subsystem_map` avoid accidental activation side effects.

## 2.2 `a2a_cli/main.py`

Current insertion anchors:
- `_load_config(...)` in [main.py](/local/mnt/workspace/A2A_CLI/a2a_cli/main.py):713

Add helper above `_load_config(...)`:

```python
def _deep_fill_missing(cfg: dict, defaults: dict) -> bool:
    changed = False
    for key, value in defaults.items():
        if key not in cfg:
            cfg[key] = value
            changed = True
            continue
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            if _deep_fill_missing(cfg[key], value):
                changed = True
    return changed
```

Replace `_load_config(...)` merge loop:
- Current logic only fills missing top-level keys.
- New logic should call `_deep_fill_missing(cfg, defaults)`.

Target behavior:
- Missing nested keys are backfilled.
- Existing user values are never overwritten.
- If a key exists with non-dict type, keep as-is (no coercion).

Implementation sketch:

```python
def _load_config(root: Path) -> dict:
    cfg_path = _config_path(root)
    cfg = load_json(cfg_path)
    defaults = default_config()
    changed = _deep_fill_missing(cfg, defaults)
    if changed:
        dump_json(cfg_path, cfg)
    return cfg
```

## 3. Unit tests required

Add new file:
- `tests/test_config_aura_defaults.py`

Required tests:

1. `test_default_config_contains_aura_export_defaults`
- Validate `default_config()` has `aura_export`.
- Validate `enabled is False`.

2. `test_load_config_deep_fills_missing_aura_nested_keys`
- Create temp `.a2a/config.json` with:
  - `{"aura_export": {"enabled": True}}`
- Call `_load_config(...)`.
- Verify missing nested keys are added.
- Verify `enabled` remains `True` (not overwritten).

3. `test_load_config_does_not_override_existing_values`
- Put custom values in existing nested groups (for example `score_thresholds`).
- Verify values remain unchanged after `_load_config(...)`.

4. `test_load_config_keeps_non_dict_user_value`
- Put invalid/user custom form:
  - `{"aura_export": "off"}`
- Verify loader does not crash and does not coerce string to dict.

5. `test_aura_disabled_default_preserves_inactive_state`
- Config missing `aura_export` entirely.
- After load, `aura_export.enabled` is `False`.

## 4. Validation commands

Run from repo root `/local/mnt/workspace/A2A_CLI`:

```bash
python -m pytest -q tests/test_config_aura_defaults.py
python -m pytest -q tests/test_score_engine.py tests/test_lgtm_decision.py tests/test_round_summary.py
python -m pytest -q tests/test_prompt_runtime_loading.py
```

Optional smoke check:

```bash
python -m a2a_cli.main init
python - <<'PY'
import json
from pathlib import Path
p = Path('.a2a/config.json')
d = json.loads(p.read_text())
print('aura_export' in d, d.get('aura_export', {}).get('enabled'))
PY
```

Expected smoke output: `True False`

## 5. Rollback procedure

If changes are not committed:

```bash
git restore -- a2a_cli/config.py a2a_cli/main.py tests/test_config_aura_defaults.py
```

If committed:

```bash
git revert <patch01_commit_sha>
```

If `.a2a/config.json` was regenerated during smoke checks, discard local workspace state if needed:

```bash
git restore -- .a2a/config.json
```

## 6. Expected diff size

Estimated net diff:
- `a2a_cli/config.py`: +10 to +18 LOC
- `a2a_cli/main.py`: +18 to +35 LOC
- `tests/test_config_aura_defaults.py`: +80 to +130 LOC

Total expected: **+108 to +183 LOC**.

## Acceptance criteria for Patch-01

1. Patch touches only the 3 planned files.
2. All listed tests pass.
3. Existing config values are preserved.
4. `aura_export.enabled` defaults to `False`.
5. No runtime behavior changes when AURA remains disabled.
