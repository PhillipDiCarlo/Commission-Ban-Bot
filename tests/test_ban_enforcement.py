"""
Unit tests for bot.enforce_bans_for_guild.

Covers two things:

1. (Phase 2) The fix that removed the "user was in the server" notification.
   That notification was gated on guild.get_member(uid) is not None, but the
   bot runs without the privileged Members intent and with an empty member
   cache (see bot.py's module docstring) -- so the check almost always
   returned False even for users who really were present, making the
   notification dead code that would rarely if ever fire. It (and the
   now-unused fetch_username_safe helper) were removed rather than adding the
   privileged intent, consistent with the bot's explicit no-privileged-intent
   design.

2. (Phase 3, bug 6) The fix that stopped enforce_bans_for_guild from doing a
   fully paginated guild.bans(limit=None) re-download of the *entire* live
   ban list every enforcement cycle, purely to compute a set difference that
   usually only changes by a handful of IDs cycle over cycle. The normal
   automatic path now diffs against a local Postgres record
   (bot.get_enforced_ban_ids / bot.record_enforced_ban) instead. A new
   force_refresh=True escape hatch (used by /banner sync-now) still does the
   old live guild.bans() pull, merging anything found there into the local
   record, for manual reconciliation (e.g. after a moderator manually unbans
   someone through Discord's own UI).

Run via (from repo root):
    .venv\\Scripts\\python.exe -m unittest tests.test_ban_enforcement -v
"""
import unittest
from unittest.mock import Mock, AsyncMock, patch

import discord

import bot


async def _empty_async_iter():
    return
    yield  # pragma: no cover


def make_guild(live_ban_ids=()):
    """Build a mock guild. live_ban_ids simulates Discord's *actual* live ban
    list, only consulted when enforce_bans_for_guild is called with
    force_refresh=True."""
    guild = Mock(spec=discord.Guild)
    guild.id = 12345

    async def _bans_iter(limit=None):
        for uid in live_ban_ids:
            entry = Mock()
            entry.user = Mock()
            entry.user.id = uid
            yield entry

    guild.bans = Mock(side_effect=lambda limit=None: _bans_iter(limit))
    guild.ban = AsyncMock()
    return guild


class EnforceBansForGuildTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Avoid real 1-second sleeps between bans in the enforcement loop.
        self._sleep_patch = patch("asyncio.sleep", new=AsyncMock())
        self._sleep_patch.start()
        self.addAsyncCleanup(self._sleep_patch.stop)

        # Default DB-helper mocks for the local enforced-bans cache. Individual
        # tests override return_value / assert on these as needed.
        self._get_enforced_patch = patch.object(bot, "get_enforced_ban_ids", return_value=set())
        self.mock_get_enforced = self._get_enforced_patch.start()
        self.addCleanup(self._get_enforced_patch.stop)

        self._record_enforced_patch = patch.object(bot, "record_enforced_ban")
        self.mock_record_enforced = self._record_enforced_patch.start()
        self.addCleanup(self._record_enforced_patch.stop)

    async def test_successful_ban_does_not_send_a_was_in_server_notification(self):
        guild = make_guild()
        with patch.object(bot, "send_info", new=AsyncMock()) as mock_send_info:
            new_count = await bot.enforce_bans_for_guild(guild, info_channel_id=999, spammer_ids=[123456789012345678])

        self.assertEqual(new_count, 1)
        guild.ban.assert_awaited_once()
        mock_send_info.assert_not_awaited()

    async def test_forbidden_still_notifies_and_stops(self):
        guild = make_guild()
        guild.ban = AsyncMock(side_effect=discord.Forbidden(Mock(status=403), "Missing Permissions"))
        with patch.object(bot, "send_info", new=AsyncMock()) as mock_send_info:
            new_count = await bot.enforce_bans_for_guild(guild, info_channel_id=999, spammer_ids=[123456789012345678])

        self.assertEqual(new_count, 0)
        mock_send_info.assert_awaited_once()
        self.assertIn("Ban Members", mock_send_info.call_args.args[2])

    async def test_fetch_username_safe_helper_was_removed(self):
        # Regression guard: this helper existed only to support the removed
        # notification and should not have been left behind as dead code.
        self.assertFalse(hasattr(bot, "fetch_username_safe"))

    # ---- Bug 6: local enforced_bans cache replaces guild.bans() on the normal path ----

    async def test_already_enforced_ids_are_skipped_without_calling_guild_bans(self):
        target = 123456789012345678
        guild = make_guild(live_ban_ids=[target])  # live Discord state is irrelevant here
        self.mock_get_enforced.return_value = {target}

        new_count = await bot.enforce_bans_for_guild(guild, info_channel_id=999, spammer_ids=[target])

        self.assertEqual(new_count, 0)
        guild.ban.assert_not_awaited()
        # The whole point of this fix: the normal automatic path must not pay for a
        # full live re-download of the guild's ban list.
        guild.bans.assert_not_called()

    async def test_normal_path_never_calls_guild_bans_even_when_banning(self):
        target = 123456789012345678
        guild = make_guild()
        self.mock_get_enforced.return_value = set()

        new_count = await bot.enforce_bans_for_guild(guild, info_channel_id=999, spammer_ids=[target])

        self.assertEqual(new_count, 1)
        guild.ban.assert_awaited_once()
        guild.bans.assert_not_called()

    async def test_successful_ban_records_enforced_ban(self):
        target = 123456789012345678
        guild = make_guild()
        self.mock_get_enforced.return_value = set()

        await bot.enforce_bans_for_guild(guild, info_channel_id=999, spammer_ids=[target])

        self.mock_record_enforced.assert_called_once_with(guild.id, target)

    async def test_get_enforced_ban_ids_failure_falls_back_to_empty_set(self):
        # If the local-cache lookup itself blows up (e.g. transient DB hiccup), the
        # guild should still be treated as "nothing enforced yet" rather than the
        # whole call raising.
        target = 123456789012345678
        guild = make_guild()
        self.mock_get_enforced.side_effect = RuntimeError("db exploded")

        new_count = await bot.enforce_bans_for_guild(guild, info_channel_id=999, spammer_ids=[target])

        self.assertEqual(new_count, 1)
        guild.ban.assert_awaited_once()

    # ---- force_refresh=True: manual /banner sync-now reconciliation path ----

    async def test_force_refresh_calls_guild_bans_and_merges_live_results(self):
        already_live_banned = 111111111111111111
        needs_ban = 222222222222222222
        guild = make_guild(live_ban_ids=[already_live_banned])
        self.mock_get_enforced.return_value = set()  # not yet recorded locally

        new_count = await bot.enforce_bans_for_guild(
            guild, info_channel_id=999, spammer_ids=[already_live_banned, needs_ban], force_refresh=True
        )

        guild.bans.assert_called_once()
        # already_live_banned was found live but not recorded -> backfilled, not re-banned.
        self.assertEqual(new_count, 1)
        guild.ban.assert_awaited_once_with(
            discord.Object(id=needs_ban),
            reason=unittest.mock.ANY,
            delete_message_seconds=0,
        )
        # Backfill recorded for the live-discovered ban, plus the record for the new ban.
        self.mock_record_enforced.assert_any_call(guild.id, already_live_banned)
        self.mock_record_enforced.assert_any_call(guild.id, needs_ban)

    async def test_force_refresh_does_not_rerecord_already_locally_known_ids(self):
        target = 123456789012345678
        guild = make_guild(live_ban_ids=[target])
        self.mock_get_enforced.return_value = {target}  # already known locally

        new_count = await bot.enforce_bans_for_guild(
            guild, info_channel_id=999, spammer_ids=[target], force_refresh=True
        )

        self.assertEqual(new_count, 0)
        guild.ban.assert_not_awaited()
        self.mock_record_enforced.assert_not_called()

    async def test_default_force_refresh_is_false(self):
        import inspect
        sig = inspect.signature(bot.enforce_bans_for_guild)
        self.assertIn("force_refresh", sig.parameters)
        self.assertEqual(sig.parameters["force_refresh"].default, False)

    async def test_force_refresh_does_not_backfill_non_spammer_bans(self):
        # Regression test for a real bug found by adversarial review: the original
        # force_refresh implementation backfilled *every* currently-banned user into
        # enforced_bans, not just spammer ids. Concretely: a moderator bans someone
        # for an unrelated reason (raiding), /banner sync-now runs and (with the old
        # code) would have recorded that unrelated ban into enforced_bans; if that
        # same user was *later* legitimately approved as a spammer via /banner
        # report, the stale enforced_bans row would make the bot think they were
        # already handled in this guild and silently never ban them here -- forever,
        # with no admin command able to fix it. The fix scopes backfill to `ids`
        # (the spammer set) only.
        unrelated_moderator_ban = 999999999999999999  # banned for an unrelated reason, NOT a spammer
        actual_spammer = 123456789012345678
        guild = make_guild(live_ban_ids=[unrelated_moderator_ban, actual_spammer])
        self.mock_get_enforced.return_value = set()

        await bot.enforce_bans_for_guild(
            guild, info_channel_id=999, spammer_ids=[actual_spammer], force_refresh=True
        )

        recorded_ids = {call.args[1] for call in self.mock_record_enforced.call_args_list}
        self.assertIn(actual_spammer, recorded_ids)
        self.assertNotIn(unrelated_moderator_ban, recorded_ids)

    async def test_force_refresh_prunes_stale_enforced_record_for_manually_unbanned_spammer(self):
        # The other half of the same fix: if a spammer id was previously recorded as
        # enforced in this guild but a force_refresh live pull shows they're not
        # actually banned anymore (a moderator manually unbanned them), the stale
        # record must be pruned -- otherwise a later re-approval of that same id
        # would be silently skipped forever, same failure mode as the backfill bug.
        manually_unbanned_spammer = 123456789012345678
        guild = make_guild(live_ban_ids=[])  # not actually banned anymore
        self.mock_get_enforced.return_value = {manually_unbanned_spammer}  # stale local record

        with patch.object(bot, "remove_enforced_ban") as mock_remove_enforced:
            new_count = await bot.enforce_bans_for_guild(
                guild, info_channel_id=999, spammer_ids=[manually_unbanned_spammer], force_refresh=True
            )

        mock_remove_enforced.assert_called_once_with(guild.id, manually_unbanned_spammer)
        # After pruning, this id is no longer "already handled" -> re-banned this cycle.
        self.assertEqual(new_count, 1)
        guild.ban.assert_awaited_once()

    async def test_force_refresh_does_not_prune_ids_still_live_banned(self):
        target = 123456789012345678
        guild = make_guild(live_ban_ids=[target])
        self.mock_get_enforced.return_value = {target}

        with patch.object(bot, "remove_enforced_ban") as mock_remove_enforced:
            await bot.enforce_bans_for_guild(
                guild, info_channel_id=999, spammer_ids=[target], force_refresh=True
            )

        mock_remove_enforced.assert_not_called()

    # ---- 30035 "already banned" branch backfills the local record ----

    async def test_already_banned_http_error_records_enforced_ban(self):
        target = 123456789012345678
        guild = make_guild()
        err = discord.HTTPException(Mock(status=400), "Already banned")
        err.code = 30035
        guild.ban = AsyncMock(side_effect=err)
        self.mock_get_enforced.return_value = set()

        new_count = await bot.enforce_bans_for_guild(guild, info_channel_id=999, spammer_ids=[target])

        # Not counted as a *new* ban (Discord didn't do anything), but it must be
        # backfilled into enforced_bans -- otherwise the bot would keep re-attempting
        # (and re-hitting this same error) for this user, forever, every cycle.
        self.assertEqual(new_count, 0)
        self.mock_record_enforced.assert_called_once_with(guild.id, target)

    async def test_unknown_user_http_error_removes_spammer_and_does_not_record(self):
        target = 123456789012345678
        guild = make_guild()
        err = discord.HTTPException(Mock(status=404), "Unknown User")
        err.code = 10013
        guild.ban = AsyncMock(side_effect=err)
        self.mock_get_enforced.return_value = set()

        with patch.object(bot, "remove_spammer_id") as mock_remove:
            new_count = await bot.enforce_bans_for_guild(guild, info_channel_id=999, spammer_ids=[target])

        self.assertEqual(new_count, 0)
        mock_remove.assert_called_once_with(target)
        self.mock_record_enforced.assert_not_called()

    # ---- Guard clauses at the top of enforce_bans_for_guild ----

    async def test_guard_clause_returns_zero_when_guild_is_none(self):
        with patch.object(bot, "get_spammer_ids") as mock_get_spammer_ids:
            new_count = await bot.enforce_bans_for_guild(None, info_channel_id=999, spammer_ids=[123456789012345678])

        self.assertEqual(new_count, 0)
        mock_get_spammer_ids.assert_not_called()
        self.mock_get_enforced.assert_not_called()
        self.mock_record_enforced.assert_not_called()

    async def test_guard_clause_returns_zero_when_info_channel_id_is_none(self):
        guild = make_guild()
        with patch.object(bot, "get_spammer_ids") as mock_get_spammer_ids:
            new_count = await bot.enforce_bans_for_guild(guild, info_channel_id=None, spammer_ids=[123456789012345678])

        self.assertEqual(new_count, 0)
        mock_get_spammer_ids.assert_not_called()
        self.mock_get_enforced.assert_not_called()
        guild.ban.assert_not_awaited()

    async def test_guard_clause_returns_zero_when_info_channel_id_is_zero(self):
        # info_channel_id=0 is falsy, same as None -- `if not info_channel_id`.
        guild = make_guild()
        with patch.object(bot, "get_spammer_ids") as mock_get_spammer_ids:
            new_count = await bot.enforce_bans_for_guild(guild, info_channel_id=0, spammer_ids=[123456789012345678])

        self.assertEqual(new_count, 0)
        mock_get_spammer_ids.assert_not_called()
        self.mock_get_enforced.assert_not_called()
        guild.ban.assert_not_awaited()

    async def test_empty_spammer_ids_returns_zero_without_banning(self):
        # spammer_ids resolves (via the DB fallback, since [] is falsy and falls
        # through to the get_spammer_ids() branch) to an empty set -> `if not ids`.
        guild = make_guild()
        with patch.object(bot, "get_spammer_ids", return_value=[]) as mock_get_spammer_ids:
            new_count = await bot.enforce_bans_for_guild(guild, info_channel_id=999, spammer_ids=None)

        self.assertEqual(new_count, 0)
        mock_get_spammer_ids.assert_called_once()
        guild.ban.assert_not_awaited()
        # The "no ids at all" guard returns before ever consulting the local
        # enforced-bans cache.
        self.mock_get_enforced.assert_not_called()

    # ---- Generic discord.HTTPException branch (not 30035, not 10013) ----

    async def test_generic_http_error_is_caught_logged_and_does_not_abort_the_batch(self):
        errored_id = 111111111111111111
        ok_id = 222222222222222222
        guild = make_guild()

        err = discord.HTTPException(Mock(status=400), "Some other error")
        err.code = 40001  # arbitrary code that is neither 30035 nor 10013

        async def ban_side_effect(obj, reason=None, delete_message_seconds=None):
            if obj.id == errored_id:
                raise err
            return None

        guild.ban = AsyncMock(side_effect=ban_side_effect)
        self.mock_get_enforced.return_value = set()

        with patch.object(bot, "remove_spammer_id") as mock_remove_spammer, \
             patch.object(bot.log, "debug") as mock_log_debug:
            new_count = await bot.enforce_bans_for_guild(
                guild, info_channel_id=999, spammer_ids=[errored_id, ok_id]
            )

        # Both ids attempted -- the generic HTTPException branch does not `break`
        # (unlike Forbidden), so the loop continues to the next id.
        self.assertEqual(guild.ban.await_count, 2)
        self.assertEqual(new_count, 1)
        # Neither the 30035 backfill nor the 10013 removal branch fired.
        mock_remove_spammer.assert_not_called()
        self.mock_record_enforced.assert_called_once_with(guild.id, ok_id)
        mock_log_debug.assert_called_once()

    # ---- Bare `except Exception` catch-all around the ban attempt ----

    async def test_unexpected_exception_during_ban_is_caught_and_does_not_abort_the_batch(self):
        bad_id = 111111111111111111
        ok_id = 222222222222222222
        guild = make_guild()

        async def ban_side_effect(obj, reason=None, delete_message_seconds=None):
            if obj.id == bad_id:
                raise RuntimeError("totally unexpected failure")
            return None

        guild.ban = AsyncMock(side_effect=ban_side_effect)
        self.mock_get_enforced.return_value = set()

        # Must not raise out of enforce_bans_for_guild, and must still process ok_id.
        new_count = await bot.enforce_bans_for_guild(
            guild, info_channel_id=999, spammer_ids=[bad_id, ok_id]
        )

        self.assertEqual(guild.ban.await_count, 2)
        self.assertEqual(new_count, 1)
        self.mock_record_enforced.assert_called_once_with(guild.id, ok_id)

    # ---- Set-difference logic with more than one id in each bucket ----

    async def test_set_difference_bans_only_the_not_yet_enforced_ids_in_a_mixed_batch(self):
        already_enforced = 111111111111111111
        needs_ban_1 = 222222222222222222
        needs_ban_2 = 333333333333333333
        guild = make_guild()
        self.mock_get_enforced.return_value = {already_enforced}

        new_count = await bot.enforce_bans_for_guild(
            guild, info_channel_id=999, spammer_ids=[already_enforced, needs_ban_1, needs_ban_2]
        )

        self.assertEqual(new_count, 2)
        self.assertEqual(guild.ban.await_count, 2)
        banned_ids = {call.args[0].id for call in guild.ban.await_args_list}
        self.assertEqual(banned_ids, {needs_ban_1, needs_ban_2})


class EnforcedBanDbHelperTests(unittest.TestCase):
    """Lightweight signature/style sanity checks for the new DB helpers -- there's no
    real Postgres in this test suite (see tests/__init__.py), so these confirm the
    helpers exist, are callable with the documented signature, and fail the same way
    sibling helpers (e.g. get_spammer_ids) do when given a connection they can't use,
    rather than exercising real SQL."""

    def test_get_enforced_ban_ids_is_callable_and_returns_a_set_like_get_spammer_ids(self):
        import inspect
        self.assertTrue(callable(bot.get_enforced_ban_ids))
        sig = inspect.signature(bot.get_enforced_ban_ids)
        self.assertEqual(list(sig.parameters), ["server_id"])

    def test_record_enforced_ban_is_callable_with_two_positional_args(self):
        import inspect
        self.assertTrue(callable(bot.record_enforced_ban))
        sig = inspect.signature(bot.record_enforced_ban)
        self.assertEqual(list(sig.parameters), ["server_id", "discord_id"])

    def test_get_enforced_ban_ids_uses_get_db_connection_like_sibling_helpers(self):
        # Same connect/finally-close pattern as get_spammer_ids: swap in a fake
        # connection/cursor and confirm it round-trips rows into a set of ints.
        fake_cursor = Mock()
        fake_cursor.__enter__ = Mock(return_value=fake_cursor)
        fake_cursor.__exit__ = Mock(return_value=False)
        fake_cursor.execute = Mock()
        fake_cursor.fetchall = Mock(return_value=[(111,), (222,)])

        fake_conn = Mock()
        fake_conn.cursor = Mock(return_value=fake_cursor)
        fake_conn.close = Mock()

        with patch.object(bot, "get_db_connection", return_value=fake_conn):
            result = bot.get_enforced_ban_ids(999)

        self.assertEqual(result, {111, 222})
        fake_conn.close.assert_called_once()

    def test_record_enforced_ban_inserts_and_closes_connection(self):
        fake_cursor = Mock()
        fake_cursor.__enter__ = Mock(return_value=fake_cursor)
        fake_cursor.__exit__ = Mock(return_value=False)
        fake_cursor.execute = Mock()

        fake_conn = Mock()
        fake_conn.__enter__ = Mock(return_value=fake_conn)
        fake_conn.__exit__ = Mock(return_value=False)
        fake_conn.cursor = Mock(return_value=fake_cursor)
        fake_conn.close = Mock()

        with patch.object(bot, "get_db_connection", return_value=fake_conn):
            bot.record_enforced_ban(999, 123456789012345678)

        fake_cursor.execute.assert_called_once()
        sql = fake_cursor.execute.call_args.args[0]
        self.assertIn("INSERT INTO public.enforced_bans", sql)
        self.assertIn("ON CONFLICT", sql)
        fake_conn.close.assert_called_once()

    def test_remove_enforced_ban_is_callable_with_two_positional_args(self):
        import inspect
        self.assertTrue(callable(bot.remove_enforced_ban))
        sig = inspect.signature(bot.remove_enforced_ban)
        self.assertEqual(list(sig.parameters), ["server_id", "discord_id"])

    def test_remove_enforced_ban_deletes_and_closes_connection(self):
        fake_cursor = Mock()
        fake_cursor.__enter__ = Mock(return_value=fake_cursor)
        fake_cursor.__exit__ = Mock(return_value=False)
        fake_cursor.execute = Mock()

        fake_conn = Mock()
        fake_conn.__enter__ = Mock(return_value=fake_conn)
        fake_conn.__exit__ = Mock(return_value=False)
        fake_conn.cursor = Mock(return_value=fake_cursor)
        fake_conn.close = Mock()

        with patch.object(bot, "get_db_connection", return_value=fake_conn):
            bot.remove_enforced_ban(999, 123456789012345678)

        fake_cursor.execute.assert_called_once()
        sql = fake_cursor.execute.call_args.args[0]
        self.assertIn("DELETE FROM public.enforced_bans", sql)
        fake_conn.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
