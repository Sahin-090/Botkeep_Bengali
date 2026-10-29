import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

import ui
from config import Config
from database import Database
from logging_setup import log_event
from models.video import Video
from services.user_service import UserService
from services.video_service import VideoService

log = logging.getLogger("delivery")

_INVALID_MARKERS = ("file identifier", "file_id", "file reference", "wrong remote file")
_GONE_MARKERS = ("message to copy not found", "message_id_invalid", "message can't be copied")


@dataclass
class DeliveryResult:
    delivered: int = 0
    failed: int = 0
    blocked: bool = False


class DeliveryService:
    def __init__(self, bot: Bot, db: Database, videos: VideoService,
                 users: UserService, cfg: Config, settings=None, events=None) -> None:
        self.settings = settings   # SettingsService (optional): caption options
        self.events = events       # EventService (optional): admin Logs panel
        self.bot = bot
        self.db = db
        self.videos = videos
        self.users = users
        self.cfg = cfg
        self._access_warned = False

    async def deliver(self, user_id: int, chat_id: int, category: str, count: int,
                      shuffle: bool = False, in_group: bool = False,
                      thread_id: int | None = None) -> DeliveryResult:
        """Send up to `count` videos this user has never received, in the order of their mode
        (`category` is the mode: new = newest -> oldest, old = oldest -> newest).
        Every sent video is written to user_video_history and queued for auto-deletion, and the
        user's own session row (mode / position / progress) is advanced. Nothing is shared
        between users.

        `in_group=True`: the videos go to a group chat. They are ALWAYS content-protected
        there (everybody in the group could otherwise save them) and a refused send is never
        counted as "this user blocked the bot". `thread_id` keeps forum-topic replies in
        their topic."""
        result = DeliveryResult()
        tried_ids: set[int] = set()
        sent_unique: set[str] = set()

        try:   # the user's own session: remember the chosen mode
            await self.users.start_session(user_id, category)
        except Exception:
            log.exception("session_start_failed")

        if not shuffle:   # statistics only; must never get in the way of a delivery
            try:
                prevented = await self.videos.count_seen_ahead_all(user_id, category, count)
                await self.videos.bump_counter("delivery_duplicates_prevented", prevented)
            except Exception:
                log.exception("prevented_counter_failed")

        for _ in range(5):  # extra rounds replace videos that failed
            need = count - result.delivered
            if need <= 0:
                break
            batch = await self.videos.fetch_session_batch(user_id, category, need,
                                                          list(tried_ids), shuffle)
            if not batch:
                break
            for video in batch:
                tried_ids.add(video.id)
                if (not self.videos.allow_duplicates
                        and video.telegram_file_unique_id in sent_unique):
                    continue
                protect = True if in_group else self.protect_for(user_id)
                outcome, message_id = await self._send_one(chat_id, video, result.delivered + 1,
                                                           protect, thread_id)
                if outcome is None:  # user blocked the bot (private) / bot cannot post (group)
                    result.blocked = True
                    if not in_group:
                        await self.users.mark_blocked(user_id)
                    await self._record(user_id, category, count, result)
                    return result
                if outcome:
                    sent_unique.add(video.telegram_file_unique_id)
                    result.delivered += 1
                    await self._record_delivery(user_id, chat_id, video, message_id, category)
                    await self._advance_session(user_id, category, video)
                    if result.delivered % max(1, self.cfg.delivery_batch_size) == 0:
                        await asyncio.sleep(1.0)   # short breather between batches (flood-friendly)
                else:
                    result.failed += 1

        await self._record(user_id, category, count, result)
        return result

    async def _advance_session(self, user_id: int, mode: str, video: Video) -> None:
        """Position/progress bookkeeping and lock heartbeat. Best effort: the delivery history
        (written separately, in its own transaction) is what guarantees no repeats."""
        try:
            await self.users.advance_session(
                user_id, mode, video.id, video.message_date or video.created_at)
            await self.users.refresh_delivery_lock(user_id)
        except Exception:
            log.exception("session_advance_failed")

    def protect_for(self, user_id: int) -> bool:
        """Telegram content protection for the videos sent to ONE user: no forwarding, no saving
        or downloading from the chat. Every user is protected until an admin allows Save/Forward
        for that user (User Manager). The global switch in Bot Settings stays a master switch:
        if an admin turns it OFF, nobody is protected."""
        if self.settings is not None and not self.settings.flag("protect_content"):
            return False
        return self.users.protect_for(user_id)

    async def _record_delivery(self, user_id: int, chat_id: int, video: Video,
                               message_id: int | None, mode: str) -> None:
        """History rows (global + this mode's) + auto-delete job, in one transaction, right
        after each send: if the process dies mid-batch, everything already sent is recorded and
        the rest is simply still unseen when the user continues."""
        for attempt in range(3):
            try:
                async with self.db.pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute(
                            "INSERT INTO user_video_history (user_id, video_id, category) "
                            "VALUES ($1, $2, $3) ON CONFLICT (user_id, video_id) DO NOTHING",
                            user_id, video.id, video.category,
                        )
                        await conn.execute(
                            "INSERT INTO user_mode_history (user_id, mode, video_id) "
                            "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                            user_id, mode, video.id,
                        )
                        if message_id is not None:
                            await conn.execute(
                                "INSERT INTO delivered_messages "
                                "(user_id, chat_id, video_id, telegram_message_id, delete_at) "
                                "VALUES ($1, $2, $3, $4, "
                                "now() + make_interval(secs => $5::double precision))",
                                user_id, chat_id, video.id, message_id,
                                float(self.cfg.auto_delete_seconds),
                            )
                return
            except Exception:
                log.exception("record_delivery_failed")
                await asyncio.sleep(0.5 * (attempt + 1))
        log_event(log, logging.ERROR, "record_delivery_gave_up", user_id=user_id,
                  video_id=video.id)

    async def _record(self, user_id: int, category: str, count: int, r: DeliveryResult) -> None:
        if r.delivered:
            try:
                await self.users.finish_batch(user_id, category)
            except Exception:
                log.exception("session_finish_failed")
        await self.db.pool.execute(
            "INSERT INTO requests (user_id, category, requested, delivered, failed) "
            "VALUES ($1, $2, $3, $4, $5)",
            user_id, category, count, r.delivered, r.failed,
        )
        log_event(log, logging.INFO, "delivery_finished", user_id=user_id, category=category,
                  requested=count, delivered=r.delivered, failed=r.failed, blocked=r.blocked)
        if self.events is not None:
            await self.events.record(
                "delivery", f"{r.delivered}/{count} {category} videos delivered"
                + (f", {r.failed} failed" if r.failed else "")
                + (", user blocked the bot" if r.blocked else ""), user_id)
            if r.failed:
                await self.events.record("error", f"{r.failed} video(s) could not be sent", user_id)

    async def _send_one(self, chat_id: int, video: Video, position: int,
                        protect: bool = True,
                        thread_id: int | None = None) -> tuple[bool | None, int | None]:
        """(True, message_id) = sent, (False, None) = failed, (None, None) = user blocked the bot.

        Delivery modes, tried in order: the stored Bot API file_id, then copying the original
        post out of the source channel (no "forwarded from", so the channel is never exposed).
        Videos found by the history scanner have no file_id and always use the copy."""
        # Clean caption of our own. The channel caption is only added if the admin opted in
        # (Bot Settings): channel captions can carry channel names or links.
        keep = self.settings is not None and self.settings.flag("keep_source_caption")
        caption = ui.video_caption(self.cfg, video, position, video.caption if keep else None)
        modes = []
        if video.telegram_file_id:
            modes.append("file")
        if video.channel_id is not None and video.telegram_message_id is not None:
            modes.append("copy")
        invalid_reasons = []
        for mode in modes:
            outcome, reason, message_id = await self._attempt(mode, chat_id, video, caption, protect,
                                                              thread_id)
            if outcome == "ok":
                return True, message_id
            if outcome == "blocked":
                return None, None
            if outcome == "error":      # transient / access problem: keep the video active
                return False, None
            invalid_reasons.append(reason)
        if len(invalid_reasons) == len(modes):   # every way of sending it is dead
            await self.videos.mark_invalid(video.id, "; ".join(invalid_reasons) or "no way to send")
            log_event(log, logging.ERROR, "video_deactivated", video_id=video.id)
            if self.events is not None:
                await self.events.record("error", f"Video ID {video.id} can no longer be sent "
                                                  "and was deactivated")
            await self._notify_admins(
                f"⚠️ Video ID {video.id} (#{video.video_number}, {video.category}) "
                "can no longer be sent and was deactivated. Re-scan the channel with /scan "
                "or send the video to the bot again to restore it."
            )
        return False, None

    async def _attempt(self, mode: str, chat_id: int, video: Video,
                       caption: str, protect: bool = True,
                       thread_id: int | None = None) -> tuple[str, str, int | None]:
        """One delivery mode. Returns ("ok"|"blocked"|"error"|"invalid", reason, message_id)."""
        for attempt in range(5):
            try:
                if mode == "file":
                    # Original dimensions/duration are passed through untouched so Telegram's own
                    # viewer shows portrait / landscape / square exactly as posted; no
                    # thumbnail, re-encode, crop or padding is ever added.
                    sent = await self.bot.send_video(
                        chat_id, video.telegram_file_id, caption=caption, parse_mode="HTML",
                        width=video.width, height=video.height, duration=video.duration,
                        supports_streaming=True, protect_content=protect,
                        message_thread_id=thread_id,
                    )
                else:
                    # copy_message keeps the original media (size, ratio, streaming) as is.
                    sent = await self.bot.copy_message(
                        chat_id, video.channel_id, video.telegram_message_id,
                        caption=caption, parse_mode="HTML",
                        protect_content=protect, message_thread_id=thread_id,
                    )
                await asyncio.sleep(self.cfg.send_delay)
                return "ok", "", sent.message_id
            except TelegramRetryAfter as e:
                log_event(log, logging.WARNING, "flood_wait", seconds=e.retry_after)
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError as e:
                if "bot was blocked" in str(e).lower() or "user is deactivated" in str(e).lower():
                    return "blocked", "", None
                if mode == "copy":  # the BOT has no access to the channel: not the user's fault
                    await self._warn_channel_access(str(e))
                    return "error", str(e), None
                return "blocked", "", None
            except TelegramBadRequest as e:
                msg = str(e).lower()
                if mode == "file" and any(m in msg for m in _INVALID_MARKERS):
                    log_event(log, logging.WARNING, "invalid_file_id", video_id=video.id)
                    return "invalid", str(e)[:100], None
                if mode == "copy" and any(m in msg for m in _GONE_MARKERS):
                    log_event(log, logging.WARNING, "source_message_gone", video_id=video.id)
                    return "invalid", str(e)[:100], None
                if mode == "copy" and "chat not found" in msg:
                    await self._warn_channel_access(str(e))
                    return "error", str(e), None
                log_event(log, logging.ERROR, "telegram_bad_request", video_id=video.id, error=str(e))
                return "error", str(e), None
            except TelegramAPIError as e:  # network / server hiccups
                log_event(log, logging.WARNING, "telegram_error", video_id=video.id,
                          attempt=attempt + 1, error=str(e))
                await asyncio.sleep(2 ** attempt)
            except Exception:
                log.exception("unexpected_send_error")
                return "error", "unexpected", None
        return "error", "retries exhausted", None

    async def _warn_channel_access(self, detail: str) -> None:
        if self._access_warned:
            return
        self._access_warned = True
        log_event(log, logging.ERROR, "channel_access_denied", error=detail[:200])
        await self._notify_admins(
            "⚠️ The bot cannot read the source channel, so scanned videos cannot be delivered. "
            "Add the bot to the channel as an administrator."
        )

    async def _notify_admins(self, text: str) -> None:
        for admin_id in self.cfg.admin_ids:
            try:
                await self.bot.send_message(admin_id, text)
            except TelegramAPIError:
                pass
