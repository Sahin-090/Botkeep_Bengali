"""Video requests inside GROUPS / SUPERGROUPS.

Public group commands (all listed by /help):

    /video [n]   choose New or Old with buttons (only the person who typed it can press them)
    /new   [n]   n unseen NEW videos
    /old   [n]   n unseen OLD videos
    /progress    how many videos you received / have left
    /help        all public commands

`n` defaults to 1 and is capped by GROUP_MAX_VIDEOS (default 10) so one person cannot flood a
group. Everything else (unique videos per user, auto-delete, history, rate limit, one request at a
time) is the same delivery pipeline the private chat uses.

Group-specific rules, because the middlewares in bot.py only guard PRIVATE messages:
  * banned users, "bot OFF", maintenance, Force-Subscribe and the 18+ confirmation are checked
    here for the person who typed the command (admins always pass);
  * a user who still has to join the channel / confirm 18+ is sent to the private chat to do it;
  * videos in a group are always content-protected (no forward / save);
  * anonymous group admins cannot be identified, so they are asked to switch it off.
"""
import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
                           User)

import ui
from config import Config
from force_subscribe import ForceSubConfig, check_all
from logging_setup import log_event
from services.delivery_service import DeliveryService
from services.force_sub_service import ForceSubService
from services.settings_service import SettingsService
from services.user_service import UserService
from services.video_service import VideoService

log = logging.getLogger("group")
router = Router(name="group")
HTML = "HTML"
CATS = ("new", "old")
ANON_ADMIN_ID = 1087968824          # Telegram's "GroupAnonymousBot"
GROUP_CHATS = F.chat.type.in_({"group", "supergroup"})
router.message.filter(GROUP_CHATS)


# ------------------------------------------------------------------ small helpers
def parse_count(raw: str | None, limit: int) -> int | None:
    """None = invalid. No argument means 1 video."""
    raw = (raw or "").strip().split(" ")[0]
    if not raw:
        return 1
    if not raw.isdigit() or len(raw) > 6:
        return None
    n = int(raw)
    return n if 1 <= n <= limit else None


def mention(user: User) -> str:
    name = html.escape(user.first_name or "User")
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def _thread(message: Message) -> int | None:
    return message.message_thread_id if message.is_topic_message else None


async def _say(bot: Bot, chat_id: int, text: str, kb: InlineKeyboardMarkup | None = None,
               thread_id: int | None = None, reply_to: int | None = None) -> Message | None:
    try:
        return await bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=kb,
                                      message_thread_id=thread_id,
                                      reply_to_message_id=reply_to,
                                      allow_sending_without_reply=True)
    except TelegramAPIError:
        return None


async def _bot_link_kb(bot: Bot, label: str = "🤖 Open bot") -> InlineKeyboardMarkup | None:
    try:
        username = (await bot.me()).username
    except TelegramAPIError:
        return None
    if not username:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, url=f"https://t.me/{username}?start=group")]])


def group_help_text(cfg: Config) -> str:
    return ("ℹ️ <b>HOW IT WORKS IN GROUPS</b>\n\n"
            f"1️⃣ Type <code>/new 5</code> or <code>/old 5</code> (1-{cfg.group_max_videos}).\n"
            "2️⃣ Or type <code>/video 5</code> and tap 🆕 New / 📦 Old.\n"
            "3️⃣ You only get videos you have not received before.\n"
            "4️⃣ Videos disappear automatically after "
            f"{ui.fmt_duration(cfg.auto_delete_seconds)}.\n\n"
            + ui.commands_text(cfg.group_max_videos))


# ------------------------------------------------------------------ access checks
async def guard(message: Message, bot: Bot, cfg: Config, settings: SettingsService,
                users: UserService, force_sub: ForceSubService,
                fs_cfg: ForceSubConfig) -> bool:
    """True = this person may request videos here. Otherwise they have been told why."""
    user = message.from_user
    chat_id, thread = message.chat.id, _thread(message)

    async def tell(text: str, kb: InlineKeyboardMarkup | None = None) -> bool:
        await _say(bot, chat_id, text, kb, thread, message.message_id)
        return False

    if user is None or user.is_bot:
        return False
    if user.id == ANON_ADMIN_ID:
        return await tell("⚠️ I can't tell who you are while <b>Remain anonymous</b> is on. "
                          "Please turn it off for your admin profile and send the command again.")
    if user.id in cfg.admin_ids:
        await users.upsert(user)
        return True

    if users.is_banned(user.id):
        return await tell(ui.banned_base_text())
    if not settings.bot_enabled:
        return await tell(ui.bot_off_text())
    if settings.maintenance:
        return await tell(settings.maintenance_text())

    await users.upsert(user)
    try:
        await users.touch(user.id)
    except Exception:  # noqa: BLE001 - last-activity only
        log.warning("touch_failed")

    channels = force_sub.enabled() if fs_cfg.enabled else []
    needs_channel = False
    if channels:
        overall, _pairs = await check_all(bot, channels, user.id)
        needs_channel = overall is not True            # False = not joined, None = could not check
    needs_age = settings.flag("age_gate_enabled") and not users.is_age_verified(user.id)
    if needs_channel or needs_age:
        steps = []
        if needs_channel:
            steps.append("join the required channel")
        if needs_age:
            steps.append("confirm that you are 18+")
        return await tell(
            f"🔒 {mention(user)}, please open the bot in private chat and "
            f"{' and '.join(steps)} first. Then come back and send the command again.",
            await _bot_link_kb(bot))
    return True


# ------------------------------------------------------------------ the group delivery pipeline
async def run_group_request(bot: Bot, users: UserService, delivery: DeliveryService,
                            videos: VideoService, cfg: Config, settings: SettingsService, *,
                            user: User, chat_id: int, thread_id: int | None, category: str,
                            count: int, reply_to: int | None = None) -> None:
    """Lock -> rate limit -> unseen check -> deliver into the group -> completion message."""
    uid = user.id

    async def say(text: str, kb=None) -> Message | None:
        return await _say(bot, chat_id, text, kb, thread_id, reply_to)

    def private(text: str) -> str:               # busy / rate texts are written for pop-ups
        return f"{mention(user)}\n\n{ui.html_escape(ui.alert_text(text))}"

    if not await users.acquire_delivery_lock(uid):
        await say(private(ui.busy_text()))
        return
    status: Message | None = None
    short_of: int | None = None
    n = 0
    try:
        if not await users.hit_rate_limit(uid, cfg.rate_limit_requests, cfg.rate_limit_window):
            log_event(log, logging.INFO, "rate_limited", user_id=uid, chat_id=chat_id)
            await say(private(ui.rate_text()))
            return

        unseen = await videos.count_unseen_all(uid, category)
        if unseen == 0:
            empty = await videos.count_available() == 0
            if empty:
                await say(ui.empty_text())
            else:
                other = ui.other(category)
                left = await videos.count_unseen_all(uid, other)
                tail = (f" Try <code>/{other}</code> instead." if left else "")
                await say(f"{mention(user)}\n\n✅ You have received every "
                          f"{ui.ICON[category]} {ui.LABEL[category]} video already.{tail}")
            return

        n = min(count, unseen)
        short_of = count if unseen < count else None
        status = await say(ui.preparing_n_text(n, category, short_of))
        if delivery.events is not None:
            await delivery.events.record(
                "user_request", f"{count} {category} video(s) requested in a group", uid)
        log_event(log, logging.INFO, "group_delivery_started", user_id=uid, chat_id=chat_id,
                  category=category, requested=count, available=unseen)
        result = await delivery.deliver(uid, chat_id, category, n, in_group=True,
                                        thread_id=thread_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("group_delivery_crashed")
        if delivery.events is not None:
            await delivery.events.record("error", f"Group delivery crashed: {type(exc).__name__}",
                                         uid)
        await say(ui.error_text())
        return
    finally:
        await users.clear_state(uid)

    if status is not None:
        try:
            await status.delete()
        except TelegramAPIError:
            pass
    if result.blocked:
        await say("⚠️ I can't send videos in this group. An admin needs to allow the bot to "
                  "send messages and media here.")
        return
    if result.delivered == 0:
        await say(ui.failed_text())
        return
    await say(f"{mention(user)}\n\n"
              + ui.delivered_text(cfg, result.delivered, category, short_of, protected=False)
              + f"\n\nMore: <code>/new</code> • <code>/old</code>")


# ------------------------------------------------------------------ commands
@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot, cfg: Config) -> None:
    await message.reply(group_help_text(cfg), parse_mode=HTML,
                        reply_markup=await _bot_link_kb(bot, "🤖 Open bot privately"))


@router.message(Command("help"))
async def cmd_help(message: Message, cfg: Config) -> None:
    await message.reply(group_help_text(cfg), parse_mode=HTML)


@router.message(Command("new", "old"))
async def cmd_collection(message: Message, command: CommandObject, bot: Bot, cfg: Config,
                         settings: SettingsService, users: UserService,
                         videos: VideoService, delivery: DeliveryService,
                         force_sub: ForceSubService, fs_cfg: ForceSubConfig) -> None:
    category = (command.command or "").lower()
    if category not in CATS:
        return
    count = parse_count(command.args, cfg.group_max_videos)
    if count is None:
        await message.reply(f"❌ Send a number from 1 to {cfg.group_max_videos}, "
                            f"for example <code>/{category} 5</code>.", parse_mode=HTML)
        return
    if not await guard(message, bot, cfg, settings, users, force_sub, fs_cfg):
        return
    await run_group_request(bot, users, delivery, videos, cfg, settings,
                            user=message.from_user, chat_id=message.chat.id,
                            thread_id=_thread(message), category=category, count=count,
                            reply_to=message.message_id)


@router.message(Command("video"))
async def cmd_video(message: Message, command: CommandObject, bot: Bot, cfg: Config,
                    settings: SettingsService, users: UserService,
                    force_sub: ForceSubService, fs_cfg: ForceSubConfig) -> None:
    count = parse_count(command.args, cfg.group_max_videos)
    if count is None:
        await message.reply(f"❌ Send a number from 1 to {cfg.group_max_videos}, "
                            "for example <code>/video 5</code>.", parse_mode=HTML)
        return
    if not await guard(message, bot, cfg, settings, users, force_sub, fs_cfg):
        return
    uid = message.from_user.id
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🆕 New", callback_data=f"gv:new:{uid}:{count}"),
        InlineKeyboardButton(text="📦 Old", callback_data=f"gv:old:{uid}:{count}"),
    ]])
    await message.reply(f"🎬 {mention(message.from_user)}, choose the collection for "
                        f"<b>{count}</b> {'video' if count == 1 else 'videos'}:",
                        parse_mode=HTML, reply_markup=kb)


@router.message(Command("progress"))
async def cmd_progress(message: Message, bot: Bot, cfg: Config, settings: SettingsService,
                       users: UserService, videos: VideoService,
                       force_sub: ForceSubService, fs_cfg: ForceSubConfig) -> None:
    if not await guard(message, bot, cfg, settings, users, force_sub, fs_cfg):
        return
    try:
        text = ui.progress_text(await videos.progress(message.from_user.id))
    except Exception:  # noqa: BLE001
        log.exception("group_progress_failed")
        text = ui.error_text()
    await message.reply(f"{mention(message.from_user)}\n\n{text}", parse_mode=HTML)


# ------------------------------------------------------------------ New / Old buttons
@router.callback_query(F.data.startswith("gv:"))
async def on_choose(cb: CallbackQuery, bot: Bot, users: UserService, delivery: DeliveryService,
                    videos: VideoService, cfg: Config, settings: SettingsService) -> None:
    parts = (cb.data or "").split(":")
    if (len(parts) != 4 or parts[1] not in CATS or not parts[2].isdigit()
            or not parts[3].isdigit() or not isinstance(cb.message, Message)):
        await cb.answer()
        return
    category, owner, count = parts[1], int(parts[2]), int(parts[3])
    if cb.from_user.id != owner:      # a button belongs to the person who typed the command
        await cb.answer("These buttons are for the person who sent the command. "
                        "Send /video yourself.", show_alert=True)
        return
    if cb.message.chat.type not in ("group", "supergroup"):
        await cb.answer()
        return
    count = max(1, min(count, cfg.group_max_videos))
    await cb.answer()
    chat_id, thread_id = cb.message.chat.id, _thread(cb.message)
    try:
        await cb.message.delete()                  # remove the chooser
    except TelegramAPIError:
        pass
    await run_group_request(bot, users, delivery, videos, cfg, settings, user=cb.from_user,
                            chat_id=chat_id, thread_id=thread_id, category=category,
                            count=count)
