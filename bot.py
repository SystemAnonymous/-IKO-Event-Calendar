"""
Discord Event Calendar Bot
- /set_event_channel - (admin) choose the channel events & reminders post in
- /event_channel  - show which channel is currently configured
- /create_event  - create an event (with optional screenshot), post RSVP buttons
- /responses     - list who said yes / no for an event
- /event_history - see a member's past accepted/declined events
- /list_events   - list upcoming events in this server
- /cancel_event  - delete an event you created (or if you're an admin)
- /event_finished - mark an event as finished and turn off its reminder(s)
- /cancel_reminder - turn off an event's reminder(s) without cancelling the event
Each event can have up to 3 reminders (presets: 1 day / 3 hours / 1 hour /
30 minutes before), each auto-pinging everyone who RSVP'd yes.
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
from views import RSVPView, build_event_embed, REMINDER_PRESETS, format_reminder_minutes

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
TIMEZONE = os.getenv("TIMEZONE", "UTC")  # e.g. "America/New_York"
LOCAL_TZ = ZoneInfo(TIMEZONE)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("event-bot")

intents = discord.Intents.default()
intents.members = True  # needed to resolve/mention users reliably

# Reused across /create_event's three reminder parameters.
REMINDER_CHOICES = [
    app_commands.Choice(name=label, value=minutes)
    for minutes, label in REMINDER_PRESETS.items()
]


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
    reminder_1="First reminder before the event (default: 1 hour before)",
    reminder_2="Optional second reminder",
    reminder_3="Optional third reminder",
    screenshot="Optional image/screenshot to attach to the event",
)
@app_commands.choices(reminder_1=REMINDER_CHOICES, reminder_2=REMINDER_CHOICES, reminder_3=REMINDER_CHOICES)
async def create_event(
    interaction: discord.Interaction,
    name: str,
    date: str,
    time: str,
    description: str = None,
    coordinates: str = None,
    reminder_1: int = 60,
    reminder_2: int = None,
    reminder_3: int = None,
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

    # Collect the chosen presets, dedupe, and drop any whose reminder time
    # has already passed relative to now (e.g. picking "1 day before" for
    # an event that's only 2 hours away).
    now_utc = datetime.now(ZoneInfo("UTC"))
    chosen = sorted(set(m for m in (reminder_1, reminder_2, reminder_3) if m is not None), reverse=True)
    valid_reminders = []
    skipped_reminders = []
    for minutes in chosen:
        remind_at = utc_dt.timestamp() - (minutes * 60)
        if remind_at > now_utc.timestamp():
            valid_reminders.append(minutes)
        else:
            skipped_reminders.append(minutes)

    event_id = await db.create_event(
        guild_id=interaction.guild_id,
        channel_id=target_channel.id,
        creator_id=interaction.user.id,
        name=name,
        description=description,
        coordinates=coordinates,
        event_time_utc=utc_dt,
        image_url=image_url,
        reminder_minutes_list=valid_reminders,
    )

    event_row = await db.get_event(event_id)
    embed = build_event_embed(event_row, 0, 0, valid_reminders)
    view = RSVPView(event_id)

    confirmation = f"Event created in {target_channel.mention}!"
    if skipped_reminders:
        skipped_labels = ", ".join(format_reminder_minutes(m) for m in skipped_reminders)
        confirmation += f"\n⚠️ Skipped reminder(s) already in the past: {skipped_labels}."
    await interaction.response.send_message(confirmation, ephemeral=True)
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
    description="Turn off the reminder(s) for an event, without cancelling the event itself.",
)
@app_commands.describe(event_id="The event ID to stop the reminder(s) for")
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

    cancelled_count = await db.cancel_event_reminders(event_id)
    if cancelled_count == 0:
        await interaction.response.send_message(
            f"There's no active reminder for **{event['name']}** to cancel "
            f"(already fired or none were set).",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Turned off {cancelled_count} reminder(s) for **{event['name']}** (#{event_id}). "
        f"The event and its RSVPs are untouched.",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Reminder background task
# ---------------------------------------------------------------------------
REMINDER_GRACE_SECONDS = 10 * 60  # if a reminder is more than 10 min overdue, skip it


@tasks.loop(seconds=60)
async def reminder_loop(bot_instance: EventBot):
    due_reminders = await db.get_pending_reminders()
    now = datetime.now(ZoneInfo("UTC"))

    for reminder in due_reminders:
        event_time = datetime.fromisoformat(reminder["event_time_utc"])
        remind_at = event_time.timestamp() - (reminder["minutes_before"] * 60)
        overdue_by = now.timestamp() - remind_at

        # This reminder's window passed too long ago (e.g. the bot was down) —
        # sending it now would just be a confusing, stale ping. Mark it sent
        # without notifying anyone.
        if overdue_by > REMINDER_GRACE_SECONDS:
            await db.mark_reminder_row_sent(reminder["reminder_id"])
            log.info(
                "Skipped stale reminder #%s for event #%s (%.0f min overdue).",
                reminder["reminder_id"], reminder["event_id"], overdue_by / 60,
            )
            continue

        channel = bot_instance.get_channel(reminder["channel_id"])
        if channel is None:
            try:
                channel = await bot_instance.fetch_channel(reminder["channel_id"])
            except discord.HTTPException:
                await db.mark_reminder_row_sent(reminder["reminder_id"])
                continue

        resp = await db.get_responses(reminder["event_id"])
        yes_users = resp["yes"]
        await db.mark_reminder_row_sent(reminder["reminder_id"])

        if not yes_users:
            continue

        unix_ts = int(event_time.timestamp())
        mentions = " ".join(f"<@{uid}>" for uid in yes_users)
        await channel.send(
            f"⏰ Reminder: **{reminder['name']}** starts <t:{unix_ts}:R>!\n{mentions}"
        )


@reminder_loop.before_loop
async def before_reminder_loop():
    await bot.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file first.")
    bot.run(TOKEN)
