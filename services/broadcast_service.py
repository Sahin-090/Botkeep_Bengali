"""Admin broadcast: a modern announcement card sent to every active user.

* The admin sends text, a photo, a video or a GIF (media may carry a caption); `render()` wraps
  the text in the bot's announcement design. Media is sent by Telegram `file_id`, so nothing is
  downloaded or re-uploaded.
* Photo / video / GIF broadcasts only go to users who confirmed 18+ (when the age gate is on);
  text announcements go to everybody.
* Delivery is rate-limited (well under Telegram's ~30 msg/s limit), honours `RetryAfter`,
  skips users blocked by an admin and users who stopped the bot, and marks users who block
  the bot so the next broadcast does not try them again.
* It runs as a background task; progress is reported through a callback and the admin can stop
  it at any time. State is in memory only - a restart simply ends a running broadcast.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest, TelegramForbiddenError,
                                TelegramRetryAfter)
from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as Kb

import ui
from database import Database
from logging_setup import log_event
from services.settings_service import SettingsService
from services.user_service import UserService

log = logging.getLogger("broadcast")

MAX_BODY = 3500            # text card: the card adds ~150 characters; Telegram's limit is 4096
MAX_CAPTION = 800          # media card: a caption may have 1024 characters in total
MEDIA_KINDS = ("photo", "video", "animation")
MEDIA_LABEL = {"photo": "🖼 Photo", "video": "🎬 Video", "animation": "🎞 GIF"}
SEND_DELAY = 0.05          # 20 messages / second
PROGRESS_EVERY = 25        # users
PROGRESS_SECONDS = 3.0


def render(body: str) -> str:
    """The modern announcement card. `body` is already safe Telegram HTML."""
    return (f"📢 <b>ANNOUNCEMENT</b>\n"
            f"{ui.LINE}\n\n"
            f"{body.strip()}\n\n"
            f"{ui.LINE}\n"
            f"{ui.FOOTER}")


def render_caption(body: str) -> str:
    """The same card for a photo / video / GIF caption (a caption may be empty)."""
    if not body.strip():
        return f"📢 <b>ANNOUNCEMENT</b>\n{ui.FOOTER}"
    return render(body)


@dataclass(frozen=True)
class Payload:
    """What is broadcast: text, or one photo / video / GIF with an optional caption."""
    text: str = ""            # safe Telegram HTML (the body, or the caption of the media)
    media: str = ""           # "" (text only) | "photo" | "video" | "animation"
    file_id: str = ""         # Bot API file_id of the media

    @property
    def is_media(self) -> bool:
        return self.media in MEDIA_KINDS and bool(self.file_id)

    def rendered(self) -> str:
        return render_caption(self.text) if self.is_media else render(self.text)

    def to_state(self) -> dict:
        return {"text": self.text, "media": self.media, "file_id": self.file_id}

    @classmethod
    def from_state(cls, data: dict) -> "Payload":
        return cls(text=data.get("text", "") or "", media=data.get("media", "") or "",
                   file_id=data.get("file_id", "") or "")


def card_kb() -> Kb:
    return Kb(inline_keyboard=[[ui.Btn("🎬 Open VideoShare", ui.PRIMARY, callback_data="menu")]])


@dataclass
class BroadcastResult:
    total: int = 0
    sent: int = 0
    blocked: int = 0      # users who stopped / blocked the bot
    failed: int = 0
    stopped: bool = False
    seconds: float = 0.0
    started: float = field(default_factory=time.monotonic)

    @property
    def done(self) -> int:
        return self.sent + self.blocked + self.failed

    @property
    def percent(self) -> int:
        return int(self.done * 100 / self.total) if self.total else 100


Progress = Callable[[BroadcastResult], Awaitable[None]]


class BroadcastService:
    def __init__(self, bot: Bot, db: Database, users: UserService,
                 settings: SettingsService | None = None) -> None:
        self.bot, self.db, self.users, self.settings = bot, db, users, settings
        self.running = False
        self.last: BroadcastResult | None = None
        self._stop = False

    # ----- audience -----
    def _adult_only(self, is_media: bool) -> bool:
        """Media goes only to users who confirmed 18+ (while the age gate is switched on)."""
        return bool(is_media and self.settings is not None
                    and self.settings.flag("age_gate_enabled"))

    def _audience_sql(self, adult_only: bool) -> str:
        extra = " AND age_verified_at IS NOT NULL" if adult_only else ""
        return f"FROM users WHERE NOT is_banned AND NOT is_blocked{extra}"

    async def audience_ids(self, is_media: bool = False) -> list[int]:
        rows = await self.db.pool.fetch(
            f"SELECT user_id {self._audience_sql(self._adult_only(is_media))} ORDER BY user_id")
        return [r["user_id"] for r in rows]

    async def audience_count(self, is_media: bool = False) -> int:
        return await self.db.pool.fetchval(
            f"SELECT COUNT(*) {self._audience_sql(self._adult_only(is_media))}")

    # ----- control -----
    def try_begin(self) -> bool:
        """Claim the single broadcast slot. False = one is already running."""
        if self.running:
            return False
        self.running, self._stop = True, False
        return True

    def stop(self) -> None:
        self._stop = True

    # ----- sending -----
    def protect_for(self, user_id: int) -> bool:
        """Same rule as video delivery: no forward / save unless an admin allowed it."""
        if self.settings is not None and not self.settings.flag("protect_content"):
            return False
        return self.users.protect_for(user_id)

    async def send_to(self, chat_id: int, payload: Payload, *, protect: bool = False) -> None:
        """One send (raises Telegram errors). Also used for the admin's preview and test."""
        text, kb = payload.rendered(), card_kb()
        common = dict(caption=text, parse_mode="HTML", reply_markup=kb, protect_content=protect)
        if payload.media == "photo" and payload.file_id:
            await self.bot.send_photo(chat_id, payload.file_id, **common)
        elif payload.media == "video" and payload.file_id:
            await self.bot.send_video(chat_id, payload.file_id, supports_streaming=True, **common)
        elif payload.media == "animation" and payload.file_id:
            await self.bot.send_animation(chat_id, payload.file_id, **common)
        else:
            await self.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb,
                                        protect_content=protect)

    async def send_one(self, user_id: int, payload: Payload) -> str:
        """Returns 'sent', 'blocked' or 'failed'."""
        protect = payload.is_media and self.protect_for(user_id)
        for _ in range(3):
            try:
                await self.send_to(user_id, payload, protect=protect)
                return "sent"
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                await self.users.mark_blocked(user_id)
                return "blocked"
            except TelegramBadRequest as e:
                reason = str(e).lower()
                if "chat not found" in reason or "deactivated" in reason:
                    await self.users.mark_blocked(user_id)
                    return "blocked"
                return "failed"
            except TelegramAPIError:
                return "failed"
        return "failed"

    async def run(self, payload: "Payload | str", progress: Progress | None = None
                  ) -> BroadcastResult:
        """Call `try_begin()` first. Always releases the slot when done.
        A plain string is a text-only announcement (old call style)."""
        if isinstance(payload, str):
            payload = Payload(text=payload)
        result = BroadcastResult()
        try:
            ids = await self.audience_ids(payload.is_media)
            result.total = len(ids)
            last_report = time.monotonic()
            for uid in ids:
                if self._stop:
                    result.stopped = True
                    break
                outcome = await self.send_one(uid, payload)
                setattr(result, outcome, getattr(result, outcome) + 1)
                await asyncio.sleep(SEND_DELAY)
                now = time.monotonic()
                if progress and (result.done % PROGRESS_EVERY == 0
                                 or now - last_report >= PROGRESS_SECONDS):
                    last_report = now
                    await progress(result)
        finally:
            result.seconds = time.monotonic() - result.started
            self.last = result
            self.running = False
            log_event(log, logging.INFO, "broadcast_finished", media=payload.media or "text",
                      total=result.total,
                      sent=result.sent, blocked=result.blocked, failed=result.failed,
                      stopped=result.stopped)
        return result
