"""Account profiles share one encrypted database and one delivery queue."""

import copy
import time
import uuid

from .store import initial_state

PROFILE_KEYS = (
    "settings",
    "active",
    "offers",
    "notified",
    "ignored",
    "audit",
    "stats",
    "last_seen",
    "account_checked",
    "arm",
    "resolve",
    "reconcile_requested",
    "inspect_requested",
    "arm_after_reconcile",
    "reconcile_panel_revision",
    "user_retry_after",
    "user_action_interval",
    "user_refresh_interval",
    "user_slow_until",
)


class AccountMissing(RuntimeError):
    pass


def profile(state):
    return {key: copy.deepcopy(state[key]) for key in PROFILE_KEYS if key in state}


def load_profile(state, data):
    for key in PROFILE_KEYS:
        state.pop(key, None)
    state.update(copy.deepcopy(data))


def select_account(state, account_id):
    accounts = state.get("accounts", {})
    if account_id not in accounts:
        raise AccountMissing()
    previous = state.get("selected_account")
    if previous == account_id:
        return
    if previous in accounts:
        accounts[previous]["profile"] = profile(state)
    load_profile(state, accounts[account_id]["profile"])
    state["selected_account"] = account_id
    state["pending"] = None
    # A background reconciliation must not replace the newly selected panel.
    state.pop("reconcile_panel_revision", None)


def add_account(state, encrypted, user_id, label, limit=3, *, legacy=False):
    accounts = state.setdefault("accounts", {})
    if len(accounts) >= limit:
        raise ValueError("account_limit")
    if user_id and any(a.get("user_id") == user_id for a in accounts.values()):
        raise ValueError("duplicate_account")
    account_id = uuid.uuid4().hex[:12]
    accounts[account_id] = {
        "session_encrypted": encrypted,
        "user_id": user_id,
        "label": str(label)[:80],
        "created": time.time(),
        "profile": profile(state if legacy else initial_state()),
    }
    if not state.get("selected_account"):
        select_account(state, account_id)
    return account_id


def remove_account(state, account_id):
    accounts = state.get("accounts", {})
    if account_id not in accounts:
        raise AccountMissing()
    data = state if state.get("selected_account") == account_id else accounts[account_id]["profile"]
    if data["active"] or data["settings"]["running"]:
        raise ValueError("account_busy")
    del accounts[account_id]
    if state.get("selected_account") == account_id:
        state["selected_account"] = None
        load_profile(state, profile(initial_state()))
        state["pending"] = None
        if accounts:
            select_account(state, next(iter(accounts)))


def account_view(root, account_id):
    if account_id not in root.get("accounts", {}):
        raise AccountMissing()
    view = copy.deepcopy(root)
    if root.get("selected_account") != account_id:
        load_profile(view, root["accounts"][account_id]["profile"])
        view.pop("reconcile_panel_revision", None)
    return view


class AccountStore:
    """Serialize every worker mutation through the root row lock."""

    def __init__(self, root, account_id):
        self.root, self.account_id = root, account_id
        self.cipher = root.cipher

    async def read(self):
        return account_view(await self.root.read(), self.account_id)

    async def owns(self, owner):
        return await self.root.owns(owner)

    async def apply(self, change):
        def scoped(root):
            view = account_view(root, self.account_id)
            result = change(view) or []
            selected = root.get("selected_account") == self.account_id
            if selected:
                load_profile(root, profile(view))
                if view.get("ui_revision") != root.get("ui_revision"):
                    root["ui_revision"] = view["ui_revision"]
            else:
                root["accounts"][self.account_id]["profile"] = profile(view)
            label = root["accounts"][self.account_id]["label"]
            messages = []
            for item in result:
                if item.get("kind") == "panel":
                    if selected:
                        messages.append(item)
                    continue
                item = copy.deepcopy(item)
                item["text"] = f"👤 {label}\n" + item["text"]
                for row in item.get("buttons", []):
                    for key in row:
                        if "callback_data" in key:
                            key["callback_data"] = f"acct:{self.account_id}:" + key["callback_data"]
                messages.append(item)
            return messages

        return await self.root.apply(scoped)


async def import_legacy_session(store, config):
    """One-time import; removing an account must not reimport it on restart."""
    state = await store.read()
    if state.get("accounts_initialized"):
        return
    encrypted = config.user_session_encrypted.get_secret_value()
    if not encrypted and config.user_session_file.is_file():
        encrypted = config.user_session_file.read_text(encoding="utf-8").strip()
    if encrypted:
        store.cipher.fernet.decrypt(encrypted.encode())

    def migrate(s):
        if s.get("accounts_initialized"):
            return
        if encrypted and not s.get("accounts"):
            add_account(s, encrypted, None, "Основной аккаунт", config.max_accounts, legacy=True)
        s["accounts_initialized"] = True

    await store.apply(migrate)
