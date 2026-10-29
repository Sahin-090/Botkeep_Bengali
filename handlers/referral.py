"""User side of the referral system: /refer, /stats, /rewards (+ the matching buttons).

/start is NOT handled here: the existing /start handler keeps working, and the referral link is
recorded earlier by services.referral_service.ReferralCapture.

/stats is the user's referral statistics. For admins /stats stays the admin statistics (the admin
router is included first); an admin sees his own referral numbers with /refer -> 📊 My Stats.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, InlineKeyboardButton as Btn,
                           InlineKeyboardMarkup as Kb, Message)

import ui
from config import Config
from services.referral_service import ClaimResult, ReferralService, ReferralSummary


log = logging.getLogger("referral_ui")
router = Router(name="referral")
HTML = "HTML"
LINE = ui.LINE
UNAVAILABLE = "🔗 <b>Referral program</b>\n\nThis feature is currently unavailable."


# ---------------------------------------------------------------- texts
def bar(done: int, total: int, width: int = 10) -> str:
    filled = min(width, int(width * done / total)) if total else 0
    return "▰" * filled + "▱" * (width - filled)


def reward_line(per_pool: int) -> str:
    return (f"🇬🇧 <b>{per_pool}</b> Exclusive English Videos\n"
            f"🇮🇳 <b>{per_pool}</b> Exclusive Desi Videos")


def progress_block(s: ReferralSummary) -> str:
    return f"{bar(s.inside, s.target)}  <b>{s.inside} / {s.target}</b>"


def refer_text(link: str, s: ReferralSummary, per_pool: int) -> str:
    return (f"🔗 <b>YOUR REFERRAL LINK</b>\n\n{LINE}\n\n"
            "Invite friends with your personal link. When "
            f"<b>{s.target}</b> new users join through it, you unlock:\n\n"
            f"{reward_line(per_pool)}\n\n{LINE}\n\n"
            f"👇 <b>Your link</b> (tap to copy)\n<code>{link}</code>\n\n"
            f"👥 Eligible referrals: <b>{s.eligible}</b>\n{progress_block(s)}")


def stats_text(s: ReferralSummary) -> str:
    lines = [f"📊 <b>YOUR REFERRAL STATS</b>\n\n{LINE}\n",
             f"👥 Eligible referrals: <b>{s.eligible}</b>",
             f"⏳ Opened your link, not joined yet: <b>{s.pending}</b>",
             f"\n🎯 Progress\n{progress_block(s)}"]
    if s.ready:
        lines.append("\n🎁 <b>A reward is ready to claim!</b>")
    elif s.next_in == 0:
        lines.append("\n🏆 <b>You unlocked and claimed your reward!</b>")
    else:
        lines.append(f"\n🎯 <b>{s.next_in}</b> more eligible "
                     f"{'user' if s.next_in == 1 else 'users'} for the next reward")
    lines += [f"\n🎁 Rewards unlocked: <b>{s.unlocked}</b>",
              f"✅ Rewards claimed: <b>{s.claimed}</b>", f"\n{LINE}",
              "ℹ️ Only brand-new users count, once each. Self-referrals and duplicates are ignored."]
    return "\n".join(lines)


def rewards_text(s: ReferralSummary, per_pool: int, repeat: bool) -> str:
    head = f"🎁 <b>REFERRAL REWARDS</b>\n\n{LINE}\n\n"
    body = (f"Reach <b>{s.target}</b> eligible referrals to unlock:\n\n"
            f"{reward_line(per_pool)}\n\n")
    if repeat:
        body += f"♻️ A new reward unlocks for every further {s.target} referrals.\n\n"
    body += f"{LINE}\n\n👥 Eligible referrals: <b>{s.eligible}</b>\n{progress_block(s)}\n\n"
    if s.ready:
        body += "✅ <b>Unlocked!</b> Tap the button below to claim your reward."
    elif s.next_in == 0:
        body += "🏆 <b>You have already claimed your reward.</b> Thank you for sharing!"
    else:
        body += (f"🔒 <b>Locked</b> — invite <b>{s.next_in}</b> more "
                 f"{'user' if s.next_in == 1 else 'users'} to unlock it.")
    return head + body


def claim_text(r: ClaimResult, per_pool: int) -> str:
    if r.status == "ok":
        return (f"🎉 <b>Reward delivered!</b>\n\n{r.sent} exclusive videos were sent to you "
                "above. Enjoy! 🔥")
    if r.status == "partial":
        return (f"⚠️ <b>{r.sent} of {r.total} videos delivered.</b>\n\n"
                "Some videos could not be sent right now. Press <b>Claim Reward</b> again to "
                "receive the rest — nothing is lost.")
    if r.status == "no_videos":
        return ("⏳ <b>Your reward is unlocked!</b>\n\nThe exclusive videos are being prepared. "
                "Please try again a little later — your reward stays safe.")
    if r.status == "locked":
        return "🔒 <b>No reward available yet.</b>\n\nKeep inviting to unlock the next one."
    if r.status == "busy":
        return "⏳ Your reward is already being delivered."
    if r.status == "banned":
        return "⛔ Rewards are not available for this account."
    if r.status == "disabled":
        return UNAVAILABLE
    return ui.error_text()


# ---------------------------------------------------------------- keyboards
def share_url(link: str, cfg: Config) -> str:
    text = "🔥 Fresh exclusive videos delivered instantly. Join me on VideoShare!"
    return f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(text)}"


def refer_kb(link: str, cfg: Config) -> Kb:
    return Kb(inline_keyboard=[
        [Btn(text="📤 Share My Link", url=share_url(link, cfg))],
        [Btn(text="📊 My Stats", callback_data="ref:stats"),
         Btn(text="🎁 Rewards", callback_data="ref:rewards")],
        [Btn(text="🏠 Main Menu", callback_data="menu")],
    ])


def stats_kb() -> Kb:
    return Kb(inline_keyboard=[
        [Btn(text="🔗 My Link", callback_data="ref:home"),
         Btn(text="🎁 Rewards", callback_data="ref:rewards")],
        [Btn(text="🏠 Main Menu", callback_data="menu")],
    ])


def rewards_kb(s: ReferralSummary) -> Kb:
    rows = []
    if s.ready:
        rows.append([Btn(text="🎁 Claim Reward", callback_data="ref:claim")])
    rows.append([Btn(text="🔗 My Link", callback_data="ref:home"),
                 Btn(text="📊 My Stats", callback_data="ref:stats")])
    rows.append([Btn(text="🏠 Main Menu", callback_data="menu")])
    return Kb(inline_keyboard=rows)


def after_claim_kb(s: ReferralSummary | None, retry: bool) -> Kb:
    rows = []
    if retry:
        rows.append([Btn(text="🎁 Claim Reward", callback_data="ref:claim")])
    rows.append([Btn(text="🔗 My Link", callback_data="ref:home"),
                 Btn(text="🏠 Main Menu", callback_data="menu")])
    return Kb(inline_keyboard=rows)


# ---------------------------------------------------------------- page builders
async def page_refer(user_id: int, rs: ReferralService, cfg: Config) -> tuple[str, Kb]:
    s = await rs.summary(user_id)
    link = rs.link_for(user_id)
    return refer_text(link, s, rs.per_pool), refer_kb(link, cfg)


async def page_stats(user_id: int, rs: ReferralService) -> tuple[str, Kb]:
    return stats_text(await rs.summary(user_id)), stats_kb()


async def page_rewards(user_id: int, rs: ReferralService) -> tuple[str, Kb]:
    s = await rs.summary(user_id)
    return rewards_text(s, rs.per_pool, rs.repeat), rewards_kb(s)


# ---------------------------------------------------------------- commands
async def _answer(message: Message, text: str, kb: Kb | None = None) -> None:
    try:
        await message.answer(text, reply_markup=kb, parse_mode=HTML)
    except TelegramAPIError:
        pass


@router.message(Command("refer"), F.chat.type == "private")
async def cmd_refer(message: Message, referrals: ReferralService, cfg: Config) -> None:
    if not referrals.enabled:
        await _answer(message, UNAVAILABLE)
        return
    await _answer(message, *await page_refer(message.from_user.id, referrals, cfg))


@router.message(Command("stats"), F.chat.type == "private")
async def cmd_stats(message: Message, referrals: ReferralService) -> None:
    # Reached by normal users only: the admin router answers /stats for admins first.
    if not referrals.enabled:
        await _answer(message, UNAVAILABLE)
        return
    await _answer(message, *await page_stats(message.from_user.id, referrals))


@router.message(Command("rewards"), F.chat.type == "private")
async def cmd_rewards(message: Message, referrals: ReferralService) -> None:
    if not referrals.enabled:
        await _answer(message, UNAVAILABLE)
        return
    await _answer(message, *await page_rewards(message.from_user.id, referrals))


# ---------------------------------------------------------------- buttons
async def _show(cb: CallbackQuery, text: str, kb: Kb | None = None) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode=HTML)
    except TelegramAPIError as exc:
        if "not modified" in str(exc).lower():
            return
        try:
            await cb.message.answer(text, reply_markup=kb, parse_mode=HTML)
        except TelegramAPIError:
            pass


@router.callback_query(F.data.in_({"ref:home", "ref:stats", "ref:rewards"}))
async def on_ref_page(cb: CallbackQuery, referrals: ReferralService, cfg: Config) -> None:
    if not referrals.enabled:
        await cb.answer("This feature is currently unavailable.", show_alert=True)
        return
    await cb.answer()
    uid = cb.from_user.id
    which = cb.data.split(":", 1)[1]
    if which == "stats":
        text, kb = await page_stats(uid, referrals)
    elif which == "rewards":
        text, kb = await page_rewards(uid, referrals)
    else:
        text, kb = await page_refer(uid, referrals, cfg)
    await _show(cb, text, kb)


@router.callback_query(F.data == "ref:claim")
async def on_ref_claim(cb: CallbackQuery, bot: Bot, referrals: ReferralService) -> None:
    if not referrals.enabled:
        await cb.answer("This feature is currently unavailable.", show_alert=True)
        return
    await cb.answer()
    status = cb.message if isinstance(cb.message, Message) else None
    if status is not None:
        try:
            await status.edit_text("⏳ <b>Preparing your reward...</b>", parse_mode=HTML)
        except TelegramAPIError:
            pass
    chat_id = status.chat.id if status is not None else cb.from_user.id
    result = await referrals.claim(cb.from_user.id, chat_id)
    if result.status == "blocked":
        return
    if status is not None and result.sent:        # the status line sits above the videos
        try:
            await status.delete()
        except TelegramAPIError:
            pass
        status = None
    retry = result.status in ("partial", "no_videos")
    text, kb = claim_text(result, referrals.per_pool), after_claim_kb(None, retry)
    if status is not None:
        await _show(cb, text, kb)
    else:
        try:
            await bot.send_message(chat_id, text, reply_markup=kb, parse_mode=HTML)
        except TelegramAPIError:
            pass
