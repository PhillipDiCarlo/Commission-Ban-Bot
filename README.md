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
  - every 15 minutes in the background (if enabled)
- Skips any server that hasn’t set an info channel

## How it works
- Pre-ban by user ID: the bot bans by Discord snowflake via REST. This does not require the Server Members privileged intent.
  - If the user is already in the server, the ban applies immediately (they are removed right away).
  - If the user is not in the server, Discord stores the ban; any future join attempt is blocked automatically.
- Message deletion: bans currently use `delete_message_seconds=0` (no message purge). You can change this value in `bot.py` to delete up to 7 days (604800 seconds) of messages per Discord API limits.

## Project Structure
```
Commission-Ban-Bot/
├── bot.py               # Main bot (single file)
├── requirements.txt     # Dependencies (discord.py 2.x)
├── .env.example         # Template for environment variables
└── README.md            # This file
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
```

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
- `/banner sync-now` — trigger a one-time scan for this server

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

## Troubleshooting
- Ensure your `DATABASE_URL` includes any required `sslmode` (e.g., `sslmode=require`) if your provider mandates SSL.
- If commands don’t appear immediately, allow a minute for global command sync to propagate, or invite using a guild-specific command sync if needed.
- If bans fail with Forbidden, grant “Ban Members” and check the bot’s role is above target users’ roles.

## License
MIT