from a2a_cli.main import _prompt_extend_after_max_rounds, _prompt_extend_after_max_rounds_count


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


def test_prompt_extend_after_max_rounds_count_accepts_number() -> None:
    count = _prompt_extend_after_max_rounds_count(
        session_id="sess-x",
        round_no=3,
        max_rounds=3,
        open_count=1,
        interactive=True,
        input_fn=lambda _prompt: "5",
    )
    assert count == 5


def test_prompt_extend_after_max_rounds_count_yes_maps_to_one() -> None:
    count = _prompt_extend_after_max_rounds_count(
        session_id="sess-x",
        round_no=3,
        max_rounds=3,
        open_count=1,
        interactive=True,
        input_fn=lambda _prompt: "yes",
    )
    assert count == 1


def test_prompt_extend_after_max_rounds_count_rejects_invalid() -> None:
    count = _prompt_extend_after_max_rounds_count(
        session_id="sess-x",
        round_no=3,
        max_rounds=3,
        open_count=1,
        interactive=True,
        input_fn=lambda _prompt: "invalid",
    )
    assert count == 0
