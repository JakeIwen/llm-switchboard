import unittest

from llm_switchboard import names, session_names
from name_contract_oracle import capture


class SessionNameTest(unittest.TestCase):
    def test_names_and_protocol_acceptance_and_refusals(self):
        self.assertEqual(len(capture()), 20)

    def test_existing_names_exports_share_the_canonical_validation(self):
        self.assertIs(names.clean_title, session_names.clean_title)
        self.assertIs(names.name_key, session_names.name_key)
        self.assertEqual(names.MAX_TITLE_LEN, session_names.MAX_TITLE_LEN)
