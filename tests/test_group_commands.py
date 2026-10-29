"""Group commands: argument parsing, the public command list and the router wiring."""
import asyncio
from types import SimpleNamespace

import ui
from handlers import group


def test_parse_count():
    assert group.parse_count(None, 10) == 1
    assert group.parse_count("", 10) == 1
    assert group.parse_count("5", 10) == 5
    assert group.parse_count("5 extra", 10) == 5
    assert group.parse_count("10", 10) == 10
    assert group.parse_count("11", 10) is None
    assert group.parse_count("0", 10) is None
    assert group.parse_count("-3", 10) is None
    assert group.parse_count("abc", 10) is None
    assert group.parse_count("9" * 7, 10**9) is None


def test_group_has_five_commands_and_help_lists_everything():
    assert [c for c, _ in ui.GROUP_COMMANDS] == ["video", "new", "old", "progress", "help"]
    text = ui.commands_text(10)
    for cmd, _ in ui.PRIVATE_COMMANDS + ui.GROUP_COMMANDS:
        assert f"/{cmd} " in text
    assert "max 10 videos" in text


def test_mention_escapes_name():
    user = SimpleNamespace(id=7, first_name="<b>Eve</b>")
    assert "&lt;b&gt;" in group.mention(user)
    assert 'tg://user?id=7' in group.mention(user)


def test_router_registers_handlers():
    names = {h.callback.__name__ for h in group.router.message.handlers}
    assert {"cmd_start", "cmd_help", "cmd_collection", "cmd_video", "cmd_progress"} <= names
    assert any(h.callback.__name__ == "on_choose" for h in group.router.callback_query.handlers)


def test_anonymous_admin_is_refused():
    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kw):
            sent.append(text)

    msg = SimpleNamespace(from_user=SimpleNamespace(id=group.ANON_ADMIN_ID, is_bot=True),
                          chat=SimpleNamespace(id=-100), message_id=1,
                          message_thread_id=None, is_topic_message=False)
    msg.from_user.is_bot = False
    cfg = SimpleNamespace(admin_ids=set())
    ok = asyncio.run(group.guard(msg, FakeBot(), cfg, None, None, None, None))
    assert ok is False and sent and "anonymous" in sent[0].lower()
