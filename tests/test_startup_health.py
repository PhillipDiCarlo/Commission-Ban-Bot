"""
Unit tests for bot._check_review_config_health, called from on_ready.

Added after the adversarial review noted that a deleted REVIEW_ROLE_ID (or an
unresolvable REVIEW_CHANNEL_ID) fails closed *silently* -- reports would pile
up unreviewable with nothing telling the operator why. This function logs a
clear warning at startup instead.

Run via (from repo root):
    .venv\\Scripts\\python.exe -m unittest tests.test_startup_health -v
"""
import unittest
from unittest.mock import Mock, AsyncMock, patch

import discord

import bot


class CheckReviewConfigHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_does_nothing(self):
        with patch.object(bot, "REVIEW_CHANNEL_ID", None), \
             patch.object(bot, "REVIEW_ROLE_ID", None), \
             patch.object(bot.bot, "get_channel") as mock_get_channel, \
             patch.object(bot.log, "warning") as mock_warning:
            await bot._check_review_config_health()

        mock_get_channel.assert_not_called()
        mock_warning.assert_not_called()

    async def test_unresolvable_channel_warns(self):
        with patch.object(bot, "REVIEW_CHANNEL_ID", 111), \
             patch.object(bot, "REVIEW_ROLE_ID", 222), \
             patch.object(bot.bot, "get_channel", return_value=None), \
             patch.object(bot.bot, "fetch_channel", new=AsyncMock(side_effect=discord.NotFound(Mock(status=404), "Unknown Channel"))), \
             patch.object(bot.log, "warning") as mock_warning:
            await bot._check_review_config_health()

        mock_warning.assert_called_once()
        self.assertIn("REVIEW_CHANNEL_ID", mock_warning.call_args.args[0])

    async def test_deleted_role_warns(self):
        channel = Mock(spec=discord.TextChannel)
        channel.guild = Mock()
        channel.guild.id = 555
        channel.guild.get_role = Mock(return_value=None)
        with patch.object(bot, "REVIEW_CHANNEL_ID", 111), \
             patch.object(bot, "REVIEW_ROLE_ID", 222), \
             patch.object(bot.bot, "get_channel", return_value=channel), \
             patch.object(bot.log, "warning") as mock_warning:
            await bot._check_review_config_health()

        mock_warning.assert_called_once()
        self.assertIn("REVIEW_ROLE_ID", mock_warning.call_args.args[0])
        channel.guild.get_role.assert_called_once_with(222)

    async def test_healthy_config_does_not_warn(self):
        channel = Mock(spec=discord.TextChannel)
        channel.guild = Mock()
        channel.guild.get_role = Mock(return_value=Mock())
        with patch.object(bot, "REVIEW_CHANNEL_ID", 111), \
             patch.object(bot, "REVIEW_ROLE_ID", 222), \
             patch.object(bot.bot, "get_channel", return_value=channel), \
             patch.object(bot.log, "warning") as mock_warning:
            await bot._check_review_config_health()

        mock_warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
