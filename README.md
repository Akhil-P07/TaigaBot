# 🐯 TaigaBot

A Discord bot for the **RIT AI Club**: university-email verification, channel
gating, auto-moderation, leveling, reaction roles, a project system, an AI
assistant, automatic backups, and a tsundere personality.

Every feature is a self-contained module in [`features/`](features/) that the bot
auto-loads on startup, so you add or remove features just by adding/deleting
files there.

**Multi-guild:** slash commands sync globally, so the bot works in **every server
it's invited to**. Per-server data (warnings, banned words, automod settings,
reaction roles, projects) is keyed by guild. Two exceptions are global — shared
across every server: a member's **XP / level** (so their rank follows them
everywhere) and their verification status (see the verification note below).

> **Cross-server repeat-offender marker:** warnings stay per-server, but `/warnings`
> and the spam auto-warn alert show Eboard a privacy-preserving **count** of how many
> *other* TaigaBot servers have also warned that user (a number only — no names or
> details), so a repeat offender across clubs is visible without exposing another
> server's moderation history.

---

## Features

| Feature | File | Commands |
|---|---|---|
| **Setup** | `features/setup.py` | `/setup` (owner/admin), `/health` (Eboard) |
| **Verification** (RIT email OTP) | `features/verification.py` | `/verify`, `/confirm`, `/recover`, `/whois` (Eboard), `/unverify` (Eboard) |
| **Auto-moderation** | `features/moderation.py` | `/automod enable\|disable\|status\|addword\|removeword` (filters: words, invites, spam, mentions, caps, **phishing**), `/kick`, `/ban`, `/timeout`, `/warn`, `/warnings`, `/clearwarnings`, `/purge` (Eboard) |
| **Welcome / onboarding** | `features/welcome.py` | auto-DM on join, `/verifyhelp` |
| **Projects** | `features/projects.py` | `/createproject`, `/editproject`, `/dropproject` (Eboard), `/joinproject`, `/leaveproject`, `/projects`, `/projecttags` |
| **AI assistant** | `features/ask.py` | `/ask` (Gemini) |
| **AI/ML resources** | `features/resources.py` | `/paper`, `/resource`, `/aiterm` |
| **Leveling / XP** | `features/leveling.py` | `/rank`, `/leaderboard` |
| **Reaction roles** | `features/reactionroles.py` | `/reactionrole post\|add\|remove\|list` (Eboard) |
| **Backups** | `features/backup.py` | `/backup` (Eboard) |
| **Tsundere personality** | `features/personality.py` | `/taiga`, `/hello` |
| **Help** | `features/help.py` | `/help` |

Moderation/admin commands require the **Eboard** role (server admins always pass).
`/help` is open to everyone, but shows the full Eboard reference only to
Eboard/admins and the member list to everyone else.

---

## Setup

### 1. Install Python deps
```powershell
py -m pip install -r requirements.txt
```

### 2. Create the Discord application
1. <https://discord.com/developers/applications> → **New Application**.
2. **Bot** tab → **Reset Token** → copy into `DISCORD_TOKEN`.
3. Under **Privileged Gateway Intents**, enable **SERVER MEMBERS INTENT** and
   **MESSAGE CONTENT INTENT** (both required).
4. To run on servers other than your own, enable **Public Bot**. (Discord requires
   app verification past **100 servers** to keep the privileged intents.)
5. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; bot
   permissions: *Manage Roles, Manage Channels, **View Channels**, **Use
   Application Commands**, Kick, Ban, Moderate Members, Manage Messages, Send
   Messages, Add Reactions, Embed Links, Read Message History*. Open/share the URL
   to invite the bot.

   > **View Channels** and **Use Application Commands** are easy to miss but
   > **required** — Discord only lets a bot edit a channel's `view_channel` /
   > `use_application_commands` overwrites if the bot *holds* those permissions.
   > Without them, `/setup` can't gate channels ("Missing Access").

### 3. Brevo for OTP emails
OTP codes are sent via **Brevo's HTTP API** (port 443) rather than SMTP, since
hosts like **Railway/Render block outbound SMTP**. Create a free
[Brevo](https://app.brevo.com) account, then:
1. **Senders & IPs** → add and verify your sender email (click the link in the
   confirmation email — **no domain required**).
2. **SMTP & API → API Keys** → create a key.

Put the key in `BREVO_API_KEY` and the verified address in `EMAIL_FROM`. (Brevo's
free tier is 300 emails/day.)

### 4. Configure
```powershell
copy .env.example .env
```
Edit `.env` (token, Brevo). Everything else has sensible defaults. Optional:
`GEMINI_API_KEY` to enable `/ask` (free key at
<https://aistudio.google.com/apikey>). `GUILD_ID` gives one server instant command
updates while developing; leave blank in production.

### 5. Run
```powershell
py bot.py
```

### 6. In Discord, as the server owner or an administrator, run `/setup` once.
`/setup` can only be run by the **server owner or an administrator** (not all
Eboard members), since it reshapes the whole server. It's interactive: it shows a
menu to **exclude** any categories/channels from gating and a toggle for the
**role reset** (both below), then runs.

It creates the roles (`Unverified`, `Verified`, `Eboard`) and channels
(`#unverified`, `#welcome`, `#mod-log`, `#taiga-backups`, `#roles`), then **gates
every channel behind the `Verified` role** (default-deny): `@everyone` is denied
view; only `Verified`/`Eboard` can see them. `#unverified` is the verification
landing, `#welcome` is a public read-only channel anyone can `/verify` from, and
`#roles` is where verified members self-assign roles. Finally it assigns
`Unverified` to every member who isn't verified yet.

Access is driven by *having* `Verified`, so a member with no roles (e.g. someone
who joined while the bot was asleep) sees nothing until they verify. `/setup` is
idempotent — re-run any time.

> **Important:** drag **TaigaBot's role above** `Unverified`/`Verified`/project
> roles in *Server Settings → Roles*, or it can't manage them. `/setup` checks
> its own permissions first and reports which channels it gated or skipped.
>
> **Already-private channels** (those that already deny `@everyone` view) are
> invisible to the bot and can't be gated — they show under "Couldn't edit …".
> Grant TaigaBot **View Channel** on them, or run `/setup` once with the bot
> temporarily set to Administrator, then re-run.

#### Excluding channels/categories from gating
By default every channel is gated to `Verified`. To keep a category gated by
**project/interest roles** instead, exclude it: pick it in the `/setup` menu, or
list its ID in `GATING_IGNORE` (a category ID skips all channels inside). Excluded
channels are never touched, even on re-runs. The **Projects** system (below)
relies on this.

#### Fresh-start role reset (destructive)
If you're adding TaigaBot to an existing server where members already hold roles
that grant channel access, gating alone won't lock them out (Discord lets a role's
"allow view" beat the `@everyone` deny). Toggle **Role reset** in the `/setup`
menu (or set `RESET_ROLES_ON_SETUP=1`) to first **remove every member's roles**
(except `Eboard`/`Verified`/`Unverified`, bot-managed roles, and roles above
TaigaBot) so nobody keeps old access until they re-verify and re-pick in `#roles`.

> ⚠️ Irreversible, and runs on every `/setup`. Use once, then leave it off.

---

## How members verify

1. `/verify name:Jane Doe email:jdoe@rit.edu`
2. TaigaBot emails a 6-digit code (the email names the server it was requested from).
3. `/confirm code:123456` → role swaps to **Verified**, the server unlocks, and
   their Discord username, real name, and email are saved.

One RIT account = one membership: `jdoe@rit.edu` and `jdoe@g.rit.edu` are treated
as the same person (matched on the part before the `@`). A member who verified on
one server the bot is in is auto-granted `Verified` when joining another — no
re-verification needed. Eboard can `/whois @member` or `/unverify @member`.

**Lost your Discord account?** Run `/recover email:<your RIT email>` on the new
account and confirm the emailed code. This **moves** your verification to the new
account and **automatically removes the Verified role from the old account across
every server** — no Eboard action needed. It's a *move*, not a copy, so only one
account per RIT email is ever verified (no alt stacking), and it's rate-limited to
once per identity per **7 days** (`RECOVERY_COOLDOWN_DAYS`) and logged to
`#mod-log`.

> **Multi-guild note:** verification settings are *global* — the allowed email
> domains and the sending address come from `.env` and apply to every server the
> bot is in. It's built for a single university club. Everything else is per-server.

---

## Projects

A lightweight project directory with self-service joining.

- **`/createproject lead:@member`** (Eboard) — pick the primary team lead (real
  member picker), then a form for name, description, and tags. Choose (or create)
  a category and optionally **add co-leads** from a member dropdown. The bot
  creates a **project role** (given to every lead immediately), a **channel**
  gated to that role, an intro message, and a DB record. Joining is via
  `/joinproject` (lead approval), so **no self-assign reaction role is created**.
  If the name matches an existing project, **no new channel is made** — you pick
  the existing role's `@` and the bot links them to that channel instead.
- **`/editproject`** (Eboard) — pick a project; a form opens **prefilled** with its
  name/description/tags. Saving updates the record, renames the role/channel if the
  name changed, and **deletes + reposts** the intro message with the new details.
- **`/projects [tag]`** — browse all projects; includes a **scrollable tag
  dropdown** to filter without typing.
- **`/projecttags`** — list every tag and how many projects use it.
- **`/joinproject [tag]`** — anyone picks a project from a dropdown and requests to
  join. **Every lead gets a DM** with Approve/Deny buttons (which survive
  restarts); any lead can decide (first to act wins). On approval the role is
  granted automatically; either way the requester is DM'd the outcome.
  Projects tagged **`open-source`** skip approval entirely — `/joinproject` grants
  the role instantly (tag match is case/space/hyphen-insensitive).
- **`/leaveproject`** — leave a project you're in; drops its role. **Project leads
  can't leave this way** (they'd orphan the project) — an Eboard member uses
  `/dropproject` instead.
- **`/dropproject`** (Eboard) — select a project to delete its channel and role.

**Recommended:** put your projects in one category and **exclude that category**
from gating (see above) so project channels stay visible only to their role
holders — i.e. a member must verify *and* hold the project role to see it.

---

## AI assistant (`/ask`)

`/ask prompt:<question>` answers via Google's **Gemini** (free tier). Set
`GEMINI_API_KEY` to enable it (blank = command reports it's not configured). Has a
per-user cooldown; if the free quota runs out it replies **"Out of Gemini
credits"**. `GEMINI_MODEL` selects the model (default `gemini-2.0-flash`).

---

## Phishing / scam detection

The `phishing` automod filter catches scam messages — fake Nitro/Steam gifts,
"free giveaway" link drops, malware `.exe`s — that a static word list misses. It
uses a small **machine-learning model trained offline** on the
[`wangyuancheng/discord-phishing-scam-clean`](https://huggingface.co/datasets/wangyuancheng/discord-phishing-scam-clean)
dataset (1,830 labelled Discord messages).

- **Runs on-device, cheap.** The trained model ships as a ~55 KB JSON of token
  weights ([`dataset/phishing_model.json`](dataset/phishing_model.json)). At
  runtime the bot just tokenises the message and sums weights — **pure Python, no
  extra dependencies, well under a megabyte of RAM**, so it's happy on a 500 MB
  Railway instance. **No message data ever leaves the bot.**
- **Tuned for precision** (~0.94 on held-out data) so real members' messages
  aren't deleted; it accepts missing some scams over false positives. On a hit it
  deletes the message, auto-warns the user, and alerts the Eboard.
- **Toggle it** like any filter: `/automod disable phishing` /
  `/automod enable phishing` (on by default). If the model file is missing the
  filter silently no-ops and `/automod status` shows a ⚠️ marker.
- **Retrain / refresh** any time (no third-party packages needed):

  ```bash
  python dataset/train_phishing_model.py
  ```

  It re-downloads the dataset, retrains, prints held-out precision/recall, and
  rewrites the JSON. The shared tokenizer lives in
  [`utils/phishing.py`](utils/phishing.py) so training and runtime never drift.

---

## Customizing

- **Tsundere lines** — [`personality.py`](personality.py); set `ENABLED = False`
  for a plain bot.
- **Email domains / role & channel names / OTP timeout / Gemini model** — `.env`.
- **Banned words** — live via `/automod addword` (per-server).
- **Spam thresholds & auto-warn** — constants at the top of
  [`features/moderation.py`](features/moderation.py) (`SPAM_*`, `AUTOWARN_*`).
  Caught spammers are auto-warned; the Eboard is DMed only once a user hits
  `SPAM_WARN_ESCALATE` total warnings (and each multiple after), so their DMs
  aren't flooded.
- **Phishing/scam filter** — see [Phishing / scam detection](#phishing--scam-detection)
  above; retrain with `python dataset/train_phishing_model.py`, tune
  `TARGET_PRECISION` in that script to trade recall for precision.
- **Resources & AI terms** — `RESOURCES` / `AI_TERMS` in
  [`features/resources.py`](features/resources.py).
- **XP tuning** — top of [`features/leveling.py`](features/leveling.py).

### Adding a feature
Create `features/myfeature.py` with an async `setup(bot)` that adds a cog —
it's auto-loaded on next start. Use `self.bot.db` for storage and
`from utils.checks import is_eboard` to gate commands.

---

## Data, privacy & backups

Data lives in `taigabot.db` (SQLite): verified members' name/email/Discord ID,
automod settings, XP, warnings, reaction-role bindings, and projects. The DB and
`.env` are git-ignored. Since you store real names and emails, only give Eboard
access to the host and the `#mod-log` / `#taiga-backups` channels.

A crash, restart, or sleep never loses data (SQLite commits every write). The real
risk is the host wiping its filesystem (e.g. a Replit rebuild). Backups guard
against that and run automatically:

- `/setup` creates a private, **Eboard-only** `#taiga-backups`.
- Every `BACKUP_INTERVAL_HOURS` (default 12) the bot uploads, **per server**, a
  `.db` snapshot of *only that server's* data plus a **roster CSV** of its current
  Verified/admin members. `/backup` does it on demand; `python backup_now.py`
  triggers it from a shell (`GID=<id> python backup_now.py` for one server).
- **Restore:** download the latest `.db` from `#taiga-backups` and put it at the
  bot's `DB_PATH`.

> ⚠️ A backup holds real names and emails — keep `#taiga-backups` Eboard-only.
> The file is plain (unencrypted) SQLite. `BACKUP_CHANNEL_ID` optionally overrides
> the destination by ID.

## Project layout
```
TaigaBot/
├─ bot.py              # entry point; auto-loads features/
├─ config.py           # reads .env
├─ database.py         # async SQLite layer (bot.db)
├─ personality.py      # ✏️ editable tsundere lines
├─ keep_alive.py       # tiny HTTP server for free hosts (Replit) uptime pings
├─ backup_now.py       # one-shot backup trigger for a shell
├─ requirements.txt
├─ .env.example
├─ utils/              # checks.py, emailer.py, guildutils.py
└─ features/           # one file per feature (auto-loaded)
```
