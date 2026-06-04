# 🐯 TaigaBot

A Discord moderation + verification bot for the **RIT AI Club**, with a mean
personality, university-email verification, auto-moderation, leveling, AI/ML
commands, and reaction roles.

Every feature is a self-contained module in [`features/`](features/) that the bot
auto-loads on startup, so you can add or remove features just by adding/deleting
files there.

**Multi-guild:** slash commands sync globally, so the bot works in **every server
it's invited to**. All per-server data (XP, warnings, banned words, automod
settings, reaction roles) is keyed by guild, and roles/channels are resolved by
name within each guild. One exception — see the verification note below.

---

## Features

| Feature | File | Commands |
|---|---|---|
| **Setup** | `features/setup.py` | `/setup`, `/health` |
| **Verification** (RIT email OTP) | `features/verification.py` | `/verify`, `/confirm`, `/whois` (Eboard), `/unverify` (Eboard) |
| **Auto-moderation** | `features/moderation.py` | `/automod enable\|disable\|status\|addword\|removeword`, `/kick`, `/ban`, `/timeout`, `/warn`, `/warnings`, `/clearwarnings`, `/purge` |
| **Welcome / onboarding** | `features/welcome.py` | auto-DM on join, `/verifyhelp` |
| **Tsundere personality** | `features/personality.py` | `/taiga`, `/hello` |
| **Leveling / XP** | `features/leveling.py` | `/rank`, `/leaderboard` |
| **AI/ML resources** | `features/resources.py` | `/paper`, `/resource`, `/aiterm` |
| **Reaction roles** | `features/reactionroles.py` | `/reactionrole post\|add\|remove\|list` (Eboard) |
| **Backups** | `features/backup.py` | `/backup` (Eboard) |

All moderation/admin commands check the caller's **Eboard** role (server admins
always pass).

---

## Setup

### 1. Install Python deps
```powershell
py -m pip install -r requirements.txt
```

### 2. Create the Discord application
1. Go to <https://discord.com/developers/applications> → **New Application**.
2. **Bot** tab → **Reset Token** → copy it into `.env` as `DISCORD_TOKEN`.
3. Under **Privileged Gateway Intents**, enable **SERVER MEMBERS INTENT** and
   **MESSAGE CONTENT INTENT** (both are required).
4. To let the bot run on servers other than your own, enable **Public Bot** on the
   **Bot** tab. (Discord requires app verification once you pass **100 servers** to
   keep the privileged intents above.)
5. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`;
   bot permissions: *Manage Roles, Manage Channels, Kick, Ban, Moderate Members,
   Manage Messages, Read Messages/View Channels, Send Messages, Add Reactions,
   Embed Links, Read Message History*. Open (or share) the generated URL to invite
   the bot to any server.

### 3. Gmail for OTP emails
1. Use/create a Gmail account for the bot and enable **2-Step Verification**.
2. Create an **App Password** at <https://myaccount.google.com/apppasswords>.
3. Put the address + 16-char app password in `.env`
   (`GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`).

### 4. Configure
```powershell
copy .env.example .env
# then edit .env (token, gmail). GUILD_ID is optional — set it to a test server
# for instant command updates; leave blank in production (commands sync globally).
```

### 5. Run
```powershell
py bot.py
```

### 6. In Discord, as an admin/Eboard, run once:
```
/setup
```
This creates the roles (`Unverified`, `Verified`, `Eboard`) and channels
(`#unverified`, `#welcome`, `#mod-log`), then **gates every channel behind the
`Verified` role** (default-deny): `@everyone` is denied view, and only
`Verified`/`Eboard` can see them. `#unverified` is the verification landing, and
`#welcome` is a public, read-only channel anyone can run `/verify` from. Finally
it **assigns `Unverified` to every existing member** who isn't verified yet.

Because access is driven by *having* `Verified` (not by *lacking* `Unverified`),
a member with no roles — e.g. someone who joined while the bot was asleep — sees
nothing until they verify. `/setup` is idempotent, so re-run it any time (e.g.
after adding channels) to re-apply the gating.

> **Important:** In *Server Settings → Roles*, drag **TaigaBot's** role **above**
> the `Unverified`/`Verified` roles (and any reaction-role roles), or it can't
> manage them.

Then `/health` shows whether everything is wired up correctly.

---

## How members verify

1. `/verify name:Jane Doe email:jdoe@rit.edu`
2. TaigaBot emails a 6-digit code.
3. `/confirm code:123456` → role swaps to **Verified**, server unlocks, and their
   Discord username, real name, and email are saved to the database.

One email = one account (re-use is blocked). Eboard can `/whois @member` to see a
member's stored info or `/unverify @member` to reset them.

> **Multi-guild note:** verification is the one feature with *global* settings —
> the allowed email domains (`ALLOWED_EMAIL_DOMAINS`) and the sending Gmail account
> come from `.env` and apply to **every** server the bot is in. It's built for a
> single university club, so if you invite the bot elsewhere, those servers will
> also gatekeep on your configured domains using your Gmail account. Everything
> else (moderation, leveling, reaction roles, welcome) is fully per-server.

---

## Customizing

- **Tsundere lines** — edit [`personality.py`](personality.py). Lines are grouped
  by situation (`mention`, `verify_success`, `automod`, …). Set `ENABLED = False`
  for a plain bot.
- **Allowed email domains / role & channel names / OTP timeout** — all in `.env`.
- **Banned words** — managed live via `/automod addword` (stored per-server).
- **Resources & AI terms** — edit the `RESOURCES` and `AI_TERMS` lists in
  [`features/resources.py`](features/resources.py).
- **XP tuning** — constants at the top of [`features/leveling.py`](features/leveling.py).

### Adding a brand-new feature
Create `features/myfeature.py`:
```python
from discord.ext import commands

class MyFeature(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    # ... commands / listeners ...

async def setup(bot):
    await bot.add_cog(MyFeature(bot))
```
Restart the bot — it's loaded automatically. Use `self.bot.db` for storage and
`from utils.checks import is_eboard` to gate commands to Eboard.

---

## Data & privacy

All data is stored locally in `taigabot.db` (SQLite): verified members'
name/email/Discord ID, automod settings, XP, warnings, and reaction-role bindings.
The DB and `.env` are git-ignored. Since you collect real names and emails, only
give Eboard access to the server host and the `#mod-log` channel.

### Backups (recommended on free hosts)

A crash, restart, or sleep **never** loses data — SQLite commits every write to
disk. The one real risk is the host rebuilding its container and wiping the file
(e.g. a Replit rebuild or a Deployment). To guard against that, set
`BACKUP_CHANNEL_ID` to a **private, Eboard-only** channel: the bot uploads a
consistent snapshot of the DB there every `BACKUP_INTERVAL_HOURS` (default 12),
and Eboard can run `/backup` to snapshot on demand. To restore, download the
latest `taigabot.db` attachment and put it at the bot's `DB_PATH`.

> ⚠️ The snapshot is the full database, including real names and emails. Never
> point `BACKUP_CHANNEL_ID` at a channel non-Eboard members can read. The file
> is plain SQLite (unencrypted).

## Project layout
```
TaigaBot/
├─ bot.py              # entry point; auto-loads features/
├─ config.py           # reads .env
├─ database.py         # async SQLite layer (bot.db)
├─ personality.py      # ✏️ editable tsundere lines
├─ keep_alive.py       # tiny HTTP server for free hosts (Replit) uptime pings
├─ requirements.txt
├─ .env.example
├─ utils/              # checks.py, emailer.py, guildutils.py
└─ features/           # one file per feature (auto-loaded)
```
