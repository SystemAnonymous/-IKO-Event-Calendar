"""
Discord Event Calendar Bot
- /create_event  - create an event (with optional screenshot), post RSVP buttons
- /responses     - list who said yes / no for an event
- /list_events   - list upcoming events in this server
- /cancel_event  - delete an event you created (or if you're an admin)
Auto-reminds everyone who RSVP'd yes, N minutes before the event, on a
per-event custom timer.
"""

import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import db
from views import RSVPView, build_event_embed

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
TIMEZONE = os.getenv("TIMEZONE", "UTC")  # e.g. "America/New_York"
LOCAL_TZ = ZoneInfo(TIMEZONE)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("event-bot")

intents = discord.Intents.default()
intents.members = True  # needed to resolve/mention users reliably


class EventBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await db.init_db(db.DB_PATH)
        # Re-register persistent RSVP views for all events with a live message,
        # so buttons keep working after a bot restart.
        events = await db.list_all_active_events()
        for event in events:
            self.add_view(RSVPView(event["id"]), message_id=event["message_id"])
        await self.tree.sync()
        reminder_loop.start(self)
        log.info("Setup complete. Re-registered %d event view(s).", len(events))


bot = EventBot()


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)


# ---------------------------------------------------------------------------
# /create_event
# ---------------------------------------------------------------------------
@bot.tree.command(name="create_event", description="Create a new event with RSVP tracking.")
@app_commands.describe(
    name="Event name",
    date="Date, format YYYY-MM-DD",
    time="Time, 24h format HH:MM (server timezone)",
    description="Optional event description",
    coordinates="Optional coordinates, e.g. K:827 X:1188 Y:762",
    remind_before_minutes="How many minutes before the event to ping everyone who said yes (default 60)",
    screenshot="Optional image/screenshot to attach to the event",
    channel="Channel to post the event in (default: this channel)",
)
async def create_event(
    interaction: discord.Interaction,
    name: str,
    date: str,
    time: str,
    description: str = None,
    coordinates: str = None,
    remind_before_minutes: int = 60,
    screenshot: discord.Attachment = None,
    channel: discord.TextChannel = None,
):
    try:
        naive_dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except ValueError:
        await interaction.response.send_message(
            "Couldn't parse that date/time. Use `YYYY-MM-DD` and `HH:MM` (24h), e.g. `2026-08-20` and `18:30`.",
            ephemeral=True,
        )
        return

    local_dt = naive_dt.replace(tzinfo=LOCAL_TZ)
    utc_dt = local_dt.astimezone(ZoneInfo("UTC"))

    if utc_dt.timestamp() < datetime.now(ZoneInfo("UTC")).timestamp():
        await interaction.response.send_message(
            "That date/time is in the past. Pick a future date/time.", ephemeral=True
        )
        return

    if remind_before_minutes < 0:
        await interaction.response.send_message(
            "remind_before_minutes must be 0 or greater.", ephemeral=True
        )
        return

    target_channel = channel or interaction.channel
    image_url = screenshot.url if screenshot else None

    event_id = await db.create_event(
        guild_id=interaction.guild_id,
        channel_id=target_channel.id,
        creator_id=interaction.user.id,
        name=name,
        description=description,
        coordinates=coordinates,
        event_time_utc=utc_dt,
        image_url=image_url,
        reminder_minutes=remind_before_minutes,
    )

    event_row = await db.get_event(event_id)
    embed = build_event_embed(event_row, 0, 0)
    view = RSVPView(event_id)

    await interaction.response.send_message(
        f"Event created in {target_channel.mention}!", ephemeral=True
    )
    message = await target_channel.send(embed=embed, view=view)
    await db.set_message_id(event_id, message.id)


# ---------------------------------------------------------------------------
# /responses
# ---------------------------------------------------------------------------
@bot.tree.command(name="responses", description="See who said yes / no for an event.")
@app_commands.describe(event_id="The event ID (shown on the event card / from /list_events)")
async def responses(interaction: discord.Interaction, event_id: int):
    event = await db.get_event(event_id)
    if not event or event["guild_id"] != interaction.guild_id:
        await interaction.response.send_message("No event found with that ID.", ephemeral=True)
        return

    resp = await db.get_responses(event_id)

    def format_list(user_ids):
        if not user_ids:
            return "—"
        return "\n".join(f"<@{uid}>" for uid in user_ids)

    embed = discord.Embed(
        title=f"Responses for: {event['name']}",
        color=discord.Color.blurple(),
    )
    embed.add_field(name=f"✅ Going ({len(resp['yes'])})", value=format_list(resp["yes"]), inline=True)
    embed.add_field(name=f"❌ Not going ({len(resp['no'])})", value=format_list(resp["no"]), inline=True)
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# /list_events
# ---------------------------------------------------------------------------
@bot.tree.command(name="list_events", description="List upcoming events in this server.")
async def list_events(interaction: discord.Interaction):
    events = await db.list_upcoming_events(interaction.guild_id)
    if not events:
        await interaction.response.send_message("No upcoming events.", ephemeral=True)
        return

    lines = []
    for event in events:
        dt = datetime.fromisoformat(event["event_time_utc"])
        unix_ts = int(dt.timestamp())
        coord_suffix = f" — 📍 {event['coordinates']}" if event["coordinates"] else ""
        lines.append(f"**#{event['id']}** — {event['name']} — <t:{unix_ts}:F>{coord_suffix}")

    embed = discord.Embed(
        title="Upcoming Events",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# /cancel_event
# ---------------------------------------------------------------------------
@bot.tree.command(name="cancel_event", description="Cancel/delete an event you created.")
@app_commands.describe(event_id="The event ID to cancel")
async def cancel_event(interaction: discord.Interaction, event_id: int):
    event = await db.get_event(event_id)
    if not event or event["guild_id"] != interaction.guild_id:
        await interaction.response.send_message("No event found with that ID.", ephemeral=True)
        return

    is_creator = event["creator_id"] == interaction.user.id
    is_admin = interaction.user.guild_permissions.manage_guild
    if not (is_creator or is_admin):
        await interaction.response.send_message(
            "Only the event creator or a server admin can cancel this event.", ephemeral=True
        )
        return

    channel = bot.get_channel(event["channel_id"])
    if channel and event["message_id"]:
        try:
            msg = await channel.fetch_message(event["message_id"])
            await msg.edit(content="🚫 **This event has been cancelled.**", embed=None, view=None)
        except discord.NotFound:
            pass

    await db.delete_event(event_id)
    await interaction.response.send_message(f"Event #{event_id} cancelled.", ephemeral=True)


# ---------------------------------------------------------------------------
# Reminder background task
# ---------------------------------------------------------------------------
@tasks.loop(seconds=60)
async def reminder_loop(bot_instance: EventBot):
    due_events = await db.get_pending_reminders()
    for event in due_events:
        channel = bot_instance.get_channel(event["channel_id"])
        if channel is None:
            try:
                channel = await bot_instance.fetch_channel(event["channel_id"])
            except discord.HTTPException:
                await db.mark_reminder_sent(event["id"])
                continue

        resp = await db.get_responses(event["id"])
        yes_users = resp["yes"]
        await db.mark_reminder_sent(event["id"])

        if not yes_users:
            continue

        dt = datetime.fromisoformat(event["event_time_utc"])
        unix_ts = int(dt.timestamp())
        mentions = " ".join(f"<@{uid}>" for uid in yes_users)
        await channel.send(
            f"⏰ Reminder: **{event['name']}** starts <t:{unix_ts}:R>!\n{mentions}"
        )


@reminder_loop.before_loop
async def before_reminder_loop():
    await bot.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file first.")
    bot.run(TOKEN)
