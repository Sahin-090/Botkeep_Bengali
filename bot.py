import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (BotCommand, BotCommandScopeAllGroupChats,
                           BotCommandScopeAllPrivateChats)

import ui
from config import load_config
from database import Database
from force_subscribe import ForceSubscribeGate, load_force_sub_config, startup_check
from handlers import admin, admin_panel, group, referral, start, user
from logging_setup import log_event, setup_logging
from middlewares import AccessGate, AgeGate, on_error
from services.bridge_service import BridgeService
from services.cleanup_service import CleanupService
from services.delivery_service import DeliveryService
from services.event_service import EventService
from services.force_sub_service import ForceSubService
from services.referral_service import ReferralCapture, ReferralService
from services.scanner_service import ScannerService
from services.settings_service import SettingsService
from services.user_service import UserService
from services.video_service import VideoService

log = logging.getLogger("bot")


async def set_command_menus(bot: Bot) -> None:
    """The \"/\" command menu: one list for private chats, one for groups. Never fatal."""
    try:
        await bot.set_my_commands([BotCommand(command=c, description=d)
                                   for c, d in ui.PRIVATE_COMMANDS],
                                  scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands([BotCommand(command=c, description=d)
                                   for c, d in ui.GROUP_COMMANDS],
                                  scope=BotCommandScopeAllGroupChats())
    except TelegramAPIError:
        log.warning("set_my_commands_failed")


async def main() -> None:
    setup_logging()
    cfg = load_config()

    db = Database(cfg.database_url, cfg.db_pool_min, cfg.db_pool_max)
    await db.connect()

    # Client 1 - Bot API (BOT_TOKEN): /start, user requests, buttons, delivery, admin commands.
    bot = Bot(cfg.bot_token)
    users = UserService(db, cfg.state_ttl_minutes)
    videos = VideoService(db, cfg.old_video_order, cfg.new_video_days)
    # Admin-editable settings live in PostgreSQL (bot on/off, maintenance, texts, buttons ...),
    # so they survive Railway restarts. They are layered over the environment defaults.
    settings = SettingsService(db, cfg)
    await settings.load()
    settings.apply_runtime(videos)
    ui.bind(settings, cfg)   # lets the text helpers use the admin's custom messages
    events = EventService(db, cfg)
    await users.load_banned()
    await users.load_save_allowed()
    await users.load_age_verified()
    # Client 2 - Telethon USER client (API_ID + API_HASH + SESSION_STRING): history scanning
    # ONLY. It is created lazily inside ScannerService; the bot token is never used for it.
    scanner = ScannerService(cfg, db, videos, events)
    bridge = BridgeService(bot, videos, cfg)
    delivery = DeliveryService(bot, db, videos, users, cfg, settings, events)
    cleanup = CleanupService(bot, db, events)
    # Force-subscribe channels live in PostgreSQL and are managed from the Admin Panel.
    fs_cfg = load_force_sub_config()
    force_sub = ForceSubService(db)
    await force_sub.load()
    await force_sub.seed_default(bot, fs_cfg.channel, fs_cfg.url, fs_cfg.name)   # first start only
    # Referral links, tracking, anti-fraud and rewards (tables + settings are additive).
    referrals = ReferralService(bot, db, cfg, settings, users, events)
    await referrals.start()
    panel = admin_panel.Panel(bot, cfg, db, users, videos, scanner, bridge, settings, events,
                              force_sub=force_sub, referrals=referrals)
    await users.reset_delivering()
    await scanner.reset_stale()   # a scan that was running when Railway restarted is not running any more

    dp = Dispatcher()
    dp.workflow_data.update(cfg=cfg, db=db, users=users, videos=videos, delivery=delivery,
                            scanner=scanner, bridge=bridge, settings=settings, events=events,
                            panel=panel, referrals=referrals,
                            force_sub=force_sub, fs_cfg=fs_cfg)   # used by the group handler
    # Referral capture runs FIRST: `/start ref_<id>` is recorded even if a gate below stops it.
    dp.message.outer_middleware(ReferralCapture(referrals))
    # Bot OFF / maintenance / admin block. Admins always pass.
    gate = AccessGate(cfg, settings, users)
    dp.message.outer_middleware(gate)
    dp.callback_query.outer_middleware(gate)
    # Force subscribe: live getChatMember check on every non-admin message / button press.
    fsub = ForceSubscribeGate(cfg, settings, users, fs_cfg, force_sub)
    dp.message.outer_middleware(fsub)
    dp.callback_query.outer_middleware(fsub)
    # 18+ confirmation: after the channel check, before anything else is shown.
    age_gate = AgeGate(cfg, settings, users)
    dp.message.outer_middleware(age_gate)
    dp.callback_query.outer_middleware(age_gate)
    dp.errors.register(on_error)      # users never see Python exceptions
    dp.include_router(admin_panel.router)   # /admin dashboard (admin-only, server-side checked)
    dp.include_router(admin.router)   # admin commands; must come before the generic text handler
    dp.include_router(group.router)   # /video /new /old /progress /help inside groups
    dp.include_router(start.router)
    dp.include_router(referral.router)   # /refer /stats /rewards (before the generic text handler)
    dp.include_router(user.router)

    await bot.get_me()
    await set_command_menus(bot)
    log.info("✅ Bot client started")
    await startup_check(bot, fs_cfg, force_sub)   # logs a warning if the bot is not admin of the channel
    await scanner.validate_session()   # logs only fixed, safe messages; bot keeps running either way
    log_event(log, logging.INFO, "bot_starting", admins=len(cfg.admin_ids),
              scanner=scanner.enabled)
    background = asyncio.create_task(scanner.background_loop())
    # Deletes bot-sent videos from users' chats once delete_at passes (restart-safe, DB-backed).
    cleanup_task = asyncio.create_task(cleanup.run())
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        background.cancel()
        cleanup_task.cancel()
        await scanner.close()
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
