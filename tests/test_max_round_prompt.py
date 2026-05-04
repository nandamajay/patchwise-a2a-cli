from a2a_cli.main import _prompt_extend_after_max_rounds


def test_prompt_extend_after_max_rounds_accepts_yes() -> None:
    accepted = _prompt_extend_after_max_rounds(
        session_id="sess-x",
        round_no=3,
        max_rounds=3,
        open_count=1,
        interactive=True,
        input_fn=lambda _prompt: "yes",
    )
    assert accepted is True


def test_prompt_extend_after_max_rounds_rejects_default() -> None:
    accepted = _prompt_extend_after_max_rounds(
        session_id="sess-x",
        round_no=3,
        max_rounds=3,
        open_count=2,
        interactive=True,
        input_fn=lambda _prompt: "",
    )
    assert accepted is False


def test_prompt_extend_after_max_rounds_non_interactive_skips() -> None:
    accepted = _prompt_extend_after_max_rounds(
        session_id="sess-x",
        round_no=3,
        max_rounds=3,
        open_count=1,
        interactive=False,
    )
    assert accepted is False
