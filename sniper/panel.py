"""Deliver one persistent control panel; retries edit it instead of sending copies."""

import hashlib
import json

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup


async def deliver_panel(store, bot, owner, payload):
    state = await store.read()
    panel = state.get("control_panel", {})
    target = panel.get("message_id")
    delivery = payload["_delivery"]
    opening = payload.get("_open")
    if opening != panel.get("open_epoch"):
        target = None
    attempt = opening or delivery
    fingerprint = hashlib.sha256(
        json.dumps(
            [payload["text"], payload["buttons"]],
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if target and panel.get("fingerprint") == fingerprint:
        return
    markup = InlineKeyboardMarkup(inline_keyboard=payload["buttons"]) if payload["buttons"] else None
    if target:
        try:
            await bot.edit_message_text(
                chat_id=owner, message_id=target, text=payload["text"], reply_markup=markup, parse_mode=None
            )
        except TelegramBadRequest as error:
            reason = error.message.casefold()
            if "message is not modified" in reason:
                pass
            elif "message to edit not found" in reason or "message can't be edited" in reason:
                target = None
            else:
                raise
    if not target:
        if panel.get("send_attempt") == attempt:
            # A send may have succeeded before a disconnect/crash. Never send it
            # again automatically. A callback can recover its actual message ID.
            return
        await store.apply(lambda s: s.setdefault("control_panel", {}).update(send_attempt=attempt))
        try:
            result = await bot.send_message(owner, payload["text"], reply_markup=markup, parse_mode=None)
        except (TelegramRetryAfter, TelegramBadRequest, TelegramForbiddenError):
            # These replies definitively reject the send, so retrying is safe.
            await store.apply(lambda s: s.setdefault("control_panel", {}).pop("send_attempt", None) and None)
            raise
        target = result.message_id

    def remember(s):
        s["control_panel"] = {"message_id": target, "fingerprint": fingerprint, "open_epoch": opening}

    await store.apply(remember)
