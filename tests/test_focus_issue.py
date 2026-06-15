import unittest

from a2a_cli.main import _normalize_focus_issues


class FocusIssueTests(unittest.TestCase):
    def test_normalize_focus_issues_dedupes_and_cleans(self) -> None:
        rows = _normalize_focus_issues(
            [
                "  swr init timeout -110  ",
                "- din-ports mismatch",
                "",
                "SWR init timeout -110",
                "  \n  dout-ports mismatch  \n",
            ]
        )
        self.assertEqual(
            rows,
            [
                "swr init timeout -110",
                "din-ports mismatch",
                "dout-ports mismatch",
            ],
        )

    def test_normalize_focus_issues_handles_none(self) -> None:
        self.assertEqual(_normalize_focus_issues(None), [])


if __name__ == "__main__":
    unittest.main()
