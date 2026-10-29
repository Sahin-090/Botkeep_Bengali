"""End-to-end test of the referral system against a REAL PostgreSQL.
   DATABASE_URL=postgresql://t:t@localhost/tdb python tests/test_referrals.py"""
import asyncio, os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendVideo
from aiogram.types import CallbackQuery, Chat, Message, User

import ui
from config import Config
from database import Database
from handlers import referral as ref_ui
from handlers.admin_panel import Panel
from services.referral_service import ReferralCapture, ReferralService, parse_ref_payload
from services.settings_service import SettingsService
from services.user_service import UserService

ADMIN = 1
def ok(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: sys.exit(1)

class FakeBot:
    def __init__(s):
        s.videos, s.messages, s.fail_on, s.block = [], [], set(), False
        s._mid = 1000
    async def get_me(s): return types.SimpleNamespace(id=99, username="VideoShareProBot")
    async def send_message(s, chat_id, text, **kw): s.messages.append((chat_id, text))
    async def send_video(s, chat_id, file_id, **kw):
        if s.block: raise TelegramForbiddenError(SendVideo(chat_id=1, video="x"), "bot was blocked by the user")
        if file_id in s.fail_on: raise RuntimeError("boom")   # unexpected error -> treated as transient? see below
        s._mid += 1; s.videos.append((chat_id, file_id, kw.get("caption"))); return types.SimpleNamespace(message_id=s._mid)

def U(uid, name="U"): return types.SimpleNamespace(id=uid, username=f"u{uid}", first_name=f"{name}{uid}", is_bot=False)
def V(n): return types.SimpleNamespace(file_id=f"f{n}", file_unique_id=f"u{n}", width=1, height=1, duration=5)

async def main():
    db = Database(os.environ["DATABASE_URL"], 1, 5); await db.connect(); await db.connect()   # idempotent schema
    for t in ("referral_claim_items", "referral_claims", "referral_reward_videos", "referral_rejections",
              "referrals", "user_states", "bot_settings", "delivered_messages"):
        await db.pool.execute(f"DELETE FROM {t}")
    await db.pool.execute("DELETE FROM users WHERE user_id >= 1")
    cfg = Config(bot_token="t", database_url="x", admin_ids=frozenset({ADMIN}), source_channel_ids=(),
                 channel_auto_category=None, max_videos_per_request=50, old_video_order="asc",
                 send_delay=0.0, protect_content=False, state_ttl_minutes=30, db_pool_min=1, db_pool_max=2,
                 api_id=None, api_hash=None, session_string=None, scan_batch_size=1, scan_progress_every=1,
                 auto_scan_minutes=0, new_video_days=30)
    settings = SettingsService(db, cfg); await settings.load()
    users = UserService(db, 30)
    bot = FakeBot()
    rs = ReferralService(bot, db, cfg, settings, users, None); await rs.start()

    # ---- payload parsing + links
    ok("parse /start ref_123", parse_ref_payload("/start ref_123") == 123)
    ok("parse with bot name", parse_ref_payload("/start@VideoShareProBot ref_77") == 77)
    ok("parse ignores other payloads", parse_ref_payload("/start") is None and parse_ref_payload("/start abc") is None
       and parse_ref_payload("/start ref_12x") is None and parse_ref_payload("hello ref_5") is None)
    ok("personal link format", rs.link_for(123456) == "https://t.me/VideoShareProBot?start=ref_123456")

    # ---- the referrer must be a real bot user
    R = 100
    ok("unknown referrer rejected", await rs.capture(U(200), R) == "invalid_referrer")
    await users.upsert(U(R))
    # ---- anti-fraud: self, duplicate, existing user
    ok("self referral rejected", await rs.capture(U(R), R) == "self_referral")
    ok("brand-new user -> pending", await rs.capture(U(201), R) == "pending")
    s = await rs.summary(R); ok("pending is not counted yet", s.eligible == 0 and s.pending == 1)
    ok("same user + same link again -> duplicate", await rs.capture(U(201), R) == "duplicate")
    await users.upsert(U(300))   # another referrer
    ok("same user + another link -> duplicate", await rs.capture(U(201), 300) == "duplicate")
    await users.upsert(U(202))
    ok("existing user cannot be referred", await rs.capture(U(202), R) == "already_user")
    ok("bot accounts ignored", await rs.capture(types.SimpleNamespace(id=203, is_bot=True), R) == "bot")
    # ---- qualification happens when the bot lets him in (first users row)
    new = await users.upsert(U(201)); ok("upsert reports a new user", new is True)
    await asyncio.sleep(0.2)
    s = await rs.summary(R); ok("qualified after first upsert", s.eligible == 1 and s.pending == 0)
    ok("referrer was notified", any(c == R and "New referral" in t for c, t in bot.messages))
    ok("second upsert is not new and not counted twice", (await users.upsert(U(201))) is False
       and (await rs.summary(R)).eligible == 1)
    rej = {r["reason"]: r["n"] for r in await db.pool.fetch("SELECT reason, COUNT(*) n FROM referral_rejections GROUP BY reason")}
    ok("rejections recorded", rej.get("self_referral") == 1 and rej.get("duplicate", 0) >= 1 and rej.get("already_user") == 1)

    # ---- banned referred users are not counted; banned/unknown referrers cannot be used
    await rs.capture(U(210), R); await users.upsert(U(210)); await asyncio.sleep(0.1)
    ok("2 eligible", (await rs.summary(R)).eligible == 2)
    await users.set_banned(210, True); ok("banned referred user not counted", (await rs.summary(R)).eligible == 1)
    await users.set_banned(210, False)
    await users.set_banned(300, True); ok("banned referrer rejected", await rs.capture(U(211), 300) == "invalid_referrer")
    await users.set_banned(300, False)

    # ---- middleware records before gates, never blocks
    calls = []
    async def handler(e, d): calls.append(e.text); return "handled"
    mw = ReferralCapture(rs)
    m = Message.model_construct(from_user=User.model_construct(id=220, is_bot=False, first_name="x"),
                                chat=Chat.model_construct(id=220, type="private"), text="/start ref_%d" % R)
    ok("middleware passes the update on", await mw(handler, m, {}) == "handled" and calls == [m.text])
    ok("middleware recorded pending referral", await db.pool.fetchval("SELECT status FROM referrals WHERE referred_id=220") == "pending")
    g = Message.model_construct(from_user=User.model_construct(id=221, is_bot=False, first_name="x"),
                                chat=Chat.model_construct(id=221, type="group"), text="/start ref_%d" % R)
    await mw(handler, g, {}); ok("groups ignored", await db.pool.fetchval("SELECT 1 FROM referrals WHERE referred_id=221") is None)
    await users.upsert(U(220)); await asyncio.sleep(0.1)       # e.g. after Force Subscribe verify
    ok("pending -> eligible after he gets in", (await rs.summary(R)).eligible == 3)

    # ---- reward unlock at 20, claim flow
    ok("locked below target", (await rs.claim(R, R)).status == "locked")
    for i in range(17):
        await rs.capture(U(1000 + i), R); await users.upsert(U(1000 + i))
    await asyncio.sleep(0.3)
    s = await rs.summary(R); ok("20 eligible -> reward unlocked", s.eligible == 20 and s.unlocked == 1 and s.ready == 1)
    ok("unlock message sent to referrer", any("Reward unlocked" in t for c, t in bot.messages if c == R))
    ok("no reward videos yet -> no_videos (nothing consumed)", (await rs.claim(R, R)).status == "no_videos"
       and await db.pool.fetchval("SELECT COUNT(*) FROM referral_claims") == 0)
    for i in range(1, 6): ok(f"add english {i}", await rs.add_reward_video("english", V(i), ADMIN))
    ok("duplicate reward video refused", not await rs.add_reward_video("english", V(1), ADMIN))
    for i in range(11, 14): await rs.add_reward_video("desi", V(i), ADMIN)
    ok("still short of 5 desi -> no_videos", (await rs.claim(R, R)).status == "no_videos")
    for i in range(14, 17): await rs.add_reward_video("desi", V(i), ADMIN)
    r = await asyncio.gather(rs.claim(R, R), rs.claim(R, R))     # double tap
    ok("double tap: exactly one delivery", sorted(x.status for x in r) == ["busy", "ok"] and len(bot.videos) == 10)
    ok("5 english + 5 desi", sum(1 for v in bot.videos if v[1] in {f"f{i}" for i in range(1, 6)}) == 5
       and sum(1 for v in bot.videos if v[1] in {f"f{i}" for i in range(11, 17)}) == 5)
    ok("captions label the pools", sum("English" in v[2] for v in bot.videos) == 5 and sum("Desi" in v[2] for v in bot.videos) == 5)
    ok("rewards queued for auto-delete", await db.pool.fetchval("SELECT COUNT(*) FROM delivered_messages WHERE user_id=$1", R) == 10)
    s = await rs.summary(R); ok("claimed", s.claimed == 1 and s.ready == 0 and s.next_in == 0)
    ok("cannot claim again (one-time)", (await rs.claim(R, R)).status == "locked" and len(bot.videos) == 10)

    # ---- repeat mode: next reward at 40, never the same video twice
    for i in range(20):
        await rs.capture(U(2000 + i), R); await users.upsert(U(2000 + i))
    await asyncio.sleep(0.3)
    ok("one-time mode: 40 referrals still no second reward", (await rs.summary(R)).ready == 0)
    await settings.set_bool("ref_repeat", True)
    s = await rs.summary(R); ok("repeat ON: second reward unlocked", s.eligible == 40 and s.unlocked == 2 and s.ready == 1)
    ok("second reward but pools are used up -> no_videos", (await rs.claim(R, R)).status == "no_videos")
    for i in range(21, 26): await rs.add_reward_video("english", V(i), ADMIN)
    for i in range(31, 36): await rs.add_reward_video("desi", V(i), ADMIN)
    bot.videos.clear(); ok("second claim ok", (await rs.claim(R, R)).status == "ok")
    first = {f"f{i}" for i in list(range(1, 6)) + list(range(11, 16))}   # what claim 1 sent (f16 was never sent)
    ok("no video repeated for the same user", not any(v[1] in first for v in bot.videos) and len(bot.videos) == 10)

    # ---- partial delivery resumes without resending
    await db.pool.execute("DELETE FROM referral_claim_items; DELETE FROM referral_claims; DELETE FROM referral_reward_videos")
    await settings.set_bool("ref_repeat", False)
    for i in range(1, 6): await rs.add_reward_video("english", V(i), ADMIN)
    for i in range(11, 16): await rs.add_reward_video("desi", V(i), ADMIN)
    bot.videos.clear(); bot.fail_on = {"f12"}
    # RuntimeError is an unexpected error: the claim must stay resumable
    import services.referral_service as RS
    orig = RS.ReferralService._send_reward
    async def flaky(self, chat_id, it, position, protect):
        if it["telegram_file_id"] in bot.fail_on: return "error", None
        return await orig(self, chat_id, it, position, protect)
    RS.ReferralService._send_reward = flaky
    r = await rs.claim(R, R); ok("transient failure -> partial", r.status == "partial" and r.sent == 9)
    ok("claim stays pending", await db.pool.fetchval("SELECT status FROM referral_claims") == "pending")
    bot.fail_on = set(); before = len(bot.videos)
    r = await rs.claim(R, R); ok("retry finishes only the missing video", r.status == "ok" and len(bot.videos) == before + 1 and bot.videos[-1][1] == "f12")
    RS.ReferralService._send_reward = orig
    ok("claim marked done", await db.pool.fetchval("SELECT status FROM referral_claims") == "done")

    # ---- user blocked the bot / banned user / disabled
    await users.upsert(U(500)); 
    ok("banned referrer cannot claim", (await (users.set_banned(R, True) or asyncio.sleep(0)), (await rs.claim(R, R)).status)[1] == "banned")
    await users.set_banned(R, False)
    await settings.set_bool("ref_enabled", False)
    ok("disabled system: no capture, no claim", await rs.capture(U(600), R) == "disabled" and (await rs.claim(R, R)).status == "disabled")
    await settings.set_bool("ref_enabled", True)

    # ---- user texts / keyboards
    s = await rs.summary(R)
    t, kb = await ref_ui.page_refer(R, rs, cfg)
    ok("/refer shows personal link and share button", f"start=ref_{R}" in t and any(b.url and "t.me/share/url" in b.url for r in kb.inline_keyboard for b in r))
    t, kb = await ref_ui.page_stats(R, rs); ok("/stats text", "YOUR REFERRAL STATS" in t and "40" in t)
    await users.upsert(U(700)); t, kb = await ref_ui.page_rewards(700, rs)
    ok("/rewards locked for a user with no referrals", "Locked" in t and "ref:claim" not in [b.callback_data for r in kb.inline_keyboard for b in r])
    ok("main menu has Refer & Earn", "ref:home" in [b.callback_data for r in ui.main_menu_kb(settings).inline_keyboard for b in r])
    await settings.set_bool("ref_enabled", False)
    ok("button hidden when system is OFF", "ref:home" not in [b.callback_data for r in ui.main_menu_kb(settings).inline_keyboard for b in r])
    await settings.set_bool("ref_enabled", True)

    # ---- admin panel
    class FMsg(Message):
        async def answer(self, text, **kw): self.out.append((text, kw.get("reply_markup")))
        async def edit_text(self, text, **kw): self.out.append((text, kw.get("reply_markup")))
    class FCb(CallbackQuery):
        async def answer(self, text=None, show_alert=None, **kw): self.alerts.append(text)
    def mk_msg(uid, text="x", video=None):
        m = FMsg.model_construct(from_user=User.model_construct(id=uid, is_bot=False), chat=Chat.model_construct(id=uid, type="private"), text=text, video=video)
        object.__setattr__(m, "out", []); return m
    def mk_cb(uid, data):
        host = mk_msg(uid); cb = FCb.model_construct(from_user=User.model_construct(id=uid, is_bot=False), data=data, message=host)
        object.__setattr__(cb, "alerts", []); object.__setattr__(cb, "out", host.out); return cb
    def kbdata(markup): return [b.callback_data for r in markup.inline_keyboard for b in r]
    panel = object.__new__(Panel)
    panel.bot, panel.cfg, panel.db, panel.users = bot, cfg, db, users
    panel.force_sub, panel._fsub_health, panel._scan_msg, panel._tasks = None, {}, None, set()
    panel.referrals, panel.settings = rs, settings
    class Ev:
        async def record(*a, **k): pass
    panel.events = Ev()
    class Videos:
        async def dashboard(s): return dict(total=0, users=0, deliveries=0, new=0, old=0)
    panel.videos = Videos()
    text, kb = await panel.page("adm:home"); ok("dashboard has Referral Manager", "adm:ref" in kbdata(kb))
    text, kb = await panel.page("adm:ref")
    ok("overview numbers", "REFERRAL MANAGER" in text and "Eligible referrals" in text and "adm:ref:top:1" in kbdata(kb))
    text, kb = await panel.page("adm:ref:top:1")
    ok("ranking shows who referred how many", "TOP REFERRERS" in text and f"U{R}" in text and "<b>40</b> eligible" in text)
    text, kb = await panel.page(f"adm:ref:user:{R}")
    ok("per-user page lists referred users", "40" in text and "u2019" in text or "U2019" in text)
    t0, _ = await panel.page("adm:usr:view:%d" % R); ok("user card shows users referred", "Users referred: <b>40</b>" in t0)
    text, kb = await panel.page("adm:ref:vids"); ok("reward videos page", "English: <b>5</b>" in text and "Desi: <b>5</b>" in text)
    text, kb = await panel.page("adm:ref:vlist:english:1"); ok("video list with delete buttons", any(d.startswith("adm:ref:vdel:") for d in kbdata(kb)))
    vid = await db.pool.fetchval("SELECT id FROM referral_reward_videos WHERE pool='english' ORDER BY id LIMIT 1")
    await panel.handle_callback(mk_cb(ADMIN, f"adm:ref:vdelok:{vid}")); ok("remove reward video", (await rs.pool_counts())["english"] == 4)
    # settings
    await panel.handle_callback(mk_cb(ADMIN, "adm:ref:tgl:repeat")); ok("toggle repeat", rs.repeat is True)
    await panel.handle_callback(mk_cb(ADMIN, "adm:ref:edit:target")); m = mk_msg(ADMIN, "abc"); await panel.on_input(m)
    ok("target validation", "whole number" in m.out[0][0] and rs.target == 20)
    m = mk_msg(ADMIN, "25"); await panel.on_input(m); ok("target saved (persisted, applies at once)", rs.target == 25 and settings.get("ref_target") == "25")
    await panel.handle_callback(mk_cb(ADMIN, "adm:ref:edit:target")); await panel.on_input(mk_msg(ADMIN, "20"))
    await settings.set_bool("ref_repeat", False)
    # add reward videos through the panel
    await panel.handle_callback(mk_cb(ADMIN, "adm:ref:add:desi")); st, d = await users.get_state(ADMIN)
    ok("add-video state", st == "admin_input" and d["kind"] == "refvid" and d["pool"] == "desi")
    m = mk_msg(ADMIN, None, video=V(99)); await panel.on_input_video(m); ok("video saved to pool", "Saved to" in m.out[0][0] and (await rs.pool_counts())["desi"] == 6)
    m = mk_msg(ADMIN, None, video=V(99)); await panel.on_input_video(m); ok("same video refused", "already" in m.out[0][0])
    await panel.handle_callback(mk_cb(ADMIN, "adm:ref:done")); ok("done clears state", (await users.get_state(ADMIN))[0] != "admin_input")
    # find user by @username and /referrals
    await panel.handle_callback(mk_cb(ADMIN, "adm:ref:find")); m = mk_msg(ADMIN, f"@u{R}"); await panel.on_input(m)
    ok("find user by @username", "REFERRALS OF" in m.out[0][0])
    m = mk_msg(ADMIN); await panel.open_referrals(m, str(R)); ok("/referrals <id>", "REFERRALS OF" in m.out[0][0])
    # non-admin can never reach the panel
    c = mk_cb(5, "adm:ref:tgl:enabled"); await panel.handle_callback(c); ok("non-admin refused", c.alerts and rs.enabled)

    print("ALL REFERRAL TESTS PASSED")
    await db.close()

asyncio.run(main())
