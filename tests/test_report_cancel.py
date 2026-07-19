"""
Unit tests for the /banner report-cancel command (bot.report_cancel_cmd).

Added after the adversarial review noted there was no operator tooling to
recover a report stuck at status='pending' (e.g. its review message was
deleted out-of-band, or -- before the finding #2 fix -- a failed
submission had orphaned it). This command lets a review-role holder force
-clear one by id.

Run via (from repo root):
    .venv\\Scripts\\python.exe -m unittest tests.test_report_cancel -v
"""
import unittest
from unittest.mock import Mock, AsyncMock, patch

import discord

import bot


REVIEW_ROLE_ID = 444555666


def make_interaction(user):
    interaction = Mock()
    interaction.user = user
    interaction.response = Mock()
    interaction.response.send_message = AsyncMock()
    return interaction


def make_member(has_role: bool):
    member = Mock(spec=discord.Member)
    member.get_role = Mock(return_value=Mock() if has_role else None)
    return member


class ReportCancelCmdTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_member_denied(self):
        interaction = make_interaction(Mock())  # not Member-spec'd
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report") as mock_get_report:
            await bot.report_cancel_cmd.callback(interaction, 7)

        interaction.response.send_message.assert_awaited_once_with(
            "You don't have permission to manage reports.", ephemeral=True
        )
        mock_get_report.assert_not_called()

    async def test_member_without_role_denied(self):
        member = make_member(has_role=False)
        interaction = make_interaction(member)
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report") as mock_get_report:
            await bot.report_cancel_cmd.callback(interaction, 7)

        interaction.response.send_message.assert_awaited_once_with(
            "You don't have permission to manage reports.", ephemeral=True
        )
        mock_get_report.assert_not_called()

    async def test_unknown_report_id(self):
        member = make_member(has_role=True)
        interaction = make_interaction(member)
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report", return_value=None), \
             patch.object(bot, "delete_report") as mock_delete:
            await bot.report_cancel_cmd.callback(interaction, 999)

        interaction.response.send_message.assert_awaited_once_with(
            "No report with id #999.", ephemeral=True
        )
        mock_delete.assert_not_called()

    async def test_already_decided_report_is_not_deleted(self):
        member = make_member(has_role=True)
        interaction = make_interaction(member)
        report = {"id": 7, "status": "approved"}
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report", return_value=report), \
             patch.object(bot, "delete_report") as mock_delete:
            await bot.report_cancel_cmd.callback(interaction, 7)

        interaction.response.send_message.assert_awaited_once_with(
            "Report #7 is already approved, nothing to cancel.", ephemeral=True
        )
        mock_delete.assert_not_called()

    async def test_pending_report_is_cancelled(self):
        member = make_member(has_role=True)
        interaction = make_interaction(member)
        report = {"id": 7, "status": "pending"}
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report", return_value=report), \
             patch.object(bot, "delete_report") as mock_delete:
            await bot.report_cancel_cmd.callback(interaction, 7)

        mock_delete.assert_called_once_with(7)
        interaction.response.send_message.assert_awaited_once()
        self.assertIn("cancelled", interaction.response.send_message.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
