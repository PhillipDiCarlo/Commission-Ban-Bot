"""
Unit tests for the /banner report command handler (bot.report_cmd).

app_commands.Command wraps the async function; the original callback is
reachable via `bot.report_cmd.callback`, which lets these tests call it
directly without going through Discord's interaction/permission plumbing
(admin_only() is a separate app_commands.check, not part of the callback
body, so calling .callback bypasses it deliberately -- these tests are
about the validation/error-handling logic inside the command, not the
permission gate, which isn't exercised here).

Covers the fixes from the adversarial review: BIGINT-overflow snowflakes
(is_valid_snowflake), evidence size/content-type validation, and the
orphaned-report cleanup when something fails after create_report() has
already run.

Run via (from repo root):
    .venv\\Scripts\\python.exe -m unittest tests.test_report_cmd -v
"""
import unittest
from unittest.mock import Mock, AsyncMock, patch

import discord
import psycopg2

import bot


REVIEW_CHANNEL_ID = 111222333
REVIEW_ROLE_ID = 444555666


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


def make_evidence(content_type="image/png", size=1024, filename="proof.png"):
    evidence = Mock(spec=discord.Attachment)
    evidence.content_type = content_type
    evidence.size = size
    evidence.filename = filename
    evidence.read = AsyncMock(return_value=b"fake-image-bytes")
    return evidence


def make_review_channel():
    channel = Mock(spec=discord.TextChannel)
    sent_message = Mock()
    sent_message.id = 987654321
    channel.send = AsyncMock(return_value=sent_message)
    return channel


class ReportCmdGuardClauseTests(unittest.IsolatedAsyncioTestCase):
    """Cheap, no-DB-touched validation guards near the top of report_cmd."""

    async def test_invalid_user_id_rejected_before_any_db_call(self):
        interaction = make_interaction()
        with patch.object(bot, "REVIEW_CHANNEL_ID", REVIEW_CHANNEL_ID), \
             patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "is_spammer_id") as mock_is_spammer:
            await bot.report_cmd.callback(interaction, "not-a-real-id", make_evidence())

        interaction.response.send_message.assert_awaited_once()
        self.assertIn("valid Discord user ID", interaction.response.send_message.call_args.args[0])
        mock_is_spammer.assert_not_called()

    async def test_bigint_overflow_user_id_rejected(self):
        # 20 nines: passes SNOWFLAKE_RE's digit-count check but overflows Postgres BIGINT
        # (max 9223372036854775807, 19 digits). is_valid_snowflake must catch this.
        interaction = make_interaction()
        overflowing_id = "9" * 20
        with patch.object(bot, "REVIEW_CHANNEL_ID", REVIEW_CHANNEL_ID), \
             patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "is_spammer_id") as mock_is_spammer:
            await bot.report_cmd.callback(interaction, overflowing_id, make_evidence())

        interaction.response.send_message.assert_awaited_once()
        self.assertIn("valid Discord user ID", interaction.response.send_message.call_args.args[0])
        mock_is_spammer.assert_not_called()

    async def test_non_image_evidence_rejected(self):
        interaction = make_interaction()
        evidence = make_evidence(content_type="application/zip")
        with patch.object(bot, "REVIEW_CHANNEL_ID", REVIEW_CHANNEL_ID), \
             patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "is_spammer_id") as mock_is_spammer:
            await bot.report_cmd.callback(interaction, "123456789012345678", evidence)

        interaction.response.send_message.assert_awaited_once_with(
            "Evidence must be an image file.", ephemeral=True
        )
        mock_is_spammer.assert_not_called()

    async def test_missing_content_type_rejected(self):
        # discord.Attachment.content_type can legitimately be None; must not crash on it.
        interaction = make_interaction()
        evidence = make_evidence(content_type=None)
        with patch.object(bot, "REVIEW_CHANNEL_ID", REVIEW_CHANNEL_ID), \
             patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "is_spammer_id") as mock_is_spammer:
            await bot.report_cmd.callback(interaction, "123456789012345678", evidence)

        interaction.response.send_message.assert_awaited_once_with(
            "Evidence must be an image file.", ephemeral=True
        )
        mock_is_spammer.assert_not_called()

    async def test_oversized_evidence_rejected(self):
        interaction = make_interaction()
        evidence = make_evidence(size=bot.MAX_EVIDENCE_SIZE_BYTES + 1)
        with patch.object(bot, "REVIEW_CHANNEL_ID", REVIEW_CHANNEL_ID), \
             patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "is_spammer_id") as mock_is_spammer:
            await bot.report_cmd.callback(interaction, "123456789012345678", evidence)

        interaction.response.send_message.assert_awaited_once()
        self.assertIn("too large", interaction.response.send_message.call_args.args[0])
        mock_is_spammer.assert_not_called()

    async def test_valid_input_passes_all_guards_and_reaches_ban_list_check(self):
        # Confirms the guards above don't over-trigger on legitimate input -- reaching
        # is_spammer_id proves user_id and evidence both passed validation.
        interaction = make_interaction()
        with patch.object(bot, "REVIEW_CHANNEL_ID", REVIEW_CHANNEL_ID), \
             patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "count_recent_reports_by_reporter", return_value=0), \
             patch.object(bot, "is_spammer_id", return_value=True) as mock_is_spammer:
            await bot.report_cmd.callback(interaction, "123456789012345678", make_evidence())

        mock_is_spammer.assert_called_once_with(123456789012345678)
        interaction.response.send_message.assert_awaited_once_with(
            "That user is already on the ban list.", ephemeral=True
        )


class ReportCmdFullFlowTests(unittest.IsolatedAsyncioTestCase):
    """Deeper flow: create_report -> post to review channel, and the error paths
    added to prevent orphaned 'pending' rows (adversarial review finding #2) and
    to surface the duplicate-pending race caught by the DB's partial unique index
    (finding #9)."""

    def _common_patches(self, review_channel):
        return [
            patch.object(bot, "REVIEW_CHANNEL_ID", REVIEW_CHANNEL_ID),
            patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID),
            patch.object(bot, "count_recent_reports_by_reporter", return_value=0),
            patch.object(bot, "is_spammer_id", return_value=False),
            patch.object(bot, "get_pending_report_for_target", return_value=None),
            patch.object(bot.bot, "get_channel", return_value=review_channel),
            patch.object(bot.bot, "fetch_user", new=AsyncMock(return_value=None)),
            patch.object(bot, "build_report_embed", return_value=Mock()),
            patch.object(bot, "ReportReviewView", return_value=Mock()),
        ]

    async def test_duplicate_pending_race_caught_by_unique_index(self):
        # get_pending_report_for_target's pre-check missed it (simulating the race), but
        # create_report's INSERT hits the DB's partial unique index and raises
        # IntegrityError -- report_cmd must translate that into the same friendly
        # message the pre-check would have given, not a generic/internal error.
        interaction = make_interaction()
        review_channel = make_review_channel()
        patches = self._common_patches(review_channel)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], \
             patch.object(bot, "create_report", side_effect=psycopg2.IntegrityError("dup")) as mock_create, \
             patch.object(bot, "delete_report") as mock_delete:
            await bot.report_cmd.callback(interaction, "123456789012345678", make_evidence())

        mock_create.assert_called_once()
        interaction.followup.send.assert_awaited_once_with(
            "That user already has a pending report.", ephemeral=True
        )
        mock_delete.assert_not_called()  # nothing was ever created, so nothing to clean up

    async def test_post_to_review_channel_failure_deletes_orphaned_report(self):
        # create_report succeeds (report_id=42), but review_channel.send blows up (e.g. a
        # permissions/upload-limit issue). The report row must not be left behind at
        # status='pending' with no review_message_id -- delete_report must run.
        interaction = make_interaction()
        review_channel = make_review_channel()
        review_channel.send = AsyncMock(side_effect=discord.HTTPException(Mock(status=403), "Forbidden"))
        patches = self._common_patches(review_channel)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], \
             patch.object(bot, "create_report", return_value=42) as mock_create, \
             patch.object(bot, "delete_report") as mock_delete, \
             patch.object(bot, "set_report_review_message") as mock_set_msg:
            await bot.report_cmd.callback(interaction, "123456789012345678", make_evidence())

        mock_create.assert_called_once()
        mock_delete.assert_called_once_with(42)
        mock_set_msg.assert_not_called()
        interaction.followup.send.assert_awaited_once()
        self.assertIn("internal error", interaction.followup.send.call_args.args[0])

    async def test_successful_submission_links_review_message_and_confirms(self):
        interaction = make_interaction()
        review_channel = make_review_channel()
        patches = self._common_patches(review_channel)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], \
             patch.object(bot, "create_report", return_value=42) as mock_create, \
             patch.object(bot, "delete_report") as mock_delete, \
             patch.object(bot, "set_report_review_message") as mock_set_msg:
            await bot.report_cmd.callback(interaction, "123456789012345678", make_evidence())

        mock_create.assert_called_once_with(123456789012345678, interaction.user.id, interaction.guild.id)
        review_channel.send.assert_awaited_once()
        mock_set_msg.assert_called_once_with(42, 987654321)
        mock_delete.assert_not_called()
        interaction.followup.send.assert_awaited_once()
        self.assertIn("#42", interaction.followup.send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
