# Event Calendar Discord Bot

A Discord bot for scheduling events with RSVP tracking, screenshot attachments,
and automatic reminder pings before the event starts.

## Features

- **`/create_event`** — create an event with a name, date, time, optional
  description, optional coordinates (e.g. `K:827 X:1188 Y:762` for games
  with a coordinate system), and an optional screenshot/image attachment.
  Posts a card with **Yes / No** RSVP buttons.
- Members click the buttons to RSVP; the card's Going/Not Going counts
  update live.
- **`/responses`** — list everyone who said yes and everyone who said no
  for a given event.
- **`/list_events`** — list all upcoming events in the server with their IDs.
- **`/cancel_event`** — cancel an event (creator or admin only).
- **Automatic reminders** — each event has its own reminder timer
  (`remind_before_minutes`, default 60). When that many minutes remain
  before the event starts, the bot pings everyone who RSVP'd yes.
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
   Slash commands sync automatically on startup (may take up to an hour to
   appear globally the very first time; per-server sync via `guild=` can be
   added in `bot.py` if you want instant testing in one server).

## Usage

```
/create_event name:"Movie Night" date:2026-08-20 time:19:00 
              description:"Bring snacks" coordinates:"K:827 X:1188 Y:762"
              remind_before_minutes:30 screenshot:<attach image>
```
Posts an event card with RSVP buttons in the current channel (or the
`channel` you specify).

```
/responses event_id:3
```
Shows everyone who's going and everyone who declined for event #3.

```
/list_events
```
Shows all upcoming events with their IDs.

```
/cancel_event event_id:3
```
Cancels event #3 (only the creator or a member with **Manage Server**
permission can do this).

## How reminders work

Each event stores its own `event_time`, and `remind_before_minutes`
(how long before the event to notify). A background task checks every
60 seconds for events whose reminder window has arrived; when it fires,
the bot sends a message in the event's channel pinging every user who
clicked **Yes**. Each event only reminds once.

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
