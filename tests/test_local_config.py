from pathlib import Path
import tempfile
import unittest

from llm_switchboard import local_config
from llm_switchboard.local_config import MAC_NAME, PI_SSH, load


class LocalConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "local.env"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_gives_generic_names(self):
        self.assertEqual(load(self.path, {}), local_config.DEFAULTS)

    def test_page_origins_come_from_file_or_environment(self):
        self.path.write_text(f'{local_config.PAGE_ORIGINS}="https://pi.example,https://other.example:443"\n')
        values = load(self.path, {})
        self.assertEqual(local_config.page_origins(values), {"https://pi.example", "https://other.example:443"})
        values = load(self.path, {local_config.PAGE_ORIGINS: "https://override.example"})
        self.assertEqual(local_config.page_origins(values), {"https://override.example"})
        values = load(self.path, {local_config.PAGE_ORIGINS: ""})
        self.assertEqual(local_config.page_origins(values), frozenset())
        self.assertEqual(local_config.page_origins({}), frozenset())
        self.assertEqual(local_config.page_origins({local_config.PAGE_ORIGINS: " , "}), frozenset())

    def test_page_origins_reject_non_origins(self):
        for origin in ["*", "null", "https://*.example", "https://pi.example/", "https://pi.example/path",
                       "https://user@pi.example", "https://pi.example?q=1", "https://pi.example#fragment",
                       "ftp://pi.example", "https://pi.example:bad", "https://pi.example:99999",
                       "https://pi.example\nX-Header: value"]:
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                local_config.page_origins({local_config.PAGE_ORIGINS: origin})

    def test_file_lines_then_environment(self):
        self.path.write_text('# names\nLLM_SWITCHBOARD_PI_SSH=box\n\nLLM_SWITCHBOARD_MAC_NAME="laptop"\n#LLM_SWITCHBOARD_PI_SSH=old\n')
        self.assertEqual((load(self.path, {})[PI_SSH], load(self.path, {})[MAC_NAME]), ("box", "laptop"))
        env = {PI_SSH: "other", MAC_NAME: "", "HOME": "/x"}
        values = load(self.path, env)
        self.assertEqual((values[PI_SSH], values[MAC_NAME]), ("other", "laptop"))
        self.assertNotIn("HOME", values)


if __name__ == "__main__":
    unittest.main()
