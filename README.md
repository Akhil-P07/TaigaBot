# 🐯 TaigaBot

A Discord moderation + verification bot for the **RIT AI Club**, with a mean
personality, university-email verification, auto-moderation, leveling, AI/ML
commands, and reaction roles.

Every feature is a self-contained module in [`features/`](features/) that the bot
auto-loads on startup, so you can add or remove features just by adding/deleting
files there.

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
4. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`;
   bot permissions: *Manage Roles, Manage Channels, Kick, Ban, Moderate Members,
   Manage Messages, Read Messages/View Channels, Send Messages, Add Reactions,
   Embed Links, Read Message History*. Open the generated URL to invite the bot.

### 3. Gmail for OTP emails
1. Use/create a Gmail account for the bot and enable **2-Step Verification**.
2. Create an **App Password** at <https://myaccount.google.com/apppasswords>.
3. Put the address + 16-char app password in `.env`
   (`GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`).

### 4. Configure
```powershell
copy .env.example .env
# then edit .env (token, gmail, GUILD_ID for instant command sync)
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
(`#unverified`, `#welcome`, `#mod-log`), locks the server so unverified members
can only talk in `#unverified`, and **assigns `Unverified` to every existing
member** who isn't verified yet.

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

## Project layout
```
TaigaBot/
├─ bot.py              # entry point; auto-loads features/
├─ config.py           # reads .env
├─ database.py         # async SQLite layer (bot.db)
├─ personality.py      # ✏️ editable tsundere lines
├─ requirements.txt
├─ .env.example
├─ utils/              # checks.py, emailer.py, guildutils.py
└─ features/           # one file per feature (auto-loaded)
```
