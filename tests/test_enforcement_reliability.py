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
from unittest.mock import AsyncMock, Mock, patch

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


class EnforceBansOnceGlobalFreshSpammerIdsPerGuildTests(unittest.IsolatedAsyncioTestCase):
    """Regression test for a CRITICAL bug found by adversarial review: enforce_bans_once_global
    used to fetch spammer_ids *once* and thread that single frozen snapshot through its entire
    sequential, jittered loop over every enabled+configured guild (which can run for tens of
    seconds to minutes across many guilds). If a spammer id was removed mid-loop (e.g. via
    /banner unban, which also wipes that id's enforced_bans rows across every guild), a guild
    processed later in the loop would still see the stale id in its snapshot, re-ban it, and
    re-record it as enforced -- and since the id is gone from public.users, no future cycle
    would ever revisit or fix it, making the re-ban silently permanent. The fix: don't pass a
    spammer_ids override down to enforce_bans_for_guild from this loop at all, so each guild
    fetches its own fresh copy right before it's processed."""

    async def test_enforce_bans_for_guild_is_called_without_a_spammer_ids_override(self):
        guild1, guild2 = object(), object()
        with patch.object(bot, "get_enabled_configured_servers", return_value=[(111, 1), (222, 2)]), \
             patch.object(bot, "get_spammer_ids", return_value=[999]), \
             patch.object(bot.bot, "get_guild", side_effect=lambda gid: {111: guild1, 222: guild2}[gid]), \
             patch.object(bot, "enforce_bans_for_guild", new=AsyncMock(return_value=0)) as mock_enforce, \
             patch("asyncio.sleep", new=AsyncMock()):
            await bot.enforce_bans_once_global()

        self.assertEqual(mock_enforce.await_count, 2)
        for call in mock_enforce.await_args_list:
            # Exactly (guild, channel_id) -- no spammer_ids passed, positionally or by
            # keyword, so each call falls through to enforce_bans_for_guild's own
            # default (a fresh await asyncio.to_thread(get_spammer_ids) per guild).
            self.assertEqual(len(call.args), 2)
            self.assertNotIn("spammer_ids", call.kwargs)

    async def test_early_exit_check_still_uses_a_count_not_a_reused_list(self):
        # The early "is there anything to enforce at all" check still needs *a* fetch of
        # spammer_ids, but that value must never be threaded down into the per-guild
        # calls (covered above) -- this just confirms the early-exit path still works
        # when there are zero spammer ids.
        with patch.object(bot, "get_enabled_configured_servers", return_value=[(111, 1)]), \
             patch.object(bot, "get_spammer_ids", return_value=[]), \
             patch.object(bot, "enforce_bans_for_guild", new=AsyncMock()) as mock_enforce:
            await bot.enforce_bans_once_global()

        mock_enforce.assert_not_called()


class EnforceBansOnceGlobalPerGuildErrorHandlingTests(unittest.IsolatedAsyncioTestCase):
    """The per-guild call inside enforce_bans_once_global's loop is wrapped in its
    own try/except Exception (bot.py ~826-832), separate from the two top-level DB
    guards above. Confirms one guild's enforce_bans_for_guild raising doesn't abort
    the whole multi-guild cycle -- the loop should log and move on to the next
    target instead."""

    async def test_one_guild_raising_does_not_abort_the_rest_of_the_loop(self):
        # Mock(id=...), not plain object(): the success path also does
        # `log.info(f"Guild {guild.id}: ...")` after the call, which needs a
        # real .id attribute to not itself raise (and get counted as a second
        # per-guild failure).
        guild1, guild2 = Mock(id=111), Mock(id=222)

        async def enforce_side_effect(guild, channel_id, *args, **kwargs):
            if guild is guild1:
                raise RuntimeError("boom")
            return 0

        with patch.object(bot, "get_enabled_configured_servers", return_value=[(111, 1), (222, 2)]), \
             patch.object(bot, "get_spammer_ids", return_value=[999]), \
             patch.object(bot.bot, "get_guild", side_effect=lambda gid: {111: guild1, 222: guild2}[gid]), \
             patch.object(bot, "enforce_bans_for_guild", new=AsyncMock(side_effect=enforce_side_effect)) as mock_enforce, \
             patch.object(bot.log, "exception") as mock_log_exception, \
             patch("asyncio.sleep", new=AsyncMock()):
            await bot.enforce_bans_once_global()  # must not raise

        # Both guilds were attempted -- guild1's failure didn't stop guild2's call.
        self.assertEqual(mock_enforce.await_count, 2)
        mock_log_exception.assert_called_once()

    async def test_unresolvable_guild_is_skipped_without_aborting_the_rest_of_the_loop(self):
        # Sibling branch to the one above: `if not guild: continue` (bot.py ~819) --
        # bot.get_guild(server_id) returns None when the bot is no longer in a guild
        # that's still enabled+configured in the DB (e.g. it was kicked). That target
        # should be skipped silently, with no call to enforce_bans_for_guild for it,
        # and the loop must still proceed to the next (resolvable) guild.
        guild2 = Mock(id=222)

        with patch.object(bot, "get_enabled_configured_servers", return_value=[(111, 1), (222, 2)]), \
             patch.object(bot, "get_spammer_ids", return_value=[999]), \
             patch.object(bot.bot, "get_guild", side_effect=lambda gid: {111: None, 222: guild2}[gid]), \
             patch.object(bot, "enforce_bans_for_guild", new=AsyncMock(return_value=0)) as mock_enforce, \
             patch.object(bot.log, "exception") as mock_log_exception, \
             patch("asyncio.sleep", new=AsyncMock()):
            await bot.enforce_bans_once_global()  # must not raise

        # Only the resolvable guild was attempted; the unresolvable one was skipped
        # via `continue`, not treated as an error.
        mock_enforce.assert_awaited_once_with(guild2, 2)
        mock_log_exception.assert_not_called()


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
