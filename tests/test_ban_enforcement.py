"""
Unit tests for bot.enforce_bans_for_guild, focused on the Phase 2 fix that
removed the "user was in the server" notification.

That notification was gated on guild.get_member(uid) is not None, but the
bot runs without the privileged Members intent and with an empty member
cache (see bot.py's module docstring) -- so the check almost always
returned False even for users who really were present, making the
notification dead code that would rarely if ever fire. It (and the
now-unused fetch_username_safe helper) were removed rather than adding the
privileged intent, consistent with the bot's explicit no-privileged-intent
design.

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


def make_guild(existing_ban_ids=()):
    guild = Mock(spec=discord.Guild)
    guild.id = 12345

    async def _bans_iter(limit=None):
        for uid in existing_ban_ids:
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

    async def test_already_banned_ids_are_skipped(self):
        target = 123456789012345678
        guild = make_guild(existing_ban_ids=[target])
        new_count = await bot.enforce_bans_for_guild(guild, info_channel_id=999, spammer_ids=[target])

        self.assertEqual(new_count, 0)
        guild.ban.assert_not_awaited()

    async def test_fetch_username_safe_helper_was_removed(self):
        # Regression guard: this helper existed only to support the removed
        # notification and should not have been left behind as dead code.
        self.assertFalse(hasattr(bot, "fetch_username_safe"))


if __name__ == "__main__":
    unittest.main()
