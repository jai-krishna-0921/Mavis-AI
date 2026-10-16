"""ConnectFlow with the real Microsoft family: menu row, family state, announcement, whole-grant forget."""

from __future__ import annotations

from mavis.domain.integrations import ConnectionState
from mavis.domain.policy import Capability as C
from tests.tools.integrations.test_connect_flow_registry import changed, make_flow

OL, OC, OD, TE = C.OUTLOOK, C.OUTLOOK_CALENDAR, C.ONEDRIVE, C.TEAMS


def _live(provider, *caps):
    for c in caps:
        provider.set_state(1, c, ConnectionState.ACTIVE)


async def test_menu_has_one_microsoft_row_on_the_anchor(
    db, ms_env, provider, cache, fake_bus, rec, state
):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    rows = [(c, label) for c, label in flow._menu() if label == "Microsoft 365"]
    assert rows == [(OL, "Microsoft 365")]
    assert not [1 for c, _ in flow._menu() if c in (OC, TE, OD)]


async def test_menu_has_no_microsoft_row_when_it_is_off(db, settings, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    assert all("Microsoft" not in label for _, label in flow._menu())


async def test_starting_teams_while_outlook_is_active_does_not_ask_again(
    db, ms_env, provider, cache, fake_bus, rec, state
):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    _live(provider, OL, OC, OD)
    assert flow._state_of(TE, {"outlook": ConnectionState.ACTIVE}) is ConnectionState.ACTIVE
    before = len(rec.sent)
    await flow.start(1, TE, "")
    assert not any("accounts.google" in (m.text or "") or "oauth/microsoft" in (m.text or "")
                   for m in rec.sent[before:])


async def test_announcement_names_the_four_services(db, ms_env, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    pid = await flow.start(1, OL, "")
    _live(provider, OL, OC, OD)
    ev = changed(pid)
    ev.payload["capability"] = "outlook"
    await flow.on_connection_changed(ev)
    assert "Outlook mail, Outlook Calendar, Teams chats and OneDrive" in rec.sent[-1].text
    assert set((await state.get(1))["synced"]) == {"outlook", "outlookcalendar", "onedrive"}


async def test_disconnecting_any_service_forgets_all_four_prefixes(
    db, ms_env, provider, cache, fake_bus, rec, state
):
    calls = []

    async def forget(user_id, prefix):
        calls.append(prefix)
        return 1

    flow = make_flow(provider, cache, fake_bus, rec, state, forget=forget)
    _live(provider, OL, OC, OD)
    assert await flow.disconnect(1, OD) is True
    assert sorted(calls) == ["ocal:", "onedrive:", "outlook:", "teams:"]


class _Granting:
    """Wraps the provider double with the router's provider_grant."""

    def __init__(self, inner, grant):
        self._inner, self._grant = inner, grant

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def provider_grant(self, user_id, provider):
        return self._grant


def _grant(tid, scopes=None):
    from mavis.tools.integrations.native.base import NativeProvider
    from mavis.tools.integrations.native.microsoft_spec import MS_SCOPES
    from mavis.tools.integrations.native.tokens import ACTIVE, Grant

    names = list(MS_SCOPES if scopes is None else scopes)
    return Grant(user_id=1, provider=NativeProvider.MICROSOFT, status=ACTIVE,
                 account={"tid": tid, "oid": "o", "mail": "m@x.test", "scopes": names})


async def _announce(db, provider, cache, fake_bus, rec, state, grant):
    flow = make_flow(_Granting(provider, grant), cache, fake_bus, rec, state)
    pid = await flow.start(1, OL, "")
    _live(provider, OL, OC, OD)
    ev = changed(pid)
    ev.payload["capability"] = "outlook"
    await flow.on_connection_changed(ev)
    return rec.sent[-1].text


async def test_work_announcement_names_all_four(db, ms_env, provider, cache, fake_bus, rec, state):
    work = _grant("11111111-2222-3333-4444-555555555555")
    text = await _announce(db, provider, cache, fake_bus, rec, state, work)
    assert text == ("Connected ✓ I can now work with your Outlook mail, Outlook Calendar, Teams chats and "
                    "OneDrive.")


async def test_personal_announcement_drops_teams_and_adds_the_note(
    db, ms_env, provider, cache, fake_bus, rec, state
):
    from mavis.tools.integrations.native.microsoft_spec import PERSONAL_TENANT

    text = await _announce(db, provider, cache, fake_bus, rec, state, _grant(PERSONAL_TENANT))
    assert text == ("Connected ✓ I can now work with your Outlook mail, Outlook Calendar and OneDrive. "
                    "Note: Teams is not available for personal Microsoft accounts.")
    assert "Not allowed on" not in text


async def test_disconnect_clears_microsoft_cursors_teams_owed_and_its_defaults_only(
    db, ms_env, provider, cache, fake_bus, rec, state
):
    await state.update(1, {
        "cursors": {"outlook_delta": "L", "ocal_delta": "L", "ocal_delta_at": "t", "teams_at": "t",
                    "teams_chats": {"ms:a": "t"}, "teams_resume": {"ms:a": {}}, "onedrive_seen": ["x"],
                    "onedrive_at": "t", "ms_backoff_n": 2, "ms_backoff_until": "t",
                    "gmail_after": 5, "slack_ts": {"C1": "1.0"}},
        "slack_owed": {"teams:abc": {"source": "teams", "chat": "ms:a", "from": "Alex", "ts": "1.0"},
                       "D123": {"team": "T", "from": "Sam", "ts": "2.0", "notified": False}},
        "defaults": {"mail": "microsoft", "calendar": "google"},
    })
    flow = make_flow(provider, cache, fake_bus, rec, state)
    _live(provider, OL, OC, OD)
    assert await flow.disconnect(1, TE, forget=False) is True
    st = await state.get(1)
    assert st["cursors"] == {"gmail_after": 5, "slack_ts": {"C1": "1.0"}}
    assert list(st["slack_owed"]) == ["D123"]
    assert st["defaults"] == {"calendar": "google"}


async def test_disconnecting_slack_clears_its_cursors_and_owed_dms_but_not_teams(
    db, settings, provider, cache, fake_bus, rec, state
):
    await state.update(1, {
        "cursors": {"slack_ts": {"C1": "1.0"}, "slack_offset": 3, "gmail_after": 5, "teams_at": "t"},
        "slack_owed": {"teams:abc": {"source": "teams", "chat": "ms:a"}, "D123": {"team": "T", "ts": "2.0"}},
    })
    flow = make_flow(provider, cache, fake_bus, rec, state)
    provider.set_state(1, C.SLACK, ConnectionState.ACTIVE)
    assert await flow.disconnect(1, C.SLACK, forget=False) is True
    st = await state.get(1)
    assert st["cursors"] == {"gmail_after": 5, "teams_at": "t"}
    assert list(st["slack_owed"]) == ["teams:abc"]


async def test_disconnect_clears_under_the_row_lock_and_keeps_a_cursor_written_meanwhile(
    db, ms_env, provider, cache, fake_bus, rec, user
):
    from mavis.store.repo import users
    from mavis.tools.integrations.connect_flow import RepoUserState

    class Stale(RepoUserState):
        """get() returns what a tick had written before the poll wrote gmail_after=9 (a racing writer)."""

        async def get(self, user_id):
            snap = await super().get(user_id)
            await users.update_nested(user_id, "cursors", {"gmail_after": 9, "teams_at": "late"})
            return snap

    await users.update_state(user.id, {
        "cursors": {"teams_at": "t", "gmail_after": 1},
        "slack_owed": {"teams:abc": {"source": "teams"}}, "defaults": {"mail": "microsoft"}})
    flow = make_flow(provider, cache, fake_bus, rec, Stale())
    provider.set_state(user.id, OL, ConnectionState.ACTIVE)
    assert await flow.disconnect(user.id, OL, forget=False) is True
    st = await users.get_state(user.id)
    # the racing tick's Microsoft cursor is gone, Gmail's is kept
    assert st["cursors"] == {"gmail_after": 9}
    assert st["slack_owed"] == {} and st["defaults"] == {}
