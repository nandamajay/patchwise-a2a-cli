from a2a_cli.finding_advertiser import (
    extract_advertised_findings,
    render_advertised_findings_text,
    should_advertise,
)


def test_high_severity_always_advertised() -> None:
    ok, reason = should_advertise(
        {
            "severity": "high",
            "status": "open",
            "title": "unsafe rail drop",
            "required_action": "fix",
            "evidence": ["proof"],
        },
        1,
    )
    assert ok is True
    assert "HIGH severity" in reason


def test_medium_open_advertised() -> None:
    ok, reason = should_advertise(
        {
            "severity": "medium",
            "status": "open",
            "title": "ordering risk",
            "required_action": "fix",
            "evidence": [],
        },
        1,
    )
    assert ok is True
    assert "MEDIUM severity open" in reason


def test_round_gt1_new_finding_advertised() -> None:
    rows = extract_advertised_findings(
        {
            "findings": [
                {
                    "severity": "low",
                    "status": "open",
                    "title": "newly introduced concern",
                    "required_action": "check",
                    "evidence": [],
                    "source_comment_id": "issue-new",
                }
            ]
        },
        {"findings": {"new_ids": ["issue-new"]}},
        2,
    )
    assert len(rows) == 1
    assert "NEW finding raised in round 2" in rows[0].reason


def test_upstream_evidence_advertised() -> None:
    rows = extract_advertised_findings(
        {
            "findings": [
                {
                    "severity": "low",
                    "status": "closed",
                    "title": "evidence-backed note",
                    "required_action": "none",
                    "evidence": [],
                    "upstream_evidence": {"elixir_url": "https://elixir.bootlin.com"},
                    "source_comment_id": "issue-evidence",
                }
            ]
        },
        {"findings": {"new_ids": []}},
        1,
    )
    assert len(rows) == 1
    assert rows[0].reason == "upstream evidence attached"


def test_hardware_keyword_advertised() -> None:
    rows = extract_advertised_findings(
        {
            "findings": [
                {
                    "severity": "low",
                    "status": "closed",
                    "title": "Refcount mismatch can drop shared rail",
                    "required_action": "fix sequencing",
                    "evidence": [],
                    "source_comment_id": "issue-hw",
                }
            ]
        },
        {"findings": {"new_ids": []}},
        1,
    )
    assert len(rows) == 1
    assert "hardware risk detected" in rows[0].reason


def test_sort_order_critical_high_medium_low() -> None:
    rows = extract_advertised_findings(
        {
            "findings": [
                {
                    "severity": "low",
                    "status": "open",
                    "title": "l",
                    "required_action": "a",
                    "evidence": ["x"],
                    "source_comment_id": "l",
                },
                {
                    "severity": "medium",
                    "status": "open",
                    "title": "m",
                    "required_action": "a",
                    "evidence": [],
                    "source_comment_id": "m",
                },
                {
                    "severity": "critical",
                    "status": "open",
                    "title": "c",
                    "required_action": "a",
                    "evidence": [],
                    "source_comment_id": "c",
                },
                {
                    "severity": "high",
                    "status": "open",
                    "title": "h",
                    "required_action": "a",
                    "evidence": [],
                    "source_comment_id": "h",
                },
            ]
        },
        {"findings": {"new_ids": []}},
        1,
    )
    assert [row.severity for row in rows] == ["critical", "high", "medium", "low"]


def test_render_block_contains_id_location_reason() -> None:
    rows = extract_advertised_findings(
        {
            "findings": [
                {
                    "severity": "high",
                    "status": "open",
                    "title": "AUX rail timing issue",
                    "location": "0001.patch:61",
                    "required_action": "move disable to POST_PMD",
                    "evidence": ["line evidence"],
                    "source_comment_id": "issue-123",
                }
            ]
        },
        {"findings": {"new_ids": []}},
        1,
    )
    text = render_advertised_findings_text(rows, round_number=1)
    assert "issue-123" in text
    assert "0001.patch:61" in text
    assert "Advertised because" in text


def test_empty_findings_renders_nothing() -> None:
    rows = extract_advertised_findings({"findings": []}, {"findings": {"new_ids": []}}, 1)
    assert rows == []
    assert render_advertised_findings_text(rows, round_number=1) == ""


def test_ascii_fallback_render() -> None:
    rows = extract_advertised_findings(
        {
            "findings": [
                {
                    "severity": "high",
                    "status": "open",
                    "title": "Hardware race risk",
                    "location": "0001.patch:10",
                    "required_action": "fix",
                    "evidence": ["proof"],
                    "source_comment_id": "issue-hw-1",
                }
            ]
        },
        {"findings": {"new_ids": []}},
        1,
    )
    text = render_advertised_findings_text(rows, round_number=1, ascii_only=True)
    assert "┌" not in text
