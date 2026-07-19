"""
Unit tests for bot.build_report_embed.

Run via (from repo root):
    .venv\\Scripts\\python.exe -m unittest tests.test_report_embed -v

Must be run through the tests package (unittest discover / dotted module
name) so tests/__init__.py sets dummy DATABASE_URL/DISCORD_TOKEN before
`import bot` executes.
"""
import unittest
from unittest.mock import Mock

import discord

import bot


class StubUser:
    """Lightweight stand-in for discord.User with just the attributes
    build_report_embed touches (global_name, name, display_avatar.url).
    Real discord.User requires a live connection state to construct, so we
    can't instantiate one directly in a unit test.
    """

    def __init__(self, global_name, name, avatar_url):
        self.global_name = global_name
        self.name = name
        self.display_avatar = Mock()
        self.display_avatar.url = avatar_url


class BuildReportEmbedTests(unittest.TestCase):
    # ---- status -> color mapping ----

    def test_pending_status_uses_gold_color(self):
        embed = bot.build_report_embed(
            report_id=1,
            target_id=123456789012345678,
            target_user=None,
            reporter_id=111,
            reporter_server_name="Test Server",
            filename="evidence.png",
            status="pending",
        )
        self.assertEqual(embed.color, discord.Color.gold())

    def test_approved_status_uses_green_color(self):
        embed = bot.build_report_embed(
            report_id=2,
            target_id=123456789012345678,
            target_user=None,
            reporter_id=111,
            reporter_server_name="Test Server",
            filename="evidence.png",
            status="approved",
            reviewer_id=999,
        )
        self.assertEqual(embed.color, discord.Color.green())

    def test_rejected_status_uses_red_color(self):
        embed = bot.build_report_embed(
            report_id=3,
            target_id=123456789012345678,
            target_user=None,
            reporter_id=111,
            reporter_server_name="Test Server",
            filename="evidence.png",
            status="rejected",
            reviewer_id=999,
        )
        self.assertEqual(embed.color, discord.Color.red())

    # ---- footer text per status ----

    def test_pending_footer_text(self):
        embed = bot.build_report_embed(
            report_id=1,
            target_id=123456789012345678,
            target_user=None,
            reporter_id=111,
            reporter_server_name="Test Server",
            filename="evidence.png",
            status="pending",
        )
        self.assertEqual(embed.footer.text, "Awaiting review")

    def test_approved_footer_text_contains_reviewer(self):
        embed = bot.build_report_embed(
            report_id=1,
            target_id=123456789012345678,
            target_user=None,
            reporter_id=111,
            reporter_server_name="Test Server",
            filename="evidence.png",
            status="approved",
            reviewer_id=42,
        )
        self.assertIn("Approved by 42", embed.footer.text)

    def test_rejected_footer_text_contains_reviewer(self):
        embed = bot.build_report_embed(
            report_id=1,
            target_id=123456789012345678,
            target_user=None,
            reporter_id=111,
            reporter_server_name="Test Server",
            filename="evidence.png",
            status="rejected",
            reviewer_id=77,
        )
        self.assertIn("Rejected by 77", embed.footer.text)

    # ---- target_user=None path ----

    def test_target_user_none_field_mentions_lookup_failure(self):
        target_id = 123456789012345678
        embed = bot.build_report_embed(
            report_id=1,
            target_id=target_id,
            target_user=None,
            reporter_id=111,
            reporter_server_name="Test Server",
            filename="evidence.png",
        )
        target_field = discord.utils.get(embed.fields, name="Target")
        self.assertIsNotNone(target_field)
        self.assertIn("profile lookup failed", target_field.value)
        self.assertIn(str(target_id), target_field.value)

    def test_target_user_none_thumbnail_unset(self):
        embed = bot.build_report_embed(
            report_id=1,
            target_id=123456789012345678,
            target_user=None,
            reporter_id=111,
            reporter_server_name="Test Server",
            filename="evidence.png",
        )
        # discord.py 2.6.4 reports an unset embed thumbnail's url as None.
        self.assertFalse(embed.thumbnail.url)

    # ---- target_user provided ----

    def test_target_user_with_global_name_shown_in_field(self):
        user = StubUser(global_name="Global Nick", name="fallback_name", avatar_url="https://cdn.example/avatar1.png")
        embed = bot.build_report_embed(
            report_id=1,
            target_id=123456789012345678,
            target_user=user,
            reporter_id=111,
            reporter_server_name="Test Server",
            filename="evidence.png",
        )
        target_field = discord.utils.get(embed.fields, name="Target")
        self.assertIsNotNone(target_field)
        self.assertIn("Global Nick", target_field.value)
        self.assertNotIn("fallback_name", target_field.value)

    def test_target_user_without_global_name_falls_back_to_name(self):
        user = StubUser(global_name=None, name="fallback_name", avatar_url="https://cdn.example/avatar2.png")
        embed = bot.build_report_embed(
            report_id=1,
            target_id=123456789012345678,
            target_user=user,
            reporter_id=111,
            reporter_server_name="Test Server",
            filename="evidence.png",
        )
        target_field = discord.utils.get(embed.fields, name="Target")
        self.assertIsNotNone(target_field)
        self.assertIn("fallback_name", target_field.value)

    def test_target_user_sets_thumbnail_to_avatar_url(self):
        user = StubUser(global_name="Global Nick", name="fallback_name", avatar_url="https://cdn.example/avatar3.png")
        embed = bot.build_report_embed(
            report_id=1,
            target_id=123456789012345678,
            target_user=user,
            reporter_id=111,
            reporter_server_name="Test Server",
            filename="evidence.png",
        )
        self.assertEqual(embed.thumbnail.url, "https://cdn.example/avatar3.png")

    # ---- title ----

    def test_title_contains_report_id(self):
        embed = bot.build_report_embed(
            report_id=555,
            target_id=123456789012345678,
            target_user=None,
            reporter_id=111,
            reporter_server_name="Test Server",
            filename="evidence.png",
        )
        self.assertIn("#555", embed.title)

    # ---- image ----

    def test_image_url_is_attachment_reference(self):
        embed = bot.build_report_embed(
            report_id=1,
            target_id=123456789012345678,
            target_user=None,
            reporter_id=111,
            reporter_server_name="Test Server",
            filename="proof.jpg",
        )
        self.assertEqual(embed.image.url, "attachment://proof.jpg")

    # ---- invalid status ----

    def test_invalid_status_raises_key_error(self):
        # Documents existing behavior: the color dict lookup raises KeyError
        # for any status not in {"pending", "approved", "rejected"}. This is
        # not a fix target -- just pinning current behavior.
        with self.assertRaises(KeyError):
            bot.build_report_embed(
                report_id=1,
                target_id=123456789012345678,
                target_user=None,
                reporter_id=111,
                reporter_server_name="Test Server",
                filename="evidence.png",
                status="bogus",
            )

    # ---- account created field ----

    def test_account_created_field_is_nonempty_string(self):
        embed = bot.build_report_embed(
            report_id=1,
            target_id=123456789012345678,
            target_user=None,
            reporter_id=111,
            reporter_server_name="Test Server",
            filename="evidence.png",
        )
        created_field = discord.utils.get(embed.fields, name="Account created")
        self.assertIsNotNone(created_field)
        self.assertIsInstance(created_field.value, str)
        self.assertTrue(len(created_field.value) > 0)

    def test_reported_by_field_contains_reporter_and_server(self):
        embed = bot.build_report_embed(
            report_id=1,
            target_id=123456789012345678,
            target_user=None,
            reporter_id=222,
            reporter_server_name="My Cool Server",
            filename="evidence.png",
        )
        reported_field = discord.utils.get(embed.fields, name="Reported by")
        self.assertIsNotNone(reported_field)
        self.assertIn("222", reported_field.value)
        self.assertIn("My Cool Server", reported_field.value)


if __name__ == "__main__":
    unittest.main()
