"""Full-dispatcher test: real routers + middlewares (AccessGate, ReferralCapture, Force Subscribe),
a fake Telegram transport and a REAL PostgreSQL.
   DATABASE_URL=postgresql://t:t@localhost/tdb python tests/test_referral_flow.py"""
import asyncio, datetime as dt, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import (AnswerCallbackQuery, EditMessageText, GetChatMember, GetMe, SendMessage,
                             SendVideo, DeleteMessage)
from aiogram.types import (Video, CallbackQuery, Chat, ChatMemberLeft, ChatMemberMember, Message, Update, User)

from config import Config
from database import Database
from force_subscribe import ForceSubscribeGate, load_force_sub_config
from handlers import admin, admin_panel, referral, start, user
from middlewares import AccessGate
from services.delivery_service import DeliveryService
from services.event_service import EventService
from services.force_sub_service import ForceSubService
from services.referral_service import ReferralCapture, ReferralService
from services.settings_service import SettingsService
from services.user_service import UserService
from services.video_service import VideoService

ADMIN, R, N1, N2 = 1, 100, 201, 202
def ok(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: sys.exit(1)

class Session(BaseSession):
    def __init__(s):
        super().__init__(); s.sent, s.members, s._id = [], {}, 5000
    async def close(s): pass
    async def stream_content(s, *a, **k): yield b""
    async def make_request(s, bot, method, timeout=None):
        now = dt.datetime.now(dt.timezone.utc)
        if isinstance(method, GetMe):
            return User(id=99, is_bot=True, first_name="VS", username="VideoShareProBot")
        if isinstance(method, GetChatMember):
            st = s.members.get(method.user_id, "left")
            u = User(id=method.user_id, is_bot=False, first_name="x")
            return ChatMemberMember(status="member", user=u) if st == "member" else ChatMemberLeft(status="left", user=u)
        if isinstance(method, (SendMessage, SendVideo, EditMessageText)):
            s._id += 1; text = getattr(method, "text", None) or getattr(method, "caption", None)
            s.sent.append((method.chat_id if hasattr(method, "chat_id") else None, text, method.reply_markup))
            return Message(message_id=s._id, date=now, chat=Chat(id=method.chat_id or 0, type="private"), text=text)
        return True

_uid = [0]
def msg(uid, text, name="N"):
    _uid[0] += 1
    return Update(update_id=_uid[0], message=Message(message_id=_uid[0], date=dt.datetime.now(dt.timezone.utc),
        chat=Chat(id=uid, type="private"), from_user=User(id=uid, is_bot=False, first_name=f"{name}{uid}", username=f"u{uid}"),
        text=text, entities=[{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else None))
def press(uid, data):
    _uid[0] += 1
    host = Message(message_id=9000 + _uid[0], date=dt.datetime.now(dt.timezone.utc), chat=Chat(id=uid, type="private"), text="menu",
                   from_user=User(id=99, is_bot=True, first_name="VS"))
    return Update(update_id=_uid[0], callback_query=CallbackQuery(id=str(_uid[0]), chat_instance="c", data=data,
                  from_user=User(id=uid, is_bot=False, first_name=f"N{uid}", username=f"u{uid}"), message=host))

async def main():
    db = Database(os.environ["DATABASE_URL"], 1, 5); await db.connect()
    for t in ("referral_claim_items", "referral_claims", "referral_reward_videos", "referral_rejections", "referrals",
              "user_states", "bot_settings", "force_sub_channels", "delivered_messages"):
        await db.pool.execute(f"DELETE FROM {t}")
    await db.pool.execute("DELETE FROM users")
    cfg = Config(bot_token="1:t", database_url="x", admin_ids=frozenset({ADMIN}), source_channel_ids=(), channel_auto_category=None,
                 max_videos_per_request=50, old_video_order="asc", send_delay=0.0, protect_content=False, state_ttl_minutes=30,
                 db_pool_min=1, db_pool_max=2, api_id=None, api_hash=None, session_string=None, scan_batch_size=1,
                 scan_progress_every=1, auto_scan_minutes=0, new_video_days=30)
    session = Session(); bot = Bot("1:AAAA", session=session)
    users = UserService(db, 30); videos = VideoService(db, "asc", 30)
    settings = SettingsService(db, cfg); await settings.load()
    events = EventService(db, cfg); force_sub = ForceSubService(db); await force_sub.load()
    rs = ReferralService(bot, db, cfg, settings, users, events); await rs.start()
    delivery = DeliveryService(bot, db, videos, users, cfg, settings, events)
    panel = admin_panel.Panel(bot, cfg, db, users, videos, None, None, settings, events, force_sub=force_sub, referrals=rs)
    dp = Dispatcher()
    dp.workflow_data.update(cfg=cfg, db=db, users=users, videos=videos, delivery=delivery, settings=settings, events=events,
                            panel=panel, referrals=rs, scanner=None, bridge=None)
    dp.message.outer_middleware(ReferralCapture(rs))
    gate = AccessGate(cfg, settings, users); dp.message.outer_middleware(gate); dp.callback_query.outer_middleware(gate)
    fs = load_force_sub_config(); fsub = ForceSubscribeGate(cfg, settings, users, fs, force_sub)
    dp.message.outer_middleware(fsub); dp.callback_query.outer_middleware(fsub)
    for r in (admin_panel.router, admin.router, start.router, referral.router, user.router): dp.include_router(r)
    feed = lambda u: dp.feed_update(bot, u)
    texts = lambda uid: [t or "" for c, t, _ in session.sent if c == uid]

    # the referrer and the admin exist
    await feed(msg(R, "/start")); await feed(msg(ADMIN, "/start"))
    session.sent.clear()
    await feed(msg(R, "/refer"))
    ok("/refer returns the personal link", any(f"https://t.me/VideoShareProBot?start=ref_{R}" in t for t in texts(R)))
    session.sent.clear()
    await feed(msg(R, "/rewards")); ok("/rewards works", any("REFERRAL REWARDS" in t for t in texts(R)))
    await feed(msg(R, "/stats")); ok("/stats for a user = referral stats", any("YOUR REFERRAL STATS" in t for t in texts(R)))
    session.sent.clear()
    await feed(msg(ADMIN, "/stats")); ok("/stats for admin stays the admin statistics",
        any("ADMIN STATS" in t and "Referrals:" in t for t in texts(ADMIN)))
    ok("admin /stats did not show the user view", not any("YOUR REFERRAL STATS" in t for t in texts(ADMIN)))

    # ---- new user through the link, no force-subscribe channel configured
    session.sent.clear()
    await feed(msg(N1, f"/start ref_{R}")); await asyncio.sleep(0.3)
    ok("new user gets the normal welcome", any("WELCOME" in t.upper() for t in texts(N1)))
    ok("referral counted", (await rs.summary(R)).eligible == 1)
    ok("referrer notified", any("New referral" in t for t in texts(R)))
    await feed(msg(N1, f"/start ref_{R}")); await feed(msg(N1, "/start"))
    ok("re-opening the link never counts twice", (await rs.summary(R)).eligible == 1)
    await feed(msg(R, f"/start ref_{R}")); ok("own link ignored", (await rs.summary(R)).eligible == 1)

    # ---- Force Subscribe in front: referral survives the join + verify step
    await force_sub.add(-1001111111111, "The Royal Zone", "RoyalZone", "https://t.me/RoyalZone")
    session.sent.clear()
    await feed(msg(N2, f"/start ref_{R}")); await asyncio.sleep(0.2)
    ok("blocked by force subscribe", any("ACCESS RESTRICTED" in t for t in texts(N2)))
    ok("referral is pending, not counted", (await rs.summary(R)).eligible == 1 and (await rs.summary(R)).pending == 1)
    session.members[N2] = "member"
    await feed(press(N2, "fsub:verify")); await asyncio.sleep(0.3)
    s = await rs.summary(R); ok("counted once he joined and verified", s.eligible == 2 and s.pending == 0)

    # ---- button works in the menu
    session.members[R] = "member"      # the referrer is a channel member too
    session.sent.clear(); await feed(press(R, "ref:home"))
    ok("Refer & Earn button opens the link page", any(f"start=ref_{R}" in t for t in texts(R)))
    # ---- admin uploads reward videos: the panel handler must win over the library's /addvideo handler
    def vid(uid, n):
        _uid[0] += 1
        return Update(update_id=_uid[0], message=Message(message_id=_uid[0], date=dt.datetime.now(dt.timezone.utc),
            chat=Chat(id=uid, type="private"), from_user=User(id=uid, is_bot=False, first_name="A"),
            video=Video(file_id=f"fid{n}", file_unique_id=f"uid{n}", width=1, height=1, duration=3)))
    before = await db.pool.fetchval("SELECT COUNT(*) FROM videos")
    await feed(press(ADMIN, "adm:ref:add:english")); session.sent.clear()
    await feed(vid(ADMIN, 1)); await feed(vid(ADMIN, 2))
    ok("admin videos go to the English reward pool", (await rs.pool_counts())["english"] == 2)
    ok("...and not into the normal video library", await db.pool.fetchval("SELECT COUNT(*) FROM videos") == before)
    await feed(press(ADMIN, "adm:ref:done")); session.sent.clear()
    await feed(vid(ADMIN, 3))
    ok("outside that mode the normal /addvideo flow is unchanged",
       await db.pool.fetchval("SELECT COUNT(*) FROM videos") == before + 1 and (await rs.pool_counts())["english"] == 2)
    # ---- a normal user can never use the admin referral pages
    session.sent.clear(); await feed(press(R, "adm:ref")); await feed(msg(R, "/referrals"))
    ok("non-admin gets nothing from the admin referral pages", not any("REFERRAL MANAGER" in t for t in texts(R)))
    print("ALL FLOW TESTS PASSED")
    await db.close()

asyncio.run(main())
