import os
import io
import re
import asyncio
import logging
import random
from typing import Optional, List, Tuple, Set

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
  * periodically in the background (every ENFORCE_INTERVAL_HOURS, default 24h) while enabled
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

log = logging.getLogger("bot_banner")


def _parse_optional_int_env(raw: Optional[str], name: str) -> Optional[int]:
    """Parse an optional env var as an int without taking the whole bot down on a typo."""
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        log.warning(f"{name} is set but isn't a valid integer ({raw!r}); ignoring it.")
        return None


# Optional: report/review queue config. If unset (or malformed), /banner report is disabled,
# but the rest of the bot (core ban enforcement) still starts normally.
_REVIEW_CHANNEL_ID_RAW = os.getenv("REVIEW_CHANNEL_ID")
_REVIEW_ROLE_ID_RAW = os.getenv("REVIEW_ROLE_ID")
REVIEW_CHANNEL_ID = _parse_optional_int_env(_REVIEW_CHANNEL_ID_RAW, "REVIEW_CHANNEL_ID")
REVIEW_ROLE_ID = _parse_optional_int_env(_REVIEW_ROLE_ID_RAW, "REVIEW_ROLE_ID")

SNOWFLAKE_RE = re.compile(r"^\d{15,20}$")
# Postgres BIGINT max — SNOWFLAKE_RE's digit-count check alone lets through values that
# would overflow the column (up to 20 digits; BIGINT tops out at 19).
DISCORD_MAX_SNOWFLAKE = 9223372036854775807


def is_valid_snowflake(value: str) -> bool:
    if not SNOWFLAKE_RE.match(value):
        return False
    return int(value) <= DISCORD_MAX_SNOWFLAKE


MAX_EVIDENCE_SIZE_BYTES = 8 * 1024 * 1024  # 8 MiB

# /banner history caps the number of report rows rendered as embed fields -- Discord embeds
# have a hard 25-field limit, so this stays comfortably under that regardless of how much
# history a given target has accumulated. Older rows beyond the cap are just noted as an
# omitted count rather than shown.
HISTORY_DISPLAY_LIMIT = 20

# Optional: sync slash commands to a single guild instead of globally. Guild-scoped syncs
# propagate near-instantly (global syncs can take up to ~1hr), so set this during development
# for fast iteration; leave unset in production for the normal global sync.
DEV_GUILD_ID = _parse_optional_int_env(os.getenv("DEV_GUILD_ID"), "DEV_GUILD_ID")


def _parse_float_env(raw: Optional[str], default: float, name: str) -> float:
    """Parse an optional env var as a float, falling back to `default` (never raises)."""
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning(f"{name} is set but isn't a valid number ({raw!r}); using the default of {default}.")
        return default


# How often the background job re-enforces the ban list across all enabled+configured
# servers. Was hardcoded to 1 hour, which didn't match the actually-intended cadence
# (once a day / every 12h); now configurable, defaulting to daily.
ENFORCE_INTERVAL_HOURS = _parse_float_env(os.getenv("ENFORCE_INTERVAL_HOURS"), 24.0, "ENFORCE_INTERVAL_HOURS")

# Rate-limit /banner report submissions per-reporter, so a single admin/mod can't spam the
# shared review queue. A reporter at or over REPORT_RATE_LIMIT_MAX reports within the last
# REPORT_RATE_LIMIT_WINDOW_HOURS hours is blocked from submitting another until the window
# rolls forward. _parse_optional_int_env doesn't take a default param (see REVIEW_CHANNEL_ID
# / DEV_GUILD_ID above), so the default is applied afterward rather than passed in.
_REPORT_RATE_LIMIT_MAX_RAW = _parse_optional_int_env(os.getenv("REPORT_RATE_LIMIT_MAX"), "REPORT_RATE_LIMIT_MAX")
REPORT_RATE_LIMIT_MAX = _REPORT_RATE_LIMIT_MAX_RAW if _REPORT_RATE_LIMIT_MAX_RAW is not None else 5
REPORT_RATE_LIMIT_WINDOW_HOURS = _parse_float_env(
    os.getenv("REPORT_RATE_LIMIT_WINDOW_HOURS"), 24.0, "REPORT_RATE_LIMIT_WINDOW_HOURS"
)

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
            if DEV_GUILD_ID is not None:
                # Guild-scoped sync propagates near-instantly, unlike a global sync (up to
                # ~1hr) -- much faster iteration while developing.
                dev_guild = discord.Object(id=DEV_GUILD_ID)
                self.tree.copy_global_to(guild=dev_guild)
                await self.tree.sync(guild=dev_guild)
                log.info(f"Application commands synced to dev guild {DEV_GUILD_ID}.")
            else:
                await self.tree.sync()
                log.info("Application commands synced globally.")
        except Exception as e:
            log.warning(f"Command sync failed: {e}")


bot = BotBanner()


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
                # At most one pending report per target at a time — closes the race where
                # two reports for the same user get created within the same instant.
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS reports_one_pending_per_target
                    ON public.reports (target_user_id)
                    WHERE status = 'pending';
                    """
                )
                # Local record of (guild, user) pairs this bot has already confirmed
                # banned, so enforce_bans_for_guild can diff against this instead of
                # re-downloading the guild's entire live ban list from Discord every
                # enforcement cycle (see enforce_bans_for_guild's docstring).
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS public.enforced_bans (
                        server_id BIGINT NOT NULL,
                        discord_id BIGINT NOT NULL,
                        PRIMARY KEY (server_id, discord_id)
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


def get_enforced_ban_ids(server_id: int) -> Set[int]:
    """Return the set of discord_ids already recorded as enforced (bot-confirmed banned) for this guild."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT discord_id FROM public.enforced_bans WHERE server_id = %s;",
                (server_id,),
            )
            return {int(r[0]) for r in cur.fetchall()}
    finally:
        conn.close()


def record_enforced_ban(server_id: int, discord_id: int):
    """Record that discord_id has been confirmed banned in server_id, so future enforcement
    cycles skip it without needing to re-check Discord's live ban list."""
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.enforced_bans (server_id, discord_id)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING;
                    """,
                    (server_id, discord_id),
                )
    finally:
        conn.close()


def remove_enforced_ban(server_id: int, discord_id: int):
    """Remove a stale enforced-ban record, e.g. after a force_refresh reconciliation finds
    discord_id is no longer actually banned in server_id (a moderator manually unbanned them)."""
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM public.enforced_bans WHERE server_id = %s AND discord_id = %s;",
                    (server_id, discord_id),
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


def count_recent_reports_by_reporter(reporter_user_id: int, window_hours: float) -> int:
    """Count how many reports reporter_user_id has filed within the last window_hours,
    used to rate-limit /banner report. Uses Postgres's own time arithmetic (now() -
    interval) rather than computing a cutoff timestamp in Python, so this stays correct
    regardless of any clock skew between the app host and the DB host."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM public.reports
                WHERE reporter_user_id = %s AND created_at > now() - (%s * interval '1 hour');
                """,
                (reporter_user_id, window_hours),
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


def get_report_history_for_target(target_user_id: int) -> List[dict]:
    """Every report ever filed against target_user_id (any status), most recent first --
    unlike get_pending_report_for_target, this isn't limited to the current pending report."""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, status, reporter_user_id, reporter_server_id, reviewer_user_id,
                       created_at, decided_at
                FROM public.reports
                WHERE target_user_id = %s
                ORDER BY created_at DESC;
                """,
                (target_user_id,),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def decide_report(report_id: int, status: str, reviewer_user_id: int) -> bool:
    """
    Atomically transition a report from 'pending' to the given status, and — if
    approving — add the target to the ban list in the *same* transaction.

    Returns True if this call actually performed the transition, False if the
    report was no longer 'pending' by the time this ran (e.g. another reviewer
    already decided it). The WHERE clause + single transaction is what makes
    this safe under two reviewers racing: at most one caller's UPDATE can match
    a row, so the ban-list insert can only ever fire for whichever decision
    actually won, never for a decision that lost the race.
    """
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    UPDATE public.reports
                    SET status = %s, reviewer_user_id = %s, decided_at = now()
                    WHERE id = %s AND status = 'pending'
                    RETURNING target_user_id;
                    """,
                    (status, reviewer_user_id, report_id),
                )
                row = cur.fetchone()
                if row is None:
                    return False
                if status == "approved":
                    cur.execute(
                        "INSERT INTO public.users (discord_id) VALUES (%s) ON CONFLICT DO NOTHING;",
                        (row["target_user_id"],),
                    )
                return True
    finally:
        conn.close()


def delete_report(report_id: int):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM public.reports WHERE id = %s;", (report_id,))
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
# asyncio only holds a *weak* reference to a task created via asyncio.create_task, so a
# fire-and-forget task with nothing else referencing it can be garbage-collected before
# it finishes (a well-known asyncio footgun, documented in the stdlib docs). Keeping a
# strong reference here until the task is done avoids that.
_background_tasks: set = set()


def spawn_background_task(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


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
    force_refresh: bool = False,
) -> int:
    """
    Enforce bans for a single guild.
    Returns the number of *new* users added to the guild's ban list.

    "Already handled" set — normal path vs. force_refresh:
    Rather than recomputing the diff against Discord's live ban list every cycle (a
    fully paginated guild.bans(limit=None) call that re-downloads the *entire* ban
    list purely to compute a set difference that usually changes by a handful of IDs
    cycle over cycle — expensive and slow for large lists, which is the whole point
    of this bot), the normal automatic path diffs against a local Postgres record
    (public.enforced_bans) of (guild, user) pairs this bot has already confirmed
    banned. That's populated as bans succeed (and via the 30035 "already banned"
    branch below), so it stays in sync without ever needing to re-fetch Discord's
    live list.

    Trade-off: if a moderator manually *unbans* someone through Discord's own UI, the
    local record still says "banned", so the normal automatic path won't notice or
    re-ban them. That's accepted as the cost of the routine cycle. For cases that
    need Discord's live truth (e.g. reconciling after manual unbans, or catching up
    on bans that happened before this table existed), pass force_refresh=True — used
    by the manual /banner sync-now command — which still pulls the live ban list and
    backfills anything found there into enforced_bans before computing the diff.
    """
    if not guild or not info_channel_id:
        return 0

    # All spammer IDs from DB (or override if provided)
    if spammer_ids:
        ids = set(spammer_ids)
    else:
        ids = set(await asyncio.to_thread(get_spammer_ids))
    if not ids:
        log.debug(f"No spammer IDs found for guild {guild.id}. Nothing to ban.")
        return 0

    # "Already handled" set: local record of IDs this bot has already confirmed
    # banned in this guild, instead of re-fetching Discord's full live ban list.
    try:
        already_banned_ids: Set[int] = await asyncio.to_thread(get_enforced_ban_ids, guild.id)
    except Exception as e:
        log.warning(f"Failed to load enforced ban cache for guild {guild.id}: {e}")
        already_banned_ids = set()

    if force_refresh:
        # Manual reconciliation path (/banner sync-now): pull Discord's live ban list
        # and reconcile it against the local record in both directions, scoped to
        # spammer ids only (`ids`) -- NOT every banned user in the guild:
        #   - backfill: a spammer id that's live-banned but not yet recorded locally
        #     (banned manually by a moderator, or banned before enforced_bans existed).
        #   - prune: a spammer id we thought was enforced but isn't actually banned
        #     anymore (a moderator manually unbanned them). Without pruning, a stale
        #     "enforced" row would permanently and silently block ever re-banning that
        #     id in this guild, even after a later legitimate re-approval of the same
        #     id via /banner report.
        # Only ever touching ids in `ids` here is deliberate: backfilling *every*
        # currently-banned user (not just spammers) would record unrelated moderator
        # bans (e.g. a raid ban) into enforced_bans, which would then silently mask
        # that same id if it later, separately, became a real approved spammer.
        try:
            live_banned_ids: Set[int] = set()
            async for ban_entry in guild.bans(limit=None):
                live_banned_ids.add(ban_entry.user.id)

            to_backfill = (live_banned_ids & ids) - already_banned_ids
            for uid in to_backfill:
                try:
                    await asyncio.to_thread(record_enforced_ban, guild.id, uid)
                except Exception as e:
                    log.warning(f"Failed to record enforced ban for {uid} in guild {guild.id}: {e}")
            already_banned_ids |= to_backfill

            stale = (already_banned_ids & ids) - live_banned_ids
            for uid in stale:
                try:
                    await asyncio.to_thread(remove_enforced_ban, guild.id, uid)
                except Exception as e:
                    log.warning(f"Failed to prune stale enforced ban for {uid} in guild {guild.id}: {e}")
            already_banned_ids -= stale
        except Exception as e:
            log.warning(f"Failed to fetch live ban list in guild {guild.id}: {e}")

    # Only ban IDs that are NOT already banned/recorded
    to_ban = ids - already_banned_ids
    if not to_ban:
        log.debug(f"No new bans needed for guild {guild.id}.")
        return 0

    new_ban_count = 0

    for uid in to_ban:
        try:
            # Attempt the ban. There's deliberately no "was this user actually a member"
            # check/notification here -- the bot runs without the privileged Members
            # intent and with an empty member cache (see the module docstring), so
            # guild.get_member() can't reliably tell membership apart from "not cached";
            # a notification gated on that would be silently wrong almost all the time
            # rather than actually informative.
            await guild.ban(
                discord.Object(id=uid),
                reason="Listed in commissionSpammer database",
                delete_message_seconds=0,
            )

            new_ban_count += 1
            try:
                await asyncio.to_thread(record_enforced_ban, guild.id, uid)
            except Exception as e:
                log.warning(f"Failed to record enforced ban for {uid} in guild {guild.id}: {e}")

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
                # Already banned (Discord duplication). Since the normal path no longer
                # pre-checks Discord's live ban list, this is now the only way a user
                # who was already banned by some other means (manually by a moderator,
                # or before enforced_bans existed) gets backfilled into the local
                # record -- without this, the bot would keep re-attempting (and
                # re-hitting this same "already banned" error) for that user forever,
                # every cycle.
                try:
                    await asyncio.to_thread(record_enforced_ban, guild.id, uid)
                except Exception as rec_e:
                    log.warning(f"Failed to record enforced ban for {uid} in guild {guild.id}: {rec_e}")

            elif code == 10013:
                # Unknown User — account deleted or otherwise nonexistent
                log.info(f"User {uid} no longer exists on Discord. Removing from database.")
                await asyncio.to_thread(remove_spammer_id, uid)

            else:
                log.debug(f"HTTP error banning {uid} in guild {guild.id}: {e}")

            await asyncio.sleep(0.2)

        except Exception as e:
            log.debug(f"Unexpected error banning {uid} in guild {guild.id}: {e}")
            await asyncio.sleep(0.2)

    return new_ban_count

async def enforce_bans_once_global():
    # These two DB calls used to be unguarded. When a discord.ext.tasks.Loop coroutine
    # raises an unhandled exception, discord.py logs it and permanently cancels the loop
    # (no auto-retry) -- so a single transient DB hiccup here used to silently kill all
    # future scheduled enforcement for the rest of the process's life, with the bot
    # otherwise still looking healthy. Catching here means a bad cycle just gets skipped
    # and retried next time instead.
    try:
        targets = await asyncio.to_thread(get_enabled_configured_servers)
    except Exception:
        log.exception("Failed to load enabled/configured servers; skipping this enforcement cycle.")
        return
    if not targets:
        return

    try:
        spammer_ids = await asyncio.to_thread(get_spammer_ids)
    except Exception:
        log.exception("Failed to load spammer ids; skipping this enforcement cycle.")
        return
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


@tasks.loop(hours=ENFORCE_INTERVAL_HOURS)
async def enforce_bans_loop():
    # Add jitter of 0–300 seconds (0–5 minutes)
    jitter_seconds = random.randint(0, 300)
    log.info(f"Jitter delay before global ban enforcement: {jitter_seconds} seconds.")
    await asyncio.sleep(jitter_seconds)
    try:
        await enforce_bans_once_global()
    except Exception:
        # enforce_bans_once_global already guards its own DB calls, so this shouldn't
        # normally trigger -- but discord.py permanently cancels a tasks.Loop on any
        # exception that escapes the coroutine (no auto-retry for non-network errors),
        # so this is a last-resort backstop to make sure that never happens silently.
        log.exception("Unhandled error during scheduled global ban enforcement; will retry next cycle.")


@enforce_bans_loop.error
async def enforce_bans_loop_error(exc: BaseException):
    # If something still gets past the try/except above (a bug we didn't anticipate),
    # discord.py has already logged it and is about to let the task die permanently.
    # Schedule a delayed restart so a single unexpected failure doesn't silently end
    # automatic enforcement for the rest of the process's life -- which is exactly the
    # failure mode that made this loop "never really work" in production before.
    log.error(f"enforce_bans_loop crashed unexpectedly; scheduling a restart: {exc}")

    async def _restart_after_backoff():
        await asyncio.sleep(60)
        if not enforce_bans_loop.is_running():
            log.warning("Restarting enforce_bans_loop after unexpected crash.")
            enforce_bans_loop.start()

    spawn_background_task(_restart_after_backoff())


async def start_loop_if_needed():
    # Start loop only if there's at least one enabled+configured server
    if await asyncio.to_thread(get_enabled_configured_servers) and not enforce_bans_loop.is_running():
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
        display = discord.utils.escape_markdown(target_user.global_name or target_user.name)
        embed.add_field(name="Target", value=f"{display} — <@{target_id}> (`{target_id}`)", inline=False)
        embed.set_thumbnail(url=target_user.display_avatar.url)
    else:
        embed.add_field(name="Target", value=f"<@{target_id}> (`{target_id}`)\n*(profile lookup failed — account may be deleted)*", inline=False)

    created = discord.utils.snowflake_time(target_id)
    embed.add_field(name="Account created", value=discord.utils.format_dt(created, style="R"), inline=True)
    # reporter_server_name is an attacker-influenceable Discord server name (any admin can set
    # it to anything, including markdown that could otherwise mask a phishing link in this
    # trusted bot's embed) — escape it before interpolating.
    safe_server_name = discord.utils.escape_markdown(reporter_server_name)
    embed.add_field(name="Reported by", value=f"<@{reporter_id}> in {safe_server_name}", inline=True)
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

        report = await asyncio.to_thread(get_report, self.report_id)
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

        try:
            # decide_report is the single source of truth for the transition: it only
            # succeeds if the report was still 'pending' at the moment of the UPDATE, and
            # adds the target to the ban list in the same transaction when approving — so
            # two reviewers racing (or a click landing after someone else already decided)
            # can never desync the displayed status from whether the user actually got
            # banned.
            won = await asyncio.to_thread(decide_report, self.report_id, decision, interaction.user.id)
        except Exception:
            log.exception(f"Failed to record decision for report {self.report_id}")
            try:
                await interaction.followup.send(
                    "Failed to record that decision due to an internal error. Please try again.",
                    ephemeral=True,
                )
            except Exception:
                pass
            return

        if not won:
            current = await asyncio.to_thread(get_report, self.report_id)
            status = current["status"] if current else "unknown"
            await interaction.followup.send(
                f"Already reviewed by someone else (status: {status}).", ephemeral=True
            )
            return

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
            # The decision itself is already durably recorded above; a failure here is
            # cosmetic (message doesn't visually update), not a correctness problem.
            log.warning(f"Failed to update report message for report {self.report_id}: {e}")

        # Best-effort: let the original reporter know what happened to their report. Lots
        # of users have DMs closed to bots without a mutual-server relationship, so
        # fetch_user raising discord.NotFound, user.send raising discord.Forbidden, or any
        # other exception here is an expected, routine outcome -- not an error -- and must
        # never affect the review outcome (already durably recorded above) or the message
        # edit above, both of which have already happened by this point.
        try:
            reporter_id = int(report["reporter_user_id"])
            reporter = await bot.fetch_user(reporter_id)
            if decision == "approved":
                dm_text = (
                    f"Your spammer report **#{self.report_id}** for <@{target_id}> (`{target_id}`) "
                    f"has been **approved**. This user has been added to the shared ban list and "
                    f"will now be enforced across every opted-in server. Thanks for the report."
                )
            else:
                dm_text = (
                    f"Your spammer report **#{self.report_id}** for <@{target_id}> (`{target_id}`) "
                    f"has been **rejected** after review. No action will be taken against this user."
                )
            await reporter.send(dm_text)
        except Exception as e:
            log.debug(f"Could not DM reporter about report {self.report_id} outcome: {e}")


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
    info_before = await asyncio.to_thread(get_server_info, guild.id)
    await asyncio.to_thread(upsert_server, guild.id, guild.owner_id)  # ensure row exists
    await asyncio.to_thread(set_info_channel, guild.id, channel.id)
    await interaction.response.send_message(f"Info channel set to #{channel.name}.", ephemeral=True)

    # If first time and enabled, run enforcement for this guild only
    info_after = await asyncio.to_thread(get_server_info, guild.id)
    if (not info_before or not info_before.get("info_channel_id")) and info_after and info_after.get("enabler"):
        await enforce_bans_for_guild(guild, channel.id)
    # Start background loop if needed
    await start_loop_if_needed()


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

    # Update DB (offloaded to a worker thread so it doesn't stall the event loop)
    await asyncio.to_thread(upsert_server, guild.id, guild.owner_id)
    await asyncio.to_thread(set_enabler, guild.id, enabled)

    info = await asyncio.to_thread(get_server_info, guild.id)
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
                await start_loop_if_needed()
            except Exception:
                log.exception(f"Error running initial enforcement for guild {guild.id}")

        spawn_background_task(_run_enforcement())


@banner_group.command(name="status", description="Show current server settings.")
@admin_only()
async def status_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    info = await asyncio.to_thread(get_server_info, guild.id)
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

    info = await asyncio.to_thread(get_server_info, guild.id)
    if not info or not info.get("info_channel_id"):
        await interaction.response.send_message(
            "Info channel is not set yet. Use /banner set-channel first.",
            ephemeral=True
        )
        return

    await interaction.response.send_message("Sync started...", ephemeral=True)

    async def _run_sync():
        try:
            # force_refresh=True: this is the manual "make sure everything's actually
            # in sync right now" escape hatch -- unlike the automatic cycle, it's fine
            # (expected, even) for this to pay the cost of re-checking Discord's live
            # ban list, since it also catches manual unbans / out-of-band bans that the
            # cheaper local-cache diff used elsewhere can't see.
            new_count = await enforce_bans_for_guild(guild, info["info_channel_id"], force_refresh=True)
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

    spawn_background_task(_run_sync())


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

    if not is_valid_snowflake(user_id):
        await interaction.response.send_message(
            "That doesn't look like a valid Discord user ID (numbers only). "
            "Right-click the user and choose \"Copy User ID\" (Developer Mode must be enabled).",
            ephemeral=True,
        )
        return

    target_id = int(user_id)

    if not (evidence.content_type or "").startswith("image/"):
        await interaction.response.send_message("Evidence must be an image file.", ephemeral=True)
        return

    if evidence.size > MAX_EVIDENCE_SIZE_BYTES:
        await interaction.response.send_message(
            f"Evidence file is too large (max {MAX_EVIDENCE_SIZE_BYTES // (1024 * 1024)} MB).", ephemeral=True
        )
        return

    # Rate-limit per-reporter, checked before any of the heavier DB lookups below so a
    # reporter who's already over the limit fails fast and cheaply.
    recent_count = await asyncio.to_thread(
        count_recent_reports_by_reporter, interaction.user.id, REPORT_RATE_LIMIT_WINDOW_HOURS
    )
    if recent_count >= REPORT_RATE_LIMIT_MAX:
        await interaction.response.send_message(
            "You've submitted too many reports recently; please try again later.", ephemeral=True
        )
        return

    if await asyncio.to_thread(is_spammer_id, target_id):
        await interaction.response.send_message("That user is already on the ban list.", ephemeral=True)
        return

    if await asyncio.to_thread(get_pending_report_for_target, target_id):
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

    try:
        report_id = await asyncio.to_thread(create_report, target_id, interaction.user.id, guild.id)
    except psycopg2.IntegrityError:
        # The one-pending-report-per-target unique index caught a race that the earlier
        # get_pending_report_for_target check missed (two reports for the same target
        # submitted within the same instant).
        await interaction.followup.send("That user already has a pending report.", ephemeral=True)
        return
    except Exception:
        log.exception(f"Failed to create report row for target {target_id}")
        await interaction.followup.send("Failed to submit report due to an internal error.", ephemeral=True)
        return

    # From here on, a report row exists. Anything below that fails must delete it —
    # otherwise it's stuck at status='pending' forever with no review_message_id, which
    # (a) permanently blocks anyone from ever reporting this target_id again, and
    # (b) is invisible to the restart-rehydration query (it only looks at rows that DO
    # have a review_message_id), so it would never self-heal.
    try:
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

        message = await review_channel.send(embed=embed, file=discord_file, view=view)
        await asyncio.to_thread(set_report_review_message, report_id, message.id)
    except Exception:
        log.exception(f"Failed to post report {report_id} to review channel; removing orphaned report row")
        try:
            await asyncio.to_thread(delete_report, report_id)
        except Exception:
            log.exception(f"Failed to clean up orphaned report {report_id}")
        await interaction.followup.send("Failed to submit report due to an internal error. Please try again.", ephemeral=True)
        return

    await interaction.followup.send(f"Report submitted (#{report_id}) and is awaiting review.", ephemeral=True)


@banner_group.command(name="history", description="Show the report history filed against a user ID.")
@app_commands.describe(user_id="The numeric Discord user ID to look up")
@admin_only()
async def history_cmd(interaction: discord.Interaction, user_id: str):
    if not is_valid_snowflake(user_id):
        await interaction.response.send_message(
            "That doesn't look like a valid Discord user ID (numbers only). "
            "Right-click the user and choose \"Copy User ID\" (Developer Mode must be enabled).",
            ephemeral=True,
        )
        return

    target_id = int(user_id)

    try:
        history = await asyncio.to_thread(get_report_history_for_target, target_id)
    except Exception:
        log.exception(f"Failed to fetch report history for target {target_id}")
        await interaction.response.send_message(
            "Failed to look up report history due to an internal error.", ephemeral=True
        )
        return

    if not history:
        await interaction.response.send_message(
            f"No report history found for `{target_id}`.", ephemeral=True
        )
        return

    embed = discord.Embed(title=f"Report History — `{target_id}`", color=discord.Color.blurple())

    shown = history[:HISTORY_DISPLAY_LIMIT]
    if len(history) > HISTORY_DISPLAY_LIMIT:
        embed.description = f"Showing the {HISTORY_DISPLAY_LIMIT} most recent of {len(history)} total reports."

    for entry in shown:
        status = entry.get("status", "unknown")
        created_at = entry.get("created_at")
        created_str = discord.utils.format_dt(created_at, style="R") if created_at else "unknown time"

        reporter_line = f"Reported by <@{entry['reporter_user_id']}> from server `{entry['reporter_server_id']}`"

        reviewer_id = entry.get("reviewer_user_id")
        decided_at = entry.get("decided_at")
        if reviewer_id and decided_at:
            review_line = f"Reviewed by <@{reviewer_id}> {discord.utils.format_dt(decided_at, style='R')}"
        elif status == "pending":
            review_line = "Still pending review"
        else:
            review_line = "Not yet reviewed"

        embed.add_field(
            name=f"#{entry['id']} — {status} ({created_str})",
            value=f"{reporter_line}\n{review_line}",
            inline=False,
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@banner_group.command(name="report-cancel", description="Cancel a stuck pending report (review team only).")
@app_commands.describe(report_id="The report ID to cancel")
async def report_cancel_cmd(interaction: discord.Interaction, report_id: int):
    # Permission here mirrors the review-role gate on the Approve/Reject buttons, not
    # admin_only() — this is a review-team action, not a per-server admin one, since it
    # operates on the single global review queue.
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if REVIEW_ROLE_ID is None or not member or not member.get_role(REVIEW_ROLE_ID):
        await interaction.response.send_message("You don't have permission to manage reports.", ephemeral=True)
        return

    report = await asyncio.to_thread(get_report, report_id)
    if not report:
        await interaction.response.send_message(f"No report with id #{report_id}.", ephemeral=True)
        return
    if report["status"] != "pending":
        await interaction.response.send_message(
            f"Report #{report_id} is already {report['status']}, nothing to cancel.", ephemeral=True
        )
        return

    await asyncio.to_thread(delete_report, report_id)
    await interaction.response.send_message(
        f"Report #{report_id} cancelled — that user can be reported again.", ephemeral=True
    )


async def _check_review_config_health():
    """
    Log a clear, visible warning at startup if REVIEW_CHANNEL_ID/REVIEW_ROLE_ID are set but
    don't actually resolve (channel deleted/inaccessible, or role deleted from the guild).
    Without this, that failure mode is completely silent — reports would just quietly pile up
    unreviewable with nothing telling the operator why.
    """
    if REVIEW_CHANNEL_ID is None or REVIEW_ROLE_ID is None:
        return

    review_channel = bot.get_channel(REVIEW_CHANNEL_ID)
    if review_channel is None:
        try:
            review_channel = await bot.fetch_channel(REVIEW_CHANNEL_ID)
        except Exception:
            review_channel = None

    if review_channel is None:
        log.warning(
            f"REVIEW_CHANNEL_ID {REVIEW_CHANNEL_ID} could not be resolved; /banner report will fail to post."
        )
        return

    guild = getattr(review_channel, "guild", None)
    if guild is not None and guild.get_role(REVIEW_ROLE_ID) is None:
        log.warning(
            f"REVIEW_ROLE_ID {REVIEW_ROLE_ID} does not exist in guild {guild.id}; "
            "nobody will be able to approve/reject reports."
        )


# -------------------- Events --------------------
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (id: {bot.user.id})")
    # Deliberately NOT asyncio.to_thread'd, unlike every other DB call in this file.
    # A blocking call here monopolizes the event loop for its (short, one-time-real)
    # duration, which is exactly what's wanted: it guarantees no slash command
    # interaction can be dispatched and hit `public.users`/`public.servers`/etc.
    # before they exist. Offloading this specific call would let the event loop start
    # processing other coroutines (including interaction dispatch) while table
    # creation is still in flight on a worker thread -- a real race only on a fresh
    # database's very first on_ready, but a bad one (a command hitting a table that
    # doesn't exist yet, with no app_commands error handler registered to catch it).
    ensure_tables()

    # Ensure we have a row for each guild
    for g in bot.guilds:
        await asyncio.to_thread(upsert_server, g.id, g.owner_id)

    # Re-attach persistent Approve/Reject views for any reports still awaiting review,
    # otherwise their buttons stop working after this restart.
    pending_reports = await asyncio.to_thread(get_all_pending_reports)
    for report in pending_reports:
        bot.add_view(ReportReviewView(report["id"]), message_id=report["review_message_id"])

    await _check_review_config_health()

    # Run once globally (only for enabled+configured servers), then start loop if needed
    await enforce_bans_once_global()
    await start_loop_if_needed()


@bot.event
async def on_guild_join(guild: discord.Guild):
    # Bot added to a new server
    await asyncio.to_thread(upsert_server, guild.id, guild.owner_id)
    # Do not enforce until channel is set and enabled
    await start_loop_if_needed()


# -------------------- Entry --------------------
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)