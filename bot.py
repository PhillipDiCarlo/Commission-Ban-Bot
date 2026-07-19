import os
import io
import re
import asyncio
import logging
import random
from typing import Optional, List, Tuple

import discord
from discord import app_commands
from discord.ext import tasks

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

"""
Bot Banner
- Slash commands only (no prefix commands)
- No privileged member intent required; enforces bans by user ID
- Runs automatically:
  * when the bot comes online (if info channel is configured and enabled)
  * when the info channel is set the first time (if enabled)
  * periodically in the background (15 minutes) while enabled
- Will not run if the info channel is not configured for the server
"""

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
# Support either DISCORD_TOKEN or DISCORD_BOT_TOKEN for consistency with your other bot
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")

# Logging level from env to match your other project's style
LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

if not DATABASE_URL or not DISCORD_TOKEN:
    raise RuntimeError("Missing DATABASE_URL or DISCORD_TOKEN in environment.")

# Optional: report/review queue config. If unset, /banner report is disabled.
_REVIEW_CHANNEL_ID_RAW = os.getenv("REVIEW_CHANNEL_ID")
_REVIEW_ROLE_ID_RAW = os.getenv("REVIEW_ROLE_ID")
REVIEW_CHANNEL_ID = int(_REVIEW_CHANNEL_ID_RAW) if _REVIEW_CHANNEL_ID_RAW else None
REVIEW_ROLE_ID = int(_REVIEW_ROLE_ID_RAW) if _REVIEW_ROLE_ID_RAW else None

SNOWFLAKE_RE = re.compile(r"^\d{15,20}$")

# Intents: do NOT enable privileged members intent
intents = discord.Intents.none()
intents.guilds = True  # needed for guilds/channels and bans


class BotBanner(discord.Client):
    def __init__(self):
        flags = discord.MemberCacheFlags.none()
        super().__init__(
            intents=intents,
            chunk_guilds_at_startup=False,
            member_cache_flags=flags,
        )
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Register slash command group and sync
        try:
            self.tree.add_command(banner_group)
        except Exception:
            pass
        try:
            await self.tree.sync()
            log.info("Application commands synced.")
        except Exception as e:
            log.warning(f"Command sync failed: {e}")


bot = BotBanner()

log = logging.getLogger("bot_banner")


# -------------------- DB Helpers --------------------
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)


def ensure_tables():
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS public.users (
                        discord_id BIGINT PRIMARY KEY
                    );
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS public.servers (
                        server_id BIGINT PRIMARY KEY,
                        owner_id BIGINT NOT NULL,
                        info_channel_id BIGINT,
                        enabler BOOLEAN NOT NULL DEFAULT FALSE
                    );
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS public.reports (
                        id SERIAL PRIMARY KEY,
                        target_user_id BIGINT NOT NULL,
                        reporter_user_id BIGINT NOT NULL,
                        reporter_server_id BIGINT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        reviewer_user_id BIGINT,
                        review_message_id BIGINT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        decided_at TIMESTAMPTZ
                    );
                    """
                )
    finally:
        conn.close()


def get_spammer_ids() -> List[int]:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT discord_id FROM public.users;")
            rows = cur.fetchall()
            return [int(r[0]) for r in rows]
    finally:
        conn.close()


def upsert_server(server_id: int, owner_id: int, info_channel_id: Optional[int] = None, enabler: Optional[bool] = None):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.servers (server_id, owner_id, info_channel_id, enabler)
                    VALUES (%s, %s, %s, COALESCE(%s, FALSE))
                    ON CONFLICT (server_id) DO UPDATE
                    SET owner_id = EXCLUDED.owner_id,
                        info_channel_id = COALESCE(EXCLUDED.info_channel_id, public.servers.info_channel_id),
                        enabler = COALESCE(EXCLUDED.enabler, public.servers.enabler);
                    """,
                    (server_id, owner_id, info_channel_id, enabler),
                )
    finally:
        conn.close()

def remove_spammer_id(discord_id: int):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM public.users WHERE discord_id = %s;",
                    (discord_id,)
                )
    finally:
        conn.close()

def get_server_info(server_id: int) -> Optional[dict]:
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT server_id, owner_id, info_channel_id, enabler
                FROM public.servers
                WHERE server_id = %s;
                """,
                (server_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def set_info_channel(server_id: int, channel_id: Optional[int]):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.servers SET info_channel_id = %s WHERE server_id = %s;
                    """,
                    (channel_id, server_id),
                )
    finally:
        conn.close()


def set_enabler(server_id: int, enabled: bool):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.servers SET enabler = %s WHERE server_id = %s;
                    """,
                    (enabled, server_id),
                )
    finally:
        conn.close()


def get_enabled_configured_servers() -> List[Tuple[int, int]]:
    """Return (server_id, info_channel_id) for servers that are enabled and have an info channel configured."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT server_id, info_channel_id
                FROM public.servers
                WHERE enabler = TRUE AND info_channel_id IS NOT NULL;
                """
            )
            return [(int(r[0]), int(r[1])) for r in cur.fetchall()]
    finally:
        conn.close()


def add_spammer_id(discord_id: int):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.users (discord_id) VALUES (%s) ON CONFLICT DO NOTHING;",
                    (discord_id,),
                )
    finally:
        conn.close()


def is_spammer_id(discord_id: int) -> bool:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM public.users WHERE discord_id = %s;", (discord_id,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def create_report(target_user_id: int, reporter_user_id: int, reporter_server_id: int) -> int:
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.reports (target_user_id, reporter_user_id, reporter_server_id)
                    VALUES (%s, %s, %s)
                    RETURNING id;
                    """,
                    (target_user_id, reporter_user_id, reporter_server_id),
                )
                return int(cur.fetchone()[0])
    finally:
        conn.close()


def set_report_review_message(report_id: int, message_id: int):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.reports SET review_message_id = %s WHERE id = %s;",
                    (message_id, report_id),
                )
    finally:
        conn.close()


def get_pending_report_for_target(target_user_id: int) -> Optional[dict]:
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id FROM public.reports
                WHERE target_user_id = %s AND status = 'pending'
                LIMIT 1;
                """,
                (target_user_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def get_report(report_id: int) -> Optional[dict]:
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, target_user_id, reporter_user_id, reporter_server_id,
                       status, reviewer_user_id, review_message_id
                FROM public.reports
                WHERE id = %s;
                """,
                (report_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def decide_report(report_id: int, status: str, reviewer_user_id: int):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.reports
                    SET status = %s, reviewer_user_id = %s, decided_at = now()
                    WHERE id = %s;
                    """,
                    (status, reviewer_user_id, report_id),
                )
    finally:
        conn.close()


def get_all_pending_reports() -> List[dict]:
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, review_message_id
                FROM public.reports
                WHERE status = 'pending' AND review_message_id IS NOT NULL;
                """
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# -------------------- Utilities --------------------
async def fetch_username_safe(user_id: int) -> str:
    try:
        user = await bot.fetch_user(user_id)
        display = user.global_name or user.name or str(user_id)
        return f"{display} ({user.id})"
    except Exception:
        return f"{user_id}"


async def send_info(guild: discord.Guild, channel_id: Optional[int], message: str):
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            channel = None
    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        try:
            await channel.send(message)
        except Exception as e:
            log.warning(f"Failed to send message in guild {guild.id} channel {channel_id}: {e}")


# -------------------- Enforcement --------------------
async def enforce_bans_for_guild(
    guild: discord.Guild,
    info_channel_id: int,
    spammer_ids: Optional[List[int]] = None,
) -> int:
    """
    Enforce bans for a single guild.
    Returns the number of *new* users added to the guild's ban list.
    """
    if not guild or not info_channel_id:
        return 0

    # All spammer IDs from DB (or override if provided)
    ids = set(spammer_ids or get_spammer_ids())
    if not ids:
        log.debug(f"No spammer IDs found for guild {guild.id}. Nothing to ban.")
        return 0

    # Fetch current bans from Discord
    already_banned_ids: set[int] = set()
    try:
        async for ban_entry in guild.bans(limit=None):
            already_banned_ids.add(ban_entry.user.id)
    except Exception as e:
        log.debug(f"Failed to fetch ban list in guild {guild.id}: {e}")

    # Only ban IDs that are NOT already banned
    to_ban = ids - already_banned_ids
    if not to_ban:
        log.debug(f"No new bans needed for guild {guild.id}.")
        return 0

    new_ban_count = 0

    for uid in to_ban:
        try:
            # Only detect membership from cache (no intents)
            was_member = guild.get_member(uid) is not None

            # Attempt the ban
            await guild.ban(
                discord.Object(id=uid),
                reason="Listed in commissionSpammer database",
                delete_message_seconds=0,
            )

            new_ban_count += 1

            # Notify if the user was actually in the server at ban time
            if was_member:
                uname = await fetch_username_safe(uid)
                await send_info(
                    guild,
                    info_channel_id,
                    f"User {uname} was in the server and was removed and banned (on banlist).",
                )

            await asyncio.sleep(1.0)  # avoid rate limit issues

        except discord.Forbidden:
            # Bot lacks ban permissions
            await send_info(
                guild,
                info_channel_id,
                "I lack the 'Ban Members' permission. Please adjust role permissions.",
            )
            log.warning(f"Forbidden from banning {uid} in guild {guild.id}")
            break

        except discord.HTTPException as e:
            code = getattr(e, "code", None)

            if code == 30035:
                # Already banned (Discord duplication)
                pass

            elif code == 10013:
                # Unknown User — account deleted or otherwise nonexistent
                log.info(f"User {uid} no longer exists on Discord. Removing from database.")
                remove_spammer_id(uid)

            else:
                log.debug(f"HTTP error banning {uid} in guild {guild.id}: {e}")

            await asyncio.sleep(0.2)
    
        except Exception as e:
            log.debug(f"Unexpected error banning {uid} in guild {guild.id}: {e}")
            await asyncio.sleep(0.2)

    return new_ban_count

async def enforce_bans_once_global():
    targets = get_enabled_configured_servers()
    if not targets:
        return

    spammer_ids = get_spammer_ids()
    if not spammer_ids:
        return

    log.info(
        f"Enforcing {len(spammer_ids)} spammer IDs across {len(targets)} enabled+configured servers."
    )

    # Process each guild sequentially, with a small random jitter between them
    for server_id, channel_id in targets:
        guild = bot.get_guild(server_id)
        if not guild:
            continue

        # Per-guild jitter: 0–3 seconds so large fleets don't all fire at once
        jitter = random.uniform(0, 3)
        await asyncio.sleep(jitter)

        try:
            new_count = await enforce_bans_for_guild(guild, channel_id, spammer_ids)
            log.info(
                f"Guild {guild.id}: enforcement complete, {new_count} new user(s) added to ban list."
            )
        except Exception as e:
            log.exception(f"Error enforcing bans in guild {server_id}: {e}")


@tasks.loop(hours=1)
async def enforce_bans_loop():
    # Add jitter of 0–300 seconds (0–5 minutes)
    jitter_seconds = random.randint(0, 300)
    log.info(f"Jitter delay before global ban enforcement: {jitter_seconds} seconds.")
    await asyncio.sleep(jitter_seconds)
    await enforce_bans_once_global()


def start_loop_if_needed():
    # Start loop only if there's at least one enabled+configured server
    if get_enabled_configured_servers() and not enforce_bans_loop.is_running():
        enforce_bans_loop.start()


# -------------------- Report / Review Queue --------------------
def build_report_embed(
    report_id: int,
    target_id: int,
    target_user: Optional[discord.User],
    reporter_id: int,
    reporter_server_name: str,
    filename: str,
    status: str = "pending",
    reviewer_id: Optional[int] = None,
) -> discord.Embed:
    color = {"pending": discord.Color.gold(), "approved": discord.Color.green(), "rejected": discord.Color.red()}[status]
    embed = discord.Embed(title=f"Spammer Report #{report_id}", color=color)

    if target_user:
        display = target_user.global_name or target_user.name
        embed.add_field(name="Target", value=f"{display} — <@{target_id}> (`{target_id}`)", inline=False)
        embed.set_thumbnail(url=target_user.display_avatar.url)
    else:
        embed.add_field(name="Target", value=f"<@{target_id}> (`{target_id}`)\n*(profile lookup failed — account may be deleted)*", inline=False)

    created = discord.utils.snowflake_time(target_id)
    embed.add_field(name="Account created", value=discord.utils.format_dt(created, style="R"), inline=True)
    embed.add_field(name="Reported by", value=f"<@{reporter_id}> in {reporter_server_name}", inline=True)
    embed.set_image(url=f"attachment://{filename}")

    if status == "pending":
        embed.set_footer(text="Awaiting review")
    elif status == "approved":
        embed.set_footer(text=f"Approved by {reviewer_id} — added to the ban list")
    elif status == "rejected":
        embed.set_footer(text=f"Rejected by {reviewer_id}")

    return embed


class ReportReviewView(discord.ui.View):
    def __init__(self, report_id: int):
        super().__init__(timeout=None)
        self.report_id = report_id

        approve = discord.ui.Button(
            style=discord.ButtonStyle.green,
            label="Approve",
            emoji="✅",
            custom_id=f"report_approve:{report_id}",
        )
        reject = discord.ui.Button(
            style=discord.ButtonStyle.red,
            label="Reject",
            emoji="❌",
            custom_id=f"report_reject:{report_id}",
        )
        approve.callback = self._make_callback("approved")
        reject.callback = self._make_callback("rejected")
        self.add_item(approve)
        self.add_item(reject)

    def _make_callback(self, decision: str):
        async def callback(interaction: discord.Interaction):
            await self._handle_decision(interaction, decision)
        return callback

    async def _handle_decision(self, interaction: discord.Interaction, decision: str):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if REVIEW_ROLE_ID is None or not member or not member.get_role(REVIEW_ROLE_ID):
            await interaction.response.send_message("You don't have permission to review reports.", ephemeral=True)
            return

        report = get_report(self.report_id)
        if not report:
            await interaction.response.send_message("This report no longer exists.", ephemeral=True)
            return
        if report["status"] != "pending":
            await interaction.response.send_message(
                f"Already reviewed (status: {report['status']}).", ephemeral=True
            )
            return

        await interaction.response.defer()

        target_id = int(report["target_user_id"])
        if decision == "approved":
            add_spammer_id(target_id)
        decide_report(self.report_id, decision, interaction.user.id)

        try:
            target_user = await bot.fetch_user(target_id)
        except Exception:
            target_user = None

        message = interaction.message
        filename = "evidence.png"
        if message and message.attachments:
            filename = message.attachments[0].filename

        embed = build_report_embed(
            self.report_id,
            target_id,
            target_user,
            int(report["reporter_user_id"]),
            _guild_name_for(int(report["reporter_server_id"])),
            filename,
            status=decision,
            reviewer_id=interaction.user.id,
        )

        for item in self.children:
            item.disabled = True

        try:
            await interaction.message.edit(embed=embed, view=self)
        except Exception as e:
            log.warning(f"Failed to update report message for report {self.report_id}: {e}")


def _guild_name_for(server_id: int) -> str:
    guild = bot.get_guild(server_id)
    return guild.name if guild else f"server `{server_id}`"


# -------------------- Checks and Commands --------------------
def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return False
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        perms = member.guild_permissions if member else None
        if not perms or not (perms.administrator or perms.manage_guild):
            await interaction.response.send_message("You need Administrator or Manage Server permission.", ephemeral=True)
            return False
        return True

    return app_commands.check(predicate)


banner_group = app_commands.Group(name="banner", description="Bot Banner admin commands")


@banner_group.command(name="set-channel", description="Set the info channel where the bot posts updates.")
@admin_only()
async def set_channel_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return

    # Read existing before update to detect first-time setup
    info_before = get_server_info(guild.id)
    upsert_server(guild.id, guild.owner_id)  # ensure row exists
    set_info_channel(guild.id, channel.id)
    await interaction.response.send_message(f"Info channel set to #{channel.name}.", ephemeral=True)

    # If first time and enabled, run enforcement for this guild only
    info_after = get_server_info(guild.id)
    if (not info_before or not info_before.get("info_channel_id")) and info_after and info_after.get("enabler"):
        await enforce_bans_for_guild(guild, channel.id)
    # Start background loop if needed
    start_loop_if_needed()


@banner_group.command(name="enable", description="Enable or disable automatic banning.")
@admin_only()
async def enable_cmd(interaction: discord.Interaction, enabled: bool):
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return

    # Acknowledge quickly
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
    except Exception:
        pass

    # Update DB (still blocking, but very fast in practice)
    upsert_server(guild.id, guild.owner_id)
    set_enabler(guild.id, enabled)

    info = get_server_info(guild.id)
    note = ""
    run_now = False
    if enabled and info and info.get("info_channel_id"):
        run_now = True   # we'll run it in background
    elif enabled and (not info or not info.get("info_channel_id")):
        note = " Set the info channel with /banner set-channel to begin enforcement."

    # Tell the user right away
    try:
        await interaction.followup.send(
            f"Auto-banning is now {'enabled' if enabled else 'disabled'}.{note}",
            ephemeral=True,
        )
    except Exception as e:
        log.warning(f"Failed to send enable reply in guild {guild.id}: {e}")

    # Kick off ban enforcement in the background if needed
    if run_now:
        async def _run_enforcement():
            try:
                await enforce_bans_for_guild(guild, info["info_channel_id"])
                start_loop_if_needed()
            except Exception:
                log.exception(f"Error running initial enforcement for guild {guild.id}")

        asyncio.create_task(_run_enforcement())


@banner_group.command(name="status", description="Show current server settings.")
@admin_only()
async def status_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    info = get_server_info(guild.id)
    if not info:
        await interaction.response.send_message("No settings found. Use /banner set-channel and /banner enable.", ephemeral=True)
        return
    channel_str = f"<#{info['info_channel_id']}>" if info.get("info_channel_id") else "Not set"
    await interaction.response.send_message(
        f"Enabled: {bool(info.get('enabler'))}\nInfo channel: {channel_str}",
        ephemeral=True,
    )


@banner_group.command(name="sync-now", description="Manually trigger a ban sync now.")
@admin_only()
async def sync_now_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return

    info = get_server_info(guild.id)
    if not info or not info.get("info_channel_id"):
        await interaction.response.send_message(
            "Info channel is not set yet. Use /banner set-channel first.",
            ephemeral=True
        )
        return

    await interaction.response.send_message("Sync started...", ephemeral=True)

    async def _run_sync():
        try:
            new_count = await enforce_bans_for_guild(guild, info["info_channel_id"])
            await interaction.followup.send(
                f"Sync complete. **{new_count} new user{'s' if new_count != 1 else ''}** added to the ban list.",
                ephemeral=True
            )
        except Exception:
            log.exception(f"Error during manual sync for guild {guild.id}")
            try:
                await interaction.followup.send("Sync failed due to an internal error.", ephemeral=True)
            except:
                pass

    asyncio.create_task(_run_sync())


@banner_group.command(name="report", description="Report a user ID as a commission scammer for review.")
@app_commands.describe(user_id="The numeric Discord user ID of the suspected scammer", evidence="Screenshot proving the claim")
@admin_only()
async def report_cmd(interaction: discord.Interaction, user_id: str, evidence: discord.Attachment):
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return

    if REVIEW_CHANNEL_ID is None or REVIEW_ROLE_ID is None:
        await interaction.response.send_message(
            "Reporting isn't configured on this bot yet.", ephemeral=True
        )
        return

    if not SNOWFLAKE_RE.match(user_id):
        await interaction.response.send_message(
            "That doesn't look like a valid Discord user ID (numbers only). "
            "Right-click the user and choose \"Copy User ID\" (Developer Mode must be enabled).",
            ephemeral=True,
        )
        return

    target_id = int(user_id)

    if is_spammer_id(target_id):
        await interaction.response.send_message("That user is already on the ban list.", ephemeral=True)
        return

    if get_pending_report_for_target(target_id):
        await interaction.response.send_message("That user already has a pending report.", ephemeral=True)
        return

    review_channel = bot.get_channel(REVIEW_CHANNEL_ID)
    if review_channel is None:
        try:
            review_channel = await bot.fetch_channel(REVIEW_CHANNEL_ID)
        except Exception:
            review_channel = None
    if not isinstance(review_channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message(
            "The review channel is misconfigured; please contact the bot owner.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        target_user = await bot.fetch_user(target_id)
    except Exception:
        target_user = None

    report_id = create_report(target_id, interaction.user.id, guild.id)

    file_bytes = await evidence.read()
    discord_file = discord.File(io.BytesIO(file_bytes), filename=evidence.filename)

    embed = build_report_embed(
        report_id,
        target_id,
        target_user,
        interaction.user.id,
        guild.name,
        evidence.filename,
        status="pending",
    )
    view = ReportReviewView(report_id)

    try:
        message = await review_channel.send(embed=embed, file=discord_file, view=view)
        set_report_review_message(report_id, message.id)
    except Exception:
        log.exception(f"Failed to post report {report_id} to review channel")
        await interaction.followup.send("Failed to submit report due to an internal error.", ephemeral=True)
        return

    await interaction.followup.send(f"Report submitted (#{report_id}) and is awaiting review.", ephemeral=True)


# -------------------- Events --------------------
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (id: {bot.user.id})")
    ensure_tables()

    # Ensure we have a row for each guild
    for g in bot.guilds:
        upsert_server(g.id, g.owner_id)

    # Re-attach persistent Approve/Reject views for any reports still awaiting review,
    # otherwise their buttons stop working after this restart.
    for report in get_all_pending_reports():
        bot.add_view(ReportReviewView(report["id"]), message_id=report["review_message_id"])

    # Run once globally (only for enabled+configured servers), then start loop if needed
    await enforce_bans_once_global()
    start_loop_if_needed()


@bot.event
async def on_guild_join(guild: discord.Guild):
    # Bot added to a new server
    upsert_server(guild.id, guild.owner_id)
    # Do not enforce until channel is set and enabled
    start_loop_if_needed()


# -------------------- Entry --------------------
bot.run(DISCORD_TOKEN)