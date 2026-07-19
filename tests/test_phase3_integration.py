"""
Integration tests targeting the seam between two Phase 3 changes that were
implemented independently and then merged:

1. Every blocking psycopg2 DB helper call site in bot.py was wrapped in
   asyncio.to_thread (see tests/test_async_db_offload.py), including
   start_loop_if_needed being converted from a sync function to an async one.

2. enforce_bans_for_guild stopped re-downloading a guild's entire live ban
   list every cycle, diffing instead against a new local Postgres table via
   get_enforced_ban_ids/record_enforced_ban, with a force_refresh=True escape
   hatch for /banner sync-now (see tests/test_ban_enforcement.py).

get_enforced_ban_ids/record_enforced_ban did not exist yet when
test_async_db_offload.py was written, so its to_thread-offload assertions
never covered them -- they were wired up by hand during integration. This
file exists to cover exactly that seam, plus a few other places where the
two changes interact (ordering guarantees once DB reads/writes move behind
awaited to_thread calls; start_loop_if_needed's four call sites actually
awaiting it now that it's a coroutine; failure-mode swallowing for the new
cache path), without duplicating what each original author already tested
well in their own file.

Run via (from repo root):
    .venv\\Scripts\\python.exe -m unittest tests.test_phase3_integration -v
"""
import unittest
from unittest.mock import Mock, AsyncMock, PropertyMock, patch

import discord

import bot


async def _run_inline(func, *args, **kwargs):
    """Stand-in for asyncio.to_thread that runs the callable synchronously in
    place (no real thread), so control flow proceeds exactly as in production
    while letting us assert on how asyncio.to_thread itself was invoked."""
    return func(*args, **kwargs)


def make_guild(live_ban_ids=()):
    """Mock guild. live_ban_ids simulates Discord's live ban list, only
    consulted when enforce_bans_for_guild runs with force_refresh=True."""
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


def make_interaction():
    interaction = Mock()
    interaction.guild = Mock()
    interaction.guild.id = 12345
    interaction.guild.name = "Test Guild"
    interaction.guild.owner_id = 1
    interaction.user = Mock()
    interaction.user.id = 999
    interaction.response = Mock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = Mock()
    interaction.followup.send = AsyncMock()
    return interaction


# ---------------------------------------------------------------------------
# 1. The integration seam: get_enforced_ban_ids / record_enforced_ban must
#    actually be invoked via asyncio.to_thread, at every call site inside
#    enforce_bans_for_guild -- not called directly on the event loop.
# ---------------------------------------------------------------------------
class EnforcedBansCacheOffloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._sleep_patch = patch("asyncio.sleep", new=AsyncMock())
        self._sleep_patch.start()
        self.addAsyncCleanup(self._sleep_patch.stop)

    async def test_get_enforced_ban_ids_goes_through_asyncio_to_thread(self):
        guild = make_guild()
        target = 123456789012345678
        with patch("asyncio.to_thread", side_effect=_run_inline) as mock_to_thread, \
             patch.object(bot, "get_enforced_ban_ids", return_value=set()) as mock_get_enforced, \
             patch.object(bot, "record_enforced_ban"):
            await bot.enforce_bans_for_guild(guild, info_channel_id=999, spammer_ids=[target])

        mock_to_thread.assert_any_call(mock_get_enforced, guild.id)

    async def test_record_enforced_ban_on_successful_ban_goes_through_asyncio_to_thread(self):
        guild = make_guild()
        target = 123456789012345678
        with patch("asyncio.to_thread", side_effect=_run_inline) as mock_to_thread, \
             patch.object(bot, "get_enforced_ban_ids", return_value=set()), \
             patch.object(bot, "record_enforced_ban") as mock_record:
            await bot.enforce_bans_for_guild(guild, info_channel_id=999, spammer_ids=[target])

        mock_to_thread.assert_any_call(mock_record, guild.id, target)

    async def test_record_enforced_ban_in_force_refresh_backfill_loop_goes_through_asyncio_to_thread(self):
        # This is the backfill inside `async for ban_entry in guild.bans(...)`,
        # only reachable with force_refresh=True.
        already_live = 111111111111111111
        guild = make_guild(live_ban_ids=[already_live])
        with patch("asyncio.to_thread", side_effect=_run_inline) as mock_to_thread, \
             patch.object(bot, "get_enforced_ban_ids", return_value=set()), \
             patch.object(bot, "record_enforced_ban") as mock_record:
            await bot.enforce_bans_for_guild(
                guild, info_channel_id=999, spammer_ids=[already_live], force_refresh=True
            )

        mock_to_thread.assert_any_call(mock_record, guild.id, already_live)

    async def test_record_enforced_ban_in_30035_branch_goes_through_asyncio_to_thread(self):
        target = 123456789012345678
        guild = make_guild()
        err = discord.HTTPException(Mock(status=400), "Already banned")
        err.code = 30035
        guild.ban = AsyncMock(side_effect=err)

        with patch("asyncio.to_thread", side_effect=_run_inline) as mock_to_thread, \
             patch.object(bot, "get_enforced_ban_ids", return_value=set()), \
             patch.object(bot, "record_enforced_ban") as mock_record:
            await bot.enforce_bans_for_guild(guild, info_channel_id=999, spammer_ids=[target])

        mock_to_thread.assert_any_call(mock_record, guild.id, target)

    async def test_get_enforced_ban_ids_failure_via_asyncio_to_thread_falls_back_to_empty_set(self):
        # Combines gap 1 (is it really offloaded?) with gap 4 (does a failure
        # get swallowed?): the exception must originate from *inside* a real
        # asyncio.to_thread call and still be caught.
        target = 123456789012345678
        guild = make_guild()
        with patch("asyncio.to_thread", side_effect=_run_inline) as mock_to_thread, \
             patch.object(bot, "get_enforced_ban_ids", side_effect=RuntimeError("db exploded")) as mock_get_enforced, \
             patch.object(bot, "record_enforced_ban"):
            new_count = await bot.enforce_bans_for_guild(guild, info_channel_id=999, spammer_ids=[target])

        mock_to_thread.assert_any_call(mock_get_enforced, guild.id)
        self.assertEqual(new_count, 1)
        guild.ban.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. start_loop_if_needed became async. A dropped `await` at any call site is
#    silent: the coroutine object is created but its body -- including the
#    get_enabled_configured_servers check and enforce_bans_loop.start() --
#    never executes, and nothing raises or logs. These tests invoke the real
#    callers and assert the loop's *actual* start side effect happened, so a
#    reverted `await` fails the test instead of silently no-opping.
# ---------------------------------------------------------------------------
class StartLoopIfNeededAwaitedByCallersTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._is_running_patch = patch.object(bot.enforce_bans_loop, "is_running", return_value=False)
        self._is_running_patch.start()
        self.addCleanup(self._is_running_patch.stop)

        self._start_patch = patch.object(bot.enforce_bans_loop, "start")
        self.mock_loop_start = self._start_patch.start()
        self.addCleanup(self._start_patch.stop)

        self._get_enabled_patch = patch.object(bot, "get_enabled_configured_servers", return_value=[(1, 2)])
        self.mock_get_enabled = self._get_enabled_patch.start()
        self.addCleanup(self._get_enabled_patch.stop)

    async def test_on_guild_join_actually_starts_the_loop(self):
        guild = Mock()
        guild.id = 12345
        guild.owner_id = 999
        with patch.object(bot, "upsert_server"):
            await bot.on_guild_join(guild)

        self.mock_loop_start.assert_called_once()

    async def test_set_channel_cmd_actually_starts_the_loop(self):
        interaction = make_interaction()
        guild = interaction.guild
        channel = Mock(spec=discord.TextChannel)
        channel.id = 555666
        channel.name = "info"

        # Not "first time" (enabler False both times) so this test isolates
        # start_loop_if_needed's own effect from the enforce-on-first-setup branch.
        with patch.object(bot, "get_server_info", return_value={"info_channel_id": 555666, "enabler": False}), \
             patch.object(bot, "upsert_server"), \
             patch.object(bot, "set_info_channel"), \
             patch.object(bot, "enforce_bans_for_guild", new=AsyncMock(return_value=0)) as mock_enforce:
            await bot.set_channel_cmd.callback(interaction, channel)

        mock_enforce.assert_not_awaited()
        self.mock_loop_start.assert_called_once()

    async def test_enable_cmd_background_task_actually_starts_the_loop(self):
        # start_loop_if_needed is awaited inside enable_cmd's `_run_enforcement`
        # closure, itself fire-and-forgotten via asyncio.create_task. Capture the
        # coroutine passed to create_task and await it directly so the test
        # observes the closure's real completed effect instead of racing it.
        interaction = make_interaction()

        captured = []

        def fake_create_task(coro, *a, **kw):
            captured.append(coro)
            return Mock()

        with patch("asyncio.create_task", side_effect=fake_create_task), \
             patch.object(bot, "upsert_server"), \
             patch.object(bot, "set_enabler"), \
             patch.object(bot, "get_server_info", return_value={"info_channel_id": 555, "enabler": True}), \
             patch.object(bot, "enforce_bans_for_guild", new=AsyncMock(return_value=0)) as mock_enforce:
            await bot.enable_cmd.callback(interaction, True)

            self.assertEqual(len(captured), 1, "enable_cmd should have scheduled exactly one background task")
            # Must await the captured coroutine while the patches above are still
            # active -- it doesn't execute any of its body until awaited, so
            # awaiting it after the `with` block exits would run it against the
            # *real*, unpatched bot.enforce_bans_for_guild instead.
            await captured[0]

            mock_enforce.assert_awaited_once()
        self.mock_loop_start.assert_called_once()

    async def test_on_ready_actually_starts_the_loop(self):
        with patch.object(type(bot.bot), "user", new_callable=PropertyMock, return_value=Mock(id=1)), \
             patch.object(type(bot.bot), "guilds", new_callable=PropertyMock, return_value=[]), \
             patch.object(bot, "ensure_tables"), \
             patch.object(bot, "get_all_pending_reports", return_value=[]), \
             patch.object(bot, "_check_review_config_health", new=AsyncMock()), \
             patch.object(bot, "enforce_bans_once_global", new=AsyncMock()):
            await bot.on_ready()

        self.mock_loop_start.assert_called_once()


# ---------------------------------------------------------------------------
# 3. Ordering/atomicity: set_channel_cmd's "first time setup" detection reads
#    info_before, performs two writes, then reads info_after -- now four
#    separate awaited asyncio.to_thread calls instead of four direct calls.
#    Confirm they still run in the intended order and the detection logic
#    still works, rather than just asserting each call happened somewhere.
# ---------------------------------------------------------------------------
class SetChannelCmdOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_time_setup_detection_survives_offload_to_worker_threads(self):
        interaction = make_interaction()
        guild = interaction.guild
        channel = Mock(spec=discord.TextChannel)
        channel.id = 777888
        channel.name = "info"

        call_log = []
        state = {"calls": 0}

        def fake_get_server_info(server_id):
            call_log.append("get_server_info")
            state["calls"] += 1
            if state["calls"] == 1:
                # info_before: nothing configured yet.
                return None
            # info_after: reflects the writes below, already enabled.
            return {"server_id": guild.id, "owner_id": guild.owner_id, "info_channel_id": channel.id, "enabler": True}

        def fake_upsert_server(*a, **k):
            call_log.append("upsert_server")

        def fake_set_info_channel(*a, **k):
            call_log.append("set_info_channel")

        with patch("asyncio.to_thread", side_effect=_run_inline), \
             patch.object(bot, "get_server_info", side_effect=fake_get_server_info), \
             patch.object(bot, "upsert_server", side_effect=fake_upsert_server), \
             patch.object(bot, "set_info_channel", side_effect=fake_set_info_channel), \
             patch.object(bot, "enforce_bans_for_guild", new=AsyncMock(return_value=0)) as mock_enforce, \
             patch.object(bot, "get_enabled_configured_servers", return_value=[]):
            await bot.set_channel_cmd.callback(interaction, channel)

        self.assertEqual(
            call_log,
            ["get_server_info", "upsert_server", "set_info_channel", "get_server_info"],
            "info_before must be read before the writes, and info_after only after both writes complete",
        )
        mock_enforce.assert_awaited_once_with(guild, channel.id)

    async def test_not_first_time_setup_does_not_trigger_enforcement(self):
        interaction = make_interaction()
        guild = interaction.guild
        channel = Mock(spec=discord.TextChannel)
        channel.id = 999000
        channel.name = "info"

        # Already configured before this call (info_channel_id present) -> not
        # "first time", regardless of enabler -- must not re-run enforcement.
        already_configured = {
            "server_id": guild.id,
            "owner_id": guild.owner_id,
            "info_channel_id": 111222,
            "enabler": True,
        }

        with patch("asyncio.to_thread", side_effect=_run_inline), \
             patch.object(bot, "get_server_info", return_value=already_configured), \
             patch.object(bot, "upsert_server"), \
             patch.object(bot, "set_info_channel"), \
             patch.object(bot, "enforce_bans_for_guild", new=AsyncMock(return_value=0)) as mock_enforce, \
             patch.object(bot, "get_enabled_configured_servers", return_value=[]):
            await bot.set_channel_cmd.callback(interaction, channel)

        mock_enforce.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4. Failure-mode coverage for the new enforced_bans cache path: a
#    record_enforced_ban failure (e.g. transient DB hiccup right after a
#    successful guild.ban()) must be logged and swallowed, not propagate and
#    abort the rest of the per-guild enforcement loop.
# ---------------------------------------------------------------------------
class RecordEnforcedBanFailureSwallowingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._sleep_patch = patch("asyncio.sleep", new=AsyncMock())
        self._sleep_patch.start()
        self.addAsyncCleanup(self._sleep_patch.stop)

    async def test_record_enforced_ban_failure_after_successful_bans_does_not_crash_loop(self):
        target1 = 111111111111111111
        target2 = 222222222222222222
        guild = make_guild()

        with patch.object(bot, "get_enforced_ban_ids", return_value=set()), \
             patch.object(bot, "record_enforced_ban", side_effect=RuntimeError("db hiccup")):
            new_count = await bot.enforce_bans_for_guild(
                guild, info_channel_id=999, spammer_ids=[target1, target2]
            )

        # Both bans still happen on Discord's side even though persisting the
        # local record failed both times.
        self.assertEqual(new_count, 2)
        self.assertEqual(guild.ban.await_count, 2)

    async def test_record_enforced_ban_failure_in_force_refresh_backfill_does_not_crash(self):
        already_live = 111111111111111111
        needs_ban = 222222222222222222
        guild = make_guild(live_ban_ids=[already_live])

        with patch.object(bot, "get_enforced_ban_ids", return_value=set()), \
             patch.object(bot, "record_enforced_ban", side_effect=RuntimeError("db hiccup")):
            new_count = await bot.enforce_bans_for_guild(
                guild, info_channel_id=999, spammer_ids=[already_live, needs_ban], force_refresh=True
            )

        # The backfill's persistence failing must not stop the diff from being
        # computed correctly: already_live is still treated as handled
        # in-memory (not re-banned), and needs_ban is still banned.
        self.assertEqual(new_count, 1)
        guild.ban.assert_awaited_once_with(
            discord.Object(id=needs_ban),
            reason=unittest.mock.ANY,
            delete_message_seconds=0,
        )

    async def test_record_enforced_ban_failure_in_30035_branch_does_not_crash_loop(self):
        already_banned_elsewhere = 111111111111111111  # triggers 30035
        needs_ban = 222222222222222222  # succeeds normally
        guild = make_guild()

        err = discord.HTTPException(Mock(status=400), "Already banned")
        err.code = 30035

        async def fake_ban(obj, **kwargs):
            if obj.id == already_banned_elsewhere:
                raise err
            return None

        guild.ban = AsyncMock(side_effect=fake_ban)

        with patch.object(bot, "get_enforced_ban_ids", return_value=set()), \
             patch.object(bot, "record_enforced_ban", side_effect=RuntimeError("db hiccup")):
            new_count = await bot.enforce_bans_for_guild(
                guild, info_channel_id=999, spammer_ids=[already_banned_elsewhere, needs_ban]
            )

        # The 30035 branch's own record_enforced_ban failure must not stop the
        # loop from continuing on to the other id.
        self.assertEqual(new_count, 1)


# ---------------------------------------------------------------------------
# 5. Other edge cases arising specifically from combining the two changes.
# ---------------------------------------------------------------------------
class MiscCombinedChangeEdgeCaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._sleep_patch = patch("asyncio.sleep", new=AsyncMock())
        self._sleep_patch.start()
        self.addAsyncCleanup(self._sleep_patch.stop)

    async def test_guild_bans_iteration_failure_during_force_refresh_does_not_crash(self):
        # If Discord's live guild.bans() pagination itself blows up mid-iteration
        # (not just the record_enforced_ban backfill call), the surrounding
        # try/except must still let the rest of enforce_bans_for_guild run using
        # whatever partial already_banned_ids it collected, rather than the
        # whole per-guild enforcement pass dying.
        target = 123456789012345678
        guild = make_guild()

        async def _raising_bans_iter(limit=None):
            raise RuntimeError("Discord API hiccup mid-pagination")
            yield  # pragma: no cover -- makes this an async generator

        guild.bans = Mock(side_effect=lambda limit=None: _raising_bans_iter(limit))

        with patch.object(bot, "get_enforced_ban_ids", return_value=set()), \
             patch.object(bot, "record_enforced_ban"):
            new_count = await bot.enforce_bans_for_guild(
                guild, info_channel_id=999, spammer_ids=[target], force_refresh=True
            )

        guild.bans.assert_called_once()
        self.assertEqual(new_count, 1)
        guild.ban.assert_awaited_once()

    async def test_sync_now_cmd_offloads_get_server_info_and_uses_force_refresh(self):
        # /banner sync-now is the only caller of force_refresh=True. Confirm its
        # own get_server_info read is offloaded and it actually passes
        # force_refresh=True through to enforce_bans_for_guild (not force_refresh
        # defaulting to False by a copy/paste of the automatic-path call).
        interaction = make_interaction()
        guild = interaction.guild

        captured = []

        def fake_create_task(coro, *a, **kw):
            captured.append(coro)
            return Mock()

        with patch("asyncio.create_task", side_effect=fake_create_task), \
             patch("asyncio.to_thread", side_effect=_run_inline) as mock_to_thread, \
             patch.object(bot, "get_server_info", return_value={"info_channel_id": 555, "enabler": True}) as mock_get_info, \
             patch.object(bot, "enforce_bans_for_guild", new=AsyncMock(return_value=3)) as mock_enforce:
            await bot.sync_now_cmd.callback(interaction)

            mock_to_thread.assert_any_call(mock_get_info, guild.id)
            self.assertEqual(len(captured), 1)
            # Await the captured background-task coroutine while patches are
            # still active -- see the comment in
            # test_enable_cmd_background_task_actually_starts_the_loop above.
            await captured[0]

            mock_enforce.assert_awaited_once_with(guild, 555, force_refresh=True)


if __name__ == "__main__":
    unittest.main()
