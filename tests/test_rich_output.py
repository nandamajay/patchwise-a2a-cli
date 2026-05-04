from a2a_cli.rich_output import (
    render_finding_card,
    render_gate_status,
    render_lgtm_banner,
    render_prior_comment_table,
    render_round_table,
    render_scores,
    render_session_header,
)


def test_render_session_header_no_crash() -> None:
    out = render_session_header("sess-1", "smoke-task", 1, 3, width=90)
    assert "sess-1" in out
    assert "smoke-task" in out
    assert "1/3" in out


def test_render_round_table_all_fields_present() -> None:
    out = render_round_table(
        {
            "round": 2,
            "max_rounds": 5,
            "gate_passed": True,
            "builder_confidence": 80,
            "reviewer_confidence": 90,
            "builder_patch_gauge": 30,
            "verdict": "REJECT",
            "findings": {
                "total": 4,
                "open": 2,
                "closed": 2,
                "new_since_prev": 1,
                "resolved_since_prev": 2,
            },
            "prior_comments": {"totals": {"received_total": 6, "open": 1, "closed": 5}},
        },
        width=100,
    )
    assert "Round 2/5" in out
    assert "Gate:" in out
    assert "CHANAKYA" in out
    assert "ARYABHATTA" in out
    assert "Findings: total=4 open=2 closed=2 new=1 resolved=2" in out
    assert "Prior Comments: received=6 open=1 closed=5" in out


def test_render_finding_card_severity_colours() -> None:
    critical = render_finding_card(
        {
            "severity": "critical",
            "title": "Null deref",
            "location": "foo.c:10",
            "id": "F-1",
            "description": "possible null dereference",
        }
    )
    high = render_finding_card({"severity": "high", "title": "x", "location": "a:1", "id": "F-2"})
    medium = render_finding_card({"severity": "medium", "title": "x", "location": "a:1", "id": "F-3"})
    low = render_finding_card({"severity": "low", "title": "x", "location": "a:1", "id": "F-4"})
    assert "🔴 critical" in critical
    assert "🟠 high" in high
    assert "🟡 medium" in medium
    assert "🔵 low" in low


def test_render_scores_progress_bars() -> None:
    out = render_scores(80, 90, 30)
    assert "Chanakya  confidence" in out
    assert "Aryabhata confidence" in out
    assert "Patch gauge" in out
    assert "80%" in out
    assert "90%" in out
    assert "30%" in out


def test_render_lgtm_banner_displays_correctly() -> None:
    out = render_lgtm_banner("sess-123", rounds=3, total_findings=6)
    assert "LGTM" in out
    assert "sess-123" in out
    assert "Rounds: 3" in out
    assert "Total findings: 6" in out


def test_render_gate_passed() -> None:
    assert "PASSED" in render_gate_status(True)


def test_render_gate_failed() -> None:
    assert "FAILED" in render_gate_status(False)


def test_narrow_terminal_80_cols_no_overflow() -> None:
    out = render_round_table(
        {
            "round": 1,
            "max_rounds": 3,
            "gate_passed": False,
            "builder_confidence": None,
            "reviewer_confidence": None,
            "builder_patch_gauge": "N/A",
            "verdict": "REJECT",
            "findings": {"total": 0, "open": 0, "closed": 0, "new_since_prev": 0, "resolved_since_prev": 0},
            "prior_comments": {"totals": {"received_total": 0, "open": 0, "closed": 0}},
        },
        width=80,
    )
    for line in out.splitlines():
        assert len(line) <= 80


def test_ascii_fallback_no_unicode_box_chars() -> None:
    out = render_lgtm_banner("sess-ascii", ascii_only=True)
    assert "╔" not in out
    assert "+" in out


def test_render_prior_comment_table_with_rows() -> None:
    out = render_prior_comment_table(
        {
            "totals": {"received_total": 2, "open": 1, "closed": 1},
            "tracked": [
                {
                    "source_comment_id": "prior-1",
                    "subject": "Fix unwind path",
                    "current_status": "closed",
                    "fixed_by_a2a": True,
                    "closed_round": 2,
                    "latest_location": "foo.c:10",
                    "latest_evidence": "verified",
                },
                {
                    "source_comment_id": "prior-2",
                    "subject": "Check refcount",
                    "current_status": "open",
                    "fixed_by_a2a": False,
                    "closed_round": None,
                    "latest_location": "",
                    "latest_evidence": "",
                },
            ],
        },
        width=100,
    )
    assert "Prior Comments Table" in out
    assert "prior-1" in out
    assert "needs_eye=no" in out
    assert "prior-2" in out
    assert "needs_eye=yes" in out


def test_render_prior_comment_table_empty() -> None:
    out = render_prior_comment_table({"totals": {"received_total": 0}, "tracked": []})
    assert "Prior Comments Table" in out
    assert "Totals: received=0 open=0 closed=0" in out
    assert "no tracked comments" in out


def test_prior_comment_table_narrow_width_no_overflow() -> None:
    out = render_prior_comment_table(
        {
            "tracked": [
                {
                    "source_comment_id": "prior-very-long-comment-id",
                    "subject": "This is a deliberately long subject line for width testing",
                    "current_status": "open",
                    "fixed_by_a2a": False,
                    "closed_round": None,
                    "latest_location": "",
                    "latest_evidence": "",
                }
            ]
        },
        width=92,
    )
    for line in out.splitlines():
        assert len(line) <= 92


def test_needs_eye_computation_open_comment() -> None:
    out = render_prior_comment_table(
        {
            "tracked": [
                {
                    "source_comment_id": "prior-open",
                    "subject": "Open comment",
                    "current_status": "open",
                    "fixed_by_a2a": False,
                    "latest_location": "foo.c:1",
                    "latest_evidence": "pending",
                }
            ]
        },
        width=100,
    )
    assert "prior-open" in out
    assert "needs_eye=yes" in out


def test_needs_eye_computation_missing_evidence() -> None:
    out = render_prior_comment_table(
        {
            "tracked": [
                {
                    "source_comment_id": "prior-no-evidence",
                    "subject": "Closed but missing proof",
                    "current_status": "closed",
                    "fixed_by_a2a": True,
                    "latest_location": "foo.c:2",
                    "latest_evidence": "",
                }
            ]
        },
        width=100,
    )
    assert "prior-no-evidence" in out
    assert "needs_eye=yes" in out
