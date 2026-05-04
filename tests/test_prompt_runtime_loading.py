from pathlib import Path


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_builder_wrapper_uses_builder_template() -> None:
    text = _read("scripts/agents/builder_llm_native.sh")
    assert 'PROMPT_TEMPLATE="$REPO_ROOT/templates/prompts/builder.md"' in text
    assert 'cat "$PROMPT_TEMPLATE" >"$PROMPT_FILE"' in text


def test_reviewer_wrapper_uses_reviewer_template() -> None:
    text = _read("scripts/agents/reviewer_llm_native.sh")
    assert 'PROMPT_TEMPLATE="$REPO_ROOT/templates/prompts/aryabhatta.md"' in text
    assert 'cat "$PROMPT_TEMPLATE" >"$PROMPT_FILE"' in text


def test_builder_output_meta_chatter_rejected() -> None:
    text = _read("scripts/agents/builder_llm_native.sh")
    assert '"progress update"' in text
    assert '"starting review"' in text
    assert '"using patchwise skill workflow"' in text


def test_reviewer_output_meta_chatter_rejected() -> None:
    text = _read("scripts/agents/reviewer_llm_native.sh")
    assert '"progress update"' in text
    assert '"starting review"' in text
    assert '"using patchwise skill workflow"' in text


def test_builder_required_sections_still_enforced() -> None:
    text = _read("scripts/agents/builder_llm_native.sh").lower()
    assert '"## changes"' in text
    assert '"## rationale"' in text
    assert '"## verification commands"' in text
    assert '"## response to reviewer findings"' in text
    assert '"## residual risks"' in text


def test_reviewer_schema_pipeline_unchanged() -> None:
    text = _read("scripts/agents/reviewer_llm_native.sh")
    assert '--output-schema "$SCHEMA"' in text
    assert 'json.dumps({"findings": findings}, indent=2)' in text
