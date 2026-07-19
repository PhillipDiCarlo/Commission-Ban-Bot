"""
Test package bootstrap.

bot.py reads DATABASE_URL/DISCORD_TOKEN at import time and raises if either is
missing. It also calls load_dotenv(), which by default won't override
already-set environment variables. So we set hermetic dummy values here,
before any test module (or bot.py itself) gets imported by the discovery
loader, to keep the suite independent of whatever a local .env does or
doesn't contain.

Nothing in this suite should actually open a DB connection or hit Discord —
DB helpers and Discord objects are mocked in the tests that need them.

Values are forced (not setdefault) so the suite is hermetic even if a real
DATABASE_URL/DISCORD_TOKEN happens to be exported in the shell already —
tests must never be able to touch real credentials.
"""
import os

os.environ["DATABASE_URL"] = "postgresql://test:test@localhost/test"
os.environ["DISCORD_TOKEN"] = "test.dummy.token"
