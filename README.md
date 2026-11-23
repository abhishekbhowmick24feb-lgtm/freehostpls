# Marketplace Guard

A professional Discord bot in Python (discord.py 2.x) for a marketplace server:
moderation, owner tools, a **verification panel**, a **configurable prefix**,
**`say` as the bot**, and a **marketplace panel** with a heading, banner GIF,
category sections and priced items.

---

## Features

| Area | What you get |
| --- | --- |
| Moderation | ban, unban, kick, timeout, warnings (stored), purge, slowmode, lock/unlock, add/remove role, nickname — usable via a configurable **moderator role** or Discord permissions |
| Channel access | Give any member access to any private text channel, or remove their explicit access |
| Vouches | Buyer-role-gated seller vouches with 1–5 star ratings, seller profiles and a leaderboard |
| Security | Raid mode, anti-nuke audit detection, a scam link/invite filter, and a configurable security alert channel |
| Account age | Minimum account age for verification and purchase tickets, plus staff alt-risk checks |
| Modmail | Members DM the bot; staff reply from a private channel |
| Temp voice | Join a creator voice channel and get a private room that disappears when empty |
| Birthdays & AFK | Birthday announcements and mention-based AFK replies |
| Games | `/coinflip` duels with a heads/tails pick and a real random flip, plus a counting channel that enforces 1, 2, 3… one number per member per day |
| Tags | Reusable staff-authored answers such as rules, FAQs and marketplace terms |
| Giveaways | Persistent Enter/Leave buttons, timed draws, role/verified/booster/account-age requirements, bonus entries, rerolls, cancellation and a configurable channel |
| Backups | Snapshot and restore roles, channels and permission overwrites |
| Verification | One command sets the channel **and** the role; a persistent button grants the role on click |
| Tickets | Panel button opens a private channel, asks **what item** and **how many**, auto-fills the shop price, with staff **Claim** and **Close** buttons, plus a saved **transcript** posted to a staff channel on close |
| Honeypot | A trap channel with a warning panel (bot avatar thumbnail) and a live **Kicks** counter. Anyone who types there is softbanned automatically; staff and moderators are exempt |
| Whitelist | Roles and individual members who can talk in channels locked by the bot and are exempt from the honeypot |
| Logs | Choose a channel that receives an audit trail of moderation, ticket, shop, verification and bot-message activity |
| Bot status | A live panel in a channel you choose, showing 🟢 online / 🔴 offline, uptime and ping |
| Marketplace | Heading + GIF banner + thumbnail + footer, up to 8 category sections, each with its own GIF, description and colour, and items with price, stock and description. Management is limited to a configurable **shop staff role** |
| Owner | reload cogs, list servers, leave a server, sync commands, live stats, shutdown, eval |
| Utility | `say` / `embed` as the bot, help menu, prefix management, user/server info, ping, uptime |
| Storage | Everything is saved in `data/settings.json`, so settings survive restarts |

---

## 1. Create the bot application

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. Go to **Bot** → **Reset Token** and copy the token (never share it).
3. On the same **Bot** page enable these privileged intents:
   - **Message Content Intent** (required for prefix commands)
   - **Server Members Intent** (required for member lookups)
4. Invite the bot with this link (replace `YOUR_CLIENT_ID`):

```
https://discord.com/api/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=8&scope=bot%20applications.commands
```

Administrator is recommended for a marketplace server; without it the bot needs
Manage Roles, Manage Channels, Manage Messages, Moderate Members, Kick, Ban,
Send Messages, Embed Links and Read Message History.

> **Important:** in **Server Settings → Roles**, drag the bot's role **above** your
> verification role, otherwise Discord will not let it hand out the role.

---

## 2. Install and run

Python 3.10+ is required.

```bash
cd discord-bot
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env      # Windows  (macOS/Linux: cp .env.example .env)
```

Open `.env` and paste your token:

```env
DISCORD_TOKEN=your-token-here
DEFAULT_PREFIX=!
```

Start the bot:

```bash
python main.py
```

Logs are printed to the console and saved in `logs/bot.log`.

### Hosting (Render)

A Discord bot opens no HTTP port, so host it as a **Background Worker**, not a
web service:

| Setting | Value |
| --- | --- |
| Runtime | Python |
| Build command | `pip install -r requirements.txt` |
| Start command | `python main.py` |
| Environment variable | `DISCORD_TOKEN` = your bot token |
| Persistent disk | mount at `/opt/render/project/src/data`, 1 GB |

`render.yaml` in this folder describes exactly that, so **New → Blueprint**
creates the service. You still paste the token in the dashboard when prompted;
the value never enters git.

Workers and persistent disks need a paid instance type. Without a disk the bot
still runs, but the container is rebuilt on every restart, so `data/` starts
empty. That is why a free host forgets the log channel (no log entries arrive)
and the temp-voice setup (no rooms are created) while every command still works.

### Keeping settings on a free host

`settings.json` is mirrored into a DM with the bot's owner: one message holding
the file, replaced a few seconds after every save. On startup, when `data/` is
empty, the bot downloads that copy before any cog loads, so log channel,
prefixes, temp-voice setup, tickets, vouches, warnings and giveaways survive
restarts and redeploys. Nothing to configure — the message appears in the
owner's DMs, and deleting it only means the next save starts a fresh copy.

Set `STATE_BACKUP=0` in the service's environment variables to turn it off.

After deploying this, run these **once** so the copy is created:

```
!logs channel #bot-logs
/tempvoice setup creator:#Create-VC category:#Voice
```

`data/media/`, `data/transcripts/` and `data/backups/` stay local to the
container; re-upload banners or transcripts if the host has no disk.

Never commit `.env` — the repository is public.

### Bot Control panel (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File install-desktop-shortcuts.ps1
```

This puts a single **Bot Control** shortcut on your desktop. It opens a small
window (no PowerShell console) showing whether the bot is **RUNNING**,
**STARTING** or **OFFLINE**, with:

- **Start** — bring the bot online.
- **Stop** — shut the bot down completely (it stays off until you press Start).
- **Restart** — stop and start in one click.
- **Ping section** — live websocket ping in ms, uptime, server count and command
count, plus how old the last status update is.
- **Open logs** — opens the `logs` folder.

There is no auto-start: the bot never launches with Windows, only when you press
**Start**. Closing the panel leaves the bot exactly as it is. You can also check
the state without a window:

```powershell
powershell -ExecutionPolicy Bypass -File bot-control.ps1 -Status
```

Start/Stop/Restart run `start-bot.ps1` and `stop-bot.ps1`, which still work on
their own. The supervisor keeps the bot alive, waits for an internet connection
before launching so a manual start on a fresh boot does not crash-loop, backs off
when the bot exits immediately, and writes restarts to `logs/supervisor.log`. The
bot's own console output goes to `logs/bot-console.log` and
`logs/bot-console.err.log`.

The ping/uptime section reads `data/status.json`, which the bot rewrites every
10 seconds while it is running.

---

## 3. Quick start

```
!prefix set ?
?help
```

### Verification panel

```
!verification setup #verify @Verified
!verification title ✅ Verify to enter
!verification description Read the rules, then click the button below.\n\nBy verifying you accept the server rules.
!verification button Verify me
!verification image https://media.tenor.com/your-gif.gif
!verification image     (with a .gif/.png attached: uploaded and kept animated)
!verification image https://klipy.com/gifs/verify-discord-2
                        (GIF page links are downloaded and stored, so they stay animated)
!verification color #57F287
!verification refresh
```

- `setup #channel @role` posts the panel and stores which role it grants.
- Clicking **Verify** gives that role; staff cannot double-verify, bots above the
  verify role are checked automatically.
- `!verification remove` deletes the panel and its settings.

### Marketplace panel

```
!shop role @Shop Staff      # only this role (plus admins) can manage the shop
!shop channel #marketplace
!shop heading 🛒 Abhishek's Marketplace
!shop description Nitro, boosts and more.\nOpen a ticket to order.
!shop banner https://media.tenor.com/your-banner.gif
#   ^ or upload the picture/GIF with the command and skip the URL:
!shop banner            (with a .gif/.png attached)
!shop banner https://klipy.com/gifs/discord-nitro-21
                        (Klipy/Giphy/Tenor page links are downloaded and stored)
!shop sectionbanner Nitro   (with a .gif/.png attached)
!shop thumbnail https://i.imgur.com/your-logo.png
!shop footer Prices in USD • Open a ticket to buy
!shop color #5865F2

!shop addsection Nitro | 🚀 | Discord Nitro & Server Boosts | https://media.tenor.com/nitro.gif | #FF6B6B
!shop additem
#   ^ no arguments: the bot asks for section, name, price, stock and description one at a time
!shop additem Nitro | 1 Month Boost | $4.99 | In stock | Instant delivery
!shop additem Nitro | 1 Year Boost | $39.99 | 2 left | Best value
!shop additem Nitro | 14 Boost Pack | $24.99 | In stock

!shop post
```

- The **banner** is rendered above the heading, the heading above every section,
  and each section above its items — exactly like a store page.
- Sections use `name | emoji | description | banner URL | #colour`; everything
  after the name is optional.
- Items use `section | name | price | stock | description`.
- Every change automatically edits the posted panel. If nothing is posted yet,
  run `!shop post`.
- `!shop role @Shop Staff` restricts every shop command to that role. Only members
  with **Manage Server** can change the role; administrators and the server owner
  always keep access. Until a role is chosen, Manage Server holders can manage the shop.
- `!shop list` shows numbered sections/items (you can target them with numbers),
  `!shop preview` shows the panel without publishing it.

### Ticket panel

```
!ticket setup #support @Staff
!ticket category #Tickets
!ticket log #ticket-logs
!ticket title 🎟️ Buy something
!ticket description Click below to open a private ticket.\nTell us what you want and how many.
!ticket image https://media.tenor.com/your-banner.gif
!ticket image           (with a .gif/.png attached: uploaded and kept animated)
!ticket image https://klipy.com/gifs/discord-nitro-21
                        (GIF page links are downloaded and stored, so they stay animated)
!ticket button Open a ticket
```

- `ticket setup #channel [@staff]` posts the panel in the channel you choose.
- A member clicks **Open a ticket**, then types **what they are buying** and **how many**
  (plus an optional note). The bot creates a private channel only they and staff can see,
  and shows the item, quantity, and the matching shop price if the item exists.
- Staff press **Claim** to take the ticket (the button shows who claimed it) and
  **Close** to delete the channel after a 5-second notice. The opener can also close.
- `ticket category #Tickets` chooses where ticket channels are created (optional).
- When a ticket closes, the full conversation is saved to
  `data/transcripts/<guild id>/ticket-NNNN-<time>.txt` and uploaded to the channel
  set with `ticket log #channel` (falling back to the bot log channel).

### Bot logs

```
!logs channel #bot-logs
!logs test
```

- `logs channel #channel` picks where the bot records what it does (Manage Server).
- Recorded: bans, kicks, timeouts, warnings, purges, slowmode, lock/unlock, role and
  nickname changes, tickets opened/claimed/closed, shop edits and panel posts,
  verification panel changes, and messages sent with `say` / `embed`.
- `logs disable` turns logging off, `logs status` shows the current channel.

---

## Command reference

Default prefix is `!` — change it per server with `!prefix set <new>`.

### Configuration
| Command | Description |
| --- | --- |
| `prefix` | Show the current prefix |
| `prefix set <new>` | Change the prefix (Manage Server) |
| `prefix reset` | Back to the default prefix |
| `modrole <@role\|none>` | Staff role that can use every moderation command (Manage Server) |
| `help [command]` | Interactive command guide |

### Announcements
| Command | Description |
| --- | --- |
| `say [#channel] <text>` | Send a message as the bot (attachments are forwarded) |
| `embed [#channel] <title> \| <description>` | Send a rich embed as the bot |
| `dm <member> <message>` | DM a member as the bot without showing your name (moderator role or Manage Messages) |

Warnings, timeouts, kicks, bans and honeypot softbans **DM the member
automatically** with the server and reason; if their DMs are closed the action
still goes through and the failure is written to `logs/bot.log`.

### Moderation
Members need either the permission listed here **or** the role set with `!modrole @Moderator`; administrators and the server owner always pass.

| Command | Description |
| --- | --- |
| `ban <user> [reason]` | Ban a user (works for users who already left) |
| `unban <user id> [reason]` | Unban a user |
| `kick <member> [reason]` | Kick a member |
| `timeout <member> <duration> [reason]` | Timeout, e.g. `10m`, `1h30m`, `2d` (max 28d) |
| `untimeout <member> [reason]` | Remove a timeout |
| `warn <member> [reason]` | Add a stored warning and DM the member |
| `warnings [member]` | Show warning history |
| `delwarn <member> <id>` | Delete one warning |
| `clearwarns <member>` | Delete all warnings |
| `purge <amount\|all> [target]` | Delete messages, or `all` to clear the channel. Target can be `bots`, `humans`, or `@member` |
| `slowmode <duration\|off>` | Set channel slowmode |
| `lock [channel]` / `unlock [channel]` | Stop/allow members sending messages |
| `addrole <member> <role>` | Give a role |
| `removerole <member> <role>` | Take a role |
| `nick <member> [nickname]` | Change or reset a nickname |

### Channel access
| Command | Description |
| --- | --- |
| `channel add <member> [#channel]` | Give a member access to a private text channel |
| `channel remove <member> [#channel]` | Remove that member's explicit access |

Members need either the permission listed here **or** the role set with `!modrole @Moderator`.
The channel defaults to the one you run the command in. The bot needs **Manage
Channels** in the target channel. In slash form use
`/channel add member:@Member channel:#private`.

### Vouches
Set the buyer role and vouch channel once, give buyers the role after a purchase,
then let them vouch in the vouch channel.

| Command | Description |
| --- | --- |
| `vouchsetup role <@role>` | Set the role required to vouch (Manage Server) |
| `vouchsetup channel <#channel>` | Set where vouches are posted (Manage Server) |
| `vouchsetup show` | Show the current settings |
| `vouchsetup clear` | Clear the buyer role and vouch channel (existing vouches are kept) |
| `buyer add <member>` | Give the buyer role after a purchase (moderator role or Manage Roles) |
| `buyer remove <member>` | Remove the buyer role |
| `vouch <seller> [stars] [comment]` | Vouch for a seller in the vouch channel; stars 1–5, default 5 |
| `vouchprofile [member]` | Show a seller's vouch count, average rating and recent feedback |
| `vouchleaderboard` | Top sellers by vouch count and rating |

Slash examples: `/vouchsetup role role:@Buyer`, `/vouchsetup channel channel:#vouches`,
`/buyer add member:@Member`, `/vouch seller:@Seller stars:5 comment:Fast delivery`.

The buyer needs **Use Application Commands** in the vouch channel; they do **not**
need Send Messages because the bot posts the vouch. One buyer can vouch for the
same seller once every 24 hours. The seller cannot be the buyer, and the bot's
role must be above the buyer role.

### Security
**Raid mode**
| Command | Description |
| --- | --- |
| `raidmode on [reason]` | Disable invites, set verification to highest, filter all members, and alert staff (Administrator) |
| `raidmode off` | Restore the previous verification and content-filter settings |
| `raidmode status` | Show the current raid-mode state |

**Anti-nuke**
| Command | Description |
| --- | --- |
| `antinuke on\|off` | Enable or disable audit-log monitoring (Administrator) |
| `antinuke punish on\|off` | Turn automatic role removal on or off after a threshold is hit |
| `antinuke status` | Show thresholds and whether the bot can read the audit log |

Monitoring is **on by default**; auto-quarantine is **off by default**. It
detects **5 bans**, **3 channel deletes**, **3 role deletes**, or **3 webhook
creations** by the same executor within 10 seconds. Alerts go to the security
channel if one is set, otherwise the bot log channel; critical anti-nuke and
raid alerts also DM the server owner when no channel is available.
Auto-quarantine removes all removable roles from the executor and DMs them.

**Scam filter**
| Command | Description |
| --- | --- |
| `scamfilter on\|off` | Enable or disable deletion |
| `scamfilter add <domain>` | Add a custom blocked domain |
| `scamfilter remove <domain>` | Remove a custom domain |
| `scamfilter list` | List custom blocked domains |
| `scamfilter status` | Show the filter state |

The filter deletes Discord invites, Discord gift links, known IP-logger and
shortener domains, raw-IP links, and custom domains. Staff and whitelisted
members are exempt; bots and webhooks are ignored. It is **on by default**.

**Alert channel**
| Command | Description |
| --- | --- |
| `security channel <#channel>` | Send raid, anti-nuke and scam alerts to a separate channel (Manage Server) |
| `security clear` | Fall back to the bot log channel |
| `security status` | Show alert routing and protection status |

Security alerts use the dedicated channel when set; otherwise they use the bot
log channel. Normal moderation logs still use `/logs channel`, so you can point
the two at different channels.

### Account age
| Command | Description |
| --- | --- |
| `agecheck set <days>` | Set the minimum account age; `0` disables it (Manage Server) |
| `agecheck status` | Show the current rule |
| `agecheck member <member>` | Staff-only account-age and alt-risk report |

When a minimum is set, younger accounts are blocked from verification and from
opening purchase tickets. The alt report shows account age, server join age,
default avatar, auto-generated username patterns, and other clues. It is a
heuristic, not proof of an alt account.

### Modmail
| Command | Description |
| --- | --- |
| `modmail setup <#category> [@staff]` | Set the private category and staff role (Manage Server) |
| `modmail close [reason]` | Close the current modmail channel |
| `modmail reply <message>` | DM the user from the modmail channel |
| `modmail status` | Show settings and open conversations |

Members DM the bot and are told to type `?open` to open a private channel; a
DM without `?open` only gets those instructions, so no channel is created and
nothing is sent to staff. Staff reply by typing in that channel or with
`/modmail reply`. Replies are delivered as **Staff**, so the staff member's
name is not shown to the member. The bot needs **Manage Channels** in the
category, and the staff role must be below the bot's role.

A member closes their own ticket by typing `?close` in DMs (optionally with a
reason, e.g. `?close resolved it myself`). The bot asks **Are you sure?** with
buttons; confirming ends the conversation and posts a *Ticket closed by the
member* notice in the modmail channel with their reason. From there staff can
only close the channel (button) — replies to the member stop working. Staff can
still close a normal conversation with `/modmail close [reason]` or the
`/modmail close` command inside the channel.

### Temporary voice rooms
| Command | Description |
| --- | --- |
| `tempvoice setup <#create-vc> [category]` | Set the join-to-create voice channel and optional room category (Manage Server) |
| `tempvoice disable` | Stop creating new rooms; existing rooms stay |
| `tempvoice status` | Show settings and active rooms |
| `tempvoice lock` | Close your room so only you can see and join it |
| `tempvoice unlock` | Open your room so other members can see and join it |
| `tempvoice rename <name>` | Rename your room |
| `tempvoice transfer <@member>` | Give your room to a member already in it |
| `tempvoice permanent <#room> [permanent]` | Keep a room when empty; pass `permanent:false` to make it temporary again (Manage Channels) |

A member who joins the creator channel gets a private room, is moved into it,
and finds an **owner panel** posted in the room's text chat with buttons:

| Button | Effect |
| --- | --- |
| 🔒 Private | Hide the room from `@everyone` so only the owner can see and join |
| 🌐 Public | Let everyone with the category's access see and join |
| 👥 Set limit | Ask for a number (1–99) and cap how many members the room holds |
| ♾️ No limit | Remove the member cap |

The panel only works for the room's owner and keeps working after a restart.
Rooms start private; the owner can also `unlock` them for others or `lock` them
again, rename them, transfer them to a member in the room, and move members.
The owner controls work on the room the caller is currently connected to, so
they must be in their own room when using them. Empty rooms are deleted
automatically, including stale rooms left after a restart. Staff with **Manage
Channels** can mark a room permanent with `/tempvoice permanent`, so it is kept
when everyone leaves and is not swept at startup; removing the flag restores
the normal empty-room cleanup. The bot needs
**View Channel** and **Move Members** in the creator channel, plus **Manage
Channels** in the room category. Channel or category permission overwrites can
deny these even when the server-level role has them; `/tempvoice status` shows
what is missing.

### Birthdays and AFK
| Command | Description |
| --- | --- |
| `birthday set <month> <day>` | Save your birthday |
| `birthday remove` | Remove your birthday |
| `birthday channel <#channel>` | Choose the announcement channel (Manage Server) |
| `birthday upcoming` | Show the next saved birthdays |
| `birthday status` | Show your birthday and the announcement channel |
| `afk [reason]` | Set AFK; use `off` to clear |

Birthdays are checked every 30 minutes and announced once per day using the
computer's local date. People who mention an AFK member get the reason and how
long they have been away; the AFK member is welcomed back when they send a
normal message.

### Coin flips
| Command | Description |
| --- | --- |
| `/coinflip [opponent]` | Call heads or tails, then watch the coin spin and land |

Leave `opponent` empty to flip against yourself; the bot tells you whether you
win or lose. Add a member such as `/coinflip opponent:@Alex` to flip against
them: you call heads or tails and they get the other side, so the coin always
picks one winner. Only the challenger can press the Heads/Tails buttons, and the
side comes from `secrets.choice`, so the result cannot be rigged.

### Counting channel
| Command | Description |
| --- | --- |
| `/counting setup channel:#counting` | Choose the counting channel and start again from 1 (Manage Server) |
| `/counting reset` | Start the count again from 1 and clear today's members, without re-running setup (Manage Server) |
| `/counting` | Show the channel and the next expected number |

Members count upwards from **1**. A correct number gets a ✅ reaction; anything
else — letters, or a number out of order such as `5` after `2` — is deleted and
the bot posts a warning that disappears after 5 seconds. Each member may post one
number per day, so the same person cannot count twice in a row. Progress is saved
in `data/settings.json` and survives restarts.

### Tags
| Command | Description |
| --- | --- |
| `tag <name> [content]` | Show a tag; staff can provide content to create or update |
| `tagadd <name> <content>` | Staff create or update a tag |
| `tagremove <name>` | Staff remove a tag |
| `taglist` | List all tags |

Read a tag with `/tag name:rules`. Staff can use
`/tagadd name:rules content:...` or `/tag name:rules content:...`. Tag names
are lowercased with spaces converted to hyphens. Use `\n` for a new line.

### Giveaways
| Command | Description |
| --- | --- |
| `giveaway start <prize> <duration> [winners] [channel]` | Start a giveaway; staff only |
| `giveaway channel <#channel>` | Set the default giveaway channel (Manage Server) |
| `giveaway end <id>` | End now and draw winners |
| `giveaway cancel <id>` | Cancel without a winner (confirmation required) |
| `giveaway reroll <id> [winners]` | Pick new winners, excluding previous winners |
| `giveaway entries <id>` | Staff view of participants and entry counts |
| `giveaway list` | Show active and recent giveaways |

`/giveaway start` also accepts `description`, `required_role`, `verified`,
`booster`, `min_account_age`, `require_any`, `bonus_role`, `bonus_entries`,
`ping_role`, `image` (a direct image URL) and `banner` (upload the picture or
animated GIF with the slash command instead of pasting a link). Uploaded
files are stored by the bot, so animated GIFs keep moving and the banner
cannot expire.
The panel reads **GIVEAWAY**, then **Prize:**, the optional description and a
call to enter, with the entry conditions under a **Must have** field and a
footer of `Hosted by … · Winners … · Ends …`. Requirements are combined with
**all** by default; set
`require_any:true` to let a member satisfy just one. A `bonus_role` grants
`bonus_entries` extra chances. Use `required_role` for any role, or `booster`
for actual server boosters. A `verified` requirement uses the role from the
verification setup. Winners are rechecked at draw time, so a member who loses a
required role or leaves cannot win.

The giveaway message has persistent **Enter** and **Leave** buttons. Entries,
the configured channel and the giveaway state are saved in `settings.json`, so
active giveaways survive a bot restart. Giveaways end automatically every 30
seconds and due giveaways are caught up on startup.

### Backups
| Command | Description |
| --- | --- |
| `backup create [name]` | Save roles, channels and permission overwrites (Manage Server) |
| `backup list` | List saved backups |
| `backup restore <id>` | Restore a backup (Administrator; confirmation required) |

Backups are stored in `data/backups/<guild id>/`. Restore recreates missing
roles and channels, updates existing ones, and reapplies permission overwrites.
Messages are not restored. The @everyone role's server-wide permissions are
restored only when the bot's top role has Administrator; otherwise they are
skipped with a warning. The bot's own roles are left untouched to avoid locking
the bot out, and managed roles cannot be recreated.

### Verification
| Command | Description |
| --- | --- |
| `verification setup <#channel> <@role> [title]` | Post the panel and bind the role |
| `verification title <text>` | Panel title |
| `verification description <text>` | Panel text (`\n` for new lines) |
| `verification button <label>` | Button text |
| `verification image <url\|none>` | Banner GIF/image (or upload it) |
| `verification color <#hex>` | Embed colour |
| `verification refresh` | Re-render the panel |
| `verification remove` | Delete the panel |
| `verification status` | Show settings |

`verify` and `v` work as aliases of `verification`.

### Marketplace
| Command | Description |
| --- | --- |
| `shop role <@role\|none>` | Staff role that can manage the shop (Manage Server to set) |
| `shop channel <#channel>` | Where the panel is posted |
| `shop heading <title>` | Big title |
| `shop description <text>` | Text under the title (`\n` for new lines) |
| `shop banner <url\|none>` | GIF/picture at the very top (or upload it) |
| `shop thumbnail <url\|none>` | Small logo |
| `shop footer <text>` | Footer line |
| `shop color <#hex>` | Default colour |
| `shop addsection <name> \| [emoji] \| [description] \| [banner] \| [#color]` | Add a category |
| `shop renamesection <section> \| <name>` | Rename |
| `shop sectiondesc <section> \| <text>` | What type of items the section holds |
| `shop sectionbanner <section> \| <url\|none>` | Section GIF (or upload it) |
| `shop sectioncolor <section> \| <#hex>` | Section colour |
| `shop removesection <section>` | Delete a section |
| `shop additem` | Guided setup: asks for each field in turn |
| `shop additem <section> \| <name> \| <price> \| [stock] \| [description]` | Add an item in one line |
| `shop setprice <section> \| <item> \| <price>` | Change a price |
| `shop setstock <section> \| <item> \| <status>` | `In stock`, `Out of stock`, `2 left`… |
| `shop itemdesc <section> \| <item> \| <text>` | Item description |
| `shop itememoji <section> \| <item> \| <emoji>` | Emoji beside an item |
| `shop renameitem <section> \| <item> \| <name>` | Rename an item |
| `shop removeitem <section> \| <item>` | Delete an item |
| `shop list` | Numbered overview of the shop |
| `shop preview` | Preview the panel |
| `shop post [#channel]` | Publish/replace the panel |
| `shop reset` | Delete the panel and all shop content |

`market`, `marketplace` and `store` are aliases of `shop`.

> Comments: quote values that contain `|`? No — `|` is the separator, so avoid it
> inside names/descriptions. Use `\n` for a line break.

### Tickets
| Command | Description |
| --- | --- |
| `ticket setup <#channel> [@staff]` | Post the ticket panel and set the staff role |
| `ticket category <#category\|none>` | Where ticket channels are created |
| `ticket log <#channel\|none>` | Where closed-ticket transcripts are posted |
| `ticket role <@role\|none>` | Role that can see, claim and close tickets |
| `ticket title <text>` | Panel title |
| `ticket description <text>` | Panel text (`\n` for new lines) |
| `ticket image <url\|none>` | Banner GIF/image (or upload it) |
| `ticket thumbnail <url\|none>` | Panel thumbnail |
| `ticket footer <text>` | Panel footer |
| `ticket color <#hex>` | Embed colour |
| `ticket button <label>` | Panel button text |
| `ticket refresh` | Re-render the posted panel |
| `ticket close` | Close the ticket in the current channel |
| `ticket status` | Show ticket settings |

`tickets` and `support` are aliases of `ticket`.

### Honeypot
| Command | Description |
| --- | --- |
| `honeypot setup <#channel>` | Post the warning panel and arm the channel (Manage Server) |
| `honeypot invite <url\|none>` | Set or clear one permanent rejoin invite (Manage Server) |
| `honeypot disable` | Disarm it and delete the panel (counter is kept) |
| `honeypot reset` | Set the kick counter back to `0` |
| `honeypot status` | Show the current honeypot settings |

`hp` is an alias of `honeypot`.

Set one permanent invite with `honeypot invite https://discord.gg/...` (or
`/honeypot invite`). The bot checks that the invite belongs to this server and
saves it. Honeypot victims get that same invite in their removal DM instead of a
new one-use invite being created for every softban. If no invite is saved, the
bot falls back to creating a one-use invite per softban.

Anyone who sends a message in the armed channel has it deleted and is
**softbanned** — banned and instantly unbanned — and the panel's **Kicks**
counter goes up. Before the ban is applied, the member is DM'd what happened,
how to secure their account, Discord's hacked-account guide, and the saved
rejoin invite. Server owner, administrators, members with **Manage Messages**,
whitelisted roles/members and the configured moderator role are never punished. The bot needs **Manage
Messages**, **Ban Members** and **Create Invite** (for the fallback invite), and
its role must be above the member's.

### Whitelist
| Command | Description |
| --- | --- |
| `whitelist member:<@member>` | Give the permanent whitelist role to a member (Manage Server) |
| `whitelist role:<@role>` | Set the permanent whitelist role (Manage Server) |
| `whitelist` | Show the permanent role, extra roles and direct members |
| `unwhitelist member:<@member>` | Remove the permanent whitelist role from a member |
| `whitelistclear [all]` | Clear extra roles and direct members (`all` also clears the permanent role) |
| `whitelistrole <@role\|none>` | Prefix-only: set or clear the permanent whitelist role |

`wl` is an alias of `whitelist`; `wlremove` and `wlclear` are aliases of the
removal commands.

Set the permanent role once with `/whitelist role:@Whitelisted` (or the prefix
`!whitelistrole @Whitelisted`); it is saved in `data/settings.json` and survives
restarts. After that, `/whitelist member:@Member` gives that role to the member.
Members with the role can send messages in any channel the bot has locked with
`lock` (the bot adds an allow overwrite for them, and removes it on `unlock`),
and are never softbanned by the honeypot.


### Bot status
| Command | Description |
| --- | --- |
| `status channel <#channel>` | Post the live status panel there and keep it updated (Manage Server) |
| `status offline [reason]` | Show the panel as offline/maintenance with an optional reason (Manage Server) |
| `status online` | Clear maintenance mode and show the real bot status |
| `status refresh` | Update the panel immediately |
| `status show` | Show the current panel settings |
| `status disable` | Remove the panel and stop updating it |

Use `status offline Scheduled maintenance` to force a red **Bot Offline** panel
with the reason, and `status online` to restore the real status. Maintenance
mode is saved in `data/settings.json` and survives restarts.

The panel refreshes every 30 seconds and shows **🟢 Bot Online** or
**🔴 Bot Offline**, the bot uptime, the WebSocket ping, a rolling **24h uptime
percentage**, and the **24h average ping**. Samples are saved in settings and the
24-hour window fills in as the bot runs; gaps while the bot is stopped are counted
as offline when it starts again. The embed is green while the bot is connected
and red when it is not. When the Discord gateway connection drops, the bot
immediately tries to turn the panel red with a `Lost — reconnecting to Discord…`
note, then restores green when the session resumes. The panel can only update
while the bot process is running and its HTTP connection is reachable; if the
process is stopped, the last posted state remains until it starts again.

### Logs
| Command | Description |
| --- | --- |
| `logs channel <#channel>` | Set the bot log channel (Manage Server) |
| `logs disable` | Stop sending bot logs |
| `logs test` | Send a test entry to the log channel |
| `logs status` | Show the current log channel |

`log` and `logging` are aliases of `logs`.

### Owner only
| Command | Description |
| --- | --- |
| `reload <cog>` | Reload one cog (`moderation`, `marketplace`…) |
| `reloadall` | Reload every cog |
| `sync` | Sync application commands |
| `servers` | List servers |
| `leave <guild id>` | Leave a server |
| `shutdown` | Stop the bot |
| `eval <code>` | Run Python (owner only, careful) |
| `botstats` | Library/hosting details |

The owner is the Discord account that owns the application.

---

## Project layout

```
discord-bot/
├── main.py                 # entry point, prefix resolver, error handler
├── core/
│   ├── config.py           # paths, colours, env values
│   ├── database.py         # async JSON settings store
│   └── utils.py            # embeds, duration parsing, confirmation view
├── cogs/
│   ├── logs.py             # configurable bot audit log channel
│   ├── utility.py          # help, prefix, say, info
│   ├── moderation.py       # moderation suite
│   ├── verification.py     # persistent verification button
│   ├── marketplace.py      # shop panel builder
│   ├── tickets.py          # ticket panel, private channels, claim/close
│   └── owner.py            # owner tools
└── data/settings.json      # created automatically
```

Back up `data/settings.json` to keep your warnings and shop configuration.
