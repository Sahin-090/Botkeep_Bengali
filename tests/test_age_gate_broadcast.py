"""18+ confirmation, styled buttons and photo / video broadcast.
Real routers + middlewares, a fake Telegram transport and a REAL PostgreSQL.
   DATABASE_URL=postgresql://t:t@localhost/tdb python tests/test_age_gate_broadcast.py"""
import asyncio, datetime as dt, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import (AnswerCallbackQuery, DeleteMessage, EditMessageText, GetMe, SendAnimation,
                             SendMessage, SendPhoto, SendVideo)
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update, User, Video

import ui
from config import Config
from database import Database
from handlers import admin, admin_panel, referral, start, user
from middlewares import AccessGate, AgeGate
from services.broadcast_service import Payload
from services.delivery_service import DeliveryService
from services.event_service import EventService
from services.force_sub_service import ForceSubService
from services.referral_service import ReferralCapture, ReferralService
from services.settings_service import SettingsService
from services.user_service import UserService
from services.video_service import VideoService

ADMIN, NEW, ADULT, ADULT2, BOT_BLOCKED = 1, 301, 302, 303, 304


def ok(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        sys.exit(1)


class Session(BaseSession):
    def __init__(s):
        super().__init__(); s.calls, s._id = [], 7000
    async def close(s): pass
    async def stream_content(s, *a, **k): yield b""
    async def make_request(s, bot, method, timeout=None):
        now = dt.datetime.now(dt.timezone.utc)
        if isinstance(method, GetMe):
            return User(id=99, is_bot=True, first_name="VS", username="VideoShareProBot")
        if isinstance(method, (SendMessage, SendPhoto, SendVideo, SendAnimation, EditMessageText)):
            s._id += 1
            text = getattr(method, "text", None) or getattr(method, "caption", None)
            chat = getattr(method, "chat_id", None)
            s.calls.append((type(method).__name__, chat, text, method))
            return Message(message_id=s._id, date=now, chat=Chat(id=chat or 0, type="private"), text=text)
        if isinstance(method, (DeleteMessage, AnswerCallbackQuery)):
            s.calls.append((type(method).__name__, None, getattr(method, "text", None), method))
        return True


_n = [0]
def U(uid): return User(id=uid, is_bot=False, first_name=f"N{uid}", username=f"u{uid}")
def msg(uid, text):
    _n[0] += 1
    return Update(update_id=_n[0], message=Message(message_id=_n[0], date=dt.datetime.now(dt.timezone.utc),
        chat=Chat(id=uid, type="private"), from_user=U(uid), text=text,
        entities=[{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else None))
def media(uid, kind, caption=None, entities=None):
    _n[0] += 1
    extra = {"photo": [PhotoSize(file_id="PH1", file_unique_id="p", width=9, height=9)]} if kind == "photo" else \
            {"video": Video(file_id="VD1", file_unique_id="v", width=9, height=9, duration=3)}
    return Update(update_id=_n[0], message=Message(message_id=_n[0], date=dt.datetime.now(dt.timezone.utc),
        chat=Chat(id=uid, type="private"), from_user=U(uid), caption=caption, caption_entities=entities, **extra))
def press(uid, data):
    _n[0] += 1
    host = Message(message_id=9000 + _n[0], date=dt.datetime.now(dt.timezone.utc), chat=Chat(id=uid, type="private"),
                   text="menu", from_user=User(id=99, is_bot=True, first_name="VS"))
    return Update(update_id=_n[0], callback_query=CallbackQuery(id=str(_n[0]), chat_instance="c", data=data,
                  from_user=U(uid), message=host))


async def main():
    db = Database(os.environ["DATABASE_URL"], 1, 5); await db.connect(); await db.connect()   # schema is idempotent
    for t in ("referral_claim_items", "referral_claims", "referral_reward_videos", "referral_rejections",
              "referrals", "user_states", "bot_settings", "force_sub_channels", "delivered_messages"):
        await db.pool.execute(f"DELETE FROM {t}")
    await db.pool.execute("DELETE FROM users")
    cfg = Config(bot_token="1:t", database_url="x", admin_ids=frozenset({ADMIN}), source_channel_ids=(), channel_auto_category=None,
                 max_videos_per_request=50, old_video_order="asc", send_delay=0.0, protect_content=False, state_ttl_minutes=30,
                 db_pool_min=1, db_pool_max=2, api_id=None, api_hash=None, session_string=None, scan_batch_size=1,
                 scan_progress_every=1, auto_scan_minutes=0, new_video_days=30)
    session = Session(); bot = Bot("1:AAAA", session=session)
    users = UserService(db, 30); videos = VideoService(db, "asc", 30)
    settings = SettingsService(db, cfg); await settings.load()
    ui.bind(settings, cfg)
    events = EventService(db, cfg); force_sub = ForceSubService(db); await force_sub.load()
    rs = ReferralService(bot, db, cfg, settings, users, events); await rs.start()
    delivery = DeliveryService(bot, db, videos, users, cfg, settings, events)
    panel = admin_panel.Panel(bot, cfg, db, users, videos, None, None, settings, events, force_sub=force_sub, referrals=rs)
    dp = Dispatcher()
    dp.workflow_data.update(cfg=cfg, db=db, users=users, videos=videos, delivery=delivery, settings=settings, events=events,
                            panel=panel, referrals=rs, scanner=None, bridge=None)
    dp.message.outer_middleware(ReferralCapture(rs))
    gate = AccessGate(cfg, settings, users); dp.message.outer_middleware(gate); dp.callback_query.outer_middleware(gate)
    age = AgeGate(cfg, settings, users); dp.message.outer_middleware(age); dp.callback_query.outer_middleware(age)
    for r in (admin_panel.router, admin.router, start.router, referral.router, user.router): dp.include_router(r)
    feed = lambda u: dp.feed_update(bot, u)
    sent = lambda uid, *kinds: [(k, t, m) for k, c, t, m in session.calls if c == uid and (not kinds or k in kinds)]
    texts = lambda uid: [t or "" for _, t, _ in sent(uid)]

    # ================= 18+ gate =================
    ok("age gate is ON by default", settings.flag("age_gate_enabled"))
    await feed(msg(NEW, "/start")); await asyncio.sleep(0.2)
    ok("new user gets the 18+ card, not the welcome",
       any("AGE VERIFICATION" in t for t in texts(NEW)) and not any("WELCOME" in t.upper() for t in texts(NEW)))
    ok("no user row is created before confirming", await db.pool.fetchval("SELECT COUNT(*) FROM users WHERE user_id=$1", NEW) == 0)
    n = len(session.calls)
    await feed(msg(NEW, "5")); await asyncio.sleep(0.2)
    ok("typing a number is also stopped by the gate", any("AGE VERIFICATION" in (t or "") for _, c, t, _ in session.calls[n:] if c == NEW))
    n = len(session.calls)
    await feed(press(NEW, "menu:request")); await asyncio.sleep(0.2)
    ok("a menu button is stopped by the gate", any("AGE VERIFICATION" in (t or "") for _, _, t, _ in session.calls[n:]))
    ok("still not verified", not users.is_age_verified(NEW))

    await feed(press(NEW, "age:no")); await asyncio.sleep(0.2)
    ok("under 18 -> access denied", any("ACCESS DENIED" in (t or "") for _, _, t, _ in session.calls))
    ok("under 18 -> still not verified, nothing stored",
       not users.is_age_verified(NEW) and await db.pool.fetchval("SELECT COUNT(*) FROM users WHERE user_id=$1", NEW) == 0)
    await feed(press(NEW, "age:back")); await asyncio.sleep(0.2)
    ok("'tapped by mistake' returns to the gate", sum("AGE VERIFICATION" in (t or "") for _, _, t, _ in session.calls) >= 4)

    session.calls.clear()
    await feed(press(NEW, "age:yes")); await asyncio.sleep(0.3)
    ok("confirm -> user row created and verified in memory",
       users.is_age_verified(NEW) and await db.pool.fetchval("SELECT COUNT(*) FROM users WHERE user_id=$1", NEW) == 1)
    ok("confirm -> verified in the database",
       await db.pool.fetchval("SELECT age_verified_at IS NOT NULL FROM users WHERE user_id=$1", NEW))
    ok("confirm -> welcome screen with the new greeting", any("WELCOME TO VIDEOSHARE" in t and f"N{NEW}" in t for t in texts(NEW)))
    ok("confirm -> gate card removed", any(k == "DeleteMessage" for k, *_ in session.calls))

    session.calls.clear()
    await feed(msg(NEW, "/start")); await asyncio.sleep(0.2)
    ok("verified user is not asked again", any("WELCOME" in t.upper() for t in texts(NEW)) and not any("AGE VERIFICATION" in t for t in texts(NEW)))

    # verification survives a restart (reloaded from PostgreSQL)
    users.age_verified_ids.clear(); await users.load_age_verified()
    ok("verification is restored after a restart", users.is_age_verified(NEW))

    session.calls.clear()
    await feed(msg(ADMIN, "/start")); await asyncio.sleep(0.2)
    ok("admin skips the gate", any("WELCOME" in t.upper() for t in texts(ADMIN)) and not any("AGE VERIFICATION" in t for t in texts(ADMIN)))

    await settings.set_bool("age_gate_enabled", False)
    session.calls.clear()
    await feed(msg(777, "/start")); await asyncio.sleep(0.2)
    ok("gate switched OFF -> new user goes straight to the welcome", any("WELCOME" in t.upper() for t in texts(777)))
    await settings.set_bool("age_gate_enabled", True)

    # ================= button design =================
    kb = ui.age_gate_kb()
    ok("confirm button uses the callback age:yes", kb.inline_keyboard[0][0].callback_data == "age:yes")
    ok("exit button uses the callback age:no", kb.inline_keyboard[1][0].callback_data == "age:no")
    if ui._STYLE_SUPPORTED:
        ok("confirm is green, exit is red", kb.inline_keyboard[0][0].style == "success" and kb.inline_keyboard[1][0].style == "danger")
        main = ui.main_menu_kb(settings, True)
        ok("Request Videos is the blue primary button", main.inline_keyboard[0][0].style == "primary")
    else:
        print("SKIP coloured buttons (this aiogram has no `style`) - plain buttons are used")
    flat = [b.callback_data for r in ui.main_menu_kb(settings, True).inline_keyboard for b in r]
    ok("main menu keeps every callback", {"menu:request", "menu:new", "menu:old", "menu:more", "activity", "dev", "help", "adm:home"} <= set(flat))
    ok("after-delivery keyboard keeps every callback",
       {"more:new:5", "more:old:5", "fresh:new:5", "again", "dev", "menu"} <=
       {b.callback_data for r in ui.after_delivery_kb("new", 5, settings).inline_keyboard for b in r})

    # ================= broadcast audience =================
    await users.upsert(U(ADULT)); await users.set_age_verified(ADULT)
    await users.upsert(U(ADULT2)); await users.set_age_verified(ADULT2)
    await users.upsert(U(BOT_BLOCKED)); await users.set_age_verified(BOT_BLOCKED); await users.mark_blocked(BOT_BLOCKED)
    await users.upsert(U(500))        # never confirmed
    bc = panel.broadcast
    media_ids = await bc.audience_ids(True)
    text_ids = await bc.audience_ids(False)
    ok("photo/video audience = only users who confirmed 18+ (blocked skipped)", set(media_ids) == {NEW, ADULT, ADULT2})
    ok("text audience includes everybody who can receive", {NEW, ADULT, ADULT2, 500} <= set(text_ids) and BOT_BLOCKED not in text_ids)
    ok("audience count matches", await bc.audience_count(True) == len(media_ids) and await bc.audience_count(False) == len(text_ids))

    # ================= broadcast sending =================
    session.calls.clear()
    assert bc.try_begin(); r = await bc.run(Payload(text="<b>Big news</b>\nhello", media="photo", file_id="PH1")); await asyncio.sleep(0.1)
    photos = [(c, t, m) for k, c, t, m in session.calls if k == "SendPhoto"]
    ok("photo broadcast reached every verified user", {c for c, _, _ in photos} == set(media_ids) and r.sent == len(media_ids))
    ok("unverified user did not get the photo", 500 not in {c for c, _, _ in photos})
    ok("photo uses the stored file_id", all(m.photo == "PH1" for _, _, m in photos))
    ok("caption is the announcement card with the body", all("ANNOUNCEMENT" in t and "Big news" in t and ui.FOOTER in t for _, t, _ in photos))
    ok("card has the Open VideoShare button", all(m.reply_markup.inline_keyboard[0][0].callback_data == "menu" for _, _, m in photos))
    ok("photo is protected (no forward/save) like videos", all(m.protect_content is True for _, _, m in photos))

    session.calls.clear()
    assert bc.try_begin(); r = await bc.run(Payload(text="", media="video", file_id="VD1")); await asyncio.sleep(0.1)
    vids = [(c, t, m) for k, c, t, m in session.calls if k == "SendVideo"]
    ok("video broadcast sent as a streaming video", len(vids) == len(media_ids) and all(m.supports_streaming for _, _, m in vids))
    ok("video without caption still gets a clean card", all("ANNOUNCEMENT" in t for _, t, _ in vids))

    session.calls.clear()
    assert bc.try_begin(); r = await bc.run("plain text announcement"); await asyncio.sleep(0.1)   # old call style
    msgs = [(c, t) for k, c, t, m in session.calls if k == "SendMessage"]
    ok("text broadcast (old call style) reaches everybody", {c for c, _ in msgs} == set(text_ids))

    await settings.set_bool("age_gate_enabled", False)
    ok("gate OFF -> media goes to every active user", set(await bc.audience_ids(True)) == set(text_ids))
    await settings.set_bool("age_gate_enabled", True)

    # ================= admin flow through the real panel =================
    session.calls.clear()
    await feed(press(ADMIN, "adm:bc:new")); await asyncio.sleep(0.2)
    ok("New Broadcast explains photo / video / GIF", any("Photo" in t and "Video" in t and "GIF" in t for t in texts(ADMIN)))
    ent = [{"type": "bold", "offset": 0, "length": 8}]
    await feed(media(ADMIN, "photo", "Weekend!! free drops", ent)); await asyncio.sleep(0.3)
    prev = sent(ADMIN, "SendPhoto")
    ok("admin gets a photo preview of the final card", prev and "<b>Weekend!</b>" in prev[0][1] and "ANNOUNCEMENT" in prev[0][1])
    state, data = await users.get_state(ADMIN)
    ok("preview state keeps media + file_id", state == "admin_preview" and data.get("media") == "photo" and data.get("file_id") == "PH1")
    ok("confirm question names the media type and audience", any("Photo" in t and "18+" in t for t in texts(ADMIN)))

    await feed(press(ADMIN, "adm:bc:test")); await asyncio.sleep(0.2)
    ok("'send test to me' sends the photo to the admin", len(sent(ADMIN, "SendPhoto")) == 2)

    session.calls.clear()
    await feed(press(ADMIN, "adm:bc:go")); await asyncio.sleep(1.5)
    got = {c for k, c, t, m in session.calls if k == "SendPhoto"}
    ok("Send to everyone delivers the photo to all verified users", got == set(await bc.audience_ids(True)))
    ok("final report shown to the admin", any("Broadcast complete" in t for t in texts(ADMIN)))

    # a video with caption
    session.calls.clear()
    await feed(press(ADMIN, "adm:bc:new")); await asyncio.sleep(0.2)
    await feed(media(ADMIN, "video", "New video drop")); await asyncio.sleep(0.3)
    ok("admin gets a video preview", len(sent(ADMIN, "SendVideo")) == 1)
    await feed(press(ADMIN, "adm:bc:go")); await asyncio.sleep(1.5)
    ok("video broadcast delivered to all verified users", {c for k, c, t, m in session.calls if k == "SendVideo"} >= set(await bc.audience_ids(True)))

    # caption too long
    session.calls.clear()
    await feed(press(ADMIN, "adm:bc:new")); await asyncio.sleep(0.2)
    await feed(media(ADMIN, "photo", "x" * 900)); await asyncio.sleep(0.3)
    ok("caption over the limit is refused", any("Too long" in t for t in texts(ADMIN)))
    ok("still waiting for a valid message", (await users.get_state(ADMIN))[0] == "admin_input")

    # a normal user cannot use the media handler
    session.calls.clear()
    await feed(media(ADULT, "photo", "hi")); await asyncio.sleep(0.2)
    ok("a normal user's photo is not turned into a broadcast", not sent(ADULT2) and (await users.get_state(ADULT))[0] != "admin_preview")

    # text broadcast through the panel still works
    await users.clear_state(ADMIN)
    session.calls.clear()
    await feed(press(ADMIN, "adm:bc:new")); await asyncio.sleep(0.2)
    await feed(msg(ADMIN, "Just a text announcement")); await asyncio.sleep(0.3)
    ok("text preview is a normal message", any("ANNOUNCEMENT" in t and "Just a text" in t for t in texts(ADMIN)))
    await feed(press(ADMIN, "adm:bc:go")); await asyncio.sleep(1.5)
    ok("text broadcast reaches users who never confirmed too", 500 in {c for k, c, t, m in session.calls if k == "SendMessage"})

    # the settings page has the new switch and it flips
    await feed(press(ADMIN, "adm:set")); await asyncio.sleep(0.2)
    ok("Bot Settings shows 18+ Confirm", any("18+ Confirm" in str(m.reply_markup) for k, c, t, m in session.calls if k == "EditMessageText"))
    await feed(press(ADMIN, "adm:tgl:age_gate_enabled:s")); await asyncio.sleep(0.2)
    ok("admin can switch the age gate off", not settings.flag("age_gate_enabled"))
    await feed(press(ADMIN, "adm:tgl:age_gate_enabled:s")); await asyncio.sleep(0.2)
    ok("... and on again", settings.flag("age_gate_enabled"))
    print("ALL AGE-GATE / BROADCAST TESTS PASSED")
    await db.close()

asyncio.run(main())
