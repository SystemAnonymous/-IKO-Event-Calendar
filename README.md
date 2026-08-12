# Event Calendar Discord Bot

A Discord bot for scheduling events with RSVP tracking, screenshot attachments,
and automatic reminder pings before the event starts.

## Features

- **A single dedicated event channel** — `/set_event_channel` (admin only)
  picks the one channel where event cards and reminder pings are posted.
  Everyone can keep running commands (`/create_event`, `/responses`, etc.)
  from any channel; the event card and reminders always land in that one
  configured channel, not wherever the command was typed.
- **`/create_event`** — create an event with a name, date, time, optional
  description, optional coordinates (e.g. `K:827 X:1188 Y:762` for games
  with a coordinate system), an optional screenshot/image attachment, and
  **up to 3 reminders** picked from presets: `1 day before`, `3 hours
  before`, `1 hour before`, `30 minutes before`. Posts a card with
  **Yes / No** RSVP buttons; the card also lists which reminders are set.
- Members click the buttons to RSVP; the card's Going/Not Going counts
  update live.
- **`/responses`** — list everyone who said yes and everyone who said no
  for a given event.
- **`/event_history`** — see a member's full RSVP history in the server
  (every event they accepted and every one they declined). Defaults to
  yourself; pass `member` to check someone else.
- **`/list_events`** — list all upcoming events in the server with their IDs.
- **`/cancel_event`** — cancel an event (creator or admin only). Deletes it
  from the calendar and edits the event card to **"[Event name]" has been
  cancelled.**
- **`/event_finished`** — mark an event as finished (creator or admin only).
  Edits the event card to **"[Event name]" has finished.**, removes the
  RSVP buttons, and turns off all its reminders — but keeps the event on
  record so it still shows up in `/event_history`.
- **`/cancel_reminder`** — turn off all not-yet-fired reminders for an
  event (creator or admin only), leaving the event and everyone's RSVPs
  intact.
- **Automatic reminders** — up to 3 independent reminders per event, each
  auto-pinging everyone who RSVP'd yes when its window arrives. If the
  bot was offline and a reminder is more than 10 minutes overdue by the
  time it comes back, that particular reminder is skipped rather than
  sent late/stale.
- Survives restarts: RSVP buttons and reminder state are stored in a local
  SQLite database (`events.db`), and buttons are re-registered on startup.

## Deploy this (GitHub + Railway)

The short version: push this folder to a GitHub repo, then point Railway at
that repo. Railway builds it automatically (it's a plain Python app — no
Dockerfile needed) and runs it as a background worker (no web port required).

### 1. Push to GitHub

```bash
cd discord-event-bot
git init
git add .
git commit -m "Initial commit: event calendar bot"
gh repo create discord-event-bot --private --source=. --push
```
(No `gh` CLI? Create an empty repo on github.com, then `git remote add origin <url>`
and `git push -u origin main`.)

`.env` and `events.db` are already excluded via `.gitignore` — never commit
your bot token.

### 2. Create the Railway service

1. Go to https://railway.app → **New Project** → **Deploy from GitHub repo**
   → select the repo you just pushed.
2. Railway detects Python via Nixpacks automatically and reads
   `railway.json` / `Procfile` in this repo, which set the start command to
   `python bot.py` and run it as a worker (no HTTP port needed — Railway
   won't complain about a missing port for a worker-only service).
3. Go to the service's **Variables** tab and add:
   - `DISCORD_TOKEN` — your bot token
   - `TIMEZONE` — e.g. `America/New_York`
   - `DB_PATH` — `/data/events.db` (see step 4 below)
4. **Add a Volume** so the SQLite database survives redeploys: service →
   **Settings → Volumes → New Volume**, mount path `/data`. Without this,
   Railway's filesystem is ephemeral and `events.db` (your events + RSVPs)
   would reset on every deploy.
5. Deploy. Check the **Deployments → Logs** tab for `Logged in as ...` to
   confirm it connected to Discord.

### 3. Keep it updated

Any `git push` to the branch Railway is watching triggers an automatic
redeploy — no manual steps after the first setup.

## Local Setup

1. **Create a Discord application & bot**
   - Go to https://discord.com/developers/applications → New Application.
   - Go to the **Bot** tab → Add Bot → copy the token.
   - Under **Privileged Gateway Intents**, enable **Server Members Intent**
     (needed to mention/resolve users reliably).
   - Under **OAuth2 → URL Generator**, select scopes `bot` and
     `applications.commands`, and permissions: `Send Messages`,
     `Embed Links`, `Attach Files`, `Read Message History`,
     `Mention Everyone` (only if you want reminders to be able to ping
     roles/@everyone — not required for pinging individual users).
   - Open the generated URL to invite the bot to your server.

2. **Install dependencies**
   ```bash
   python -m venv venv
   source venv/bin/activate   # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Configure environment**
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and set:
   - `DISCORD_TOKEN` — your bot token
   - `TIMEZONE` — the timezone you'll use when typing event dates/times
     (e.g. `America/New_York`, `Europe/London`, `UTC`)
   - `DB_PATH` — leave unset for local dev (defaults to `events.db` in the
     project folder); only needed on Railway, pointing at the mounted Volume

4. **Run the bot**
   ```bash
   python bot.py
   ```
   Slash commands sync automatically on startup — instantly, in every server
   the bot is already in (and automatically again whenever it joins a new
   one), so there's no waiting on Discord's slower global command
   propagation. On the very first run after this change, the bot also
   clears out any old *global* command registrations from earlier versions
   so you don't end up with duplicate entries in Discord's command list —
   this cleanup only needs to happen once and is safe to leave in permanently.

5. **Pick your event channel** (one-time, per server, admin only)
   ```
   /set_event_channel channel:#events
   ```
   Every event card and reminder ping will be posted there from now on,
   no matter which channel someone runs `/create_event` or other commands
   from. Check it any time with `/event_channel`.

## Usage

```
/set_event_channel channel:#events
```
Admin-only, one-time setup: all event cards and reminders will be posted
in `#events` from now on. Run `/event_channel` any time to see the current
setting.

```
/create_event name:"Movie Night" date:2026-08-20 time:19:00 
              description:"Bring snacks" coordinates:"K:827 X:1188 Y:762"
              reminder_1:"1 day before" reminder_2:"1 hour before"
              reminder_3:"30 minutes before" screenshot:<attach image>
```
Can be run from **any** channel — the event card always posts in the
configured event channel, not the channel the command was typed in.
`reminder_1`/`reminder_2`/`reminder_3` are each picked from a dropdown of
presets (`1 day before`, `3 hours before`, `1 hour before`,
`30 minutes before`); only `reminder_1` is required and defaults to
`1 hour before` if you don't touch it, `reminder_2` and `reminder_3` are
optional extra reminders for the same event.

```
/responses event_id:3
```
Shows everyone who's going and everyone who declined for event #3.

```
/event_history member:@Someone
```
Shows every event `@Someone` accepted and every event they declined
(omit `member` to check your own history).

```
/list_events
```
Shows all upcoming events with their IDs.

```
/cancel_event event_id:3
```
Cancels event #3 (only the creator or a member with **Manage Server**
permission can do this).

```
/event_finished event_id:3
```
Marks event #3 as finished, edits its card to `"[Event name]" has
finished.`, removes the RSVP buttons, and turns off its reminder — but
keeps it in the database so it still shows up in `/event_history`
(only the creator or a member with **Manage Server** permission can do
this).

```
/cancel_reminder event_id:3
```
Turns off every not-yet-fired reminder for event #3 — the event stays on
the calendar and everyone's RSVPs are untouched (creator or admin only).

## How reminders work

Each event can have up to **3 independent reminders**, each stored as its
own row tied to that event (picked from the presets in `/create_event`:
`1 day before`, `3 hours before`, `1 hour before`, `30 minutes before`).
A background task checks every 60 seconds for reminders whose window has
arrived; when one fires, the bot sends a message in the event's channel
pinging every user who clicked **Yes**. Each individual reminder only
fires once — the other reminders on the same event are unaffected and
still fire at their own scheduled times.

If the bot is offline when a reminder would have fired and comes back up
more than 10 minutes after that moment, that reminder is skipped instead
of firing late — nobody wants a "starts in -3 hours" ping. That 10-minute
grace window is set by `REMINDER_GRACE_SECONDS` in `bot.py` if you want to
change it.

## Project structure

```
discord-event-bot/
├── bot.py            # entry point: slash commands + reminder loop
├── db.py             # SQLite persistence (events, RSVPs)
├── views.py          # RSVP button view + event embed
├── requirements.txt
├── Procfile          # Railway/Heroku-style start command
├── railway.json       # explicit Railway build/start config
├── .python-version    # pins Python 3.12 for Railway's Nixpacks builder
├── .env.example
└── .gitignore
```

## Notes / things you can extend

- Currently reminders only fire once per event. You could add multiple
  reminder stages (e.g. 1 day before + 1 hour before) by storing a list
  of reminder offsets instead of a single value.
- "No response" members aren't tracked/listed (Discord doesn't expose a
  clean way to know who's "seen" the card), only explicit Yes/No clicks.
- Times are entered in the `TIMEZONE` set in `.env` and displayed to
  users as Discord's native localized timestamps (`<t:...>`), so every
  member sees the event time correctly converted to their own timezone.
