"""
Discord Event Calendar Bot
- /set_event_channel - (admin) choose the channel events & reminders post in
- /event_channel  - show which channel is currently configured
- /create_event  - create an event (with optional screenshot), post RSVP buttons
- /responses     - list who said yes / no for an event
- /event_history - see a member's past accepted/declined events
- /list_events   - list upcoming events in this server
- /cancel_event  - delete an event you created (or if you're an admin)
- /event_finished - mark an event as finished and turn off its reminder
- /cancel_reminder - turn off an event's reminder without cancelling the event
Auto-reminds everyone who RSVP'd yes, N minutes before the event, on a
per-event custom timer.
Commands can be run from any channel; event cards and reminder pings
always go to the single channel configured with /set_event_channel.
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
        self._synced = False

    async def setup_hook(self):
        await db.init_db(db.DB_PATH)
        # Re-register persistent RSVP views for all events with a live message,
        # so buttons keep working after a bot restart.
        events = await db.list_all_active_events()
        for event in events:
            self.add_view(RSVPView(event["id"]), message_id=event["message_id"])
        reminder_loop.start(self)
        log.info("Setup complete. Re-registered %d event view(s).", len(events))


bot = EventBot()


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)
    # Sync commands per-guild instead of globally: guild-scoped command syncs
    # apply instantly, whereas a global sync can take up to an hour to
    # propagate to every server. Only needs to run once per process.
    if not bot._synced:
        for guild in bot.guilds:
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)

        # Remove any *global* commands registered by earlier deploys — left
        # in place, they'd show up as duplicates alongside the guild-scoped
        # commands just synced above.
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()

        bot._synced = True
        log.info("Synced commands instantly to %d guild(s) and cleared stale global commands.", len(bot.guilds))


@bot.event
async def on_guild_join(guild: discord.Guild):
    # Make sure commands are available immediately in any server the bot
    # gets added to after startup, too.
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)
    log.info("Synced commands to newly joined guild: %s (%s)", guild.name, guild.id)


# ---------------------------------------------------------------------------
# /set_event_channel
# ---------------------------------------------------------------------------
@bot.tree.command(
    name="set_event_channel",
    description="Set the channel where event cards and reminder pings get posted.",
)
@app_commands.describe(channel="The channel events and reminders should be posted in")
async def set_event_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need the **Manage Server** permission to set the event channel.",
            ephemeral=True,
        )
        return

    await db.set_event_channel(interaction.guild_id, channel.id)
    await interaction.response.send_message(
        f"Events and reminders will now be posted in {channel.mention}.\n"
        f"You can still run commands like `/create_event` from any channel.",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /event_channel
# ---------------------------------------------------------------------------
@bot.tree.command(name="event_channel", description="Show the channel currently configured for events and reminders.")
async def event_channel(interaction: discord.Interaction):
    channel_id = await db.get_event_channel(interaction.guild_id)
    if not channel_id:
        await interaction.response.send_message(
            "No event channel is set yet. An admin can set one with `/set_event_channel`.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(f"Events and reminders are posted in <#{channel_id}>.", ephemeral=True)


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

    event_channel_id = await db.get_event_channel(interaction.guild_id)
    if not event_channel_id:
        await interaction.response.send_message(
            "No event channel is set up yet. Ask an admin to run "
            "`/set_event_channel` first, then try again.",
            ephemeral=True,
        )
        return

    target_channel = bot.get_channel(event_channel_id)
    if target_channel is None:
        try:
            target_channel = await bot.fetch_channel(event_channel_id)
        except discord.HTTPException:
            await interaction.response.send_message(
                "The configured event channel no longer exists. Ask an admin to "
                "run `/set_event_channel` again to pick a new one.",
                ephemeral=True,
            )
            return

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
# /event_history
# ---------------------------------------------------------------------------
@bot.tree.command(name="event_history", description="See a member's event RSVP history (accepted / declined).")
@app_commands.describe(member="Whose history to show (defaults to you)")
async def event_history(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    history = await db.get_user_history(interaction.guild_id, target.id)

    if not history:
        await interaction.response.send_message(
            f"{target.mention} hasn't responded to any events yet.", ephemeral=True
        )
        return

    def format_entry(row):
        dt = datetime.fromisoformat(row["event_time_utc"])
        unix_ts = int(dt.timestamp())
        coord_suffix = f" — 📍 {row['coordinates']}" if row["coordinates"] else ""
        return f"**#{row['id']}** {row['name']} — <t:{unix_ts}:d>{coord_suffix}"

    accepted = [format_entry(r) for r in history if r["response"] == "yes"]
    declined = [format_entry(r) for r in history if r["response"] == "no"]

    def format_field(entries):
        if not entries:
            return "—"
        text = "\n".join(entries[:15])
        if len(entries) > 15:
            text += f"\n…and {len(entries) - 15} more"
        return text[:1024]

    embed = discord.Embed(
        title=f"Event history: {target.display_name}",
        color=discord.Color.blurple(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name=f"✅ Accepted ({len(accepted)})", value=format_field(accepted), inline=False)
    embed.add_field(name=f"❌ Declined ({len(declined)})", value=format_field(declined), inline=False)
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
            await msg.edit(
                content=f'🚫 **"{event["name"]}" has been cancelled.**', embed=None, view=None
            )
        except discord.NotFound:
            pass

    await db.delete_event(event_id)
    await interaction.response.send_message(f"Event #{event_id} cancelled.", ephemeral=True)


# ---------------------------------------------------------------------------
# /event_finished
# ---------------------------------------------------------------------------
@bot.tree.command(
    name="event_finished",
    description="Mark an event as finished and turn off its reminder.",
)
@app_commands.describe(event_id="The event ID to mark as finished")
async def event_finished(interaction: discord.Interaction, event_id: int):
    event = await db.get_event(event_id)
    if not event or event["guild_id"] != interaction.guild_id:
        await interaction.response.send_message("No event found with that ID.", ephemeral=True)
        return

    is_creator = event["creator_id"] == interaction.user.id
    is_admin = interaction.user.guild_permissions.manage_guild
    if not (is_creator or is_admin):
        await interaction.response.send_message(
            "Only the event creator or a server admin can mark this event as finished.",
            ephemeral=True,
        )
        return

    if event["finished"]:
        await interaction.response.send_message(
            f'**"{event["name"]}"** is already marked as finished.', ephemeral=True
        )
        return

    channel = bot.get_channel(event["channel_id"])
    if channel and event["message_id"]:
        try:
            msg = await channel.fetch_message(event["message_id"])
            await msg.edit(
                content=f'✅ **"{event["name"]}" has finished.**', embed=None, view=None
            )
        except discord.NotFound:
            pass

    await db.mark_finished(event_id)  # also disables the reminder
    await interaction.response.send_message(
        f'**"{event["name"]}"** (#{event_id}) marked as finished. Its reminder is now off.',
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /cancel_reminder
# ---------------------------------------------------------------------------
@bot.tree.command(
    name="cancel_reminder",
    description="Turn off the reminder ping for an event, without cancelling the event itself.",
)
@app_commands.describe(event_id="The event ID to stop the reminder for")
async def cancel_reminder(interaction: discord.Interaction, event_id: int):
    event = await db.get_event(event_id)
    if not event or event["guild_id"] != interaction.guild_id:
        await interaction.response.send_message("No event found with that ID.", ephemeral=True)
        return

    is_creator = event["creator_id"] == interaction.user.id
    is_admin = interaction.user.guild_permissions.manage_guild
    if not (is_creator or is_admin):
        await interaction.response.send_message(
            "Only the event creator or a server admin can cancel this reminder.", ephemeral=True
        )
        return

    if event["reminder_sent"]:
        await interaction.response.send_message(
            f"The reminder for **{event['name']}** has already fired (or was already cancelled) — nothing to do.",
            ephemeral=True,
        )
        return

    await db.mark_reminder_sent(event_id)  # disables it without deleting the event
    await interaction.response.send_message(
        f"Reminder for **{event['name']}** (#{event_id}) has been turned off. "
        f"The event and its RSVPs are untouched.",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Reminder background task
# ---------------------------------------------------------------------------
REMINDER_GRACE_SECONDS = 10 * 60  # if a reminder is more than 10 min overdue, skip it


@tasks.loop(seconds=60)
async def reminder_loop(bot_instance: EventBot):
    due_events = await db.get_pending_reminders()
    now = datetime.now(ZoneInfo("UTC"))

    for event in due_events:
        event_time = datetime.fromisoformat(event["event_time_utc"])
        remind_at = event_time.timestamp() - (event["reminder_minutes"] * 60)
        overdue_by = now.timestamp() - remind_at

        # The reminder window passed too long ago (e.g. the bot was down) —
        # sending it now would just be a confusing, stale ping. Mark it sent
        # without notifying anyone.
        if overdue_by > REMINDER_GRACE_SECONDS:
            await db.mark_reminder_sent(event["id"])
            log.info(
                "Skipped stale reminder for event #%s (%.0f min overdue).",
                event["id"], overdue_by / 60,
            )
            continue

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

        unix_ts = int(event_time.timestamp())
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
