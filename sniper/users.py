"""Owner-approved users with isolated encrypted state and delivery destinations."""

import copy
import re
import time
import uuid

from .accounts import AccountMissing
from .control import handle, panel_response
from .engine import button, notice
from .store import initial_state


class AccessDenied(AccountMissing):
    pass


def private_sender(update):
    callback = update.get("callback_query")
    message = (callback or {}).get("message") or update.get("message") or update.get("edited_message") or {}
    sender = (callback or message).get("from", {})
    user_id = sender.get("id")
    chat = message.get("chat", {})
    if (
        type(user_id) is int
        and user_id > 0
        and not sender.get("is_bot")
        and chat.get("id") == user_id
        and chat.get("type") == "private"
    ):
        return user_id
    return None


def approved(root, user_id, admin):
    return user_id == admin or root.get("users", {}).get(str(user_id), {}).get("status") == "approved"


def user_state(root, user_id, admin):
    if not approved(root, user_id, admin):
        raise AccessDenied()
    return root if user_id == admin else root["users"][str(user_id)]["state"]


def destinations(messages, user_id, admin):
    return [({**message, "_recipient": user_id} if user_id != admin else message) for message in messages]


def pause_user(state):
    for data in [state, *(a["profile"] for a in state.get("accounts", {}).values())]:
        data["settings"]["running"] = False
        data["arm"] = None
        data["arm_after_reconcile"] = False
        data["reconcile_requested"] = False
        if data["active"]:
            data["active"]["cancel_requested"] = False
            if data["active"].get("stage") in {"ATTEMPTING", "CANCELLING", "PREPARING"}:
                data["active"]["stage"] = "UNKNOWN"


def access_menu(root, page=0):
    entries = sorted(
        root.get("users", {}).items(),
        key=lambda item: (item[1]["status"] != "pending", -item[1].get("created", 0)),
    )
    page = max(0, min(page, max(0, (len(entries) - 1) // 8)))
    labels = {
        "pending": "⏳ Заявка",
        "approved": "✅ Разрешён",
        "denied": "⛔ Отклонён",
        "revoked": "⛔ Доступ отозван",
    }
    lines = ["👥 Доступ к боту", "", "Одобренные пользователи подключают свои аккаунты и задают свои курсы."]
    rows = []
    for key, entry in entries[page * 8 : page * 8 + 8]:
        lines.append(f"\n{labels[entry['status']]} · {entry.get('name', 'Пользователь')}\nID: {key}")
        action = "revoke" if entry["status"] == "approved" else "allow"
        label = "Отозвать" if action == "revoke" else "Разрешить"
        row = [button(f"{label} · {key}", callback=f"access:{action}:{key}:{entry['nonce']}")]
        if entry["status"] == "pending":
            row.append(button("Отклонить", callback=f"access:deny:{key}:{entry['nonce']}"))
        rows.append(row)
    if not entries:
        lines.append("\nЗаявок пока нет. Пользователь должен отправить /start.")
    navigation = []
    if page:
        navigation.append(button("‹ Назад", callback=f"access:list:{page - 1}"))
    if (page + 1) * 8 < len(entries):
        navigation.append(button("Далее ›", callback=f"access:list:{page + 1}"))
    if navigation:
        rows.append(navigation)
    return notice("\n".join(lines), rows)


def request_access(root, update, user_id):
    message = update.get("message") or {}
    command = (message.get("text") or "").strip().split(maxsplit=1)
    if not command or command[0].split("@", 1)[0] != "/start":
        return []
    entries = root.setdefault("users", {})
    entry = entries.get(str(user_id))
    messages = []
    if entry is None:
        sender = message.get("from", {})
        name = " ".join(str(sender.get("first_name", "Пользователь")).split())[:60]
        username = sender.get("username", "")
        entry = entries[str(user_id)] = {
            "status": "pending",
            "name": name,
            "created": time.time(),
            "nonce": uuid.uuid4().hex[:10],
            "state": initial_state(),
        }
        text = f"👤 Новая заявка\n{name}\nID: {user_id}"
        if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            text += f"\n@{username}"
        messages.append(
            notice(
                text,
                [
                    [
                        button("✅ Разрешить", callback=f"access:allow:{user_id}:{entry['nonce']}"),
                        button("❌ Отклонить", callback=f"access:deny:{user_id}:{entry['nonce']}"),
                    ]
                ],
            )
        )
    if time.time() - entry.get("last_reply", 0) >= 5:
        entry["last_reply"] = time.time()
        text = (
            "⏳ Заявка отправлена. Дождитесь ответа администратора."
            if entry["status"] == "pending"
            else "⛔ Доступ к боту не разрешён. Обратитесь к администратору."
        )
        messages.append({**notice(text), "_recipient": user_id, "_access_notice": True})
    return messages


def route_update(root, update, admin, status):
    user_id = private_sender(update)
    if user_id is None:
        return []
    if not approved(root, user_id, admin):
        return request_access(root, update, user_id)
    state = user_state(root, user_id, admin)
    callback = update.get("callback_query", {})
    action = callback.get("data", "")
    revision = None
    if action.startswith("ui:"):
        parts = action.split(":", 2)
        if len(parts) != 3:
            return []
        _, revision, action = parts
    # Older panels incorrectly scoped global access management to a trading account.
    # Normalize those buttons without selecting or exposing another account.
    if action.startswith("acct:"):
        scoped = action.split(":", 2)
        if len(scoped) == 3 and scoped[2].startswith("access:"):
            action = scoped[2]
    message = update.get("message") or {}
    command = (message.get("text") or "").strip().split()
    first = command[0].split("@", 1)[0] if command else ""
    if action.startswith("access:") or first in {"/users", "/allow", "/deny", "/revoke"}:
        if user_id != admin:
            return destinations(
                [notice("Управление доступом доступно только администратору.")], user_id, admin
            )
        state["pending"] = None
        if revision is not None and revision != str(state.get("ui_revision", 0)):
            return [panel_response(root, access_menu(root))]
        if first == "/users" or action.startswith("access:list:"):
            page = action.rsplit(":", 1)[-1] if action else "0"
            return [panel_response(root, access_menu(root, int(page) if page.isdigit() else 0))]
        if first in {"/allow", "/deny", "/revoke"}:
            if len(command) != 2 or not command[1].isdigit():
                return [panel_response(root, notice(f"Формат: {first} 123456789"))]
            target = command[1]
            entry = root.get("users", {}).get(target)
            verb, nonce = first[1:], (entry or {}).get("nonce")
        else:
            parts = action.split(":")
            if len(parts) != 4:
                return []
            _, verb, target, nonce = parts
            entry = root.get("users", {}).get(target)
        if not entry or int(target) == admin or verb not in {"allow", "deny", "revoke"}:
            return [panel_response(root, notice("Заявка не найдена. Пользователь должен отправить /start."))]
        if entry["nonce"] != nonce:
            return [panel_response(root, access_menu(root))]
        entry["status"] = "approved" if verb == "allow" else ("denied" if verb == "deny" else "revoked")
        entry["nonce"] = uuid.uuid4().hex[:10]
        entry["decided"] = time.time()
        messages = [panel_response(root, access_menu(root))]
        if verb == "allow":
            text = "✅ Доступ разрешён. Подключите свой Telegram-аккаунт и настройте курс."
            messages.append({**notice(text), "_recipient": int(target), "_access_notice": True})
            guest = entry["state"]
            menus = handle(guest, {"message": {"text": "/start"}}, status(guest, int(target)))
            messages.extend(destinations(menus, int(target), admin))
        else:
            pause_user(entry["state"])
            messages.append(
                {
                    **notice(
                        "⛔ Заявка отклонена."
                        if verb == "deny"
                        else "⛔ Доступ отозван. Поиск остановлен. Открытые сделки проверьте в CryptoBot."
                    ),
                    "_recipient": int(target),
                    "_access_notice": True,
                }
            )
        return messages
    return destinations(handle(state, update, status(state, user_id)), user_id, admin)


class UserStore:
    """All mutations stay under the original SQL transaction and worker lease."""

    def __init__(self, root, user_id, admin):
        self.root, self.user_id, self.admin = root, user_id, admin
        self.cipher = root.cipher

    async def read(self):
        view = user_state(await self.root.read(), self.user_id, self.admin)
        return copy.deepcopy({key: value for key, value in view.items() if key != "users"})

    async def owns(self, owner):
        return approved(await self.root.read(), self.user_id, self.admin) and await self.root.owns(owner)

    async def apply(self, change):
        def scoped(root):
            target = user_state(root, self.user_id, self.admin)
            view = copy.deepcopy({key: value for key, value in target.items() if key != "users"})
            messages = change(view) or []
            mine = {a.get("user_id") for a in view.get("accounts", {}).values()} - {None}
            others = [
                (self.admin, root),
                *((int(key), entry["state"]) for key, entry in root.get("users", {}).items()),
            ]
            for uid, data in others:
                if uid != self.user_id and mine.intersection(
                    a.get("user_id") for a in data.get("accounts", {}).values()
                ):
                    raise ValueError("account_owned_elsewhere")
            users = target.get("users") if self.user_id == self.admin else None
            target.clear()
            target.update(view)
            if users is not None:
                target["users"] = users
            return destinations(messages, self.user_id, self.admin)

        return await self.root.apply(scoped)
