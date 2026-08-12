"""
Persistent button view attached to each event announcement message,
letting members RSVP Yes / No. Also holds the reminder presets shared
between the create_event command (bot.py) and the embed renderer here.
"""

import datetime as _dt

import discord
import db

# Preset reminder lead times, in minutes-before-the-event. Order matters —
# it's the order shown in Discord's slash command choice picker.
REMINDER_PRESETS: dict[int, str] = {
    1440: "1 day before",
    180: "3 hours before",
    60: "1 hour before",
    30: "30 minutes before",
}


def format_reminder_minutes(minutes: int) -> str:
    if minutes in REMINDER_PRESETS:
        return REMINDER_PRESETS[minutes]
    if minutes % 1440 == 0:
        days = minutes // 1440
        return f"{days} day{'s' if days != 1 else ''} before"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour{'s' if hours != 1 else ''} before"
    return f"{minutes} minutes before"


def build_event_embed(
    event: dict, yes_count: int, no_count: int, reminder_minutes_list: list[int] = None
) -> discord.Embed:
    event_time = event["event_time_utc"]
    # event_time_utc is stored as ISO string; format as a Discord timestamp
    dt = _dt.datetime.fromisoformat(event_time)
    unix_ts = int(dt.timestamp())

    embed = discord.Embed(
        title=f"📅 {event['name']}",
        description=event["description"] or None,
        color=discord.Color.blurple(),
    )
    embed.add_field(name="When", value=f"<t:{unix_ts}:F> (<t:{unix_ts}:R>)", inline=False)
    if event["coordinates"]:
        embed.add_field(name="📍 Coordinates", value=event["coordinates"], inline=False)
    embed.add_field(name="✅ Going", value=str(yes_count), inline=True)
    embed.add_field(name="❌ Not going", value=str(no_count), inline=True)
    if reminder_minutes_list:
        labels = ", ".join(
            format_reminder_minutes(m) for m in sorted(reminder_minutes_list, reverse=True)
        )
        embed.add_field(name="⏰ Reminders", value=labels, inline=False)
    embed.set_footer(text=f"Event ID: {event['id']} • React with a button below to RSVP")
    if event["image_url"]:
        embed.set_image(url=event["image_url"])
    return embed


class RSVPView(discord.ui.View):
    """A view bound to a single event_id. Registered persistently (timeout=None)."""

    def __init__(self, event_id: int):
        super().__init__(timeout=None)
        self.event_id = event_id
        # Give buttons stable custom_ids so they survive bot restarts.
        self.yes_button.custom_id = f"rsvp_yes:{event_id}"
        self.no_button.custom_id = f"rsvp_no:{event_id}"

    async def _handle_rsvp(self, interaction: discord.Interaction, response: str):
        await db.add_response(self.event_id, interaction.user.id, response)
        event = await db.get_event(self.event_id)
        responses = await db.get_responses(self.event_id)
        reminders = await db.get_reminders_for_event(self.event_id)
        embed = build_event_embed(
            event,
            len(responses["yes"]),
            len(responses["no"]),
            [r["minutes_before"] for r in reminders],
        )
        await interaction.response.edit_message(embed=embed, view=self)
        label = "attending" if response == "yes" else "not attending"
        await interaction.followup.send(
            f"You're marked as **{label}** for **{event['name']}**.", ephemeral=True
        )

    @discord.ui.button(label="Yes, I'm going", style=discord.ButtonStyle.success, custom_id="rsvp_yes")
    async def yes_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_rsvp(interaction, "yes")

    @discord.ui.button(label="Can't make it", style=discord.ButtonStyle.danger, custom_id="rsvp_no")
    async def no_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_rsvp(interaction, "no")
