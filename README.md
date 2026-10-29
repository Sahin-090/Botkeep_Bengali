# Telegram Video Delivery Bot
<img width="360" height="360" alt="WhatsApp Image 2026-10-10 at 8 11 16 AM" src="https://github.com/user-attachments/assets/346183c1-f86e-4aae-b3df-7f5a61c0ca81" />

Python (aiogram 3 + asyncpg) bot for Hosting. Videos live in a private Telegram channel; the bot stores only Telegram `file_id`s and metadata in PostgreSQL and delivers videos straight into the user's chat. No Redis, no downloads, no files kept on Hostinger.

## How it works
- User sends a number → picks 🆕 New / 📦 Old → bot sends that many **unique** videos → `✅ N unique videos delivered.` + 🔄 Request Again.
- **New** = latest channel post → oldest. **Old** = oldest channel post → latest by default (ordered by post date) (`OLD_VIDEO_ORDER`: `asc`, `desc` or `random`). If a category runs short it is topped up from the rest of the library, and if the library has fewer than requested the user gets `⚠️ You requested 20 videos, but only 13 unique videos are currently available.` followed by all 13. "No videos" is only ever shown when the library is really empty.
- Session state lives in the `user_states` table, so restarts are safe. Double taps and parallel requests are blocked with an atomic state claim.
- Flood-waits are honoured automatically (delivery and scanning); videos that can no longer be sent are deactivated and reported to admins.
- The channel link/username/ID is never sent to users, and delivered videos carry no "forwarded from" header or channel caption.

## How videos get into the library
Telegram bots **cannot read channel history**, so the bot uses two clients:

1. **History scanner (Telethon / MTProto)** – logs in as a normal account that is a member of the channel, walks the *whole* history oldest → newest and registers every video (message id, `file_unique_id`, date, caption, duration, size, MIME type, width, height). Nothing is downloaded; only metadata goes into PostgreSQL.
2. **Bot API** – delivers videos to users and sees new channel posts live.

**Bot API `file_id` bridge.** A Telethon media id is not a Bot API `file_id`, so it is never stored as one. After a scan, the *bot* forwards each scanned post to the admin who ran `/scan` (a supported Bot API workflow), reads the real `file_id` from the forwarded message, stores it and deletes the forwarded copy (≈1 video/second, nothing downloaded). Delivery then uses `send_video(file_id)`. If Telegram prevents this for a post (bot not a channel admin, protected content, deleted post) no id is faked: the admin gets a report and that video keeps being delivered with `copy_message` from the channel, which requires the bot to be a channel administrator.
<img width="360" height="360" alt="WhatsApp Image 2026-10-10 at 8 11 17 AM" src="https://github.com/user-attachments/assets/ebfd3b41-9b92-4fba-b713-93a55df564b1" />

**Two separate clients:** the Bot API client (`BOT_TOKEN`) handles users, buttons, delivery and admin commands; the Telethon **user** client (`API_ID` + `API_HASH` + `SESSION_STRING`) is used *only* for reading channel history. `SESSION_STRING` must come from a normal user account that is a member of the channel. A session created by logging in with the bot token is rejected at startup, because bots may not call `GetHistoryRequest` (`BotMethodInvalidError`).

### One-time setup
1. Get `API_ID` and `API_HASH` at <https://my.telegram.org> → *API development tools*.
2. On your own computer run `pip install telethon && python generate_session.py` and log in with an account that is a member of the channel. Copy the printed `SESSION_STRING`. Treat it like a password.
3. In Hosting → *Variables* add `API_ID`, `API_HASH`, `SESSION_STRING`, `SOURCE_Url_ID` (e.g. `-1001234567890`).
4. Make the bot an administrator of the channel.
5. As an admin, send `/scan` to the bot.

### Keeping it current
- New posts are detected **automatically**: instantly through the Bot API, and every `AUTO_SCAN_MINUTES` by an incremental scan that continues from the last checkpoint (so posts made while the bot was down are still picked up). No need to run `/scan` again.
- Re-running `/scan` is always safe: it re-reads the full history and only adds what is missing.

### Duplicates (fixed)
Every channel **post** is one row, identified by `UNIQUE(channel_id, telegram_message_id)`. The same video posted twice (or in two channels) is stored as two rows, so scan totals match the channels (e.g. 5,001 found = 5,001 stored). Re-scanning is still safe: the same post is never stored twice.
- Videos added by hand are still unique by `file_unique_id` (`uq_videos_manual_uid`).
- **Admin Panel → Bot Settings → ♻️ Send duplicates** (default OFF):
  - OFF: a video that exists in several posts is sent **once per user** (New uses its latest post, Old its oldest post).
  - ON: every post is sent, copies included; only the same post is never repeated for one user.
- After upgrading, run **one Full Scan** to register the copies that the old unique index had dropped.

### Numbering
`video_number` is one global sequence (Video #1, #2, …), independent of Telegram message ids. Scanned videos are numbered oldest → newest without gaps, inside a lock so concurrent writers cannot collide. A video keeps its number for life. (Older versions numbered New and Old separately; the first start of this version renumbers existing videos once into the global sequence, ordered by date.)

### Restart safety
Scan progress (`scan_state` table) is checkpointed only *after* each batch is written. If Hosting restarts mid-scan, the next scan continues from the checkpoint; a scan that was "running" at restart is shown as `interrupted`. A single bad message or database row is logged and skipped, never fatal.

## Deploy on Hosting
1. Create the bot with @BotFather and copy the token.
2. Push this folder to a GitHub repo → Hosting → *New Project → Deploy from GitHub*.
3. In the project, *New → Database → PostgreSQL*.
4. In the bot service → *Variables*: `BOT_TOKEN`, `ADMIN_IDS`, and `DATABASE_URL` (reference the Postgres service's `DATABASE_URL`), plus the scanner variables above. Add the optional variables from `.env.example` as needed.
5. The `Procfile` runs `python main.py` as a worker. The schema and migrations are applied automatically on every start (`database/schema.sql`, idempotent).
6. Send `/scan` to the bot as an admin.

Run only **one** instance of the service (Telegram long-polling doesn't support several). No Redis, no volumes, no files kept on Hostinger.

## Group support
Add the bot to a group (it needs permission to send messages/media; give it "Delete messages" too if you want the auto-delete to be tidy). Members can then use:

| Command | What it does |
|---|---|
| `/video [n]` | Shows 🆕 New / 📦 Old buttons (only the person who typed the command can press them) |
| `/new [n]` | `n` unseen **New** videos |
| `/old [n]` | `n` unseen **Old** videos |
| `/progress` | How many videos you received / have left |
| `/help` | Every public command (private + group) |

- `n` defaults to 1 and is limited by `GROUP_MAX_VIDEOS` (default `10`).
- Same rules as the private chat: unique videos per user, rate limit, one request at a time, auto-delete, banned users, bot OFF / maintenance.
- Force-Subscribe and the 18+ confirmation are checked for the person who typed the command; if they still have to do it they get an "Open bot" button and finish it in private chat.
- Videos sent into a group are always content-protected (no forward / save), whatever the user's private "allow save" setting is.
- Anonymous group admins must turn "Remain anonymous" off (the bot cannot tell who they are otherwise).
- `/help` (private and group) now ends with the full list of public commands, and the `/` menu is set automatically for private chats and for groups.

## Admin commands (IDs in `ADMIN_IDS`)
| Command | Purpose |
|---|---|
| `/scan` | Full scan of the whole channel history, with progress and a final report |
| `/scanstatus` | Scanner state, counts and checkpoint |
| `/scanstop` | Stop a running scan (progress is kept) |
| `/stats` | Library and usage statistics |
| `/videos` | Video statistics (`/videos new\|old\|pending\|all [page]` lists videos with IDs) |
| `/addvideo`, `/batch new\|old\|off` | Add videos by sending them to the bot |
| `/editvideo <id> number <n>` / `category <new\|old>` / `caption <text>` | Edit a video (a hand-set category is no longer re-aged) |
| `/deletevideo <id>` | Delete a video |
| `/scanlegacy <new\|old> [from_id] [to_id]` | The old forward-based import, kept as a fallback that needs no API keys |
| `/help` | Command list |

Non-admins get no response to these commands.

## Broadcast (`/broadcast` or Admin Panel → 📢 Broadcast)

Send a modern announcement card to every active user:

1. Press **✍️ New Broadcast** and send your message (a bold first line works well as a title; Telegram formatting, links and emoji are kept).
2. Check the **preview** - it is exactly what users receive - optionally **🧪 Send test to me**.
3. Press **🚀 Send to everyone**. A live progress bar appears, and **⏹️ Stop Broadcast** ends it early.

Users blocked by an admin or who stopped the bot are skipped. Sending is rate-limited (about 20 messages per second) and respects Telegram flood limits. Users who block the bot are marked so the next broadcast skips them. Only one broadcast can run at a time. The finished broadcast is recorded in 📋 Logs.

## Local run
```
pip install -r requirements.txt
cp .env.example .env   # fill in, then export the variables
python main.py
```

## Upgrade: unique delivery, menus, auto-delete

Added on top of the existing bot (scanner, `/scan`, categories, duplicate protection and admin commands are unchanged):

- **Never the same video twice per user.** `user_video_history` (`UNIQUE(user_id, video_id)`) records every delivery. New, Old, More New, More Old, ✨ Fresh Batch and 🔄 New Request all exclude it. It lives in the database, so restarts, redeploys and `/scan` never clear it. Admins can wipe one user with `/resethistory <user_id>`.
- **Strict collections.** New only returns New and Old only returns Old. If fewer unseen videos remain than requested, the user is told and gets what is left, then is offered the other collection / a new request. When a collection is used up, a contextual "end of collection" message is shown.
- **2-minute auto-delete (restart-safe).** Each video message the bot sends is stored in `delivered_messages` with `delete_at`. A background worker (`services/cleanup_service.py`) deletes due messages from the *user's chat* only; the source channel, `videos` and the history are never touched. After a Hostinger restart overdue rows are deleted on the first tick. Users who delete a message themselves, or block the bot, are handled silently.
- **Request lock + rate limit** (database-based, no Redis): double taps never start parallel deliveries (`user_states`), and `RATE_LIMIT_REQUESTS` per `RATE_LIMIT_WINDOW` seconds caps request spam (`user_rate_limits`).
- **UI:** premium `/start`, main menu (`/menu`), `📊 My Requests`, `ℹ️ Help`, progress state, post-delivery buttons (🔥 More New, ♻️ More Old, ✨ Fresh Batch, 🔄 New Request, 🏠 Main Menu). All texts/keyboards are in `ui.py`.
- **Admin:** `/stats` now shows deliveries, duplicate deliveries prevented, auto-delete setting and last scan.

New environment variables (all optional): `AUTO_DELETE_SECONDS` (default 120), `RATE_LIMIT_REQUESTS` (5), `RATE_LIMIT_WINDOW` (60).

**Auto-delete env:** set `AUTO_DELETE_SECONDS` in Hostinger → *Variables* (seconds, minimum 5, default 120; e.g. `300` = 5 minutes). A value saved from the admin panel (⏱️ Auto-delete) overrides the env value; reset it in the panel to use the env again. See `.env.example` for all variables.
The schema changes are additive and applied automatically on start. History starts at the upgrade: deliveries made before it were never recorded, so users may receive those videos once more.

## Upgrade: Admin Panel, developer page, configurable public UI

Added on top of everything above; the scanner, `/scan`, categories, duplicate protection,
unique per-user history, 2-minute auto-delete and all admin commands are unchanged.

### Public UI
- `/start` is the new welcome screen with buttons `🎬 Request Videos · 🆕 New · 📦 Old · 🔥 More Videos · 👨‍💻 Developer · ℹ️ Help`. Every screen has `⬅️ Back` / `🏠 Main Menu`.
- `👨‍💻 Developer` shows a profile page with link buttons (Website / Instagram / Telegram / GitHub). A button only appears when its link is set. Defaults come from `DEVELOPER_*`; the admin can override them in the panel.
- Delivery: `⏳ Preparing N unique videos...` → clean caption (`🎬 Video #125 / 🆕 New Collection / ⏱️ Auto-delete: 2 minutes`) → `✅ N unique videos delivered.` + `🔥 More New · ♻️ More Old · ✨ Fresh Batch · 🔄 New Request · 👨‍💻 Developer · 🏠 Main Menu`.
- Video presentation: videos are never re-encoded, cropped, padded, thumbnailed or wrapped. The stored `width/height/duration` and `supports_streaming=True` are passed to Telegram (or the post is copied as is), so portrait stays portrait, landscape stays landscape and Telegram's own viewer decides how it is shown. The source-channel caption is **not** shown unless an admin turns on *Keep source caption* (channel captions can contain channel names or links).

### Admin Panel (`/admin`, only IDs in `ADMIN_IDS`)
Dashboard → 📊 Statistics · 🎬 Video Manager · 👥 User Manager · 📡 Channel Scanner · 🤖 Bot Control · 🛠️ Maintenance · 🎨 Public Settings · 👨‍💻 Developer Settings · ⚙️ Bot Settings · 📋 Logs. Every page has `⬅️ Back` and `🏠 Dashboard`.
- **Security:** a normal user sending `/admin` or pressing any `adm:` button gets `⛔ You don't have permission to access the Admin Panel.` (same reply for everyone, nothing about who is an admin). Router filters *and* a second check inside every callback verify `user_id in ADMIN_IDS` on the server. Admin buttons are never shown to normal users.
- **Bot OFF / Maintenance / Block:** enforced by one middleware in front of every message and button press. Admins always pass, so they can turn the bot back on, end maintenance, scan, read logs and manage videos while it is closed. State is stored in PostgreSQL (`bot_settings`, `users.is_banned`) and survives Hostinger restarts.
- **Dangerous actions** (delete video, block user, clear history, bot OFF, enable maintenance) ask `⚠️ Are you sure? ✅ Confirm / ❌ Cancel`.
- **Editable texts** (welcome, help, auto-delete notice, maintenance title/message/emoji, developer bio) are validated, limited to `<b> <i> <u> <s> <code>` (everything else is escaped), shown as a **preview** and only saved on `✅ Save`. `{auto_delete}` and `{max}` placeholders work. Send `default` to restore the built-in text.
- **Public buttons:** each of Request / New / Old / More / Help / Developer can be switched off; the panel refuses to switch off the last way to request videos, and the server rejects taps on old messages that still carry a disabled button.
- **Custom button:** name, emoji, position (top/bottom), enabled. Target is either an `http(s)` link (validated, no credentials / no `javascript:`) or one of a fixed list of built-in actions — never free-form callback data.
- **Bot Settings:** max videos per request (1–200), auto-delete seconds (5–86400), New / Old ordering, delivery batch size, rate limit, welcome on/off, developer button, keep source caption, maintenance. Applied immediately, no restart.
- **Scanner:** Full / Quick scan, live progress (`🔎 Scanning channel...` with scanned / found / added / duplicates / errors), Stop, and a guard against two scans at once.
- **Logs:** recent safe events (requests, deliveries, scans, added videos, duplicates, auto-deletes, errors) in the `event_log` table, filterable and paged; configured secrets (`BOT_TOKEN`, `API_HASH`, `SESSION_STRING`, `DATABASE_URL` and its password) are scrubbed before anything is stored. Entries are pruned after 14 days.
- **Errors:** users only ever see `⚠️ Something went wrong. Please try again.`; admins get the exception *type* only; tracebacks stay in the server log.
- **State:** admin typing/preview state is kept in `user_states` (`admin_input` / `admin_preview`), separate from the users' delivery lock. No Redis.

New tables (additive, applied automatically): `bot_settings`, `event_log`; new columns `users.is_banned`, `users.banned_at`. Existing `/stats`, `/scan`, `/videos`, … commands keep working next to the panel.

New optional variables: `DEVELOPER_NAME/WEBSITE/INSTAGRAM/TELEGRAM/GITHUB/BIO`, `NEW_VIDEO_ORDER`, `DELIVERY_BATCH_SIZE`.

### Fix: videos repeatedly sent and deleted after `/scan`
That was the file_id bridge (`AUTO_BRIDGE=true`): after a scan it forwarded every video without a Bot API `file_id` to the admin chat and deleted it, and videos it could not resolve were retried after every scan. The bridge no longer runs automatically (`AUTO_BRIDGE` is ignored); `/scan` and the panel's scans only read metadata. Delivery to users is unchanged (`copy_message`). `/bridge` remains as an explicit manual command.

## Multiple source channels

Set `SOURCE_Url_IDS` to a comma separated list, e.g. `-1001111111111,-1002222222222` (the old `SOURCE_Crl_ID` still works, also with a list).
<img width="360" height="360" alt="WhatsApp Image 2026-10-10 at 8 20 36 AM" src="https://github.com/user-attachments/assets/f45529c2-bf10-4615-8847-f35bcc59fef1" />

- **Requirements:** the bot must be an admin of every channel (live detection + `copy_message` delivery), and the `SESSION_STRING` account must be a member of every channel (history scan).
- **Scanning:** `/scan`, Quick Scan and the automatic catch-up go through all channels. Each channel has its own checkpoint (`scan_checkpoints`), so a new channel is imported from its first post while the others only continue where they stopped. A channel that cannot be read does not stop the others; the report says how many failed.
- **Live:** new videos posted in any configured channel are registered immediately.
- **Library:** all channels feed one library with one global video numbering; the same video posted in two channels is stored as two rows (see Duplicates).
- **Existing deployments:** the old single checkpoint is migrated automatically on the first start. `/scanlegacy` (forward based fallback) works on the first channel only.

## Referral system (Refer & Earn)

Added on top of everything above; videos, scanner, force-subscribe, auto-delete and all existing commands are unchanged. All schema changes are additive and applied automatically on start.

### For users
| Command | What it does |
|---|---|
| `/start` | Works as before. `/start ref_<id>` (a referral link) also records who invited the user |
| `/refer` | Personal link `https://t.me/<bot>?start=ref_<your_id>` + Share button |
| `/stats` | Referral statistics (eligible, waiting, progress, rewards unlocked / claimed) |
| `/rewards` | Reward info; **🎁 Claim Reward** appears when one is unlocked |

The main menu also has a **🔗 Refer & Earn** button. For admins `/stats` stays the admin statistics (now with a referral line); an admin sees his own referral numbers with `/refer` → 📊 My Stats.

BotFather → `/setcommands`:
```
start - 🚀 Start Bot & Track Referrals
refer - 🔗 Get Your Referral Link
stats - 📊 Check Referral Statistics
rewards - 🎁 Unlock & Claim Rewards
```

### How tracking works
1. `/start ref_<id>` is recorded **before** any gate runs (`ReferralCapture` middleware), so the referral survives Force Subscribe.
2. The referral is **eligible** (counted) the first time the bot actually lets the new user in - i.e. his first row in `users`. A user who has not joined the required channels yet stays *waiting* and is not counted.
3. At **20** eligible referrals (admin-changeable) a reward unlocks: **5 exclusive English + 5 exclusive Desi videos** (per-pool count changeable). The user claims it in `/rewards`.

### Anti-fraud
- Own link → rejected. Already a bot user → rejected (only brand-new users can be referred).
- One Telegram ID is counted once, ever (`referrals.referred_id` is the primary key); a second link does not replace the first.
- Unknown or blocked referrer → rejected. Blocked referred users are not counted; blocked users cannot claim.
- A reward can only be claimed once (`UNIQUE(user_id, reward_no)` + per-user lock, double taps are safe). A reward video is never sent twice to the same user.
- Refused attempts are listed in the Referral Manager ("Blocked fraud attempts").

### Admin: who referred how many
- **Admin Panel → 📊 Referral Manager**: overview, **🏆 Top Referrers** (ranking), **🔎 Find User** (ID or @username → how many he referred + the list of referred users), **🎬 Reward Videos**, **⚙️ Settings**.
- `/referrals` (overview) and `/referrals <id|@username>` do the same from the chat.
- **User Manager → a user** shows `Users referred` and a 🔗 Referrals button.

### Reward videos (one-time setup)
Admin Panel → Referral Manager → 🎬 Reward Videos → **➕ Add English** / **➕ Add Desi**, then send the videos to the bot (one or many) and press **Done**. They are kept in their own tables and never mix into the normal New / Old library. Keep at least 5 per pool (more if repeat rewards are on). If a user unlocks a reward while a pool is short, nothing is consumed, the user is asked to try later and the admins get a warning.

### Settings (Referral Manager → ⚙️ Settings, applied immediately)
System ON/OFF (the menu button disappears when OFF) · referrals needed per reward (default 20) · videos per pool (default 5) · **Repeat rewards** (default OFF = one reward per user; ON = a new reward for every further 20).

Reward videos follow the bot's existing protection: content protection and the auto-delete timer apply to them too.


## Upgrade: 18+ confirmation, modern buttons, photo / video broadcast

### 🔞 18+ confirmation ("Confirm & Start")
Every user must confirm that they are 18 or older before anything else in the bot works.

- Flow: **/start → (join channel, if Force Subscribe is on) → 🔞 age card → ✅ I'm 18+ • Confirm & Start → welcome screen**.
- Until they confirm, *every* message and button just shows the age card (`AgeGate` middleware, `middlewares.py`). Only the three `age:` buttons get through. Admins always bypass it.
- **❌ I'm under 18 • Exit** shows `ACCESS DENIED`; nothing is stored for that user. **↩️ I tapped by mistake** goes back to the card.
- The confirmation is stored once in `users.age_verified_at` (additive migration, applied on start) and survives restarts. **Existing users are asked once after this upgrade.**
- Admin Panel → ⚙️ Bot Settings → **🔞 18+ Confirm: ON/OFF** switches the whole gate.
- Referral links keep working: `/start ref_<id>` is recorded before the gate, and the invited user counts as soon as they confirm.

### 🎨 Modern buttons and texts
- Buttons are coloured with Telegram's native button styles: **blue** for the main action (🎬 Request Videos, 🔥 More New), **green** for confirm / reward actions (✅ Confirm, ✨ Fresh Batch, 🔗 Refer & Earn), **red** for exit (❌ Under 18). Needs `aiogram>=3.31` (set in `requirements.txt`); on an older library the buttons simply stay plain - nothing breaks.
- Cleaner layout: one wide primary button, then 2-column rows (`New | Old`, `More | My Requests`, `Developer | Help`).
- Welcome, Main Menu and the age card use Telegram quote blocks and a tidy header. All callbacks are unchanged, so every old message keeps working. Texts an admin customised in the panel are not touched.

### 📢 Broadcast with photo / video / GIF
Admin Panel → 📢 Broadcast (or `/broadcast`) → **✍️ New Broadcast**, then send one of:

| You send | Result |
|---|---|
| Text | Announcement card (up to 3,500 characters) |
| 🖼 Photo (+ caption) | Photo with the announcement card as caption (caption up to 800 characters) |
| 🎬 Video (+ caption) | Streaming video with the same card |
| 🎞 GIF (+ caption) | Animation with the same card |

- Send **one** photo/video per broadcast (not an album). Bold / italic / links / emoji in the caption are kept.
- You always get an exact **preview**, can press **🧪 Send test to me**, then **🚀 Send to everyone** (live progress bar, ⏹️ Stop works as before).
- Media is sent by Telegram `file_id` (nothing is downloaded or re-uploaded) and follows the bot's **protected content** rule (no forward / save unless an admin allowed that user).
- **Photos, videos and GIFs are only sent to users who confirmed 18+** (while the age gate is ON). Text announcements go to everybody. The Broadcast page shows both audience sizes.

Tests: `DATABASE_URL=postgresql://t:t@localhost/tdb python tests/test_age_gate_broadcast.py`
