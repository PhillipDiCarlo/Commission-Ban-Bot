import os
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




# -------------------- Events --------------------
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (id: {bot.user.id})")
    ensure_tables()

    # Ensure we have a row for each guild
    for g in bot.guilds:
        upsert_server(g.id, g.owner_id)

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