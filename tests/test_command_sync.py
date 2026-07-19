"""
Unit tests for BotBanner.setup_hook's command-sync behavior (bug #7 in
NOTES.md): DEV_GUILD_ID, if set, syncs to a single guild for near-instant
propagation during development instead of the slow global sync.

Run via (from repo root):
    .venv\\Scripts\\python.exe -m unittest tests.test_command_sync -v
"""
import unittest
from unittest.mock import Mock, AsyncMock, patch

import discord

import bot


class SetupHookSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_dev_guild_id_syncs_globally(self):
        with patch.object(bot, "DEV_GUILD_ID", None), \
             patch.object(bot.bot, "tree") as mock_tree:
            mock_tree.add_command = Mock()
            mock_tree.sync = AsyncMock()
            mock_tree.copy_global_to = Mock()
            await bot.bot.setup_hook()

        mock_tree.sync.assert_awaited_once_with()
        mock_tree.copy_global_to.assert_not_called()

    async def test_dev_guild_id_set_syncs_to_that_guild_only(self):
        dev_guild_id = 999888777
        with patch.object(bot, "DEV_GUILD_ID", dev_guild_id), \
             patch.object(bot.bot, "tree") as mock_tree:
            mock_tree.add_command = Mock()
            mock_tree.sync = AsyncMock()
            mock_tree.copy_global_to = Mock()
            await bot.bot.setup_hook()

        mock_tree.copy_global_to.assert_called_once()
        copy_kwargs = mock_tree.copy_global_to.call_args.kwargs
        self.assertEqual(copy_kwargs["guild"].id, dev_guild_id)

        mock_tree.sync.assert_awaited_once()
        sync_kwargs = mock_tree.sync.call_args.kwargs
        self.assertEqual(sync_kwargs["guild"].id, dev_guild_id)

    async def test_sync_failure_is_caught_and_logged_not_raised(self):
        with patch.object(bot, "DEV_GUILD_ID", None), \
             patch.object(bot.bot, "tree") as mock_tree, \
             patch.object(bot.log, "warning") as mock_warning:
            mock_tree.add_command = Mock()
            mock_tree.sync = AsyncMock(side_effect=discord.HTTPException(Mock(status=500), "server error"))
            await bot.bot.setup_hook()  # must not raise

        mock_warning.assert_called_once()


class DevGuildIdParsingTests(unittest.TestCase):
    def test_unset_is_none(self):
        self.assertIsNone(bot._parse_optional_int_env(None, "DEV_GUILD_ID"))

    def test_malformed_value_does_not_raise(self):
        try:
            result = bot._parse_optional_int_env("not-an-id", "DEV_GUILD_ID")
        except ValueError:
            self.fail("_parse_optional_int_env must not raise on a malformed value")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
