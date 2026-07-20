"""
Unit tests for ReportReviewView._handle_decision in bot.py.

Covers the review-decision state machine: permission gating, the
pending -> approved / pending -> rejected transitions, and the guards
against a missing or already-decided report.

bot.decide_report is called as the single source of truth for the
pending->decided transition (it does the status UPDATE and, on approval,
the ban-list insert in one DB transaction, returning False if the report
was no longer pending by the time it ran) -- these tests patch it as a
plain Mock returning True/False/raising, rather than asserting on a
separate add_spammer_id call.

Run via (from repo root):
    .venv\\Scripts\\python.exe -m unittest tests.test_review_decision -v

Must be run through the tests package (unittest discover / dotted module
name) so tests/__init__.py sets dummy DATABASE_URL/DISCORD_TOKEN before
`import bot` executes.
"""
import unittest
from unittest.mock import Mock, AsyncMock, patch

import discord

import bot


REVIEW_ROLE_ID = 999999


def make_interaction(user):
    """Build a Mock interaction with the pieces _handle_decision touches."""
    interaction = Mock()
    interaction.user = user
    interaction.response = Mock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = Mock()
    interaction.followup.send = AsyncMock()
    interaction.message = Mock()
    interaction.message.attachments = []
    interaction.message.edit = AsyncMock()
    return interaction


def make_member(has_role: bool, user_id: int = 42):
    """Member-spec'd mock so isinstance(user, discord.Member) is True."""
    member = Mock(spec=discord.Member)
    member.id = user_id
    member.get_role = Mock(return_value=Mock() if has_role else None)
    return member


class ReportReviewDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # discord.ui.View.__init__ creates an asyncio.Future internally, so
        # the view must be constructed while an event loop is running --
        # asyncSetUp (unlike setUp) runs inside the IsolatedAsyncioTestCase
        # loop.
        self.view = bot.ReportReviewView(report_id=7)

    # ---- permission gating ----

    async def test_no_permission_when_user_is_not_a_member(self):
        interaction = make_interaction(Mock())  # plain Mock, not Member-spec'd
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report") as mock_get_report:
            await self.view._handle_decision(interaction, "approved")

        interaction.response.send_message.assert_awaited_once_with(
            "You don't have permission to review reports.", ephemeral=True
        )
        mock_get_report.assert_not_called()

    async def test_no_permission_when_member_lacks_role(self):
        member = make_member(has_role=False)
        interaction = make_interaction(member)
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report") as mock_get_report:
            await self.view._handle_decision(interaction, "approved")

        interaction.response.send_message.assert_awaited_once_with(
            "You don't have permission to review reports.", ephemeral=True
        )
        mock_get_report.assert_not_called()
        member.get_role.assert_called_once_with(REVIEW_ROLE_ID)

    async def test_no_permission_when_review_role_id_unconfigured(self):
        member = make_member(has_role=True)
        interaction = make_interaction(member)
        with patch.object(bot, "REVIEW_ROLE_ID", None), \
             patch.object(bot, "get_report") as mock_get_report:
            await self.view._handle_decision(interaction, "approved")

        interaction.response.send_message.assert_awaited_once_with(
            "You don't have permission to review reports.", ephemeral=True
        )
        mock_get_report.assert_not_called()

    # ---- report lookup guards ----

    async def test_report_not_found(self):
        member = make_member(has_role=True)
        interaction = make_interaction(member)
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report", return_value=None), \
             patch.object(bot, "decide_report") as mock_decide:
            await self.view._handle_decision(interaction, "approved")

        interaction.response.send_message.assert_awaited_once_with(
            "This report no longer exists.", ephemeral=True
        )
        mock_decide.assert_not_called()

    async def test_already_reviewed_report_is_not_reprocessed(self):
        member = make_member(has_role=True)
        interaction = make_interaction(member)
        report = {
            "id": 7,
            "status": "approved",
            "target_user_id": 123456789012345678,
            "reporter_user_id": 111,
            "reporter_server_id": 222,
        }
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report", return_value=report), \
             patch.object(bot, "decide_report") as mock_decide:
            await self.view._handle_decision(interaction, "approved")

        interaction.response.send_message.assert_awaited_once_with(
            "Already reviewed (status: approved).", ephemeral=True
        )
        mock_decide.assert_not_called()
        interaction.response.defer.assert_not_called()

    # ---- happy paths ----
    # decide_report itself performs the ban-list insert atomically with the status
    # UPDATE (see bot.py), so these tests assert on decide_report's call args and
    # return value rather than a separate add_spammer_id call.

    async def test_approve_pending_report(self):
        member = make_member(has_role=True, user_id=555)
        interaction = make_interaction(member)
        target_id = 123456789012345678
        report = {
            "id": 7,
            "status": "pending",
            "target_user_id": target_id,
            "reporter_user_id": 111,
            "reporter_server_id": 222,
        }
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report", return_value=report), \
             patch.object(bot, "decide_report", return_value=True) as mock_decide, \
             patch.object(bot.bot, "fetch_user", new=AsyncMock(return_value=None)), \
             patch.object(bot, "_guild_name_for", return_value="Some Server"), \
             patch.object(bot, "build_report_embed", return_value=Mock()):
            await self.view._handle_decision(interaction, "approved")

        interaction.response.defer.assert_awaited_once()
        mock_decide.assert_called_once_with(7, "approved", member.id)
        interaction.message.edit.assert_awaited_once()
        self.assertTrue(len(self.view.children) > 0)
        for item in self.view.children:
            self.assertTrue(item.disabled)

    async def test_reject_pending_report(self):
        member = make_member(has_role=True, user_id=555)
        interaction = make_interaction(member)
        target_id = 123456789012345678
        report = {
            "id": 7,
            "status": "pending",
            "target_user_id": target_id,
            "reporter_user_id": 111,
            "reporter_server_id": 222,
        }
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report", return_value=report), \
             patch.object(bot, "decide_report", return_value=True) as mock_decide, \
             patch.object(bot.bot, "fetch_user", new=AsyncMock(return_value=None)), \
             patch.object(bot, "_guild_name_for", return_value="Some Server"), \
             patch.object(bot, "build_report_embed", return_value=Mock()):
            await self.view._handle_decision(interaction, "rejected")

        interaction.response.defer.assert_awaited_once()
        mock_decide.assert_called_once_with(7, "rejected", member.id)
        interaction.message.edit.assert_awaited_once()
        self.assertTrue(len(self.view.children) > 0)
        for item in self.view.children:
            self.assertTrue(item.disabled)

    # ---- reporter DM notification ----
    # After a decision is durably recorded, the reporter (report["reporter_user_id"])
    # should be best-effort DMed with the outcome. This must never be able to break the
    # rest of _handle_decision (the embed/message edit), since many users have DMs closed
    # to bots -- discord.Forbidden (or fetch_user raising discord.NotFound) here is a
    # routine, expected outcome, not an error.

    async def test_approve_notifies_reporter_with_approved_dm(self):
        member = make_member(has_role=True, user_id=555)
        interaction = make_interaction(member)
        target_id = 123456789012345678
        reporter_id = 111
        report = {
            "id": 7,
            "status": "pending",
            "target_user_id": target_id,
            "reporter_user_id": reporter_id,
            "reporter_server_id": 222,
        }
        reporter_user = Mock()
        reporter_user.send = AsyncMock()

        async def fake_fetch_user(uid):
            # target-user lookup (for the embed) returns None like other tests; the
            # reporter lookup returns a mock user whose .send() we can assert on.
            if uid == reporter_id:
                return reporter_user
            return None

        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report", return_value=report), \
             patch.object(bot, "decide_report", return_value=True), \
             patch.object(bot.bot, "fetch_user", new=AsyncMock(side_effect=fake_fetch_user)), \
             patch.object(bot, "_guild_name_for", return_value="Some Server"), \
             patch.object(bot, "build_report_embed", return_value=Mock()):
            await self.view._handle_decision(interaction, "approved")

        interaction.message.edit.assert_awaited_once()
        reporter_user.send.assert_awaited_once()
        dm_text = reporter_user.send.await_args.args[0]
        self.assertIn("approved", dm_text)
        self.assertIn("7", dm_text)

    async def test_reject_notifies_reporter_with_rejected_dm(self):
        member = make_member(has_role=True, user_id=555)
        interaction = make_interaction(member)
        target_id = 123456789012345678
        reporter_id = 111
        report = {
            "id": 7,
            "status": "pending",
            "target_user_id": target_id,
            "reporter_user_id": reporter_id,
            "reporter_server_id": 222,
        }
        reporter_user = Mock()
        reporter_user.send = AsyncMock()

        async def fake_fetch_user(uid):
            if uid == reporter_id:
                return reporter_user
            return None

        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report", return_value=report), \
             patch.object(bot, "decide_report", return_value=True), \
             patch.object(bot.bot, "fetch_user", new=AsyncMock(side_effect=fake_fetch_user)), \
             patch.object(bot, "_guild_name_for", return_value="Some Server"), \
             patch.object(bot, "build_report_embed", return_value=Mock()):
            await self.view._handle_decision(interaction, "rejected")

        interaction.message.edit.assert_awaited_once()
        reporter_user.send.assert_awaited_once()
        dm_text = reporter_user.send.await_args.args[0]
        self.assertIn("rejected", dm_text)
        self.assertIn("7", dm_text)

    async def test_dm_forbidden_does_not_break_rest_of_handler(self):
        # The single most important case: user.send() raising discord.Forbidden (DMs
        # closed) must be swallowed entirely -- the embed/message edit already happened
        # before the DM attempt, and no exception should propagate out of
        # _handle_decision.
        member = make_member(has_role=True, user_id=555)
        interaction = make_interaction(member)
        target_id = 123456789012345678
        reporter_id = 111
        report = {
            "id": 7,
            "status": "pending",
            "target_user_id": target_id,
            "reporter_user_id": reporter_id,
            "reporter_server_id": 222,
        }
        reporter_user = Mock()
        reporter_user.send = AsyncMock(
            side_effect=discord.Forbidden(Mock(status=403), "Cannot send messages to this user")
        )

        async def fake_fetch_user(uid):
            if uid == reporter_id:
                return reporter_user
            return None

        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report", return_value=report), \
             patch.object(bot, "decide_report", return_value=True), \
             patch.object(bot.bot, "fetch_user", new=AsyncMock(side_effect=fake_fetch_user)), \
             patch.object(bot, "_guild_name_for", return_value="Some Server"), \
             patch.object(bot, "build_report_embed", return_value=Mock()):
            # Must not raise.
            await self.view._handle_decision(interaction, "approved")

        interaction.message.edit.assert_awaited_once()
        reporter_user.send.assert_awaited_once()
        for item in self.view.children:
            self.assertTrue(item.disabled)

    async def test_dm_fetch_user_not_found_does_not_break_rest_of_handler(self):
        # fetch_user itself can raise (e.g. discord.NotFound) rather than the send call --
        # that must be equally non-fatal.
        member = make_member(has_role=True, user_id=555)
        interaction = make_interaction(member)
        target_id = 123456789012345678
        reporter_id = 111
        report = {
            "id": 7,
            "status": "pending",
            "target_user_id": target_id,
            "reporter_user_id": reporter_id,
            "reporter_server_id": 222,
        }

        async def fake_fetch_user(uid):
            if uid == reporter_id:
                raise discord.NotFound(Mock(status=404), "Unknown User")
            return None

        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report", return_value=report), \
             patch.object(bot, "decide_report", return_value=True), \
             patch.object(bot.bot, "fetch_user", new=AsyncMock(side_effect=fake_fetch_user)), \
             patch.object(bot, "_guild_name_for", return_value="Some Server"), \
             patch.object(bot, "build_report_embed", return_value=Mock()):
            await self.view._handle_decision(interaction, "rejected")

        interaction.message.edit.assert_awaited_once()

    async def test_notification_not_sent_when_decision_loses_the_race(self):
        # The reporter must only be notified once the decision is actually, durably
        # committed -- not when this click lost a race to another reviewer (decide_report
        # returns False and the early-return fires before any DM logic).
        member = make_member(has_role=True, user_id=555)
        interaction = make_interaction(member)
        report = {
            "id": 7,
            "status": "pending",
            "target_user_id": 123456789012345678,
            "reporter_user_id": 111,
            "reporter_server_id": 222,
        }
        current_after_race = {**report, "status": "rejected"}
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report", side_effect=[report, current_after_race]), \
             patch.object(bot, "decide_report", return_value=False), \
             patch.object(bot.bot, "fetch_user", new=AsyncMock()) as mock_fetch_user, \
             patch.object(bot, "build_report_embed") as mock_build_embed:
            await self.view._handle_decision(interaction, "approved")

        mock_fetch_user.assert_not_called()
        mock_build_embed.assert_not_called()

    async def test_notification_not_sent_when_permission_denied(self):
        member = make_member(has_role=False)
        interaction = make_interaction(member)
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report") as mock_get_report, \
             patch.object(bot.bot, "fetch_user", new=AsyncMock()) as mock_fetch_user:
            await self.view._handle_decision(interaction, "approved")

        mock_get_report.assert_not_called()
        mock_fetch_user.assert_not_called()

    async def test_notification_not_sent_when_report_not_found(self):
        member = make_member(has_role=True)
        interaction = make_interaction(member)
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report", return_value=None), \
             patch.object(bot, "decide_report") as mock_decide, \
             patch.object(bot.bot, "fetch_user", new=AsyncMock()) as mock_fetch_user:
            await self.view._handle_decision(interaction, "approved")

        mock_decide.assert_not_called()
        mock_fetch_user.assert_not_called()

    # ---- race guard: decide_report is the atomic source of truth ----

    async def test_decide_report_losing_the_race_sends_already_reviewed_followup(self):
        # Simulates two reviewers clicking within the same window: this call's
        # get_report() read still saw 'pending', but by the time decide_report's
        # conditional UPDATE ran, the other reviewer's decision had already won --
        # so decide_report returns False. No embed/message update should happen,
        # and critically nothing here should imply a ban was (or wasn't) applied by
        # *this* click, since decide_report only inserts into the ban list when its
        # own UPDATE is the one that wins.
        member = make_member(has_role=True, user_id=555)
        interaction = make_interaction(member)
        target_id = 123456789012345678
        report = {
            "id": 7,
            "status": "pending",
            "target_user_id": target_id,
            "reporter_user_id": 111,
            "reporter_server_id": 222,
        }
        current_after_race = {**report, "status": "rejected"}
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report", side_effect=[report, current_after_race]), \
             patch.object(bot, "decide_report", return_value=False) as mock_decide, \
             patch.object(bot, "build_report_embed") as mock_build_embed:
            await self.view._handle_decision(interaction, "approved")

        mock_decide.assert_called_once_with(7, "approved", member.id)
        interaction.followup.send.assert_awaited_once_with(
            "Already reviewed by someone else (status: rejected).", ephemeral=True
        )
        mock_build_embed.assert_not_called()
        interaction.message.edit.assert_not_awaited()

    async def test_decide_report_raising_sends_error_followup_instead_of_hanging(self):
        # A DB hiccup inside decide_report must not leave the interaction stuck in
        # "thinking..." forever with zero feedback (this was finding #3 from the
        # adversarial review).
        member = make_member(has_role=True, user_id=555)
        interaction = make_interaction(member)
        report = {
            "id": 7,
            "status": "pending",
            "target_user_id": 123456789012345678,
            "reporter_user_id": 111,
            "reporter_server_id": 222,
        }
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report", return_value=report), \
             patch.object(bot, "decide_report", side_effect=RuntimeError("db exploded")), \
             patch.object(bot, "build_report_embed") as mock_build_embed:
            await self.view._handle_decision(interaction, "approved")

        interaction.followup.send.assert_awaited_once_with(
            "Failed to record that decision due to an internal error. Please try again.",
            ephemeral=True,
        )
        mock_build_embed.assert_not_called()
        interaction.message.edit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
