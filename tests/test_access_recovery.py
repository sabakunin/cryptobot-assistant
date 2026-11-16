import copy

from test_users import ADMIN, allow, callback, message, route

from sniper.accounts import add_account
from sniper.store import initial_state


def find_action(panel, fragment):
    return next(
        key["callback_data"]
        for row in panel["buttons"]
        for key in row
        if fragment in key.get("callback_data", "")
    )


def test_admin_with_selected_account_can_open_access_deny_and_then_allow_again():
    state = initial_state()
    account = add_account(state, "encrypted", 700, "Owner account")
    request = route(state, message(99))[0]
    stale_allow = find_action(request, "access:allow:")
    denied = route(state, callback(ADMIN, find_action(request, "access:deny:")))[0]
    assert state["users"]["99"]["status"] == "denied"
    fresh_allow = find_action(denied, "access:allow:")
    assert "acct:" not in fresh_allow
    # Clicking the old request does not silently do nothing: it presents current controls.
    updated = route(state, callback(ADMIN, stale_allow))[0]
    assert state["users"]["99"]["status"] == "denied"
    route(state, callback(ADMIN, find_action(updated, "access:allow:")))
    assert state["users"]["99"]["status"] == "approved"
    assert state["selected_account"] == account
    home = route(state, message(ADMIN))[0]
    access = find_action(home, "access:list:")
    assert "acct:" not in access
    result = route(state, callback(ADMIN, access))[0]
    assert "Доступ к боту" in result["text"]
    assert all(len(key.get("callback_data", "").encode()) <= 64 for row in result["buttons"] for key in row)


def test_legacy_account_scoped_access_button_works_without_switching_accounts():
    state = initial_state()
    account = add_account(state, "encrypted", 700, "Owner account")
    route(state, message(99))
    panel = route(state, message(ADMIN, "/users"))[0]
    action = find_action(panel, "access:allow:")
    _, revision, action = action.split(":", 2)
    legacy = f"ui:{revision}:acct:old-account:{action}"
    route(state, callback(ADMIN, legacy))
    assert state["users"]["99"]["status"] == "approved"
    assert state["selected_account"] == account


def test_stale_scoped_approval_cannot_undo_revocation_and_guest_cannot_approve():
    state = initial_state()
    account = add_account(state, "encrypted", 700, "Owner account")
    route(state, message(99))
    panel = route(state, message(ADMIN, "/users"))[0]
    _, revision, action = find_action(panel, "access:allow:").split(":", 2)
    legacy = f"ui:{revision}:acct:{account}:{action}"
    allow(state, 100)
    access_before = copy.deepcopy(state["users"]["99"])
    route(state, callback(100, legacy))
    assert state["users"]["99"] == access_before
    route(state, message(ADMIN, "/allow 99"))
    route(state, message(ADMIN, "/revoke 99"))
    refreshed = route(state, callback(ADMIN, legacy))
    assert state["users"]["99"]["status"] == "revoked"
    assert refreshed and "Доступ к боту" in refreshed[0]["text"]
