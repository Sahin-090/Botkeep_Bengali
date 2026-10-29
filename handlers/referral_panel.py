"""📊 Referral Manager - a section of the Admin Panel (mixin for handlers.admin_panel.Panel).

Like the Force Subscribe Manager it plugs into the panel's own machinery (callback routing
`adm:ref:...`, the database-backed text-input flow, the admin-only server-side checks), so every
action here is already restricted to ADMIN_IDS before any method runs.

Pages     adm:ref                         overview + menu
          adm:ref:top:<page>              🏆 who referred how many (ranking)
          adm:ref:user:<id>               one user: eligible / pending / rewards + his referrals
          adm:ref:vids                    🎬 reward videos (English / Desi)
          adm:ref:vlist:<pool>:<page>     list of one pool, with delete buttons
          adm:ref:vdel:<id>               remove confirmation
          adm:ref:set                     ⚙️ settings
Actions   adm:ref:find | add:<pool> | done | vdelok:<id> | tgl:<key> | edit:<key>
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone

from aiogram.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup as Kb, Message

import ui
from services.referral_service import POOL_LABEL, POOLS

log = logging.getLogger("referral_panel")
LINE = ui.LINE
esc = html.escape
PAGE_TOP = 8
PAGE_VIDS = 8
IST = timezone(timedelta(hours=5, minutes=30), "IST")
REASON_LABEL = {"self_referral": "🙅 Own link used", "already_user": "👤 Already a user",
                "duplicate": "♻️ Duplicate / second link", "invalid_referrer": "❓ Unknown/blocked referrer"}


def _n(v) -> str:
    return f"{int(v or 0):,}"


def _when(dt: datetime | None) -> str:
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"{dt.astimezone(IST):%Y-%m-%d %I:%M %p}"


def _kb(*rows: list[B], back: str = "adm:ref") -> Kb:
    return Kb(inline_keyboard=[*rows, [B(text="⬅️ Back", callback_data=back),
                                       B(text="🏠 Dashboard", callback_data="adm:home")]])


def _who(first_name: str | None, username: str | None, user_id: int) -> str:
    name = esc((first_name or "User")[:24])
    return f"{name} (@{esc(username)})" if username else f"{name}"


class ReferralPanelMixin:
    referrals = None   # ReferralService, set by Panel.__init__

    # ------------------------------------------------------------------ pages
    async def p_ref(self, a: list[str]) -> tuple[str, Kb]:
        rs = self.referrals
        if rs is None:
            return "⚠️ The referral system is not available.", _kb(back="adm:home")
        sub = a[0] if a else ""
        if sub == "top":
            return await self._p_ref_top(int(a[1]) if len(a) > 1 and a[1].isdigit() else 1)
        if sub == "user" and len(a) > 1 and re.fullmatch(r"-?\d+", a[1]):
            return await self._p_ref_user(int(a[1]))
        if sub == "vids":
            return await self._p_ref_vids()
        if sub == "vlist" and len(a) > 1 and a[1] in POOLS:
            return await self._p_ref_vlist(a[1], int(a[2]) if len(a) > 2 and a[2].isdigit() else 1)
        if sub == "vdel" and len(a) > 1 and a[1].isdigit():
            return await self._p_ref_vdel(int(a[1]))
        if sub == "set":
            return self._p_ref_set()
        return await self._p_ref_home()

    async def _p_ref_home(self) -> tuple[str, Kb]:
        rs = self.referrals
        o = await rs.overview()
        status = "🟢 ON" if rs.enabled else "🔴 OFF"
        half = (f" · half-delivered: <b>{_n(o['claims_pending'])}</b>"
                if o["claims_pending"] else "")
        rej = "\n".join(f"   {REASON_LABEL.get(k, k)}: <b>{_n(v)}</b>"
                        for k, v in o["rejections"].items()) or "   none"
        text = (f"📊 <b>REFERRAL MANAGER</b>\n\n{LINE}\n\n"
                f"Status: <b>{status}</b>\n"
                f"🎯 Reward at: <b>{rs.target}</b> eligible referrals"
                f"{' (repeats)' if rs.repeat else ''}\n\n"
                f"👥 Eligible referrals: <b>{_n(o['eligible'])}</b>\n"
                f"🧑‍🤝‍🧑 Users who referred: <b>{_n(o['referrers'])}</b>\n"
                f"⏳ Waiting (link opened, not joined): <b>{_n(o['pending'])}</b>\n"
                f"🎁 Rewards claimed: <b>{_n(o['claims_done'])}</b>{half}\n"
                f"🎬 Reward videos: 🇬🇧 <b>{o['pools']['english']}</b> · 🇮🇳 <b>{o['pools']['desi']}</b>\n\n"
                f"🛡️ Blocked fraud attempts (7 days):\n{rej}\n\n{LINE}")
        rows = [
            [B(text="🏆 Top Referrers", callback_data="adm:ref:top:1"),
             B(text="🔎 Find User", callback_data="adm:ref:find")],
            [B(text="🎬 Reward Videos", callback_data="adm:ref:vids"),
             B(text="⚙️ Settings", callback_data="adm:ref:set")],
        ]
        return text, _kb(*rows, back="adm:home")

    async def _p_ref_top(self, page: int) -> tuple[str, Kb]:
        rs = self.referrals
        page = max(1, page)
        rows, total = await rs.top(PAGE_TOP, (page - 1) * PAGE_TOP)
        pages = max(1, (total + PAGE_TOP - 1) // PAGE_TOP)
        if page > pages:
            page = pages
            rows, total = await rs.top(PAGE_TOP, (page - 1) * PAGE_TOP)
        if not rows:
            return (f"🏆 <b>TOP REFERRERS</b>\n\nNobody has referred an eligible user yet.",
                    _kb())
        base = (page - 1) * PAGE_TOP
        lines = [f"{base + i}. <b>{_who(r['first_name'], r['username'], r['user_id'])}</b> · "
                 f"<code>{r['user_id']}</code>\n    👥 <b>{_n(r['eligible'])}</b> eligible"
                 + (f" · ⏳ {_n(r['pending'])} waiting" if r["pending"] else "")
                 for i, r in enumerate(rows, 1)]
        text = (f"🏆 <b>TOP REFERRERS</b>\n\n{LINE}\n\n" + "\n\n".join(lines) +
                f"\n\n{LINE}\nPage {page}/{pages} · {_n(total)} referrers\n"
                "Tap a name to see everyone he referred.")
        buttons = [[B(text=f"{base + i}. {(r['first_name'] or 'User')[:16]} · {r['eligible']}",
                      callback_data=f"adm:ref:user:{r['user_id']}")] for i, r in enumerate(rows, 1)]
        pager = []
        if page > 1:
            pager.append(B(text="◀️ Prev", callback_data=f"adm:ref:top:{page - 1}"))
        if page < pages:
            pager.append(B(text="Next ▶️", callback_data=f"adm:ref:top:{page + 1}"))
        return text, _kb(*buttons, *([pager] if pager else []))

    async def _p_ref_user(self, user_id: int) -> tuple[str, Kb]:
        rs = self.referrals
        prof = await self.users.profile(user_id)
        if prof is None:
            return "User not found.", _kb()
        s = await rs.summary(user_id)
        referred = await rs.referred_list(user_id, 15)
        by = await rs.referred_by(user_id)
        lines = []
        for r in referred:
            mark = "⛔" if r["is_banned"] else ("✅" if r["status"] == "qualified" else "⏳")
            lines.append(f"{mark} {_who(r['first_name'], r['username'], r['referred_id'])} · "
                         f"<code>{r['referred_id']}</code> · {_when(r['qualified_at'] or r['created_at'])}")
        shown = len(referred)
        total_rows = s.eligible + s.pending
        more = f"\n… and {_n(total_rows - shown)} more" if total_rows > shown else ""
        text = (f"🔗 <b>REFERRALS OF {_who(prof['first_name'], prof['username'], user_id).upper()}</b>\n\n"
                f"🆔 <code>{user_id}</code>\n{LINE}\n\n"
                f"👥 Eligible referrals: <b>{_n(s.eligible)}</b>\n"
                f"⏳ Waiting (not joined yet): <b>{_n(s.pending)}</b>\n"
                f"🎁 Rewards unlocked / claimed: <b>{s.unlocked} / {s.claimed}</b>\n"
                + (f"🔗 Was referred by: <code>{by}</code>\n" if by else "")
                + f"\n{LINE}\n\n"
                + ("\n".join(lines) + more if lines else "He has not referred anyone yet.")
                + "\n\n✅ eligible  ⏳ waiting  ⛔ blocked (not counted)")
        rows = [[B(text="👤 User profile", callback_data=f"adm:usr:view:{user_id}")]]
        return text, _kb(*rows, back="adm:ref:top:1")

    async def _p_ref_vids(self) -> tuple[str, Kb]:
        rs = self.referrals
        c = await rs.pool_counts()
        need = rs.per_pool
        def state(n: int) -> str:
            return "✅" if n >= need else "⚠️ add more"
        text = (f"🎬 <b>REWARD VIDEOS</b>\n\n{LINE}\n\n"
                f"Each reward = <b>{need}</b> English + <b>{need}</b> Desi videos.\n"
                "A user never gets the same reward video twice, so keep more than "
                f"{need} per pool if rewards can repeat.\n\n"
                f"🇬🇧 English: <b>{c['english']}</b> videos  {state(c['english'])}\n"
                f"🇮🇳 Desi: <b>{c['desi']}</b> videos  {state(c['desi'])}\n\n{LINE}\n"
                "Press ➕ Add, then send the videos to the bot (one or many).")
        rows = [
            [B(text="➕ Add English", callback_data="adm:ref:add:english"),
             B(text="➕ Add Desi", callback_data="adm:ref:add:desi")],
            [B(text="📋 English list", callback_data="adm:ref:vlist:english:1"),
             B(text="📋 Desi list", callback_data="adm:ref:vlist:desi:1")],
        ]
        return text, _kb(*rows)

    async def _p_ref_vlist(self, pool: str, page: int) -> tuple[str, Kb]:
        rs = self.referrals
        page = max(1, page)
        rows, total = await rs.reward_videos(pool, PAGE_VIDS, (page - 1) * PAGE_VIDS)
        pages = max(1, (total + PAGE_VIDS - 1) // PAGE_VIDS)
        if page > pages:
            return await self._p_ref_vlist(pool, pages)
        label = POOL_LABEL[pool]
        if not rows:
            return (f"📋 <b>{label} reward videos</b>\n\nNone yet.",
                    _kb([B(text="➕ Add", callback_data=f"adm:ref:add:{pool}")], back="adm:ref:vids"))
        buttons = []
        for r in rows:
            dur = f"{r['duration']}s" if r["duration"] else "?"
            buttons.append([B(text=f"🗑️ #{r['id']} · {dur} · {_when(r['created_at'])[:10]}",
                              callback_data=f"adm:ref:vdel:{r['id']}")])
        pager = []
        if page > 1:
            pager.append(B(text="◀️ Prev", callback_data=f"adm:ref:vlist:{pool}:{page - 1}"))
        if page < pages:
            pager.append(B(text="Next ▶️", callback_data=f"adm:ref:vlist:{pool}:{page + 1}"))
        text = (f"📋 <b>{label} REWARD VIDEOS</b>\n\nPage {page}/{pages} · {_n(total)} videos\n"
                "Tap a video to remove it.")
        return text, _kb(*buttons, *([pager] if pager else []), back="adm:ref:vids")

    async def _p_ref_vdel(self, video_id: int) -> tuple[str, Kb]:
        v = await self.referrals.reward_video(video_id)
        if v is None:
            return "Video not found.", _kb(back="adm:ref:vids")
        text = (f"⚠️ <b>Are you sure?</b>\n\nRemove {POOL_LABEL[v['pool']]} reward video "
                f"<b>#{v['id']}</b>? Users who already received it keep it.")
        return text, Kb(inline_keyboard=[[
            B(text="✅ Confirm", callback_data=f"adm:ref:vdelok:{video_id}"),
            B(text="❌ Cancel", callback_data=f"adm:ref:vlist:{v['pool']}:1")]])

    def _p_ref_set(self) -> tuple[str, Kb]:
        rs = self.referrals
        text = (f"⚙️ <b>REFERRAL SETTINGS</b>\n\n{LINE}\n\n"
                f"System: <b>{'✅ ON' if rs.enabled else '❌ OFF'}</b>\n"
                f"🎯 Referrals needed per reward: <b>{rs.target}</b>\n"
                f"🎬 Videos per pool (English / Desi): <b>{rs.per_pool}</b>\n"
                f"♻️ Repeat rewards: <b>{'✅ ON — a new reward for every further ' + str(rs.target) if rs.repeat else '❌ OFF — one reward per user'}</b>\n\n"
                f"{LINE}\nChanges apply immediately. Rewards already claimed are never affected.")
        rows = [
            [B(text="⏸ Turn OFF" if rs.enabled else "▶️ Turn ON", callback_data="adm:ref:tgl:enabled")],
            [B(text="🎯 Change target", callback_data="adm:ref:edit:target"),
             B(text="🎬 Videos per pool", callback_data="adm:ref:edit:pool")],
            [B(text="♻️ Repeat: ON → OFF" if rs.repeat else "♻️ Repeat: OFF → ON",
               callback_data="adm:ref:tgl:repeat")],
        ]
        return text, _kb(*rows)

    # ------------------------------------------------------------------ callbacks
    async def _r_ref(self, cb: CallbackQuery, a: list[str]) -> None:
        rs = self.referrals
        sub = a[0] if a else ""
        arg = a[1] if len(a) > 1 else ""
        uid = cb.from_user.id
        if rs is None:
            await cb.answer("The referral system is not available.", show_alert=True)
            return
        if sub == "find":
            await self._prompt(cb, "reffind", "Find user",
                               "Send a Telegram user ID or @username to see how many users "
                               "he referred.", "adm:ref")
            return
        if sub == "add" and arg in POOLS:
            ok = await self.users.set_state(uid, "admin_input", {
                "kind": "refvid", "back": "adm:ref:vids", "pool": arg, "count": 0})
            if not ok:
                await cb.answer("One of your video requests is still running. Try again in a "
                                "moment.", show_alert=True)
                return
            await cb.answer()
            await self._show(cb, (f"➕ <b>Add {POOL_LABEL[arg]} reward videos</b>\n\n"
                                  "Send the videos now — one or many. Each one is saved "
                                  "automatically.\n\nPress <b>Done</b> when finished."),
                             Kb(inline_keyboard=[[B(text="✅ Done", callback_data="adm:ref:done")]]))
            return
        if sub == "done":
            await self.users.clear_state(uid)
            await cb.answer("Done")
            text, markup = await self.page("adm:ref:vids")
            await self._show(cb, text, markup)
            return
        if sub == "vdelok" and arg.isdigit():
            if await rs.remove_reward_video(int(arg)):
                await self._log(uid, f"referral reward video {arg} removed")
                await cb.answer("🗑️ Removed")
            else:
                await cb.answer("Video not found.", show_alert=True)
            text, markup = await self.page("adm:ref:vids")
            await self._show(cb, text, markup)
            return
        if sub == "tgl" and arg in ("enabled", "repeat"):
            key = "ref_enabled" if arg == "enabled" else "ref_repeat"
            new = not self.settings.flag(key)
            await self.settings.set_bool(key, new)
            await self._log(uid, f"referral {arg} {'on' if new else 'off'}")
            await cb.answer("Saved")
            text, markup = self._p_ref_set()
            await self._show(cb, text, markup)
            return
        if sub == "edit" and arg in ("target", "pool"):
            if arg == "target":
                await self._prompt(cb, "reftarget", "Referrals needed per reward",
                                   f"Current: <b>{rs.target}</b>\nSend a whole number from 1 to 10000.",
                                   "adm:ref:set")
            else:
                await self._prompt(cb, "refpool", "Reward videos per pool",
                                   f"Current: <b>{rs.per_pool}</b> English + <b>{rs.per_pool}</b> Desi\n"
                                   "Send a whole number from 1 to 20.", "adm:ref:set")
            return
        text, markup = await self.p_ref(a)
        await cb.answer()
        await self._show(cb, text, markup)

    # ------------------------------------------------------------------ text / video input
    async def _i_reffind(self, message: Message, data: dict, text: str) -> None:
        u = await self.users.find(text)
        if u is None:
            await self._retry(message, "No user found. Only people who have written to the bot "
                                       "can be found.")
            return
        await self._finish(message, f"adm:ref:user:{u['user_id']}")

    async def _i_number(self, message: Message, text: str, low: int, high: int, key: str,
                        label: str) -> None:
        if not re.fullmatch(r"\d{1,6}", text) or not low <= int(text) <= high:
            await self._retry(message, f"Send a whole number from {low} to {high}.")
            return
        await self.settings.set(key, str(int(text)))
        await self._log(message.from_user.id, f"referral {label} set to {int(text)}")
        await self._finish(message, "adm:ref:set", "✅ <b>Saved.</b>\n\n")

    async def _i_reftarget(self, message: Message, data: dict, text: str) -> None:
        await self._i_number(message, text, 1, 10000, "ref_target", "target")

    async def _i_refpool(self, message: Message, data: dict, text: str) -> None:
        await self._i_number(message, text, 1, 20, "ref_per_pool", "videos per pool")

    async def on_input_video(self, message: Message) -> None:
        uid = message.from_user.id
        state, data = await self.users.get_state(uid)
        if state != "admin_input" or data.get("kind") != "refvid" or data.get("pool") not in POOLS:
            return
        pool = data["pool"]
        added = await self.referrals.add_reward_video(pool, message.video, uid)
        count = int(data.get("count", 0)) + (1 if added else 0)
        await self.users.set_state(uid, "admin_input", {**data, "count": count})   # keep alive
        c = (await self.referrals.pool_counts())[pool]
        if added:
            await self._log(uid, f"referral {pool} reward video added")
        msg = (f"✅ Saved to {POOL_LABEL[pool]} ({c} total)" if added
               else "⚠️ That video is already in this pool.")
        await self._send(message, msg + "\nSend more, or press Done.",
                         Kb(inline_keyboard=[[B(text="✅ Done", callback_data="adm:ref:done")]]))

    # ------------------------------------------------------------------ /referrals
    async def open_referrals(self, message: Message, arg: str = "") -> None:
        uid = message.from_user.id
        state, _ = await self.users.get_state(uid)
        if state in ("admin_input", "admin_preview"):
            await self.users.clear_state(uid)
        arg = (arg or "").strip()
        if arg:
            u = await self.users.find(arg)
            if u is None:
                await self._send(message, "No user found for that ID / @username.")
                return
            text, markup = await self.page(f"adm:ref:user:{u['user_id']}")
        else:
            text, markup = await self.page("adm:ref")
        await self._send(message, text, markup)
