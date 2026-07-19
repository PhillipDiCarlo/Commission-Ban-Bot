"""
Unit tests for the Phase 2 fix to the auto-ban background loop (bug #8 in
NOTES.md): enforce_bans_once_global's two top-level DB calls used to be
unguarded, and discord.ext.tasks.Loop permanently cancels itself (no
auto-retry) if any non-network exception escapes the decorated coroutine.
A single transient DB hiccup used to silently kill all future scheduled
enforcement for the rest of the process's life. These tests confirm the
DB calls are now caught and the cycle is skipped-and-logged instead.

Also covers ENFORCE_INTERVAL_HOURS (bug #2: the loop's cadence is now
configurable instead of hardcoded to 1 hour) and its safe env parsing.

Run via (from repo root):
    .venv\\Scripts\\python.exe -m unittest tests.test_enforcement_reliability -v
"""
import unittest
from unittest.mock import patch

import bot


class EnforceBansOnceGlobalReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_survives_get_enabled_configured_servers_raising(self):
        with patch.object(bot, "get_enabled_configured_servers", side_effect=RuntimeError("db exploded")), \
             patch.object(bot, "get_spammer_ids") as mock_get_spammer_ids, \
             patch.object(bot.log, "exception") as mock_log_exception:
            await bot.enforce_bans_once_global()  # must not raise

        mock_get_spammer_ids.assert_not_called()
        mock_log_exception.assert_called_once()

    async def test_survives_get_spammer_ids_raising(self):
        with patch.object(bot, "get_enabled_configured_servers", return_value=[(111, 222)]), \
             patch.object(bot, "get_spammer_ids", side_effect=RuntimeError("db exploded")), \
             patch.object(bot.log, "exception") as mock_log_exception:
            await bot.enforce_bans_once_global()  # must not raise

        mock_log_exception.assert_called_once()

    async def test_no_targets_returns_without_error(self):
        with patch.object(bot, "get_enabled_configured_servers", return_value=[]), \
             patch.object(bot, "get_spammer_ids") as mock_get_spammer_ids:
            await bot.enforce_bans_once_global()

        mock_get_spammer_ids.assert_not_called()


class EnforceBansLoopBodyTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_body_survives_enforce_bans_once_global_raising(self):
        # Backstop: even if enforce_bans_once_global's own guards somehow miss
        # something, the loop body itself must not let an exception escape,
        # since that's what causes discord.py to permanently cancel the loop.
        with patch.object(bot, "enforce_bans_once_global", side_effect=RuntimeError("unexpected")), \
             patch("asyncio.sleep") as mock_sleep, \
             patch.object(bot.log, "exception") as mock_log_exception:
            await bot.enforce_bans_loop.coro()  # the raw undecorated coroutine

        mock_log_exception.assert_called_once()


class EnforceIntervalConfigTests(unittest.TestCase):
    def test_default_interval_is_24_hours(self):
        self.assertEqual(bot._parse_float_env(None, 24.0, "ENFORCE_INTERVAL_HOURS"), 24.0)

    def test_valid_override_is_used(self):
        self.assertEqual(bot._parse_float_env("12", 24.0, "ENFORCE_INTERVAL_HOURS"), 12.0)

    def test_malformed_value_falls_back_to_default_instead_of_raising(self):
        try:
            result = bot._parse_float_env("not-a-number", 24.0, "ENFORCE_INTERVAL_HOURS")
        except ValueError:
            self.fail("_parse_float_env must not raise on a malformed value")
        self.assertEqual(result, 24.0)

    def test_loop_is_configured_with_the_parsed_interval(self):
        # ENFORCE_INTERVAL_HOURS is read once at import time into the @tasks.loop(hours=...)
        # decorator; confirm the two stay in sync rather than the loop silently having its
        # own separate hardcoded value.
        self.assertEqual(bot.enforce_bans_loop.hours, bot.ENFORCE_INTERVAL_HOURS)


if __name__ == "__main__":
    unittest.main()
