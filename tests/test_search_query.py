import unittest

from llm_switchboard.search_api import remote_title_matches
from llm_switchboard.search_query import QueryError, TextTerm, fts_literal, parse_query


class SearchQueryTest(unittest.TestCase):
    def test_words_phrases_exclusions_and_filters(self):
        query = parse_query('alpha beta "two words" -gamma tool:codex dir:work by:me|reply age:<2d', now=1_000_000)
        self.assertEqual([t.text for t in query.positive_terms], ["alpha", "beta", "two words"])
        self.assertEqual([t.text for t in query.negative_terms], ["gamma"])
        self.assertEqual(query.roles, ("me", "reply"))
        self.assertIn("codex", [f.value for f in query.values])
        self.assertEqual(query.dates[0].operator, ">=")

    def test_fts_operators_are_always_quoted_as_literal_text(self):
        attempts = ['OR', 'alpha OR beta', 'alpha" OR *', 'NEAR(alpha beta)', 'alpha*', '{alpha beta}']
        for attempt in attempts:
            with self.subTest(attempt=attempt):
                literal = fts_literal(TextTerm(attempt, phrase=True))
                self.assertTrue(literal.startswith('"') and literal.endswith('"'))
                self.assertNotEqual(literal, attempt)
                if '"' in attempt:
                    self.assertIn('""', literal)

    def test_bad_filters_and_unusable_queries_are_rejected(self):
        for query in ("", "-only", "tool:gpt alpha", "by:system alpha", "by:tool alpha", "age:yesterday alpha"):
            with self.subTest(query=query), self.assertRaises(QueryError):
                parse_query(query)

    def test_remote_rows_match_only_title_and_folder_plain_terms(self):
        row = {"host": "m4mac", "tool": "claude", "id": "abc", "short_id": "abc",
               "title": "Compass decoder", "cwd": "/Users/j/dev/van", "project": "~/dev/van",
               "mtime": 900, "resume_cmd": "ssh m4mac"}
        matches = remote_title_matches(parse_query("compass dir:van", now=1000), [row], now=1000)
        self.assertEqual(len(matches), 1)
        self.assertTrue(matches[0]["title_only"])
        self.assertEqual(matches[0]["host"], "m4mac")
        self.assertEqual(remote_title_matches(parse_query("by:me compass"), [row]), [])
        self.assertEqual(remote_title_matches(parse_query("compass -decoder"), [row]), [])

    def test_after_and_before_use_iso_dates(self):
        query = parse_query("needle after:2026-01-01 before:2026-02-01")
        self.assertEqual([f.operator for f in query.dates], [">=", "<"])
        self.assertLess(query.dates[0].timestamp, query.dates[1].timestamp)


if __name__ == "__main__":
    unittest.main()
