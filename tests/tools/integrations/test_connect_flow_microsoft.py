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
