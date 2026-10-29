"""Referral system: personal links, tracking, anti-fraud and rewards.

How it works
  * Every user has the link  https://t.me/<bot>?start=ref_<telegram_id>.
  * ReferralCapture (outer middleware, runs before every other gate) sees `/start ref_<id>` and
    records a *pending* row in `referrals`. The row is keyed by the NEW user's id, so one
    Telegram account can only ever be counted once, for the first link it opened.
  * The referral becomes *qualified* (= eligible, counted) the first time the bot actually lets
    the new user in. That moment is the first INSERT into `users`, which UserService.upsert
    reports through `on_new_user` - so a user blocked by Force Subscribe, a banned user or a
    closed bot never counts until they really get in (and joined the required channels).
  * When the number of eligible referrals reaches the target (default 20) a reward unlocks:
    N exclusive English + N exclusive Desi videos (default 5 + 5), uploaded by an admin in the
    Admin Panel -> Referral Manager and delivered with the user's own /rewards -> Claim.

Anti-fraud (all enforced on the server, in the database)
  * self referral                         -> rejected (user id == referrer id)
  * already a user of the bot              -> rejected (only brand-new users can be referred)
  * same user twice / second link          -> rejected (PRIMARY KEY referred_id)
  * unknown or banned referrer             -> rejected
  * banned referred users                  -> not counted
  * a reward can only be claimed once      -> UNIQUE (user_id, reward_no) + per-user lock
  * a reward video is never sent twice to the same user -> UNIQUE (user_id, reward_video_id)
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import asyncpg
from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest, TelegramForbiddenError,
                                TelegramRetryAfter)
from aiogram.types import Message, TelegramObject

from config import Config
from database import Database
from logging_setup import log_event
import ui
from services.settings_service import SettingsService

log = logging.getLogger("referral")

POOLS = ("english", "desi")
POOL_LABEL = {"english": "🇬🇧 English", "desi": "🇮🇳 Desi"}
_START_REF = re.compile(r"^/start(?:@\w+)?\s+ref_(\d{1,15})\s*$", re.I)
_INVALID_MARKERS = ("file identifier", "file_id", "file reference", "wrong remote file")


def parse_ref_payload(text: str | None) -> int | None:
    """`/start ref_123456` -> 123456 (None for anything else)."""
    m = _START_REF.match((text or "").strip())
    return int(m.group(1)) if m else None


@dataclass
class ReferralSummary:
    eligible: int = 0          # qualified referrals (counted)
    pending: int = 0           # opened the link but have not got into the bot yet
    target: int = 20
    unlocked: int = 0          # rewards unlocked so far
    claimed: int = 0           # rewards fully claimed
    ready: int = 0             # unlocked but not yet claimed (incl. half-delivered)
    repeat: bool = False       # a new reward for every further `target` referrals

    @property
    def next_in(self) -> int:
        """Eligible referrals still needed for the next reward."""
        if not self.repeat and self.eligible >= self.target:
            return 0
        return self.target - (self.eligible % self.target)

    @property
    def inside(self) -> int:
        """Progress inside the current reward (0..target) for the progress bar."""
        if not self.repeat:
            return min(self.eligible, self.target)
        done = self.eligible % self.target
        return self.target if done == 0 and self.eligible and self.ready else done


@dataclass
class ClaimResult:
    status: str                # ok | locked | no_videos | busy | blocked | disabled | banned | partial | error
    sent: int = 0
    total: int = 0
    short_pools: list[str] = field(default_factory=list)
    reward_no: int = 0


class ReferralService:
    def __init__(self, bot: Bot, db: Database, cfg: Config, settings: SettingsService,
                 users, events=None) -> None:
        self.bot, self.db, self.cfg = bot, db, cfg
        self.settings, self.users, self.events = settings, users, events
        self.bot_username: str | None = None
        self._locks: dict[int, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        self._pool_warned: dict[str, float] = {}

    # ------------------------------------------------------------------ setup / settings
    async def start(self) -> None:
        try:
            me = await self.bot.get_me()
            self.bot_username = me.username
        except TelegramAPIError:
            log.warning("referral_bot_username_unavailable")
        self.users.on_new_user = self.on_new_user

    @property
    def enabled(self) -> bool:
        return self.settings.flag("ref_enabled")

    @property
    def target(self) -> int:
        return max(1, self.settings.number("ref_target"))

    @property
    def per_pool(self) -> int:
        return max(1, self.settings.number("ref_per_pool"))

    @property
    def repeat(self) -> bool:
        return self.settings.flag("ref_repeat")

    def link_for(self, user_id: int) -> str:
        return f"https://t.me/{self.bot_username or 'YourBot'}?start=ref_{user_id}"

    def unlocked_for(self, eligible: int) -> int:
        n = eligible // self.target
        return n if self.repeat else min(n, 1)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _reject(self, user_id: int, referrer_id: int | None, reason: str) -> str:
        """Statistics only (max one row per user and reason per day, so spam stays cheap)."""
        try:
            await self.db.pool.execute(
                "INSERT INTO referral_rejections (user_id, referrer_id, reason) "
                "SELECT $1, $2, $3 WHERE NOT EXISTS (SELECT 1 FROM referral_rejections "
                "WHERE user_id = $1 AND reason = $3 AND created_at > now() - interval '1 day')",
                user_id, referrer_id, reason)
        except Exception:  # noqa: BLE001
            log.warning("referral_reject_log_failed")
        log_event(log, logging.INFO, "referral_rejected", user_id=user_id, reason=reason)
        return reason

    # ------------------------------------------------------------------ tracking
    async def capture(self, user, referrer_id: int) -> str:
        """Record `user` as referred by `referrer_id` if (and only if) that is legitimate.
        Returns "pending" or the reason it was refused."""
        if not self.enabled:
            return "disabled"
        if user.is_bot:
            return "bot"
        if referrer_id == user.id:
            return await self._reject(user.id, referrer_id, "self_referral")
        pool = self.db.pool
        if await pool.fetchval("SELECT 1 FROM users WHERE user_id = $1", user.id):
            return await self._reject(user.id, referrer_id, "already_user")
        ref = await pool.fetchrow("SELECT is_banned FROM users WHERE user_id = $1", referrer_id)
        if ref is None or ref["is_banned"]:
            return await self._reject(user.id, referrer_id, "invalid_referrer")
        inserted = await pool.fetchval(
            "INSERT INTO referrals (referred_id, referrer_id) VALUES ($1, $2) "
            "ON CONFLICT (referred_id) DO NOTHING RETURNING 1", user.id, referrer_id)
        if not inserted:
            return await self._reject(user.id, referrer_id, "duplicate")
        log_event(log, logging.INFO, "referral_pending", user_id=user.id, referrer=referrer_id)
        return "pending"

    async def on_new_user(self, user_id: int) -> None:
        """Called by UserService.upsert exactly once per user: when his row is first created,
        i.e. when the bot let him in. Turns a pending referral into an eligible one."""
        row = await self.db.pool.fetchrow(
            "UPDATE referrals SET status = 'qualified', qualified_at = now() "
            "WHERE referred_id = $1 AND status = 'pending' RETURNING referrer_id", user_id)
        if row is None:
            return
        referrer_id = row["referrer_id"]
        log_event(log, logging.INFO, "referral_qualified", user_id=user_id, referrer=referrer_id)
        if self.events is not None:
            await self.events.record("admin", f"Referral: user {user_id} joined via {referrer_id}",
                                     referrer_id)
        self._spawn(self._notify_referrer(referrer_id, user_id))

    async def _notify_referrer(self, referrer_id: int, new_user_id: int) -> None:
        try:
            s = await self.summary(referrer_id)
            name = await self.db.pool.fetchval(
                "SELECT first_name FROM users WHERE user_id = $1", new_user_id)
            before = self.unlocked_for(s.eligible - 1)
            text = (f"🎉 <b>New referral!</b>\n\n<b>{html.escape(name or 'A new user')}</b> joined "
                    f"with your link.\n\n👥 Eligible referrals: <b>{s.eligible}</b>")
            if s.unlocked > before:
                text += "\n\n🎁 <b>Reward unlocked!</b> Send /rewards to claim it."
            else:
                text += f"\n🎯 {s.next_in} more for your next reward."
            await self.bot.send_message(referrer_id, text, parse_mode="HTML")
        except TelegramAPIError:
            pass   # the referrer blocked the bot: nothing to do
        except Exception:  # noqa: BLE001
            log.exception("referral_notify_failed")

    # ------------------------------------------------------------------ numbers
    _COUNT_SQL = """
        SELECT COUNT(*) FILTER (WHERE r.status = 'qualified'
                                  AND NOT COALESCE(u.is_banned, FALSE)) AS eligible,
               COUNT(*) FILTER (WHERE r.status = 'pending') AS pending
        FROM referrals r LEFT JOIN users u ON u.user_id = r.referred_id
        WHERE r.referrer_id = $1
    """

    async def summary(self, user_id: int) -> ReferralSummary:
        row = await self.db.pool.fetchrow(self._COUNT_SQL, user_id)
        claims = await self.db.pool.fetchrow(
            "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE status = 'done') AS done "
            "FROM referral_claims WHERE user_id = $1", user_id)
        s = ReferralSummary(eligible=row["eligible"], pending=row["pending"], target=self.target,
                            repeat=self.repeat)
        s.unlocked = max(self.unlocked_for(s.eligible), claims["total"])
        s.claimed = claims["done"]
        s.ready = max(0, s.unlocked - s.claimed)
        return s

    # ------------------------------------------------------------------ rewards
    async def pool_counts(self) -> dict[str, int]:
        rows = await self.db.pool.fetch(
            "SELECT pool, COUNT(*) AS n FROM referral_reward_videos WHERE is_active GROUP BY pool")
        out = {p: 0 for p in POOLS}
        out.update({r["pool"]: r["n"] for r in rows})
        return out

    def _lock(self, user_id: int) -> asyncio.Lock:
        if len(self._locks) > 5000:
            self._locks = {k: v for k, v in self._locks.items() if v.locked()}
        return self._locks.setdefault(user_id, asyncio.Lock())

    async def claim(self, user_id: int, chat_id: int) -> ClaimResult:
        """Claim (or finish delivering) the user's next reward. Safe against double taps."""
        if not self.enabled:
            return ClaimResult("disabled")
        lock = self._lock(user_id)
        if lock.locked():
            return ClaimResult("busy")
        async with lock:
            try:
                return await self._claim(user_id, chat_id)
            except Exception:  # noqa: BLE001
                log.exception("referral_claim_failed")
                return ClaimResult("error")

    async def _claim(self, user_id: int, chat_id: int) -> ClaimResult:
        pool = self.db.pool
        if self.users.is_banned(user_id):
            return ClaimResult("banned")
        # A reward that was claimed but not fully delivered (crash, network) is finished first.
        claim = await pool.fetchrow(
            "SELECT id, reward_no FROM referral_claims "
            "WHERE user_id = $1 AND status = 'pending' ORDER BY reward_no LIMIT 1", user_id)
        if claim is None:
            s = await self.summary(user_id)
            if s.ready <= 0:
                return ClaimResult("locked")
            picks, short = await self._pick_videos(user_id)
            if short:
                await self._warn_pool_low(short)
                return ClaimResult("no_videos", short_pools=short)
            reward_no = (await pool.fetchval(
                "SELECT COALESCE(MAX(reward_no), 0) FROM referral_claims WHERE user_id = $1",
                user_id)) + 1
            try:
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        claim_id = await conn.fetchval(
                            "INSERT INTO referral_claims (user_id, reward_no) VALUES ($1, $2) "
                            "RETURNING id", user_id, reward_no)
                        for pool_name, ids in picks.items():
                            for vid in ids:
                                await conn.execute(
                                    "INSERT INTO referral_claim_items "
                                    "(claim_id, user_id, reward_video_id, pool) "
                                    "VALUES ($1, $2, $3, $4)", claim_id, user_id, vid, pool_name)
            except asyncpg.UniqueViolationError:
                return ClaimResult("busy")      # lost a race: the other request owns this reward
            claim = {"id": claim_id, "reward_no": reward_no}
            log_event(log, logging.INFO, "referral_claim_started", user_id=user_id,
                      reward_no=reward_no)
        return await self._deliver_claim(user_id, chat_id, claim["id"], claim["reward_no"])

    async def _pick_videos(self, user_id: int, only: str | None = None
                           ) -> tuple[dict[str, list[int]], list[str]]:
        """Unseen, active reward videos per pool (oldest upload first)."""
        picks: dict[str, list[int]] = {}
        short: list[str] = []
        for name in ([only] if only else POOLS):
            rows = await self.db.pool.fetch(
                "SELECT id FROM referral_reward_videos v WHERE pool = $1 AND is_active "
                "AND NOT EXISTS (SELECT 1 FROM referral_claim_items i "
                "                WHERE i.user_id = $2 AND i.reward_video_id = v.id) "
                "ORDER BY id LIMIT $3", name, user_id, self.per_pool)
            picks[name] = [r["id"] for r in rows]
            if len(rows) < self.per_pool:
                short.append(name)
        return picks, short

    async def _deliver_claim(self, user_id: int, chat_id: int, claim_id: int,
                             reward_no: int) -> ClaimResult:
        pool = self.db.pool
        items = await pool.fetch(
            "SELECT i.reward_video_id, i.pool, i.sent_at, v.telegram_file_id, v.width, "
            "v.height, v.duration FROM referral_claim_items i "
            "JOIN referral_reward_videos v ON v.id = i.reward_video_id "
            "WHERE i.claim_id = $1 ORDER BY i.pool, i.reward_video_id", claim_id)
        total = len(items)
        sent_total = sum(1 for i in items if i["sent_at"])
        position = {p: sum(1 for i in items if i["pool"] == p and i["sent_at"]) for p in POOLS}
        # Same rule as normal deliveries: protected unless an admin allowed Save/Forward for this user.
        protect = self.settings.flag("protect_content") and self.users.protect_for(user_id)
        failed = False
        for it in items:
            if it["sent_at"]:
                continue
            position[it["pool"]] += 1
            outcome, message_id = await self._send_reward(
                chat_id, it, position[it["pool"]], protect)
            if outcome == "blocked":
                await self.users.mark_blocked(user_id)
                return ClaimResult("blocked", sent_total, total, reward_no=reward_no)
            if outcome == "invalid":       # dead file_id: retire it and replace it
                position[it["pool"]] -= 1
                await pool.execute("UPDATE referral_reward_videos SET is_active = FALSE "
                                   "WHERE id = $1", it["reward_video_id"])
                await pool.execute("DELETE FROM referral_claim_items "
                                   "WHERE claim_id = $1 AND reward_video_id = $2",
                                   claim_id, it["reward_video_id"])
                await self._notify_admins(
                    f"⚠️ A {POOL_LABEL[it['pool']]} reward video (ID {it['reward_video_id']}) "
                    "can no longer be sent and was deactivated. Upload it again in "
                    "Admin Panel → Referral Manager → Reward Videos.")
                total -= 1
                failed = True
                continue
            if outcome != "ok":
                position[it["pool"]] -= 1
                failed = True
                continue
            sent_total += 1
            await pool.execute(
                "UPDATE referral_claim_items SET sent_at = now() "
                "WHERE claim_id = $1 AND reward_video_id = $2", claim_id, it["reward_video_id"])
            if message_id is not None:
                await pool.execute(
                    "INSERT INTO delivered_messages (user_id, chat_id, telegram_message_id, "
                    "delete_at) VALUES ($1, $2, $3, now() + make_interval(secs => "
                    "$4::double precision))", user_id, chat_id, message_id,
                    float(self.cfg.auto_delete_seconds))
        # replace retired videos, if the pools still have unseen ones
        remaining = await pool.fetchval(
            "SELECT COUNT(*) FROM referral_claim_items WHERE claim_id = $1 "
            "AND sent_at IS NULL", claim_id)
        have = {p: await pool.fetchval(
            "SELECT COUNT(*) FROM referral_claim_items WHERE claim_id = $1 AND pool = $2",
            claim_id, p) for p in POOLS}
        refilled = False
        for p in POOLS:
            if have[p] < self.per_pool:
                picks, _ = await self._pick_videos(user_id, only=p)
                for vid in picks[p][: self.per_pool - have[p]]:
                    try:
                        await pool.execute(
                            "INSERT INTO referral_claim_items (claim_id, user_id, "
                            "reward_video_id, pool) VALUES ($1, $2, $3, $4)",
                            claim_id, user_id, vid, p)
                        refilled = True
                    except asyncpg.UniqueViolationError:
                        pass
        if refilled:
            return await self._deliver_claim(user_id, chat_id, claim_id, reward_no)
        if remaining == 0 and not failed:
            await pool.execute("UPDATE referral_claims SET status = 'done', "
                               "completed_at = now() WHERE id = $1", claim_id)
            log_event(log, logging.INFO, "referral_claim_done", user_id=user_id,
                      reward_no=reward_no)
            if self.events is not None:
                await self.events.record("admin", f"Referral reward #{reward_no} claimed "
                                                  f"({sent_total} videos)", user_id)
            return ClaimResult("ok", sent_total, total, reward_no=reward_no)
        return ClaimResult("partial", sent_total, total, reward_no=reward_no)

    async def _send_reward(self, chat_id: int, it, position: int, protect: bool
                           ) -> tuple[str, int | None]:
        label = "🇬🇧 English" if it["pool"] == "english" else "🇮🇳 Desi"
        caption = (f"🎁 <b>Exclusive {label} Reward</b> {position}/{self.per_pool}\n\n"
                   f"⏱️ Auto-delete: {ui.fmt_duration(self.cfg.auto_delete_seconds)}")
        for attempt in range(4):
            try:
                sent = await self.bot.send_video(
                    chat_id, it["telegram_file_id"], caption=caption, parse_mode="HTML",
                    width=it["width"], height=it["height"], duration=it["duration"],
                    supports_streaming=True, protect_content=protect)
                await asyncio.sleep(self.cfg.send_delay)
                return "ok", sent.message_id
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                return "blocked", None
            except TelegramBadRequest as e:
                if any(m in str(e).lower() for m in _INVALID_MARKERS):
                    return "invalid", None
                log_event(log, logging.ERROR, "reward_bad_request", error=str(e)[:100])
                return "error", None
            except TelegramAPIError:
                await asyncio.sleep(2 ** attempt)
        return "error", None

    async def _warn_pool_low(self, short: list[str]) -> None:
        now = time.monotonic()
        key = ",".join(short)
        if now - self._pool_warned.get(key, 0.0) < 3600:
            return
        self._pool_warned[key] = now
        names = " and ".join(POOL_LABEL[p] for p in short)
        await self._notify_admins(
            f"⚠️ A user unlocked a referral reward, but there are fewer than {self.per_pool} "
            f"unseen {names} reward videos. Upload more in Admin Panel → Referral Manager → "
            "Reward Videos.")

    async def _notify_admins(self, text: str) -> None:
        for admin_id in self.cfg.admin_ids:
            try:
                await self.bot.send_message(admin_id, text)
            except TelegramAPIError:
                pass

    # ------------------------------------------------------------------ admin: reward videos
    async def add_reward_video(self, pool_name: str, video, admin_id: int) -> bool:
        """False if this video is already in that pool."""
        row = await self.db.pool.fetchval(
            "INSERT INTO referral_reward_videos (pool, telegram_file_id, file_unique_id, "
            "width, height, duration, added_by) VALUES ($1, $2, $3, $4, $5, $6, $7) "
            "ON CONFLICT (pool, file_unique_id) DO UPDATE SET telegram_file_id = "
            "EXCLUDED.telegram_file_id, is_active = TRUE "
            "WHERE NOT referral_reward_videos.is_active RETURNING id",
            pool_name, video.file_id, video.file_unique_id, video.width, video.height,
            video.duration, admin_id)
        return row is not None

    async def reward_videos(self, pool_name: str, limit: int, offset: int):
        rows = await self.db.pool.fetch(
            "SELECT id, duration, width, height, created_at FROM referral_reward_videos "
            "WHERE pool = $1 AND is_active ORDER BY id LIMIT $2 OFFSET $3",
            pool_name, limit, offset)
        total = await self.db.pool.fetchval(
            "SELECT COUNT(*) FROM referral_reward_videos WHERE pool = $1 AND is_active",
            pool_name)
        return rows, total

    async def reward_video(self, video_id: int):
        return await self.db.pool.fetchrow(
            "SELECT id, pool, duration, created_at FROM referral_reward_videos "
            "WHERE id = $1 AND is_active", video_id)

    async def remove_reward_video(self, video_id: int) -> bool:
        res = await self.db.pool.execute(
            "UPDATE referral_reward_videos SET is_active = FALSE WHERE id = $1 AND is_active",
            video_id)
        return res.endswith("1")

    # ------------------------------------------------------------------ admin: numbers
    async def overview(self) -> dict:
        pool = self.db.pool
        r = await pool.fetchrow(
            "SELECT COUNT(*) FILTER (WHERE r.status = 'qualified' "
            "         AND NOT COALESCE(u.is_banned, FALSE)) AS eligible, "
            "COUNT(*) FILTER (WHERE r.status = 'pending') AS pending, "
            "COUNT(DISTINCT r.referrer_id) FILTER (WHERE r.status = 'qualified') AS referrers "
            "FROM referrals r LEFT JOIN users u ON u.user_id = r.referred_id")
        claims = await pool.fetchrow(
            "SELECT COUNT(*) FILTER (WHERE status = 'done') AS done, "
            "COUNT(*) FILTER (WHERE status = 'pending') AS pending FROM referral_claims")
        rej = await pool.fetch(
            "SELECT reason, COUNT(*) AS n FROM referral_rejections "
            "WHERE created_at > now() - interval '7 days' GROUP BY reason ORDER BY n DESC")
        return {"eligible": r["eligible"], "pending": r["pending"], "referrers": r["referrers"],
                "claims_done": claims["done"], "claims_pending": claims["pending"],
                "rejections": {x["reason"]: x["n"] for x in rej},
                "pools": await self.pool_counts()}

    async def top(self, limit: int, offset: int):
        rows = await self.db.pool.fetch(
            """
            SELECT r.referrer_id AS user_id, u.first_name, u.username,
                   COUNT(*) FILTER (WHERE r.status = 'qualified'
                                      AND NOT COALESCE(ru.is_banned, FALSE)) AS eligible,
                   COUNT(*) FILTER (WHERE r.status = 'pending') AS pending
            FROM referrals r
            LEFT JOIN users u  ON u.user_id  = r.referrer_id
            LEFT JOIN users ru ON ru.user_id = r.referred_id
            GROUP BY r.referrer_id, u.first_name, u.username
            HAVING COUNT(*) FILTER (WHERE r.status = 'qualified'
                                      AND NOT COALESCE(ru.is_banned, FALSE)) > 0
            ORDER BY eligible DESC, MIN(r.created_at) ASC
            LIMIT $1 OFFSET $2
            """, limit, offset)
        total = await self.db.pool.fetchval(
            "SELECT COUNT(DISTINCT referrer_id) FROM referrals WHERE status = 'qualified'")
        return rows, total

    async def referred_list(self, referrer_id: int, limit: int):
        return await self.db.pool.fetch(
            "SELECT r.referred_id, r.status, r.created_at, r.qualified_at, u.first_name, "
            "u.username, COALESCE(u.is_banned, FALSE) AS is_banned "
            "FROM referrals r LEFT JOIN users u ON u.user_id = r.referred_id "
            "WHERE r.referrer_id = $1 ORDER BY r.created_at DESC LIMIT $2", referrer_id, limit)

    async def referred_by(self, user_id: int) -> int | None:
        return await self.db.pool.fetchval(
            "SELECT referrer_id FROM referrals WHERE referred_id = $1", user_id)

    async def eligible_count(self, user_id: int) -> int:
        return (await self.db.pool.fetchrow(self._COUNT_SQL, user_id))["eligible"]


# ---------------------------------------------------------------------- middleware
class ReferralCapture(BaseMiddleware):
    """Outermost middleware on messages. Records `/start ref_<id>` BEFORE any gate runs, so the
    referral is not lost when Force Subscribe, maintenance or a closed bot stops the update.
    It never blocks or changes the update; every error is swallowed."""

    def __init__(self, referrals: ReferralService) -> None:
        self.referrals = referrals

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        if isinstance(event, Message) and event.chat.type == "private" and event.from_user:
            referrer_id = parse_ref_payload(event.text)
            if referrer_id is not None:
                try:
                    await self.referrals.capture(event.from_user, referrer_id)
                except Exception:  # noqa: BLE001
                    log.exception("referral_capture_failed")
        return await handler(event, data)
