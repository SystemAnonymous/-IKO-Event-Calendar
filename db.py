"""
Async SQLite persistence layer for the event calendar bot.
Handles events, RSVP responses, reminder bookkeeping (multiple reminders
per event), and per-guild settings.
"""

import os
import aiosqlite
from datetime import datetime, timezone
from typing import Optional

# On Railway, set DB_PATH to a path inside a mounted Volume (e.g. /data/events.db)
# so the database survives redeploys. Defaults to a local file for local dev.
DB_PATH = os.getenv("DB_PATH", "events.db")

MAX_REMINDERS_PER_EVENT = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    message_id      INTEGER,
    creator_id      INTEGER NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT,
    coordinates     TEXT,
    event_time_utc  TEXT NOT NULL,       -- ISO 8601, UTC
    image_url       TEXT,
    reminder_minutes INTEGER NOT NULL DEFAULT 60,  -- legacy, kept for migration only
    reminder_sent   INTEGER NOT NULL DEFAULT 0,     -- legacy, kept for migration only
    finished        INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id        INTEGER PRIMARY KEY,
    event_channel_id INTEGER
);

CREATE TABLE IF NOT EXISTS reminders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        INTEGER NOT NULL,
    minutes_before  INTEGER NOT NULL,
    sent            INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
);
"""


async def init_db(path: str = DB_PATH) -> None:
    async with aiosqlite.connect(path) as db:
        await db.executescript(SCHEMA)
        # Migrations: add columns to any events.db created before they existed.
        async with db.execute("PRAGMA table_info(events)") as cur:
            columns = [row[1] async for row in cur]
        if "coordinates" not in columns:
            await db.execute("ALTER TABLE events ADD COLUMN coordinates TEXT")
        if "finished" not in columns:
            await db.execute("ALTER TABLE events ADD COLUMN finished INTEGER NOT NULL DEFAULT 0")

        # Migrate any pre-existing single reminder (old reminder_minutes/reminder_sent
        # columns on events) into the new reminders table. Idempotent: only touches
        # events that don't already have a row in `reminders`.
        await db.execute(
            """INSERT INTO reminders (event_id, minutes_before, sent)
               SELECT e.id, e.reminder_minutes, e.reminder_sent
               FROM events e
               WHERE NOT EXISTS (SELECT 1 FROM reminders r WHERE r.event_id = e.id)"""
        )
        await db.commit()


async def set_event_channel(guild_id: int, channel_id: int, path: str = DB_PATH) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """INSERT INTO guild_settings (guild_id, event_channel_id)
               VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET event_channel_id = excluded.event_channel_id""",
            (guild_id, channel_id),
        )
        await db.commit()


async def get_event_channel(guild_id: int, path: str = DB_PATH) -> Optional[int]:
    async with aiosqlite.connect(path) as db:
        async with db.execute(
            "SELECT event_channel_id FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def create_event(
    guild_id: int,
    channel_id: int,
    creator_id: int,
    name: str,
    description: Optional[str],
    coordinates: Optional[str],
    event_time_utc: datetime,
    image_url: Optional[str],
    reminder_minutes_list: list[int],
    path: str = DB_PATH,
) -> int:
    """Creates an event and up to MAX_REMINDERS_PER_EVENT reminders for it."""
    minutes_list = sorted(set(reminder_minutes_list), reverse=True)[:MAX_REMINDERS_PER_EVENT]

    async with aiosqlite.connect(path) as db:
        cursor = await db.execute(
            """INSERT INTO events
               (guild_id, channel_id, creator_id, name, description, coordinates,
                event_time_utc, image_url, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                guild_id,
                channel_id,
                creator_id,
                name,
                description,
                coordinates,
                event_time_utc.isoformat(),
                image_url,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        event_id = cursor.lastrowid
        for minutes in minutes_list:
            await db.execute(
                "INSERT INTO reminders (event_id, minutes_before, sent) VALUES (?, ?, 0)",
                (event_id, minutes),
            )
        await db.commit()
        return event_id


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
               WHERE guild_id = ? AND event_time_utc >= ? AND finished = 0
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


async def get_user_history(guild_id: int, user_id: int, path: str = DB_PATH) -> list[aiosqlite.Row]:
    """All events in a guild a user has RSVP'd to (yes or no), most recent first."""
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT events.id, events.name, events.event_time_utc,
                      events.coordinates, responses.response, responses.responded_at
               FROM responses
               JOIN events ON events.id = responses.event_id
               WHERE events.guild_id = ? AND responses.user_id = ?
               ORDER BY events.event_time_utc DESC""",
            (guild_id, user_id),
        ) as cur:
            return await cur.fetchall()


async def get_reminders_for_event(event_id: int, path: str = DB_PATH) -> list[aiosqlite.Row]:
    """All configured reminders for an event, largest lead time first."""
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reminders WHERE event_id = ? ORDER BY minutes_before DESC",
            (event_id,),
        ) as cur:
            return await cur.fetchall()


async def get_pending_reminders(path: str = DB_PATH) -> list[aiosqlite.Row]:
    """Individual reminders whose window has arrived but haven't fired, for
    events that haven't started or been marked finished yet."""
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT
                   reminders.id AS reminder_id,
                   reminders.minutes_before AS minutes_before,
                   events.id AS event_id,
                   events.guild_id, events.channel_id, events.message_id,
                   events.creator_id, events.name, events.description,
                   events.coordinates, events.event_time_utc, events.image_url,
                   events.finished
               FROM reminders
               JOIN events ON events.id = reminders.event_id
               WHERE reminders.sent = 0 AND events.finished = 0 AND events.event_time_utc >= ?""",
            (now.isoformat(),),
        ) as cur:
            rows = await cur.fetchall()
    due = []
    for row in rows:
        event_time = datetime.fromisoformat(row["event_time_utc"])
        remind_at = event_time.timestamp() - (row["minutes_before"] * 60)
        if now.timestamp() >= remind_at:
            due.append(row)
    return due


async def mark_reminder_row_sent(reminder_id: int, path: str = DB_PATH) -> None:
    """Marks a single reminder (by its own row id) as sent."""
    async with aiosqlite.connect(path) as db:
        await db.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))
        await db.commit()


async def cancel_event_reminders(event_id: int, path: str = DB_PATH) -> int:
    """Disables all not-yet-sent reminders for an event. Returns how many were cancelled."""
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute(
            "UPDATE reminders SET sent = 1 WHERE event_id = ? AND sent = 0", (event_id,)
        )
        await db.commit()
        return cursor.rowcount


async def mark_finished(event_id: int, path: str = DB_PATH) -> None:
    """Marks an event as finished and disables all its reminders, without
    deleting it (so it still shows up in /event_history)."""
    async with aiosqlite.connect(path) as db:
        await db.execute("UPDATE events SET finished = 1 WHERE id = ?", (event_id,))
        await db.execute(
            "UPDATE reminders SET sent = 1 WHERE event_id = ? AND sent = 0", (event_id,)
        )
        await db.commit()


async def delete_event(event_id: int, path: str = DB_PATH) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute("DELETE FROM responses WHERE event_id = ?", (event_id,))
        await db.execute("DELETE FROM reminders WHERE event_id = ?", (event_id,))
        await db.execute("DELETE FROM events WHERE id = ?", (event_id,))
        await db.commit()
