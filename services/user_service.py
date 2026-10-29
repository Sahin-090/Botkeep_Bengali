import logging

from database import Database

log = logging.getLogger("users")


class UserService:
    """Users + database-backed session state (no Redis)."""

    def __init__(self, db: Database, ttl_minutes: int) -> None:
        self.db = db
        self.ttl = ttl_minutes
        self.banned_ids: set[int] = set()   # mirror of users.is_banned (single instance)
        self.ban_reasons: dict[int, str] = {}   # mirror of users.ban_reason (only non-empty)
        self.save_allowed_ids: set[int] = set()   # mirror of users.allow_save_forward
        self.age_verified_ids: set[int] = set()   # mirror of users.age_verified_at IS NOT NULL
        # Optional hook (set by the referral system): awaited once, when a user row is created.
        self.on_new_user = None

    async def upsert(self, user) -> bool:
        """Create/refresh the user. Returns True only the first time (row newly created)."""
        inserted = await self.db.pool.fetchval(
            """
            INSERT INTO users (user_id, username, first_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE
            SET username = EXCLUDED.username, first_name = EXCLUDED.first_name,
                last_seen = now(), is_blocked = FALSE
            RETURNING (xmax = 0)
            """,
            user.id, user.username, user.first_name,
        )
        if inserted and self.on_new_user is not None:
            try:
                await self.on_new_user(user.id)
            except Exception:  # noqa: BLE001 - a referral problem must never break /start
                log.exception("on_new_user_failed")
        return bool(inserted)

    async def mark_blocked(self, user_id: int) -> None:
        await self.db.pool.execute(
            "UPDATE users SET is_blocked = TRUE WHERE user_id = $1", user_id
        )

    async def set_state(self, user_id: int, state: str, data: dict | None = None) -> bool:
        """Set state unless a delivery is running. Returns False if user is busy."""
        row = await self.db.pool.fetchval(
            """
            INSERT INTO user_states (user_id, state, data) VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE
            SET state = EXCLUDED.state, data = EXCLUDED.data, updated_at = now()
            WHERE user_states.state <> 'delivering'
            RETURNING 1
            """,
            user_id, state, data or {},
        )
        return row is not None

    async def get_state(self, user_id: int) -> tuple[str | None, dict]:
        row = await self.db.pool.fetchrow(
            """
            SELECT state, data FROM user_states
            WHERE user_id = $1
              AND (state = 'delivering'
                   OR updated_at > now() - make_interval(mins => $2::int))
            """,
            user_id, self.ttl,
        )
        return (row["state"], row["data"]) if row else (None, {})

    async def claim_state(self, user_id: int, from_state: str, to_state: str) -> dict | None:
        """Atomically move state; guards against double taps and duplicate sessions."""
        row = await self.db.pool.fetchrow(
            """
            UPDATE user_states SET state = $3, updated_at = now()
            WHERE user_id = $1 AND state = $2
              AND updated_at > now() - make_interval(mins => $4::int)
            RETURNING data
            """,
            user_id, from_state, to_state, self.ttl,
        )
        return row["data"] if row else None

    async def clear_state(self, user_id: int) -> None:
        await self.db.pool.execute("DELETE FROM user_states WHERE user_id = $1", user_id)

    async def reset_delivering(self) -> None:
        """After a restart, no delivery is actually running any more."""
        await self.db.pool.execute("DELETE FROM user_states WHERE state = 'delivering'")

    # ---------- request lock, rate limit, one-time notice (upgrade) ----------
    async def acquire_delivery_lock(self, user_id: int) -> bool:
        """Atomically mark the user as 'delivering'. False = a request is already running.
        A lock older than 10 minutes is treated as dead (it cannot outlive a delivery)."""
        row = await self.db.pool.fetchval(
            """
            INSERT INTO user_states (user_id, state, data) VALUES ($1, 'delivering', '{}'::jsonb)
            ON CONFLICT (user_id) DO UPDATE
            SET state = 'delivering', data = '{}'::jsonb, updated_at = now()
            WHERE user_states.state <> 'delivering'
               OR user_states.updated_at < now() - interval '10 minutes'
            RETURNING 1
            """,
            user_id,
        )
        return row is not None

    async def refresh_delivery_lock(self, user_id: int) -> None:
        """Heartbeat while a delivery runs, so a long batch is never mistaken for a dead lock."""
        await self.db.pool.execute(
            "UPDATE user_states SET updated_at = now() "
            "WHERE user_id = $1 AND state = 'delivering'", user_id)

    # ---------- per-user delivery session (mode, position, progress) ----------
    async def start_session(self, user_id: int, mode: str) -> None:
        """Record the mode the user chose. Touches only this user's row."""
        await self.db.pool.execute(
            """
            INSERT INTO user_delivery_sessions (user_id, mode) VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET mode = EXCLUDED.mode, updated_at = now()
            """,
            user_id, mode)

    async def advance_session(self, user_id: int, mode: str, video_id: int, position) -> None:
        """Move this user's position IN THIS MODE to the video just delivered (per-user rows)."""
        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO user_mode_progress
                        (user_id, mode, last_video_id, last_position, delivered_total)
                    VALUES ($1, $2, $3, $4, 1)
                    ON CONFLICT (user_id, mode) DO UPDATE SET
                        last_video_id = EXCLUDED.last_video_id,
                        last_position = EXCLUDED.last_position,
                        delivered_total = user_mode_progress.delivered_total + 1,
                        updated_at = now()
                    """, user_id, mode, video_id, position)
                await conn.execute(
                    """
                    INSERT INTO user_delivery_sessions
                        (user_id, mode, last_video_id, last_position, delivered_total)
                    VALUES ($1, $2, $3, $4, 1)
                    ON CONFLICT (user_id) DO UPDATE SET
                        mode = EXCLUDED.mode, last_video_id = EXCLUDED.last_video_id,
                        last_position = EXCLUDED.last_position,
                        delivered_total = user_delivery_sessions.delivered_total + 1,
                        updated_at = now()
                    """, user_id, mode, video_id, position)

    async def finish_batch(self, user_id: int, mode: str) -> None:
        await self.db.pool.execute(
            "UPDATE user_mode_progress SET batches = batches + 1, updated_at = now() "
            "WHERE user_id = $1 AND mode = $2", user_id, mode)
        await self.db.pool.execute(
            "UPDATE user_delivery_sessions SET batches = batches + 1, updated_at = now() "
            "WHERE user_id = $1", user_id)

    async def get_session(self, user_id: int):
        return await self.db.pool.fetchrow(
            "SELECT * FROM user_delivery_sessions WHERE user_id = $1", user_id)

    async def hit_rate_limit(self, user_id: int, max_hits: int, window_seconds: int) -> bool:
        """Count one request in a fixed window. True = allowed, False = limit exceeded."""
        hits = await self.db.pool.fetchval(
            """
            INSERT INTO user_rate_limits (user_id, window_start, hits) VALUES ($1, now(), 1)
            ON CONFLICT (user_id) DO UPDATE SET
                hits = CASE WHEN user_rate_limits.window_start
                                 <= now() - make_interval(secs => $2::double precision)
                            THEN 1 ELSE user_rate_limits.hits + 1 END,
                window_start = CASE WHEN user_rate_limits.window_start
                                         <= now() - make_interval(secs => $2::double precision)
                                    THEN now() ELSE user_rate_limits.window_start END
            RETURNING hits
            """,
            user_id, float(window_seconds),
        )
        return hits <= max_hits

    async def claim_delete_notice(self, user_id: int) -> bool:
        """True exactly once per user: the first batch carries the auto-delete notice."""
        row = await self.db.pool.fetchval(
            "UPDATE users SET delete_notice_shown = TRUE "
            "WHERE user_id = $1 AND NOT delete_notice_shown RETURNING 1",
            user_id,
        )
        return row is not None

    # ---------- admin block system (users.is_banned, persistent) ----------
    async def load_banned(self) -> None:
        rows = await self.db.pool.fetch("SELECT user_id, ban_reason FROM users WHERE is_banned")
        self.banned_ids = {r["user_id"] for r in rows}
        self.ban_reasons = {r["user_id"]: r["ban_reason"] for r in rows if r["ban_reason"]}

    def is_banned(self, user_id: int) -> bool:
        return user_id in self.banned_ids

    # ---------- 18+ confirmation (users.age_verified_at) ----------
    async def load_age_verified(self) -> None:
        rows = await self.db.pool.fetch(
            "SELECT user_id FROM users WHERE age_verified_at IS NOT NULL")
        self.age_verified_ids = {r["user_id"] for r in rows}

    def is_age_verified(self, user_id: int) -> bool:
        return user_id in self.age_verified_ids

    async def set_age_verified(self, user_id: int) -> None:
        """Remember that the user pressed "I am 18+". The user row must already exist."""
        await self.db.pool.execute(
            "UPDATE users SET age_verified_at = COALESCE(age_verified_at, now()) "
            "WHERE user_id = $1", user_id)
        self.age_verified_ids.add(user_id)

    async def age_verified_count(self) -> int:
        return len(self.age_verified_ids)

    # ---------- per-user Save / Forward permission (users.allow_save_forward) ----------
    # Everybody is protected (no save / no forward) until an admin allows it for that user.
    async def load_save_allowed(self) -> None:
        rows = await self.db.pool.fetch("SELECT user_id FROM users WHERE allow_save_forward")
        self.save_allowed_ids = {r["user_id"] for r in rows}

    def is_save_allowed(self, user_id: int) -> bool:
        return user_id in self.save_allowed_ids

    def protect_for(self, user_id: int) -> bool:
        """True = videos sent to this user carry Telegram content protection."""
        return user_id not in self.save_allowed_ids

    async def set_save_allowed(self, user_id: int, allowed: bool) -> None:
        """Works for users the bot has never seen too, so the setting is in place on arrival."""
        await self.db.pool.execute(
            "INSERT INTO users (user_id, allow_save_forward) VALUES ($1, $2) "
            "ON CONFLICT (user_id) DO UPDATE SET allow_save_forward = EXCLUDED.allow_save_forward",
            user_id, allowed)
        (self.save_allowed_ids.add if allowed else self.save_allowed_ids.discard)(user_id)

    def ban_reason(self, user_id: int) -> str | None:
        return self.ban_reasons.get(user_id)

    async def set_banned(self, user_id: int, banned: bool, reason: str | None = None) -> None:
        """Works for users the bot has never seen too, so the block is in place when they arrive.
        `reason` is stored with the block and cleared again on unblock."""
        reason = (reason or "").strip() or None
        if not banned:
            reason = None
        await self.db.pool.execute(
            """
            INSERT INTO users (user_id, is_banned, banned_at, ban_reason)
            VALUES ($1, $2, CASE WHEN $2 THEN now() END, $3::text)
            ON CONFLICT (user_id) DO UPDATE
            SET is_banned = EXCLUDED.is_banned, banned_at = EXCLUDED.banned_at,
                ban_reason = EXCLUDED.ban_reason
            """,
            user_id, banned, reason)
        (self.banned_ids.add if banned else self.banned_ids.discard)(user_id)
        if reason:
            self.ban_reasons[user_id] = reason
        else:
            self.ban_reasons.pop(user_id, None)

    async def set_ban_reason(self, user_id: int, reason: str | None) -> bool:
        """Change the reason of an existing admin block (keeps banned_at). False if not blocked."""
        if user_id not in self.banned_ids:
            return False
        reason = (reason or "").strip() or None
        await self.db.pool.execute(
            "UPDATE users SET ban_reason = $2::text WHERE user_id = $1 AND is_banned",
            user_id, reason)
        if reason:
            self.ban_reasons[user_id] = reason
        else:
            self.ban_reasons.pop(user_id, None)
        return True

    async def touch(self, user_id: int) -> None:
        """Record activity (last_seen) for an existing user."""
        await self.db.pool.execute(
            "UPDATE users SET last_seen = now() WHERE user_id = $1", user_id)

    # ---------- admin: safe user information ----------
    _PROFILE_SQL = """
        SELECT u.user_id, u.first_name, u.username, u.first_seen, u.last_seen, u.is_banned,
               u.is_blocked, u.ban_reason, u.allow_save_forward,
               (SELECT COUNT(*) FROM user_video_history h WHERE h.user_id = u.user_id) AS videos,
               (SELECT COUNT(*) FROM requests r WHERE r.user_id = u.user_id) AS requests,
               (SELECT COUNT(*) FROM referrals rf WHERE rf.referrer_id = u.user_id
                  AND rf.status = 'qualified') AS referred
        FROM users u
    """

    async def profile(self, user_id: int):
        return await self.db.pool.fetchrow(self._PROFILE_SQL + " WHERE u.user_id = $1", user_id)

    async def find(self, text: str):
        """Look a user up by Telegram ID or @username (only users who wrote to the bot exist)."""
        text = text.strip()
        if text.lstrip("-").isdigit():
            return await self.profile(int(text))
        name = text.lstrip("@")
        if not name:
            return None
        return await self.db.pool.fetchrow(
            self._PROFILE_SQL + " WHERE lower(u.username) = lower($1) "
            "ORDER BY u.last_seen DESC LIMIT 1", name)

    async def recent(self, limit: int, offset: int = 0) -> tuple[list, int]:
        rows = await self.db.pool.fetch(
            self._PROFILE_SQL + " ORDER BY u.last_seen DESC LIMIT $1 OFFSET $2", limit, offset)
        total = await self.db.pool.fetchval("SELECT COUNT(*) FROM users")
        return rows, total

    async def user_numbers(self) -> dict:
        r = await self.db.pool.fetchrow(
            "SELECT COUNT(*) AS total, "
            "COUNT(*) FILTER (WHERE last_seen > now() - interval '5 minutes') AS online, "
            "COUNT(*) FILTER (WHERE last_seen > now() - interval '24 hours') AS d1, "
            "COUNT(*) FILTER (WHERE last_seen > now() - interval '7 days') AS d7, "
            "COUNT(*) FILTER (WHERE first_seen > now() - interval '24 hours') AS new_1d, "
            "COUNT(*) FILTER (WHERE first_seen > now() - interval '7 days') AS new_7d, "
            "COUNT(*) FILTER (WHERE is_banned) AS banned, "
            "COUNT(*) FILTER (WHERE is_blocked) AS left_bot FROM users")
        return dict(r)
