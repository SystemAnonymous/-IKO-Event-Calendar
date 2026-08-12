"""
Async SQLite persistence layer for the event calendar bot.
Handles events, RSVP responses, and reminder bookkeeping.
"""

import os
import aiosqlite
from datetime import datetime, timezone
from typing import Optional

# On Railway, set DB_PATH to a path inside a mounted Volume (e.g. /data/events.db)
# so the database survives redeploys. Defaults to a local file for local dev.
DB_PATH = os.getenv("DB_PATH", "events.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    message_id      INTEGER,
    creator_id      INTEGER NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT,
    event_time_utc  TEXT NOT NULL,       -- ISO 8601, UTC
    image_url       TEXT,
    reminder_minutes INTEGER NOT NULL DEFAULT 60,
    reminder_sent   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS responses (
    event_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    response    TEXT NOT NULL CHECK (response IN ('yes', 'no')),
    responded_at TEXT NOT NULL,
    PRIMARY KEY (event_id, user_id),
    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
);
"""


async def init_db(path: str = DB_PATH) -> None:
    async with aiosqlite.connect(path) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def create_event(
    guild_id: int,
    channel_id: int,
    creator_id: int,
    name: str,
    description: Optional[str],
    event_time_utc: datetime,
    image_url: Optional[str],
    reminder_minutes: int,
    path: str = DB_PATH,
) -> int:
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute(
            """INSERT INTO events
               (guild_id, channel_id, creator_id, name, description,
                event_time_utc, image_url, reminder_minutes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                guild_id,
                channel_id,
                creator_id,
                name,
                description,
                event_time_utc.isoformat(),
                image_url,
                reminder_minutes,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def set_message_id(event_id: int, message_id: int, path: str = DB_PATH) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "UPDATE events SET message_id = ? WHERE id = ?", (message_id, event_id)
        )
        await db.commit()


async def get_event(event_id: int, path: str = DB_PATH) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM events WHERE id = ?", (event_id,)) as cur:
            return await cur.fetchone()


async def get_event_by_message(message_id: int, path: str = DB_PATH) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM events WHERE message_id = ?", (message_id,)
        ) as cur:
            return await cur.fetchone()


async def list_upcoming_events(guild_id: int, path: str = DB_PATH) -> list[aiosqlite.Row]:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM events
               WHERE guild_id = ? AND event_time_utc >= ?
               ORDER BY event_time_utc ASC""",
            (guild_id, now),
        ) as cur:
            return await cur.fetchall()


async def list_all_active_events(path: str = DB_PATH) -> list[aiosqlite.Row]:
    """All events with a posted message, regardless of time (used to re-register views)."""
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM events WHERE message_id IS NOT NULL"
        ) as cur:
            return await cur.fetchall()


async def add_response(event_id: int, user_id: int, response: str, path: str = DB_PATH) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """INSERT INTO responses (event_id, user_id, response, responded_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(event_id, user_id)
               DO UPDATE SET response = excluded.response, responded_at = excluded.responded_at""",
            (event_id, user_id, response, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def get_responses(event_id: int, path: str = DB_PATH) -> dict[str, list[int]]:
    async with aiosqlite.connect(path) as db:
        async with db.execute(
            "SELECT user_id, response FROM responses WHERE event_id = ?", (event_id,)
        ) as cur:
            rows = await cur.fetchall()
    result: dict[str, list[int]] = {"yes": [], "no": []}
    for user_id, response in rows:
        result[response].append(user_id)
    return result


async def get_pending_reminders(path: str = DB_PATH) -> list[aiosqlite.Row]:
    """Events whose reminder window has arrived but hasn't fired, and haven't started yet."""
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM events WHERE reminder_sent = 0 AND event_time_utc >= ?",
            (now.isoformat(),),
        ) as cur:
            rows = await cur.fetchall()
    due = []
    for row in rows:
        event_time = datetime.fromisoformat(row["event_time_utc"])
        remind_at = event_time.timestamp() - (row["reminder_minutes"] * 60)
        if now.timestamp() >= remind_at:
            due.append(row)
    return due


async def mark_reminder_sent(event_id: int, path: str = DB_PATH) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "UPDATE events SET reminder_sent = 1 WHERE id = ?", (event_id,)
        )
        await db.commit()


async def delete_event(event_id: int, path: str = DB_PATH) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute("DELETE FROM responses WHERE event_id = ?", (event_id,))
        await db.execute("DELETE FROM events WHERE id = ?", (event_id,))
        await db.commit()
