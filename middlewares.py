"""Access gate (bot OFF / maintenance / admin block) and the global error handler.

The gate runs before every private message and button press. Admins (ADMIN_IDS) always pass,
so they can turn the bot back on, end maintenance and use every admin tool while the public
side is closed. The decision is made on the server for every update.
"""
from __future__ import annotations

import html
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (CallbackQuery, ErrorEvent, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, TelegramObject)

import ui
from config import Config
from logging_setup import log_event
from services.settings_service import SettingsService, banned_text, contact_handle, plain_text
from services.user_service import UserService

log = logging.getLogger("gate")


class AccessGate(BaseMiddleware):
    def __init__(self, cfg: Config, settings: SettingsService, users: UserService) -> None:
        self.cfg = cfg
        self.settings = settings
        self.users = users

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        user = getattr(event, "from_user", None)
        if user is None or user.id in self.cfg.admin_ids:
            return await handler(event, data)
        if isinstance(event, Message):
            if event.chat.type != "private":
                return await handler(event, data)
            if (event.text or "").lower().split("@")[0].split(" ")[0] == "/admin":
                return await handler(event, data)    # answered with the permission notice
        if isinstance(event, CallbackQuery) and (event.data or "").startswith("adm:"):
            return await handler(event, data)        # rejected by the admin-callback guard

        banned = self.users.is_banned(user.id)
        text = self._blocked_text(user.id)
        if text is None:
            try:
                await self.users.touch(user.id)      # last activity
            except Exception:  # noqa: BLE001
                log.warning("touch_failed")
            return await handler(event, data)

        markup = self._contact_kb() if banned else None
        if isinstance(event, CallbackQuery):
            try:
                await event.answer(plain_text(text)[:190], show_alert=True)
            except TelegramAPIError:
                pass
            if markup is not None and event.message is not None:
                # A pop-up cannot carry a button, so a blocked user also gets a message with it.
                try:
                    await event.message.answer(text, parse_mode="HTML", reply_markup=markup)
                except TelegramAPIError:
                    pass
        elif isinstance(event, Message):
            try:
                await event.answer(text, parse_mode="HTML", reply_markup=markup)
            except TelegramAPIError:
                pass
        return None

    def _contact_kb(self) -> InlineKeyboardMarkup | None:
        """'Contact Developer' button for blocked users: the developer's Telegram link from the
        Developer settings (falls back to another configured link). None if none is set."""
        d = self.settings.developer()
        url = d["telegram"] or d["website"] or d["instagram"] or d["github"]
        if not url:
            return None
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📩 Contact Developer", url=url)]])

    def _blocked_text(self, user_id: int) -> str | None:
        if self.users.is_banned(user_id):
            return banned_text(self.users.ban_reason(user_id), ui.banned_base_text(),
                               contact_handle(self.settings.developer()["telegram"]))
        if not self.settings.bot_enabled:
            return ui.bot_off_text()
        if self.settings.maintenance:
            return self.settings.maintenance_text()
        return None


class AgeGate(BaseMiddleware):
    """18+ confirmation. Runs AFTER the access gate and Force Subscribe, so the flow is
    join channel -> confirm 18+ -> welcome. Until a user has pressed "I'm 18+" every message
    and every button (except the three `age:` buttons) just shows the confirmation card.
    Admins always pass. Switch it off in Admin Panel -> Bot Settings -> 18+ Confirm."""

    def __init__(self, cfg: Config, settings: SettingsService, users: UserService) -> None:
        self.cfg, self.settings, self.users = cfg, settings, users

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        user = getattr(event, "from_user", None)
        if (user is None or user.is_bot or user.id in self.cfg.admin_ids
                or not self.settings.flag("age_gate_enabled")
                or self.users.is_age_verified(user.id)):
            return await handler(event, data)
        if isinstance(event, Message):
            if event.chat.type != "private":
                return await handler(event, data)
            if (event.text or "").lower().split("@")[0].split(" ")[0] == "/admin":
                return await handler(event, data)    # answered with the permission notice
        if isinstance(event, CallbackQuery):
            payload = event.data or ""
            if payload.startswith(("age:", "adm:")):
                return await handler(event, data)    # the gate's own buttons / admin guard

        name = html.escape(user.first_name or "") or None
        text, markup = ui.age_gate_text(name), ui.age_gate_kb()
        if isinstance(event, CallbackQuery):
            try:
                await event.answer("🔞 Please confirm that you are 18+ first.")
            except TelegramAPIError:
                pass
            if event.message is not None:
                try:
                    await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
                except TelegramAPIError:
                    try:
                        await event.message.answer(text, reply_markup=markup, parse_mode="HTML")
                    except TelegramAPIError:
                        pass
        elif isinstance(event, Message):
            try:
                await event.answer(text, reply_markup=markup, parse_mode="HTML")
            except TelegramAPIError:
                pass
        return None


async def on_error(event: ErrorEvent, bot: Bot, cfg: Config) -> bool:
    """Never show Python exceptions to anyone. Users get a generic notice, admins a short
    safe summary (exception type only); the full traceback goes to the server log."""
    exc = event.exception
    if isinstance(exc, TelegramAPIError) and "not modified" in str(exc).lower():
        return True
    log.error("update_failed", exc_info=exc)
    update = event.update
    target = update.callback_query or update.message
    user = getattr(target, "from_user", None)
    if user is None:
        return True
    is_admin = user.id in cfg.admin_ids
    text = (f"⚠️ Something went wrong ({type(exc).__name__}). Details are in the server log."
            if is_admin else ui.error_text())
    try:
        if update.callback_query is not None:
            await update.callback_query.answer(text[:190], show_alert=True)
        elif update.message is not None:
            await update.message.answer(text)
    except TelegramAPIError:
        pass
    log_event(log, logging.ERROR, "handled_error", error=type(exc).__name__, admin=is_admin)
    return True
