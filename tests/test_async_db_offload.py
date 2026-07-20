"""
Unit tests proving DB helper calls are actually offloaded off the event loop
via asyncio.to_thread (Bug 3 in NOTES.md).

Every synchronous psycopg2 DB helper in bot.py used to be called directly
from async Discord event handlers / slash-command callbacks with zero
offloading. Since discord.py's asyncio event loop also drives the gateway
heartbeat and every other guild's events, any blocking DB call -- even one
taking a few hundred ms under load or DB latency -- stalled everything else
the bot was doing, not just the one interaction being served. The worst
concrete case was set_channel_cmd doing two blocking DB round-trips before
ever acknowledging the interaction, at real risk of Discord's 3-second
interaction timeout under DB latency.

The rest of the suite (test_report_cmd.py, test_review_decision.py, etc.)
patches DB helpers directly and asserts they were *called with the right
args* -- that keeps passing whether or not the call is offloaded, since
asyncio.to_thread(func, *args) just invokes func(*args) in a worker thread.
It would NOT catch a regression back to a direct blocking call. These tests
close that gap by patching asyncio.to_thread itself and asserting it is the
thing invoking the DB helpers, for a representative sample of call sites
across different commands/handlers.

Note: assertions on asyncio.to_thread's call args must reference the patched
Mock *objects* (captured while the `with patch.object(...)` block is still
open), not `bot.some_helper` looked up afterwards -- once the patch context
exits, `bot.some_helper` is restored to the original function, which is not
the object that was actually passed to asyncio.to_thread during the call.

Run via (from repo root):
    .venv\\Scripts\\python.exe -m unittest tests.test_async_db_offload -v
"""
import unittest
from unittest.mock import Mock, AsyncMock, PropertyMock, call, patch

import discord

import bot


async def _run_inline(func, *args, **kwargs):
    """Stand-in for asyncio.to_thread that runs the callable synchronously
    in-place (no real thread), so the rest of the caller's control flow
    proceeds exactly as it would in production, while still letting us
    assert on how asyncio.to_thread itself was invoked."""
    return func(*args, **kwargs)


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


class SetChannelCmdOffloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_db_calls_go_through_asyncio_to_thread(self):
        interaction = make_interaction()
        guild = interaction.guild
        channel = Mock(spec=discord.TextChannel)
        channel.id = 555666
        channel.name = "info"

        with patch("asyncio.to_thread", side_effect=_run_inline) as mock_to_thread, \
             patch.object(bot, "get_server_info", return_value={"info_channel_id": None, "enabler": False}) as mock_get_info, \
             patch.object(bot, "upsert_server") as mock_upsert, \
             patch.object(bot, "set_info_channel") as mock_set_channel, \
             patch.object(bot, "get_enabled_configured_servers", return_value=[]):
            await bot.set_channel_cmd.callback(interaction, channel)

            # The bug being fixed: these DB calls must be routed through
            # asyncio.to_thread, not invoked directly on the event loop.
            # (Assertions run inside the `with` block so the mocks referenced
            # below are the exact objects asyncio.to_thread was called with.)
            mock_to_thread.assert_any_call(mock_upsert, guild.id, guild.owner_id)
            mock_to_thread.assert_any_call(mock_set_channel, guild.id, channel.id)
            # get_server_info is called twice here (info_before and info_after) with
            # identical args -- assert_any_call alone would still pass even if only one
            # of the two were offloaded (the other match hides the regression), so tie
            # every actual invocation of get_server_info back to a to_thread call.
            self.assertEqual(
                mock_to_thread.call_args_list.count(call(mock_get_info, guild.id)),
                mock_get_info.call_count,
            )

        # Sanity: the underlying helpers were still actually invoked (via the
        # inline stand-in), so the command's own logic is unaffected.
        mock_get_info.assert_called()
        mock_upsert.assert_called_once_with(guild.id, guild.owner_id)
        mock_set_channel.assert_called_once_with(guild.id, channel.id)


class ReportCmdOffloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_is_spammer_id_and_create_report_go_through_asyncio_to_thread(self):
        interaction = make_interaction()
        evidence = Mock(spec=discord.Attachment)
        evidence.content_type = "image/png"
        evidence.size = 1024
        evidence.filename = "proof.png"
        evidence.read = AsyncMock(return_value=b"fake-image-bytes")

        review_channel = Mock(spec=discord.TextChannel)
        sent_message = Mock()
        sent_message.id = 987654321
        review_channel.send = AsyncMock(return_value=sent_message)

        with patch("asyncio.to_thread", side_effect=_run_inline) as mock_to_thread, \
             patch.object(bot, "REVIEW_CHANNEL_ID", 111222333), \
             patch.object(bot, "REVIEW_ROLE_ID", 444555666), \
             patch.object(bot, "count_recent_reports_by_reporter", return_value=0), \
             patch.object(bot, "is_spammer_id", return_value=False) as mock_is_spammer, \
             patch.object(bot, "get_pending_report_for_target", return_value=None), \
             patch.object(bot, "create_report", return_value=42) as mock_create, \
             patch.object(bot, "set_report_review_message") as mock_set_msg, \
             patch.object(bot.bot, "get_channel", return_value=review_channel), \
             patch.object(bot.bot, "fetch_user", new=AsyncMock(return_value=None)), \
             patch.object(bot, "build_report_embed", return_value=Mock()), \
             patch.object(bot, "ReportReviewView", return_value=Mock()):
            await bot.report_cmd.callback(interaction, "123456789012345678", evidence)

            mock_to_thread.assert_any_call(mock_is_spammer, 123456789012345678)
            mock_to_thread.assert_any_call(mock_create, 123456789012345678, interaction.user.id, interaction.guild.id)
            mock_to_thread.assert_any_call(mock_set_msg, 42, 987654321)

        mock_is_spammer.assert_called_once()
        mock_create.assert_called_once()
        mock_set_msg.assert_called_once_with(42, 987654321)


class EnforceBansForGuildOffloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._sleep_patch = patch("asyncio.sleep", new=AsyncMock())
        self._sleep_patch.start()
        self.addAsyncCleanup(self._sleep_patch.stop)

    async def test_fallback_get_spammer_ids_goes_through_asyncio_to_thread(self):
        # spammer_ids is deliberately omitted so enforce_bans_for_guild falls back to
        # calling get_spammer_ids() itself -- that fallback call is the one that must
        # be offloaded.
        guild = Mock(spec=discord.Guild)
        guild.id = 12345

        async def _empty_bans(limit=None):
            return
            yield  # pragma: no cover

        guild.bans = Mock(side_effect=lambda limit=None: _empty_bans(limit))
        guild.ban = AsyncMock()

        with patch("asyncio.to_thread", side_effect=_run_inline) as mock_to_thread, \
             patch.object(bot, "get_spammer_ids", return_value=[123456789012345678]) as mock_get_ids:
            new_count = await bot.enforce_bans_for_guild(guild, info_channel_id=999)

            mock_to_thread.assert_any_call(mock_get_ids)

        mock_get_ids.assert_called_once()
        self.assertEqual(new_count, 1)

    async def test_remove_spammer_id_on_unknown_user_goes_through_asyncio_to_thread(self):
        guild = Mock(spec=discord.Guild)
        guild.id = 12345
        target = 123456789012345678

        async def _empty_bans(limit=None):
            return
            yield  # pragma: no cover

        guild.bans = Mock(side_effect=lambda limit=None: _empty_bans(limit))
        http_error = discord.HTTPException(Mock(status=404, headers={}), "Unknown User")
        http_error.code = 10013
        guild.ban = AsyncMock(side_effect=http_error)

        with patch("asyncio.to_thread", side_effect=_run_inline) as mock_to_thread, \
             patch.object(bot, "remove_spammer_id") as mock_remove:
            await bot.enforce_bans_for_guild(guild, info_channel_id=999, spammer_ids=[target])

            mock_to_thread.assert_any_call(mock_remove, target)

        mock_remove.assert_called_once_with(target)


class OnReadyOffloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_db_calls_go_through_asyncio_to_thread(self):
        guild1 = Mock()
        guild1.id = 111
        guild1.owner_id = 1
        guild2 = Mock()
        guild2.id = 222
        guild2.owner_id = 2

        with patch("asyncio.to_thread", side_effect=_run_inline) as mock_to_thread, \
             patch.object(type(bot.bot), "user", new_callable=PropertyMock, return_value=Mock(id=1)), \
             patch.object(type(bot.bot), "guilds", new_callable=PropertyMock, return_value=[guild1, guild2]), \
             patch.object(bot, "ensure_tables") as mock_ensure_tables, \
             patch.object(bot, "upsert_server") as mock_upsert, \
             patch.object(bot, "get_all_pending_reports", return_value=[]) as mock_get_pending, \
             patch.object(bot, "get_enabled_configured_servers", return_value=[]) as mock_get_enabled, \
             patch.object(bot, "_check_review_config_health", new=AsyncMock()), \
             patch.object(bot, "enforce_bans_once_global", new=AsyncMock()):
            await bot.on_ready()

            # ensure_tables is deliberately NOT offloaded (see the comment above its
            # call site in on_ready): it must run as a direct blocking call so it
            # monopolizes the event loop for its short, one-time-real duration,
            # guaranteeing no interaction can be dispatched and hit a table that
            # doesn't exist yet on a fresh database's very first on_ready.
            to_thread_calls = [c.args[0] for c in mock_to_thread.call_args_list]
            self.assertNotIn(mock_ensure_tables, to_thread_calls)

            mock_to_thread.assert_any_call(mock_upsert, guild1.id, guild1.owner_id)
            mock_to_thread.assert_any_call(mock_upsert, guild2.id, guild2.owner_id)
            mock_to_thread.assert_any_call(mock_get_pending)
            mock_to_thread.assert_any_call(mock_get_enabled)

        mock_ensure_tables.assert_called_once()
        self.assertEqual(mock_upsert.call_count, 2)
        mock_get_pending.assert_called_once()


if __name__ == "__main__":
    unittest.main()
