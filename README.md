# Bot Banner

## Overview
Bot Banner is a Discord bot that uses slash commands only and bans by user ID from a PostgreSQL "commissionSpammer" database. It posts updates to a per-server info channel and runs automatically without requiring the privileged Server Members intent.

## Features
- Slash commands only (no prefix commands)
- No privileged intents required; bans by user ID
- Per-server configuration stored in Postgres (`public.servers`)
- Auto enforcement runs:
  - when the bot comes online
  - the first time the info channel is set (if enabled)
  - periodically in the background (every `ENFORCE_INTERVAL_HOURS`, default 24h, if enabled)
- Skips any server that hasn’t set an info channel

## How it works
- Pre-ban by user ID: the bot bans by Discord snowflake via REST. This does not require the Server Members privileged intent.
  - If the user is already in the server, the ban applies immediately (they are removed right away).
  - If the user is not in the server, Discord stores the ban; any future join attempt is blocked automatically.
- Message deletion: bans currently use `delete_message_seconds=0` (no message purge). You can change this value in `bot.py` to delete up to 7 days (604800 seconds) of messages per Discord API limits.

## Project Structure
```
Commission-Ban-Bot/
├── bot.py                       # Main bot (single file)
├── requirements.txt             # Dependencies (discord.py 2.x)
├── .env.example                 # Template for environment variables
├── Dockerfile-bot                # Container image (runs as a non-root user)
├── docker-compose.yml            # Single-service compose file
├── tests/                       # unittest suite (no live Discord/Postgres needed)
├── .github/workflows/ci.yml     # CI: py_compile + full test suite on push/PR
└── README.md                    # This file
```

## Setup

1) Install Python 3.10+ and dependencies

```powershell
python -m pip install --upgrade pip ; pip install -r requirements.txt
```

2) Configure environment variables

Create a `.env` (from `.env.example`) with:

```
DATABASE_URL=postgres://username:password@hostname:5432/commissionSpammer
DISCORD_TOKEN=your_discord_bot_token
# Or alternatively, you can use:
# DISCORD_BOT_TOKEN=your_discord_bot_token

# Optional:
# LOG_LEVEL=INFO   # DEBUG, INFO, WARNING, ERROR, CRITICAL

# Optional: how often (in hours) the background job re-enforces the ban list across all
# enabled+configured servers. Defaults to 24 (once a day).
# ENFORCE_INTERVAL_HOURS=24

# Optional: sync slash commands to a single guild instead of globally, for near-instant
# propagation while developing (a global sync can take up to ~1hr to show up everywhere).
# Leave unset in production.
# DEV_GUILD_ID=123456789012345678

# Optional, required only for /banner report:
# REVIEW_CHANNEL_ID=123456789012345678   # channel where reports are posted for review
# REVIEW_ROLE_ID=123456789012345678      # role (in that channel's server) allowed to approve/reject

# Optional: per-reporter rate limit on /banner report submissions. Defaults to 5 reports
# per reporter per 24 hours.
# REPORT_RATE_LIMIT_MAX=5
# REPORT_RATE_LIMIT_WINDOW_HOURS=24
```

A ready-to-copy template lives at `.env.example`.

3) Database tables

On startup the bot creates tables if missing:

```
public.users(discord_id BIGINT PRIMARY KEY)
public.servers(server_id BIGINT PRIMARY KEY,
               owner_id BIGINT NOT NULL,
               info_channel_id BIGINT,
               enabler BOOLEAN NOT NULL DEFAULT FALSE)
```

Insert spammer IDs (Discord snowflakes) into `public.users.discord_id`.

4) Run the bot

```powershell
python .\bot.py
```

## Usage (Slash Commands)

- `/banner set-channel <#channel>` — set the info channel where updates are posted
- `/banner enable <true|false>` — toggle automatic enforcement for this server
- `/banner status` — show current settings
- `/banner sync-now` — trigger a one-time scan for this server; unlike the periodic background
  job, this also reconciles against Discord's actual live ban list (catching e.g. manual
  unbans), not just the bot's local record of who it's already banned
- `/banner report <user_id> <evidence>` — report a suspected scammer's numeric Discord user ID,
  with a screenshot (image file, max 8 MB) as evidence. Posts to a global review channel (set via
  `REVIEW_CHANNEL_ID`) where anyone holding `REVIEW_ROLE_ID` in that server can Approve (adds the
  ID to the ban list) or Reject via buttons on the report message. Requires
  `REVIEW_CHANNEL_ID`/`REVIEW_ROLE_ID` to be configured; otherwise the command replies that
  reporting isn't set up yet.
- `/banner report-cancel <report_id>` — cancel a stuck pending report (e.g. its review message
  was deleted). Restricted to members holding `REVIEW_ROLE_ID`, same as the Approve/Reject
  buttons.
- `/banner history <user_id>` — show every report ever filed against a user ID (not just the
  current pending one), with status, reporter, and reviewer. Restricted to `REVIEW_ROLE_ID`
  holders, same as the buttons and `report-cancel`.
- `/banner unban <user_id>` — reverse a ban-list decision: removes the ID from the shared ban
  list and unbans them in every server the bot is currently in. Restricted to `REVIEW_ROLE_ID`
  holders. The original report stays visible in `/banner history` as an audit trail.

The original reporter is DMed (best-effort) when their report is approved or rejected.
`/banner report` submissions are rate-limited per reporter (`REPORT_RATE_LIMIT_MAX` per
`REPORT_RATE_LIMIT_WINDOW_HOURS`, default 5 per 24h) to prevent the review queue from being
spammed.

Notes:
- The bot needs the “Ban Members” permission.
- Without the privileged Members intent, the bot does not receive member join events; instead it proactively bans by ID so listed users can’t join. If you want immediate actions on actual join events, you must enable the Server Members intent and we can add an `on_member_join` handler.

## Permissions
- The bot’s role must have the “Ban Members” permission.
- The bot’s highest role must be above any role assigned to users it needs to ban (Discord role hierarchy applies).

## Data model (Postgres)
- `public.users`
  - `discord_id BIGINT PRIMARY KEY` — the global list of user IDs to ban
- `public.servers`
  - `server_id BIGINT PRIMARY KEY`
  - `owner_id BIGINT NOT NULL`
  - `info_channel_id BIGINT` — where updates are posted
  - `enabler BOOLEAN NOT NULL DEFAULT FALSE` — whether enforcement runs for this server
- `public.reports` — the `/banner report` review queue (pending/approved/rejected); see the
  `report`/`report-cancel` commands above
- `public.enforced_bans`
  - `server_id BIGINT`, `discord_id BIGINT` (composite primary key) — local record of which
    users this bot has already confirmed banned in which server, so the periodic enforcement
    job can compute what still needs banning without re-downloading each server's entire live
    ban list from Discord every cycle. `/banner sync-now` reconciles this against Discord's
    live ban list (see below); the periodic job trusts it as-is.

## Testing & CI
- Unit tests live in `tests/` (`python -m unittest discover -s tests -t . -v`, run from the repo
  root). No live Discord or Postgres connection is needed — `tests/__init__.py` sets dummy
  `DATABASE_URL`/`DISCORD_TOKEN` before importing `bot.py`, and all DB/Discord calls in the suite
  are mocked. The `-t .` (top-level directory) flag matters: without it, Python imports test
  modules as bare `test_xxx` instead of `tests.test_xxx`, which skips `tests/__init__.py`
  entirely and falls through to whatever a local `.env` happens to contain instead of the
  hermetic dummy values.
- `.github/workflows/ci.yml` runs `python -m py_compile bot.py` and the full test suite on every
  push to `main` and on every pull request.

## Troubleshooting
- Ensure your `DATABASE_URL` includes any required `sslmode` (e.g., `sslmode=require`) if your provider mandates SSL.
- If commands don’t appear immediately, allow a minute for global command sync to propagate, or invite using a guild-specific command sync if needed.
- If bans fail with Forbidden, grant “Ban Members” and check the bot’s role is above target users’ roles.

## License
MIT