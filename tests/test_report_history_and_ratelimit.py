"""
Unit tests for Phase 4's two report-queue extensions:

1. /banner history <user_id> -- surfaces every report ever filed against a
   target (bot.get_report_history_for_target), not just the single
   currently-pending one that get_pending_report_for_target looks at.
2. Per-reporter rate limiting on /banner report
   (bot.count_recent_reports_by_reporter), so a single admin/mod can't spam
   the shared review queue with unlimited submissions.

Run via (from repo root):
    .venv\\Scripts\\python.exe -m unittest tests.test_report_history_and_ratelimit -v
"""
import inspect
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, AsyncMock, patch

import discord

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


def make_member(has_role: bool, user_id: int = 999):
    """Member-spec'd mock so isinstance(user, discord.Member) is True -- the pattern
    history_cmd, report_cancel_cmd, and unban_cmd all use for their REVIEW_ROLE_ID gate."""
    member = Mock(spec=discord.Member)
    member.id = user_id
    member.get_role = Mock(return_value=Mock() if has_role else None)
    return member


def make_reviewer_interaction():
    """An interaction from a member holding the review role -- history_cmd is gated
    the same way as report_cancel_cmd/unban_cmd, not admin_only()."""
    interaction = make_interaction()
    interaction.user = make_member(has_role=True)
    return interaction


def make_evidence(content_type="image/png", size=1024, filename="proof.png"):
    evidence = Mock(spec=discord.Attachment)
    evidence.content_type = content_type
    evidence.size = size
    evidence.filename = filename
    evidence.read = AsyncMock(return_value=b"fake-image-bytes")
    return evidence


def make_report_row(id, status, reporter_user_id=1, reporter_server_id=2,
                     reviewer_user_id=None, decided_at=None):
    return {
        "id": id,
        "status": status,
        "reporter_user_id": reporter_user_id,
        "reporter_server_id": reporter_server_id,
        "reviewer_user_id": reviewer_user_id,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "decided_at": decided_at,
    }


# -------------------- DB helper signature/style tests --------------------
class ReportHistoryAndRateLimitDbHelperTests(unittest.TestCase):
    """Lightweight signature/style sanity checks for the two new DB helpers --
    no real Postgres in this suite (see tests/__init__.py), so these confirm the
    helpers exist, are callable with the documented signature, and interact with
    a fake connection/cursor the same way sibling helpers (e.g. get_report,
    get_enforced_ban_ids) do, rather than exercising real SQL."""

    def test_get_report_history_for_target_signature(self):
        self.assertTrue(callable(bot.get_report_history_for_target))
        sig = inspect.signature(bot.get_report_history_for_target)
        self.assertEqual(list(sig.parameters), ["target_user_id"])

    def test_count_recent_reports_by_reporter_signature(self):
        self.assertTrue(callable(bot.count_recent_reports_by_reporter))
        sig = inspect.signature(bot.count_recent_reports_by_reporter)
        self.assertEqual(list(sig.parameters), ["reporter_user_id", "window_hours"])

    def test_get_report_history_for_target_queries_and_orders_by_created_at_desc(self):
        rows = [make_report_row(2, "pending"), make_report_row(1, "approved")]
        fake_cursor = Mock()
        fake_cursor.__enter__ = Mock(return_value=fake_cursor)
        fake_cursor.__exit__ = Mock(return_value=False)
        fake_cursor.execute = Mock()
        fake_cursor.fetchall = Mock(return_value=rows)

        fake_conn = Mock()
        fake_conn.cursor = Mock(return_value=fake_cursor)
        fake_conn.close = Mock()

        with patch.object(bot, "get_db_connection", return_value=fake_conn):
            result = bot.get_report_history_for_target(123456789012345678)

        self.assertEqual(result, rows)
        sql = fake_cursor.execute.call_args.args[0]
        self.assertIn("public.reports", sql)
        self.assertIn("target_user_id", sql)
        self.assertIn("ORDER BY created_at DESC", sql)
        self.assertIn("LIMIT", sql)
        params = fake_cursor.execute.call_args.args[1]
        # Bounds the query itself (a much larger ceiling than the display cap) so a
        # pathologically over-reported target can't force an unbounded fetch.
        self.assertEqual(params, (123456789012345678, bot.HISTORY_QUERY_LIMIT))
        fake_conn.close.assert_called_once()

    def test_get_report_history_for_target_empty_returns_empty_list(self):
        fake_cursor = Mock()
        fake_cursor.__enter__ = Mock(return_value=fake_cursor)
        fake_cursor.__exit__ = Mock(return_value=False)
        fake_cursor.execute = Mock()
        fake_cursor.fetchall = Mock(return_value=[])

        fake_conn = Mock()
        fake_conn.cursor = Mock(return_value=fake_cursor)
        fake_conn.close = Mock()

        with patch.object(bot, "get_db_connection", return_value=fake_conn):
            result = bot.get_report_history_for_target(999)

        self.assertEqual(result, [])
        fake_conn.close.assert_called_once()

    def test_count_recent_reports_by_reporter_uses_postgres_interval_arithmetic(self):
        # The whole point of this helper (per the task spec) is to let Postgres compute
        # the cutoff via now() - interval, rather than the caller computing a timestamp
        # in Python -- that's what keeps it correct under app/DB clock skew.
        fake_cursor = Mock()
        fake_cursor.__enter__ = Mock(return_value=fake_cursor)
        fake_cursor.__exit__ = Mock(return_value=False)
        fake_cursor.execute = Mock()
        fake_cursor.fetchone = Mock(return_value=(3,))

        fake_conn = Mock()
        fake_conn.cursor = Mock(return_value=fake_cursor)
        fake_conn.close = Mock()

        with patch.object(bot, "get_db_connection", return_value=fake_conn):
            result = bot.count_recent_reports_by_reporter(555, 24.0)

        self.assertEqual(result, 3)
        sql = fake_cursor.execute.call_args.args[0]
        self.assertIn("public.reports", sql)
        self.assertIn("reporter_user_id", sql)
        self.assertIn("now()", sql)
        self.assertIn("interval", sql)
        params = fake_cursor.execute.call_args.args[1]
        self.assertEqual(params, (555, 24.0))
        fake_conn.close.assert_called_once()

    def test_count_recent_reports_by_reporter_closes_connection_on_success(self):
        fake_cursor = Mock()
        fake_cursor.__enter__ = Mock(return_value=fake_cursor)
        fake_cursor.__exit__ = Mock(return_value=False)
        fake_cursor.execute = Mock()
        fake_cursor.fetchone = Mock(return_value=(0,))

        fake_conn = Mock()
        fake_conn.cursor = Mock(return_value=fake_cursor)
        fake_conn.close = Mock()

        with patch.object(bot, "get_db_connection", return_value=fake_conn):
            bot.count_recent_reports_by_reporter(1, 1.0)

        fake_conn.close.assert_called_once()


# -------------------- /banner history command tests --------------------
class HistoryCmdTests(unittest.IsolatedAsyncioTestCase):
    # ---- permission gate: review-role only, not admin_only() ----
    # history_cmd surfaces reporter/reviewer Discord ids, which everywhere else in this
    # bot is review-team-scoped data -- these tests were added when the gate was fixed
    # from @admin_only() to match report_cancel_cmd/unban_cmd's inline REVIEW_ROLE_ID
    # check (an adversarial-review finding: admin_only() let any opted-in server's admin
    # enumerate that identity data, broader exposure than intended).

    async def test_non_member_denied(self):
        interaction = make_interaction()  # plain Mock, not Member-spec'd
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report_history_for_target") as mock_history:
            await bot.history_cmd.callback(interaction, "123456789012345678")

        interaction.response.send_message.assert_awaited_once_with(
            "You don't have permission to view report history.", ephemeral=True
        )
        mock_history.assert_not_called()

    async def test_member_without_role_denied(self):
        interaction = make_interaction()
        interaction.user = make_member(has_role=False)
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report_history_for_target") as mock_history:
            await bot.history_cmd.callback(interaction, "123456789012345678")

        interaction.response.send_message.assert_awaited_once_with(
            "You don't have permission to view report history.", ephemeral=True
        )
        mock_history.assert_not_called()

    async def test_review_role_id_unconfigured_denies_even_a_role_holding_member(self):
        interaction = make_reviewer_interaction()
        with patch.object(bot, "REVIEW_ROLE_ID", None), \
             patch.object(bot, "get_report_history_for_target") as mock_history:
            await bot.history_cmd.callback(interaction, "123456789012345678")

        interaction.response.send_message.assert_awaited_once_with(
            "You don't have permission to view report history.", ephemeral=True
        )
        mock_history.assert_not_called()

    # ---- actual command behavior (permission granted) ----

    async def test_invalid_user_id_rejected_before_any_db_call(self):
        interaction = make_reviewer_interaction()
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report_history_for_target") as mock_history:
            await bot.history_cmd.callback(interaction, "not-a-real-id")

        interaction.response.send_message.assert_awaited_once()
        self.assertIn("valid Discord user ID", interaction.response.send_message.call_args.args[0])
        mock_history.assert_not_called()

    async def test_no_history_says_so_clearly(self):
        interaction = make_reviewer_interaction()
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report_history_for_target", return_value=[]) as mock_history:
            await bot.history_cmd.callback(interaction, "123456789012345678")

        mock_history.assert_called_once_with(123456789012345678)
        interaction.response.send_message.assert_awaited_once()
        kwargs = interaction.response.send_message.call_args
        self.assertIn("No report history", kwargs.args[0])
        self.assertTrue(kwargs.kwargs.get("ephemeral"))

    async def test_history_present_renders_embed_with_one_field_per_report(self):
        interaction = make_reviewer_interaction()
        rows = [
            make_report_row(3, "pending"),
            make_report_row(2, "approved", reviewer_user_id=777,
                             decided_at=datetime(2026, 1, 2, tzinfo=timezone.utc)),
            make_report_row(1, "rejected", reviewer_user_id=778,
                             decided_at=datetime(2026, 1, 3, tzinfo=timezone.utc)),
        ]
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report_history_for_target", return_value=rows):
            await bot.history_cmd.callback(interaction, "123456789012345678")

        interaction.response.send_message.assert_awaited_once()
        call_kwargs = interaction.response.send_message.call_args.kwargs
        embed = call_kwargs["embed"]
        self.assertIsInstance(embed, discord.Embed)
        self.assertEqual(len(embed.fields), 3)
        self.assertTrue(call_kwargs.get("ephemeral"))

        # Pending report has no reviewer yet -- must read as still pending, not "not
        # yet reviewed" (those are different states the spec calls out explicitly).
        pending_field = next(f for f in embed.fields if f.name.startswith("#3"))
        self.assertIn("Still pending", pending_field.value)

        approved_field = next(f for f in embed.fields if f.name.startswith("#2"))
        self.assertIn("777", approved_field.value)

    async def test_history_capped_at_display_limit_with_a_note_about_the_total(self):
        # Guards against an oversized-embed error: Discord embeds cap at 25 fields, so
        # a target with more history than bot.HISTORY_DISPLAY_LIMIT must still render
        # (capped), not blow up or silently drop the note about what was omitted.
        rows = [make_report_row(i, "approved", reviewer_user_id=1,
                                 decided_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
                for i in range(bot.HISTORY_DISPLAY_LIMIT + 10)]
        interaction = make_reviewer_interaction()
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report_history_for_target", return_value=rows):
            await bot.history_cmd.callback(interaction, "123456789012345678")

        call_kwargs = interaction.response.send_message.call_args.kwargs
        embed = call_kwargs["embed"]
        self.assertLessEqual(len(embed.fields), 25)
        self.assertEqual(len(embed.fields), bot.HISTORY_DISPLAY_LIMIT)
        self.assertIsNotNone(embed.description)
        self.assertIn(str(len(rows)), embed.description)

    async def test_db_failure_is_caught_and_reported_not_raised(self):
        interaction = make_reviewer_interaction()
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "get_report_history_for_target", side_effect=RuntimeError("db exploded")):
            await bot.history_cmd.callback(interaction, "123456789012345678")  # must not raise

        interaction.response.send_message.assert_awaited_once()
        self.assertIn("internal error", interaction.response.send_message.call_args.args[0])


# -------------------- /banner report rate-limit tests --------------------
class ReportCmdRateLimitTests(unittest.IsolatedAsyncioTestCase):
    """Confirms the rate-limit check added to report_cmd fails fast: it must run
    (and reject) before is_spammer_id / get_pending_report_for_target are ever
    touched, per the task's fail-fast-and-cheap ordering requirement."""

    async def test_under_limit_proceeds_normally(self):
        interaction = make_interaction()
        with patch.object(bot, "REVIEW_CHANNEL_ID", REVIEW_CHANNEL_ID), \
             patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "REPORT_RATE_LIMIT_MAX", 5), \
             patch.object(bot, "count_recent_reports_by_reporter", return_value=4) as mock_count, \
             patch.object(bot, "is_spammer_id", return_value=True) as mock_is_spammer:
            await bot.report_cmd.callback(interaction, "123456789012345678", make_evidence())

        mock_count.assert_called_once_with(interaction.user.id, bot.REPORT_RATE_LIMIT_WINDOW_HOURS)
        mock_is_spammer.assert_called_once_with(123456789012345678)
        interaction.response.send_message.assert_awaited_once_with(
            "That user is already on the ban list.", ephemeral=True
        )

    async def test_at_limit_rejected_before_touching_is_spammer_id(self):
        interaction = make_interaction()
        with patch.object(bot, "REVIEW_CHANNEL_ID", REVIEW_CHANNEL_ID), \
             patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "REPORT_RATE_LIMIT_MAX", 5), \
             patch.object(bot, "count_recent_reports_by_reporter", return_value=5) as mock_count, \
             patch.object(bot, "is_spammer_id") as mock_is_spammer, \
             patch.object(bot, "get_pending_report_for_target") as mock_pending:
            await bot.report_cmd.callback(interaction, "123456789012345678", make_evidence())

        mock_count.assert_called_once_with(interaction.user.id, bot.REPORT_RATE_LIMIT_WINDOW_HOURS)
        mock_is_spammer.assert_not_called()
        mock_pending.assert_not_called()
        interaction.response.send_message.assert_awaited_once()
        self.assertIn("too many reports", interaction.response.send_message.call_args.args[0])
        self.assertTrue(interaction.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_over_limit_rejected_before_touching_is_spammer_id(self):
        interaction = make_interaction()
        with patch.object(bot, "REVIEW_CHANNEL_ID", REVIEW_CHANNEL_ID), \
             patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "REPORT_RATE_LIMIT_MAX", 5), \
             patch.object(bot, "count_recent_reports_by_reporter", return_value=9) as mock_count, \
             patch.object(bot, "is_spammer_id") as mock_is_spammer:
            await bot.report_cmd.callback(interaction, "123456789012345678", make_evidence())

        mock_count.assert_called_once()
        mock_is_spammer.assert_not_called()
        interaction.response.send_message.assert_awaited_once()
        self.assertIn("too many reports", interaction.response.send_message.call_args.args[0])


# -------------------- rate-limit env-var config parsing tests --------------------
class RateLimitConfigParsingTests(unittest.TestCase):
    def test_default_max_is_five(self):
        self.assertEqual(bot.REPORT_RATE_LIMIT_MAX, 5)

    def test_default_window_is_24_hours(self):
        self.assertEqual(bot.REPORT_RATE_LIMIT_WINDOW_HOURS, 24.0)

    def test_malformed_max_env_falls_back_to_default_instead_of_raising(self):
        raw = bot._parse_optional_int_env("not-an-int", "REPORT_RATE_LIMIT_MAX")
        self.assertIsNone(raw)  # matches the module-load-time fallback-to-5 behavior

    def test_malformed_window_env_does_not_raise(self):
        try:
            result = bot._parse_float_env("not-a-number", 24.0, "REPORT_RATE_LIMIT_WINDOW_HOURS")
        except ValueError:
            self.fail("_parse_float_env must not raise on a malformed value")
        self.assertEqual(result, 24.0)


if __name__ == "__main__":
    unittest.main()
