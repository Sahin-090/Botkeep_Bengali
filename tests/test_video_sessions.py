"""New / Old video sessions against a REAL PostgreSQL (fake Telegram bot).
   DATABASE_URL=postgresql://t:t@localhost/tdb python tests/test_video_sessions.py"""
import asyncio, os, sys, types, random
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import Database
from services.delivery_service import DeliveryService
from services.user_service import UserService
from services.video_service import VideoService


def ok(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        sys.exit(1)


class FakeBot:
    def __init__(self):
        self.sent = {}            # chat_id -> [file_id, ...] in send order
        self.protect = []
        self.fail_once = set()    # file_ids whose first send fails (transient error)
        self._mid = 0

    async def send_video(self, chat_id, file_id, **kw):
        self.protect.append(kw.get("protect_content"))
        await asyncio.sleep(random.random() * 0.01)   # interleave concurrent users
        if file_id in self.fail_once:
            self.fail_once.discard(file_id)
            raise RuntimeError("boom")
        self.sent.setdefault(chat_id, []).append(file_id)
        self._mid += 1
        return types.SimpleNamespace(message_id=self._mid)

    async def send_message(self, *a, **k):
        pass


async def main():
    db = Database(os.environ["DATABASE_URL"], 2, 20)
    await db.connect()
    await db.pool.execute("TRUNCATE videos, user_video_history, user_delivery_sessions, user_mode_history, user_mode_progress, "
                          "user_states, requests, delivered_messages RESTART IDENTITY CASCADE")
    cfg = types.SimpleNamespace(delivery_batch_size=1000, send_delay=0, protect_content=False,
                                auto_delete_seconds=120, admin_ids=frozenset())
    users = UserService(db, 30)
    videos = VideoService(db, "asc", 30)
    bot = FakeBot()
    delivery = DeliveryService(bot, db, videos, users, cfg)

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    async def add(n, uid, day, cat):
        await db.pool.execute(
            "INSERT INTO videos (video_number, category, telegram_file_id, telegram_file_unique_id,"
            " is_active, channel_id, telegram_message_id, message_date) "
            "VALUES ($1,$2,$3,$4,TRUE,-100,$5,$6)",
            n, cat, f"f{uid}", f"u{uid}", uid, base + timedelta(days=day))

    # 25 videos; file ids f1..f25 are in upload order; categories deliberately mixed
    for i in range(1, 26):
        await add(i, i, i, "old" if i <= 15 else "new")

    exp_new = [f"f{i}" for i in range(25, 0, -1)]
    exp_old = [f"f{i}" for i in range(1, 26)]

    async def req(uid, mode, n):
        assert await users.acquire_delivery_lock(uid)
        try:
            r = await delivery.deliver(uid, uid, mode, n)
        finally:
            await users.clear_state(uid)
        return r

    # 1) New: newest first, continues backwards, ends at the oldest, no repeats
    for n in (10, 10, 10):
        await req(1, "new", n)
    ok("New: newest -> oldest across both categories, all 25 once", bot.sent[1] == exp_new)
    ok("New: exhausted -> nothing more", await videos.count_unseen_all(1, "new") == 0)
    s = await users.get_session(1)
    ok("session: mode/position/progress stored", s["mode"] == "new" and s["delivered_total"] == 25
       and s["batches"] == 3 and s["last_video_id"] == 1)

    # 2) Old: independent user, oldest first
    await req(2, "old", 10); await req(2, "old", 10); await req(2, "old", 10)
    ok("Old: oldest -> newest, all 25 once", bot.sent[2] == exp_old)
    ok("user 1 session untouched by user 2",
       (await users.get_session(1))["delivered_total"] == 25
       and (await users.get_session(1))["mode"] == "new")

    # 3) interleaved partial progress between two users, different modes
    await req(3, "new", 5); await req(4, "old", 5); await req(3, "new", 5); await req(4, "old", 5)
    ok("interleaved users keep own order",
       bot.sent[3] == exp_new[:10] and bot.sent[4] == exp_old[:10])

    # 4) 40 users at once, random modes and sizes, several rounds
    rnd = random.Random(7)
    plan = {u: (rnd.choice(["new", "old"]), [rnd.randint(1, 9) for _ in range(5)])
            for u in range(100, 140)}

    async def run_user(u):
        mode, sizes = plan[u]
        for n in sizes:
            await req(u, mode, n)

    await asyncio.gather(*(run_user(u) for u in plan))
    good = True
    for u, (mode, sizes) in plan.items():
        exp = (exp_new if mode == "new" else exp_old)[:sum(sizes)]
        good &= bot.sent[u] == exp
        sess = await users.get_session(u)
        good &= sess["delivered_total"] == min(sum(sizes), 25) and sess["mode"] == mode
    ok("40 concurrent users: each exact order, no duplicates, own session", good)

    # 5) same user cannot run two deliveries at once
    ok("lock: first acquire wins", await users.acquire_delivery_lock(500))
    ok("lock: second concurrent request blocked", not await users.acquire_delivery_lock(500))
    await users.clear_state(500)

    # 6) a video added later: top of New, end of Old; nothing is skipped
    await add(26, 26, 30, "new")
    await req(5, "new", 3)
    ok("late upload comes first in New", bot.sent[5] == ["f26", "f25", "f24"])
    await req(6, "old", 25); await req(6, "old", 5)
    ok("late upload comes last in Old", bot.sent[6] == [f"f{i}" for i in range(1, 27)])
    await req(1, "new", 5)      # user 1 had finished New earlier
    ok("finished user receives only the new upload", bot.sent[1][25:] == ["f26"])

    # 7) failed send is retried, never skipped
    bot.fail_once.add("f20")
    await req(7, "new", 8)       # f26..f19 with f20 failing -> one fewer delivered
    await req(7, "new", 5)
    got = bot.sent[7]
    ok("failed video not lost and not duplicated",
       len(got) == len(set(got)) and "f20" in got)

    # 8) admin reset clears history AND session
    await videos.reset_user_history(2)
    ok("reset clears session", await users.get_session(2) is None
       and await videos.count_unseen_all(2, "old") == 26)

    # 9) New and Old progress are separate per user and resume where each stopped
    await req(8, "new", 5); await req(8, "old", 5); await req(8, "new", 5); await req(8, "old", 3)
    ok("separate progress per mode", bot.sent[8] == (
        ["f26", "f25", "f24", "f23", "f22"] + ["f1", "f2", "f3", "f4", "f5"]
        + ["f21", "f20", "f19", "f18", "f17"] + ["f6", "f7", "f8"]))
    pr = await videos.progress(8)
    ok("progress summary per mode", pr["new"]["received"] == 10 and pr["old"]["received"] == 8
       and pr["new"]["left"] == 16 and pr["old"]["left"] == 18)

    # 10) restart: brand-new service objects, same database -> resume exactly there
    users2 = UserService(db, 30); videos2 = VideoService(db, "asc", 30)
    await users2.reset_delivering()
    delivery2 = DeliveryService(bot, db, videos2, users2, cfg)
    assert await users2.acquire_delivery_lock(8)
    await delivery2.deliver(8, 8, "new", 2); await users2.clear_state(8)
    ok("resume after restart", bot.sent[8][-2:] == ["f16", "f15"])

    # 11) interrupted delivery (process killed mid-batch): sent ones recorded, rest resumes
    class Dying(FakeBot):
        n = 0
        async def send_video(self, chat_id, file_id, **kw):
            Dying.n += 1
            if Dying.n == 4:
                raise asyncio.CancelledError()
            return await super().send_video(chat_id, file_id, **kw)
    dbot = Dying(); ddel = DeliveryService(dbot, db, videos, users, cfg)
    assert await users.acquire_delivery_lock(9)
    try:
        await ddel.deliver(9, 9, "old", 6)
    except asyncio.CancelledError:
        pass
    await users.reset_delivering()
    ok("interrupted: 3 sent and recorded", dbot.sent[9] == ["f1", "f2", "f3"]
       and await videos.count_unseen_all(9, "old") == 23)
    await req(9, "old", 3)
    ok("interrupted: resumes at the next video, no repeat",
       bot.sent[9] == ["f4", "f5", "f6"])

    # 12) deleted videos: a delivered one and an undelivered one disappear safely
    await db.pool.execute("DELETE FROM videos WHERE telegram_file_id = 'f3'")
    await db.pool.execute("DELETE FROM videos WHERE telegram_file_id = 'f7'")
    await req(9, "old", 3)
    ok("deleted videos skipped, order kept", bot.sent[9][-3:] == ["f8", "f9", "f10"])
    ok("history rows of deleted video gone",
       await db.pool.fetchval("SELECT COUNT(*) FROM user_mode_history WHERE video_id NOT IN "
                              "(SELECT id FROM videos)") == 0)

    # 13) empty library
    await db.pool.execute("TRUNCATE videos RESTART IDENTITY CASCADE")
    ok("empty library: nothing available", await videos.count_available() == 0
       and await videos.count_unseen_all(1, "new") == 0
       and (await videos.progress(1))["new"]["left"] == 0)
    r = await req(10, "new", 5)
    ok("empty library: deliver is a safe no-op", r.delivered == 0 and not r.blocked)

    # 14) content protection (no forward / no save) on by default, admin can switch it
    class S:
        def __init__(self, v): self.v = v
        def flag(self, k): return self.v if k == "protect_content" else False
    await db.pool.execute(
        "INSERT INTO videos (video_number, category, telegram_file_id, telegram_file_unique_id,"
        " is_active) VALUES (1,'new','p1','up1',TRUE),(2,'new','p2','up2',TRUE)")
    pbot = FakeBot()
    for flag, uid in ((True, 31), (False, 32)):
        d = DeliveryService(pbot, db, videos, users, cfg, settings=S(flag))
        assert await users.acquire_delivery_lock(uid)
        await d.deliver(uid, uid, "new", 1); await users.clear_state(uid)
    ok("protect_content True when enabled, False when admin turns it off",
       pbot.protect == [True, False])
    d = DeliveryService(pbot, db, videos, users, cfg)
    ok("protected by default without settings", d.protect_for(99) is True)

    await db.close()
    print("ALL OK")

asyncio.run(main())
