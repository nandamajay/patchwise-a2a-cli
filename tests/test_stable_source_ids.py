import unittest

from a2a_cli.main import (
    _canonicalize_generated_source_ids,
    _location_path_only,
)


class StableSourceIdTests(unittest.TestCase):
    def test_location_path_only_strips_line_number(self) -> None:
        self.assertEqual(_location_path_only("foo.patch:123"), "foo.patch")
        self.assertEqual(_location_path_only("foo.patch:not-a-line"), "foo.patch:not-a-line")
        self.assertEqual(_location_path_only("foo.patch"), "foo.patch")

    def test_prior_ids_are_preserved(self) -> None:
        findings = [
            {
                "severity": "high",
                "title": "Prior issue",
                "location": "a.patch:10",
                "evidence": ["x"],
                "required_action": "y",
                "status": "open",
                "source_comment_id": "prior-msg:abc@example.com",
            }
        ]
        out, mapping, changed = _canonicalize_generated_source_ids(findings, {})
        self.assertFalse(changed)
        self.assertEqual(out[0]["source_comment_id"], "prior-msg:abc@example.com")
        self.assertEqual(mapping, {})

    def test_volatile_ids_become_stable_and_reused(self) -> None:
        first = [
            {
                "severity": "medium",
                "title": "Runtime PM issue",
                "location": "lpi/foo.patch:65",
                "evidence": ["a"],
                "required_action": "Fix it",
                "status": "open",
                "source_comment_id": "round1-new-1",
            }
        ]
        out1, mapping1, changed1 = _canonicalize_generated_source_ids(first, {})
        self.assertTrue(changed1)
        stable_id = out1[0]["source_comment_id"]
        self.assertTrue(stable_id.startswith("issue-"))

        second = [
            {
                "severity": "medium",
                "title": "Runtime PM issue",
                "location": "lpi/foo.patch:71",
                "evidence": ["b"],
                "required_action": "Fix it",
                "status": "open",
                "source_comment_id": "round2-new-9",
            }
        ]
        out2, _mapping2, changed2 = _canonicalize_generated_source_ids(second, mapping1)
        self.assertTrue(changed2)
        self.assertEqual(out2[0]["source_comment_id"], stable_id)

    def test_missing_source_id_gets_stable_id(self) -> None:
        findings = [
            {
                "severity": "high",
                "title": "Missing id finding",
                "location": "a.patch:10",
                "evidence": ["x"],
                "required_action": "y",
                "status": "open",
            }
        ]
        out, mapping, changed = _canonicalize_generated_source_ids(findings, {})
        self.assertTrue(changed)
        self.assertEqual(len(mapping), 1)
        self.assertTrue(str(out[0]["source_comment_id"]).startswith("issue-"))

    def test_different_findings_get_different_ids(self) -> None:
        findings = [
            {
                "severity": "high",
                "title": "Issue A",
                "location": "a.patch:10",
                "evidence": ["x"],
                "required_action": "y",
                "status": "open",
                "source_comment_id": "new-a",
            },
            {
                "severity": "high",
                "title": "Issue B",
                "location": "b.patch:10",
                "evidence": ["x"],
                "required_action": "y",
                "status": "open",
                "source_comment_id": "new-b",
            },
        ]
        out, _mapping, _changed = _canonicalize_generated_source_ids(findings, {})
        self.assertNotEqual(out[0]["source_comment_id"], out[1]["source_comment_id"])


if __name__ == "__main__":
    unittest.main()
