"""
Unit tests for the /banner unban command (bot.unban_cmd).

Added to give the review team a way to reverse a wrongly-approved report or
otherwise forgive a previously-banned user. Before this command existed,
once an id landed in public.users there was no way to undo it -- the only
existing self-heal (the 10013 "Unknown User" branch inside
enforce_bans_for_guild) only fires when the Discord account itself no
longer exists, not for "this was a mistake."

Mirrors the review-role permission gate from tests/test_report_cancel.py
(REVIEW_ROLE_ID check, not admin_only -- this is a global action) and the
background-task capture style from tests/test_phase3_integration.py
(patching asyncio.create_task to capture+await the coroutine directly,
since /banner unban defers immediately and does the actual multi-guild
sweep in a spawn_background_task-wrapped closure).

Run via (from repo root):
    .venv\\Scripts\\python.exe -m unittest tests.test_unban_command -v
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
    interaction.response.defer = AsyncMock()
    interaction.followup = Mock()
    interaction.followup.send = AsyncMock()
    return interaction


def make_member(has_role: bool):
    member = Mock(spec=discord.Member)
    member.get_role = Mock(return_value=Mock() if has_role else None)
    return member


def make_guild(guild_id, unban_side_effect=None):
    guild = Mock(spec=discord.Guild)
    guild.id = guild_id
    if unban_side_effect is not None:
        guild.unban = AsyncMock(side_effect=unban_side_effect)
    else:
        guild.unban = AsyncMock(return_value=None)
    return guild


def _capture_create_task():
    """Returns (fake_create_task, captured_list) -- patches asyncio.create_task so the
    coroutine passed to it is captured instead of scheduled, letting the test await it
    directly (under the same patches) instead of racing real event-loop scheduling."""
    captured = []

    def fake_create_task(coro, *a, **kw):
        captured.append(coro)
        return Mock()

    return fake_create_task, captured


class UnbanCmdPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_member_denied(self):
        interaction = make_interaction(Mock())  # not Member-spec'd
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "is_spammer_id") as mock_is_spammer:
            await bot.unban_cmd.callback(interaction, "123456789012345678")

        interaction.response.send_message.assert_awaited_once_with(
            "You don't have permission to manage reports.", ephemeral=True
        )
        mock_is_spammer.assert_not_called()

    async def test_member_without_role_denied(self):
        member = make_member(has_role=False)
        interaction = make_interaction(member)
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "is_spammer_id") as mock_is_spammer:
            await bot.unban_cmd.callback(interaction, "123456789012345678")

        interaction.response.send_message.assert_awaited_once_with(
            "You don't have permission to manage reports.", ephemeral=True
        )
        mock_is_spammer.assert_not_called()

    async def test_review_role_id_unset_denied(self):
        member = make_member(has_role=True)
        interaction = make_interaction(member)
        with patch.object(bot, "REVIEW_ROLE_ID", None), \
             patch.object(bot, "is_spammer_id") as mock_is_spammer:
            await bot.unban_cmd.callback(interaction, "123456789012345678")

        interaction.response.send_message.assert_awaited_once_with(
            "You don't have permission to manage reports.", ephemeral=True
        )
        mock_is_spammer.assert_not_called()


class UnbanCmdValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_user_id_rejected_before_any_db_call(self):
        member = make_member(has_role=True)
        interaction = make_interaction(member)
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "is_spammer_id") as mock_is_spammer:
            await bot.unban_cmd.callback(interaction, "not-a-real-id")

        interaction.response.send_message.assert_awaited_once()
        self.assertIn("valid Discord user ID", interaction.response.send_message.call_args.args[0])
        mock_is_spammer.assert_not_called()

    async def test_bigint_overflow_user_id_rejected(self):
        member = make_member(has_role=True)
        interaction = make_interaction(member)
        overflowing_id = "9" * 20
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "is_spammer_id") as mock_is_spammer:
            await bot.unban_cmd.callback(interaction, overflowing_id)

        interaction.response.send_message.assert_awaited_once()
        self.assertIn("valid Discord user ID", interaction.response.send_message.call_args.args[0])
        mock_is_spammer.assert_not_called()

    async def test_not_currently_spammer_short_circuits_no_sweep(self):
        member = make_member(has_role=True)
        interaction = make_interaction(member)
        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "is_spammer_id", return_value=False) as mock_is_spammer, \
             patch.object(bot, "get_enabled_configured_servers") as mock_get_servers, \
             patch.object(bot, "remove_spammer_id") as mock_remove:
            await bot.unban_cmd.callback(interaction, "123456789012345678")

        mock_is_spammer.assert_called_once_with(123456789012345678)
        interaction.response.send_message.assert_awaited_once()
        self.assertIn("not currently on the ban list", interaction.response.send_message.call_args.args[0])
        interaction.response.defer.assert_not_awaited()
        mock_get_servers.assert_not_called()
        mock_remove.assert_not_called()


class UnbanCmdSweepTests(unittest.IsolatedAsyncioTestCase):
    """The multi-guild reversal sweep, run via spawn_background_task's underlying
    asyncio.create_task -- captured and awaited directly under the same patches."""

    async def test_happy_path_unbans_in_every_enrolled_guild(self):
        member = make_member(has_role=True)
        interaction = make_interaction(member)
        target_id = 123456789012345678

        guild_a = make_guild(111)
        guild_b = make_guild(222)

        def fake_get_guild(gid):
            return {111: guild_a, 222: guild_b}.get(gid)

        fake_create_task, captured = _capture_create_task()

        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "is_spammer_id", return_value=True), \
             patch.object(bot, "remove_spammer_id") as mock_remove_spammer, \
             patch.object(bot, "remove_all_enforced_bans_for_target") as mock_remove_enforced, \
             patch.object(bot, "get_enabled_configured_servers", return_value=[(111, 999), (222, 888)]), \
             patch.object(bot.bot, "get_guild", side_effect=fake_get_guild), \
             patch("asyncio.create_task", side_effect=fake_create_task):
            await bot.unban_cmd.callback(interaction, str(target_id))

            self.assertEqual(len(captured), 1, "unban_cmd should schedule exactly one background task")
            await captured[0]

            mock_remove_spammer.assert_called_once_with(target_id)
            mock_remove_enforced.assert_called_once_with(target_id)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        guild_a.unban.assert_awaited_once_with(discord.Object(id=target_id), reason=unittest.mock.ANY)
        guild_b.unban.assert_awaited_once_with(discord.Object(id=target_id), reason=unittest.mock.ANY)

        interaction.followup.send.assert_awaited_once()
        summary = interaction.followup.send.call_args.args[0]
        self.assertIn(str(target_id), summary)
        self.assertIn("2 of 2", summary)

    async def test_not_found_in_one_guild_does_not_stop_the_others_and_is_not_a_success(self):
        member = make_member(has_role=True)
        interaction = make_interaction(member)
        target_id = 123456789012345678

        guild_a = make_guild(111, unban_side_effect=discord.NotFound(Mock(status=404), "Unknown Ban"))
        guild_b = make_guild(222)

        def fake_get_guild(gid):
            return {111: guild_a, 222: guild_b}.get(gid)

        fake_create_task, captured = _capture_create_task()

        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "is_spammer_id", return_value=True), \
             patch.object(bot, "remove_spammer_id"), \
             patch.object(bot, "remove_all_enforced_bans_for_target"), \
             patch.object(bot, "get_enabled_configured_servers", return_value=[(111, 999), (222, 888)]), \
             patch.object(bot.bot, "get_guild", side_effect=fake_get_guild), \
             patch("asyncio.create_task", side_effect=fake_create_task):
            await bot.unban_cmd.callback(interaction, str(target_id))
            await captured[0]

        guild_a.unban.assert_awaited_once()
        guild_b.unban.assert_awaited_once()

        summary = interaction.followup.send.call_args.args[0]
        self.assertIn("1 of 2", summary)

    async def test_forbidden_in_one_guild_does_not_stop_the_others(self):
        member = make_member(has_role=True)
        interaction = make_interaction(member)
        target_id = 123456789012345678

        guild_a = make_guild(111, unban_side_effect=discord.Forbidden(Mock(status=403), "Missing Permissions"))
        guild_b = make_guild(222)

        def fake_get_guild(gid):
            return {111: guild_a, 222: guild_b}.get(gid)

        fake_create_task, captured = _capture_create_task()

        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "is_spammer_id", return_value=True), \
             patch.object(bot, "remove_spammer_id"), \
             patch.object(bot, "remove_all_enforced_bans_for_target"), \
             patch.object(bot, "get_enabled_configured_servers", return_value=[(111, 999), (222, 888)]), \
             patch.object(bot.bot, "get_guild", side_effect=fake_get_guild), \
             patch("asyncio.create_task", side_effect=fake_create_task), \
             patch.object(bot.log, "warning") as mock_warning:
            await bot.unban_cmd.callback(interaction, str(target_id))
            await captured[0]

        guild_a.unban.assert_awaited_once()
        guild_b.unban.assert_awaited_once()
        mock_warning.assert_called_once()

        summary = interaction.followup.send.call_args.args[0]
        self.assertIn("1 of 2", summary)

    async def test_unexpected_exception_in_one_guild_does_not_stop_the_others(self):
        member = make_member(has_role=True)
        interaction = make_interaction(member)
        target_id = 123456789012345678

        guild_a = make_guild(111, unban_side_effect=RuntimeError("boom"))
        guild_b = make_guild(222)

        def fake_get_guild(gid):
            return {111: guild_a, 222: guild_b}.get(gid)

        fake_create_task, captured = _capture_create_task()

        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "is_spammer_id", return_value=True), \
             patch.object(bot, "remove_spammer_id"), \
             patch.object(bot, "remove_all_enforced_bans_for_target"), \
             patch.object(bot, "get_enabled_configured_servers", return_value=[(111, 999), (222, 888)]), \
             patch.object(bot.bot, "get_guild", side_effect=fake_get_guild), \
             patch("asyncio.create_task", side_effect=fake_create_task), \
             patch.object(bot.log, "exception") as mock_exception:
            await bot.unban_cmd.callback(interaction, str(target_id))
            await captured[0]

        guild_a.unban.assert_awaited_once()
        guild_b.unban.assert_awaited_once()
        mock_exception.assert_called_once()

        summary = interaction.followup.send.call_args.args[0]
        self.assertIn("1 of 2", summary)

    async def test_guild_no_longer_resolvable_is_skipped_without_error(self):
        member = make_member(has_role=True)
        interaction = make_interaction(member)
        target_id = 123456789012345678

        guild_b = make_guild(222)

        def fake_get_guild(gid):
            return {222: guild_b}.get(gid)  # 111 resolves to None -- bot no longer in it

        fake_create_task, captured = _capture_create_task()

        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "is_spammer_id", return_value=True), \
             patch.object(bot, "remove_spammer_id"), \
             patch.object(bot, "remove_all_enforced_bans_for_target"), \
             patch.object(bot, "get_enabled_configured_servers", return_value=[(111, 999), (222, 888)]), \
             patch.object(bot.bot, "get_guild", side_effect=fake_get_guild), \
             patch("asyncio.create_task", side_effect=fake_create_task):
            await bot.unban_cmd.callback(interaction, str(target_id))
            await captured[0]

        guild_b.unban.assert_awaited_once()

        # Only 1 guild was actually attempted (111 was skipped entirely, not counted).
        summary = interaction.followup.send.call_args.args[0]
        self.assertIn("1 of 1", summary)

    async def test_unexpected_failure_before_sweep_still_sends_a_followup(self):
        # remove_spammer_id blowing up must not leave the interaction hanging silently --
        # mirrors sync_now_cmd's _run_sync error handling.
        member = make_member(has_role=True)
        interaction = make_interaction(member)
        target_id = 123456789012345678

        fake_create_task, captured = _capture_create_task()

        with patch.object(bot, "REVIEW_ROLE_ID", REVIEW_ROLE_ID), \
             patch.object(bot, "is_spammer_id", return_value=True), \
             patch.object(bot, "remove_spammer_id", side_effect=RuntimeError("db down")), \
             patch.object(bot, "remove_all_enforced_bans_for_target"), \
             patch.object(bot, "get_enabled_configured_servers") as mock_get_servers, \
             patch("asyncio.create_task", side_effect=fake_create_task), \
             patch.object(bot.log, "exception") as mock_exception:
            await bot.unban_cmd.callback(interaction, str(target_id))
            await captured[0]

        mock_exception.assert_called_once()
        mock_get_servers.assert_not_called()
        interaction.followup.send.assert_awaited_once_with(
            "Unban failed due to an internal error.", ephemeral=True
        )


if __name__ == "__main__":
    unittest.main()
