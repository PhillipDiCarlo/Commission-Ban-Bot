r"""
Tests for bot.SNOWFLAKE_RE, the regex used to validate a user-submitted
Discord snowflake ID string before it's treated as real (see
`/banner report user_id evidence` in bot.py, around line 807).

SNOWFLAKE_RE = re.compile(r"^\d{15,20}$")

Discord snowflakes are 64-bit unsigned integers; in practice they've been
17-19 decimal digits for years, but the bot accepts a 15-20 digit range.
These tests exercise the boundary, obviously-invalid input a mod might
paste (mentions, usernames, decimals, negatives, whitespace), and a
documented quirk of Python's `$` anchor with trailing newlines.
"""
import unittest

import bot


class TestSnowflakeValidation(unittest.TestCase):
    # --- Valid inputs -----------------------------------------------------

    def test_valid_realistic_18_digit_snowflake(self):
        self.assertIsNotNone(bot.SNOWFLAKE_RE.match("123456789012345678"))

    def test_valid_lower_boundary_15_digits(self):
        self.assertIsNotNone(bot.SNOWFLAKE_RE.match("1" * 15))

    def test_valid_upper_boundary_20_digits(self):
        self.assertIsNotNone(bot.SNOWFLAKE_RE.match("1" * 20))

    def test_valid_16_digits(self):
        # Sanity check for a value strictly inside the accepted range.
        self.assertIsNotNone(bot.SNOWFLAKE_RE.match("1" * 16))

    def test_valid_19_digits(self):
        # Sanity check for a value strictly inside the accepted range.
        self.assertIsNotNone(bot.SNOWFLAKE_RE.match("1" * 19))

    # --- Invalid: length boundaries ----------------------------------------

    def test_invalid_14_digits_below_lower_boundary(self):
        self.assertIsNone(bot.SNOWFLAKE_RE.match("1" * 14))

    def test_invalid_21_digits_above_upper_boundary(self):
        self.assertIsNone(bot.SNOWFLAKE_RE.match("1" * 21))

    # --- Invalid: empty / whitespace ---------------------------------------

    def test_invalid_empty_string(self):
        self.assertIsNone(bot.SNOWFLAKE_RE.match(""))

    def test_invalid_pure_whitespace(self):
        self.assertIsNone(bot.SNOWFLAKE_RE.match("                  "))

    def test_invalid_leading_and_trailing_whitespace(self):
        # An otherwise-valid 18-digit ID, but padded with spaces -- e.g. a
        # mod accidentally including surrounding whitespace when pasting.
        self.assertIsNone(bot.SNOWFLAKE_RE.match(" 123456789012345678 "))

    def test_invalid_leading_whitespace_only(self):
        self.assertIsNone(bot.SNOWFLAKE_RE.match(" 123456789012345678"))

    def test_invalid_trailing_whitespace_only(self):
        self.assertIsNone(bot.SNOWFLAKE_RE.match("123456789012345678 "))

    # --- Invalid: non-numeric / malformed content ---------------------------

    def test_invalid_non_numeric_characters_mixed_in(self):
        self.assertIsNone(bot.SNOWFLAKE_RE.match("12345678901234567a"))

    def test_invalid_discord_mention_format(self):
        # A mod might paste the raw mention text instead of right-clicking
        # "Copy User ID".
        self.assertIsNone(bot.SNOWFLAKE_RE.match("<@123456789012345678>"))

    def test_invalid_discord_nickname_mention_format(self):
        self.assertIsNone(bot.SNOWFLAKE_RE.match("<@!123456789012345678>"))

    def test_invalid_plain_username_string(self):
        self.assertIsNone(bot.SNOWFLAKE_RE.match("SomeUser#1234"))

    def test_invalid_negative_number_string(self):
        self.assertIsNone(bot.SNOWFLAKE_RE.match("-123456789012345678"))

    def test_invalid_decimal_number_string(self):
        self.assertIsNone(bot.SNOWFLAKE_RE.match("123456789012345678.0"))

    def test_invalid_hex_like_string(self):
        self.assertIsNone(bot.SNOWFLAKE_RE.match("0x123456789012345"))

    def test_invalid_digits_with_internal_space(self):
        self.assertIsNone(bot.SNOWFLAKE_RE.match("123456789 012345678"))

    # --- Documented edge case: trailing newline with `$` anchoring ---------

    def test_documented_edge_case_trailing_newline_matches(self):
        """
        Python's `$` anchor matches at the end of the string OR just before
        a single trailing newline (unlike `\\Z`, which matches only at the
        absolute end of the string). SNOWFLAKE_RE uses `^\\d{15,20}$`, so a
        snowflake string with exactly one trailing "\n" still matches, even
        though the raw input isn't a "clean" digit-only string.

        This is a faithful record of current behavior, not a fix or an
        endorsement -- flagging/fixing it is out of scope for this task.
        In practice, user_id typically arrives already stripped of
        surrounding whitespace by Discord's slash-command option handling,
        so this is unlikely to be reachable in production, but the regex
        itself does not guard against it.
        """
        match = bot.SNOWFLAKE_RE.match("123456789012345678\n")
        self.assertIsNotNone(
            match,
            "Documenting current behavior: re.match with a `$`-anchored "
            "pattern matches a string with one trailing newline, because "
            "`$` matches just before a trailing \\n, not only the true "
            "end of string (`\\Z` would not have this behavior).",
        )

    def test_documented_edge_case_two_trailing_newlines_do_not_match(self):
        # Contrast case: `$` only tolerates a *single* trailing newline, so
        # two trailing newlines correctly fail to match.
        self.assertIsNone(bot.SNOWFLAKE_RE.match("123456789012345678\n\n"))

    def test_invalid_leading_newline(self):
        self.assertIsNone(bot.SNOWFLAKE_RE.match("\n123456789012345678"))


if __name__ == "__main__":
    unittest.main()
