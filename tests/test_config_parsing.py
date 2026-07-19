"""
Unit tests for bot._parse_optional_int_env, used to parse the optional
REVIEW_CHANNEL_ID/REVIEW_ROLE_ID env vars.

Added after the adversarial review found that a malformed (not just
unset) value for either var used to raise ValueError at *import time*,
crashing the entire bot -- including core ban enforcement, unrelated to
the report/review feature. It should now log a warning and disable just
the report feature instead.

Run via (from repo root):
    .venv\\Scripts\\python.exe -m unittest tests.test_config_parsing -v
"""
import unittest
from unittest.mock import patch

import bot


class TestParseOptionalIntEnv(unittest.TestCase):
    def test_none_raw_returns_none(self):
        self.assertIsNone(bot._parse_optional_int_env(None, "SOME_VAR"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(bot._parse_optional_int_env("", "SOME_VAR"))

    def test_valid_integer_string_is_parsed(self):
        self.assertEqual(bot._parse_optional_int_env("123456789012345678", "SOME_VAR"), 123456789012345678)

    def test_malformed_value_returns_none_instead_of_raising(self):
        # This is the actual fix: previously `int(raw)` ran unguarded at module import
        # time, so a typo here (e.g. a stray character) would take down the whole bot.
        try:
            result = bot._parse_optional_int_env("not-an-int", "SOME_VAR")
        except ValueError:
            self.fail("_parse_optional_int_env must not raise on a malformed value")
        self.assertIsNone(result)

    def test_malformed_value_logs_a_warning(self):
        with patch.object(bot.log, "warning") as mock_warning:
            bot._parse_optional_int_env("garbage", "REVIEW_CHANNEL_ID")
        mock_warning.assert_called_once()
        self.assertIn("REVIEW_CHANNEL_ID", mock_warning.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
