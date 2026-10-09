"""Two-user isolation (spec 6.1): seed users A and B with distinctive data, read everything as B."""

from __future__ import annotations

import pytest

from mavis.domain import redis_keys
from mavis.domain.memory import Extraction
from mavis.domain.messages import Role
from mavis.store import artifacts
from mavis.store.repo import messages, users

MARKERS = {"A": "zebra-quartz-41", "B": "otter-lilac-77"}


@pytest.fixture
async def two(db, memory, fake_llm):
    a, _ = await users.get_or_create_by_chat(5001, "Priya")
    b, _ = await users.get_or_create_by_chat(7302, "Tomas")
    for u, key in ((a, "A"), (b, "B")):
        await messages.log(u.id, Role.USER, f"my code word is {MARKERS[key]}", event_id=f"iso:{u.id}")
        fake_llm.push_structured(Extraction())
        await memory.learn(u.id, f"I keep the spare key under the {MARKERS[key]} pot",
                           source_ref=f"iso:{u.id}")
    return a, b


async def test_repo_reads_and_recall_never_return_the_other_user(two, memory):
    a, b = two
    rows = await messages.recent(b.id, 50)
    assert all(MARKERS["A"] not in r.content for r in rows)
    ctx = await memory.recall(b.id, "where is the spare key")
    assert MARKERS["A"] not in ctx.model_dump_json()


@pytest.mark.parametrize("uid", [3, 41, 907])
def test_artifact_guard_rejects_other_prefixes(settings, uid):
    mine = artifacts.user_dir(uid, 12) / "chart.png"
    assert artifacts.guard(uid, mine) == mine.resolve()
    for bad in (artifacts.user_dir(uid + 1) / "x.pdf", settings.artifacts_dir / "x.pdf",
                artifacts.user_dir(uid) / ".." / f"u{uid + 1}" / "y.csv"):
        with pytest.raises(PermissionError):
            artifacts.guard(uid, bad)


@pytest.mark.parametrize("uid,area", [(1, "mbox"), (22, "lease"), (333, "spend")])
def test_user_keys_match_the_deletion_pattern(uid, area):
    import fnmatch

    key = redis_keys.user_key(area, uid, "chat")
    assert fnmatch.fnmatch(key, redis_keys.user_pattern(uid))
    assert not fnmatch.fnmatch(key, redis_keys.user_pattern(uid + 1))


def test_neighborhood_query_constrains_every_node():
    from mavis.memory.neo4j_graph import q_neighborhood

    for hops in (1, 2, 3):
        assert "all(n IN nodes(p) WHERE n.user_id = $u)" in q_neighborhood(hops)


async def test_workspace_guard_survives_a_second_process(fake_redis):
    from mavis.tools.integrations import workspace_guard as wg

    await wg.record_created(77, ["doc-1", "sheet-2"])
    wg._created.clear()  # a different worker process has no memory of it
    assert await wg.created_by(77) == {"doc-1", "sheet-2"}


async def test_outbox_never_sends_another_users_file(db, channel, settings):
    from mavis.channels.outbox_sender import OutboxSender
    from mavis.domain.messages import Outbound
    from mavis.store.repo import outbox

    a, _ = await users.get_or_create_by_chat(5101, "Priya")
    b, _ = await users.get_or_create_by_chat(7102, "Tomas")
    artifacts.user_dir(a.id).mkdir(parents=True, exist_ok=True)
    secret = artifacts.user_dir(a.id) / "taxes.pdf"
    secret.write_bytes(b"pdf")
    mine = artifacts.user_dir(b.id)
    mine.mkdir(parents=True, exist_ok=True)
    (mine / "ok.pdf").write_bytes(b"pdf")
    await outbox.enqueue_now(Outbound(user_id=b.id, text="x", document_path=str(secret), dedupe_key="leak"))
    await outbox.enqueue_now(Outbound(user_id=b.id, text="ok", document_path=str(mine / "ok.pdf"),
                                      dedupe_key="fine"))
    await OutboxSender(channel).run_once()
    await OutboxSender(channel).run_once()  # the refused row is failed; the next one is released
    assert [m.path for m in channel.sent if m.kind == "document"] == [str(mine / "ok.pdf")]


async def test_drive_upload_refuses_a_file_in_another_users_directory(db, settings):
    from mavis.tools.integrations.workspace_tools import _artifact_file

    a, _ = await users.get_or_create_by_chat(5201, "Priya")
    b, _ = await users.get_or_create_by_chat(7202, "Tomas")
    artifacts.user_dir(a.id).mkdir(parents=True, exist_ok=True)
    f = artifacts.user_dir(a.id) / "deck.pptx"
    f.write_bytes(b"PK")
    assert _artifact_file(str(f), a.id)[1] == 2
    assert _artifact_file(str(f), b.id)[1] is None


async def test_workspace_guard_without_redis_keeps_the_local_copy(db):
    from mavis.tools.integrations import workspace_guard as wg

    wg._created.clear()
    await wg.record_created(5, ["f1"])
    await wg.record_created(None, ["ignored"])
    assert await wg.created_by(5) == {"f1"} and await wg.created_by(6) == set()


async def test_legacy_flat_files_stay_readable_by_the_owner_only(db, settings, monkeypatch):
    from mavis.config import get_settings

    flat = settings.artifacts_dir / "old-deck.pptx"
    flat.write_bytes(b"PK")
    assert artifacts.guard(1, flat, legacy_ok=True) == flat.resolve()
    with pytest.raises(PermissionError):
        artifacts.guard(1, flat)  # not the owner: refused
    with pytest.raises(PermissionError):
        artifacts.guard(1, settings.artifacts_dir / "gone.pptx", legacy_ok=True)  # must exist
    other = artifacts.user_dir(2)
    other.mkdir(parents=True, exist_ok=True)
    (other / "x.pdf").write_bytes(b"x")
    with pytest.raises(PermissionError):
        artifacts.guard(1, other / "x.pdf", legacy_ok=True)  # another user's directory is never legacy
    assert get_settings()


async def test_owner_gets_a_legacy_document_and_a_stranger_does_not(db, channel, settings, monkeypatch):
    from mavis.channels.outbox_sender import OutboxSender
    from mavis.config import get_settings
    from mavis.domain.messages import Outbound
    from mavis.store.repo import outbox

    monkeypatch.setenv("OWNER_TELEGRAM_CHAT_IDS", "[5301]")
    get_settings.cache_clear()
    owner, _ = await users.get_or_create_by_chat(5301, "Priya")
    guest, _ = await users.get_or_create_by_chat(5302, "Tomas")
    flat = settings.artifacts_dir / "report.pdf"
    flat.write_bytes(b"pdf")
    for u in (owner, guest):
        await outbox.enqueue_now(Outbound(user_id=u.id, text="f", document_path=str(flat),
                                          dedupe_key=f"d{u.id}"))
    await OutboxSender(channel).run_once()
    assert [m.path for m in channel.sent if m.kind == "document"] == [str(flat)]


async def test_staged_files_land_in_the_users_directory(db, settings, tmp_path):
    src = tmp_path / "report.html"
    src.write_text("<p>hi</p>")
    out = artifacts.stage(7, src, task_id=3, name="r.html")
    assert out == artifacts.user_dir(7, 3) / "r.html" and out.read_text() == "<p>hi</p>"
    assert artifacts.guard(7, out) == out.resolve()


# --- native Google and Slack grants (spec 6.1 extended to the connectors) -----------------------------------

from datetime import timedelta  # noqa: E402

from cryptography.exceptions import InvalidTag  # noqa: E402
from sqlalchemy import update  # noqa: E402

from mavis.domain import timeutil  # noqa: E402
from mavis.domain.integrations import ToolResult, UserRef  # noqa: E402
from mavis.store import db as dbm  # noqa: E402
from mavis.store.models import NativeGrant  # noqa: E402
from mavis.tools.integrations.native.base import NativeProvider, ReauthRequired  # noqa: E402
from mavis.tools.integrations.native.oauth import OAuthError, sign_state  # noqa: E402
from mavis.tools.integrations.native.router import NativeRouter  # noqa: E402
from mavis.tools.integrations.native.tokens import AccountTaken, NativeTokenStore  # noqa: E402
from tests.tools.integrations.native.conftest import *  # noqa: E402, F403 - fixtures
from tests.tools.integrations.native.test_oauth import query  # noqa: E402

G, S = NativeProvider.GOOGLE, NativeProvider.SLACK
SECRET_A = "ya29.secret-of-user-a"


@pytest.fixture
async def duo(db, native_env, client):
    """Users A and B (both active), A with a Google and a Slack grant plus the workspace bot, B with none."""
    a, _ = await users.get_or_create_by_chat(8201, "Priya")
    b, _ = await users.get_or_create_by_chat(8202, "Tomas")
    for u in (a, b):
        await users.update(u.id, status="active")
    tokens = NativeTokenStore(client)
    await tokens.save(a.id, G, account={"email": "priya@x.com", "scopes": ["gmail.readonly"]},
                      access_token=SECRET_A, refresh_token="r-a",
                      expires_at=timeutil.now() + timedelta(hours=1))
    await tokens.save(a.id, S, account={"team_id": "T1", "user_id": "UA", "scopes": []},
                      access_token="xoxp-a", refresh_token=None, expires_at=None)
    await tokens.save(a.id, NativeProvider.SLACK_BOT, account={"team_id": "T1", "user_id": "UA", "dm": "DA"},
                      access_token="xoxb-a", refresh_token=None, expires_at=None)
    return a.id, b.id, tokens


async def test_user_b_never_reads_a_grant_or_token_of_user_a(duo):
    a, b, tokens = duo
    assert await tokens.grants(b) == [] and await tokens.grant(b, G) is None
    assert await tokens.account(b, G) is None
    with pytest.raises(ReauthRequired):
        await tokens.access_token(b, G)
    assert await tokens.access_token(a, G) == SECRET_A
    assert await tokens.reveal(b, G) is None


async def test_b_cannot_claim_a_vendor_account(duo):
    a, b, tokens = duo
    with pytest.raises(AccountTaken):
        await tokens.save(b, G, account={"email": "Priya@X.com", "scopes": []}, access_token="b",
                          refresh_token=None, expires_at=None)
    with pytest.raises(AccountTaken):
        await tokens.save(b, S, account={"team_id": "T1", "user_id": "UA", "scopes": []}, access_token="b",
                          refresh_token=None, expires_at=None)
    assert await tokens.grants(b) == []


async def test_a_sealed_token_copied_into_another_users_row_does_not_open(duo):
    """Envelope encryption is bound to user, provider and column: a row copied between users is noise."""
    a, b, tokens = duo
    async with dbm.Session() as s:
        stolen = await s.scalar(select_grant(a, G))
    await tokens.save(b, G, account={"email": "tomas@x.com", "scopes": []}, access_token="own",
                      refresh_token=None, expires_at=timeutil.now() + timedelta(hours=1))
    async with dbm.Session() as s:
        await s.execute(update(NativeGrant).where(NativeGrant.user_id == b, NativeGrant.provider == G.value)
                        .values(access_token=stolen.access_token))
        await s.commit()
    with pytest.raises(InvalidTag):
        await tokens.access_token(b, G)
    assert await tokens.access_token(a, G) == SECRET_A


def select_grant(uid, provider):
    from sqlalchemy import select

    return select(NativeGrant).where(NativeGrant.user_id == uid, NativeGrant.provider == provider.value)


class _Exec:
    provider = G

    def __init__(self):
        self.calls: list[int] = []

    def handles(self, action):
        return action == "mail.search"

    async def execute(self, user, action, args):
        self.calls.append(user.user_id)
        return ToolResult(ok=True, data={"native": True})


async def test_router_never_runs_user_a_grant_for_user_b(duo, client):
    from tests.tools.integrations.fakes import FakeProvider

    a, b, tokens = duo
    fallback, ex = FakeProvider(), _Exec()
    router = NativeRouter(fallback, tokens, NativeOAuthFor(tokens, client), [ex], client)
    a_scopes = {"https://www.googleapis.com/auth/gmail.readonly"}
    async with dbm.Session() as s:
        await s.execute(update(NativeGrant).where(NativeGrant.user_id == a, NativeGrant.provider == G.value)
                        .values(account={"email": "priya@x.com", "scopes": sorted(a_scopes)}))
        await s.commit()
    assert (await router.execute(UserRef(user_id=a), "mail.search", {})).data == {"native": True}
    await router.execute(UserRef(user_id=b), "mail.search", {})
    assert ex.calls == [a] and [e[0] for e in fallback.executed] == [b]  # B went to its own Composio identity
    states = await router.status(UserRef(user_id=b))
    assert "gmail" not in {k for k, v in states.items() if v.name == "ACTIVE"}


async def test_disconnecting_b_leaves_a_connected(duo, client):
    from tests.tools.integrations.fakes import FakeProvider

    a, b, tokens = duo
    router = NativeRouter(FakeProvider(), tokens, NativeOAuthFor(tokens, client), [], client)
    await router.disconnect(UserRef(user_id=b), "slack")
    await router.disconnect(UserRef(user_id=b), "gmail")
    assert {g.provider for g in await tokens.grants(a)} == {G, S, NativeProvider.SLACK_BOT}


async def test_a_consent_started_by_a_cannot_be_finished_as_b(duo, client, vendor):
    a, b, tokens = duo
    oauth_ = NativeOAuthFor(tokens, client)
    url = await oauth_.authorize_url(a, G)
    nonce_state = query(url)["state"]
    from mavis.tools.integrations.native.oauth import verify_state

    st = verify_state(nonce_state)
    from sqlalchemy import select

    from mavis.store.models import NativeOAuthState

    async with dbm.Session() as s:
        nonce = await s.scalar(select(NativeOAuthState.nonce).where(NativeOAuthState.user_id == a))
    forged = sign_state(b, G.value, st.verifier, nonce=nonce)  # B's identity on A's single-use nonce
    with pytest.raises(OAuthError) as err:
        await oauth_.complete(forged, "code", G)
    assert err.value.kind == "replayed_state" and await tokens.grant(b, G) is None
    assert vendor.requests == []  # the vendor was never reached


def NativeOAuthFor(tokens, client):  # noqa: N802
    from mavis.tools.integrations.native.oauth import NativeOAuth

    return NativeOAuth(tokens, client)


async def test_slack_identity_maps_to_exactly_one_user_and_one_workspace(duo):
    a, b, tokens = duo
    assert await tokens.user_for_slack("T1", "UA") == a
    assert await tokens.user_for_slack("T2", "UA") is None  # same Slack user id in another workspace
    assert await tokens.user_for_slack("T1", "UB") is None
    assert await tokens.bot_dm_owner("T1", "DA") == a and await tokens.bot_dm_owner("T1", "DB") is None
    await tokens.save(b, S, account={"team_id": "T1", "user_id": "UB", "scopes": []}, access_token="xoxp-b",
                      refresh_token=None, expires_at=None)
    assert await tokens.user_for_slack("T1", "UB") == b and await tokens.user_for_slack("T1", "UA") == a


async def test_slack_inbound_events_carry_the_senders_own_user_id(duo, monkeypatch):
    from mavis.channels import routing, slack_inbound
    from mavis.tools.integrations.native import slack_events
    from tests.channels.slack_fakes import SlackFake, callback, dm_message
    from tests.tools.integrations.fakes import FakeBus

    a, b, tokens = duo
    await tokens.save(b, S, account={"team_id": "T1", "user_id": "UB", "scopes": []}, access_token="xoxp-b",
                      refresh_token=None, expires_at=None)
    slack_inbound._told.clear()
    monkeypatch.setattr(slack_events, "_dedupe", slack_events.EventDedupe())
    routing.set_slack_channel(SlackFake())
    try:
        bus = FakeBus()
        for n, slack_user in enumerate(("UA", "UB", "UX")):
            payload = callback(dm_message("hello", user=slack_user, ts=f"17600002{n}0.000100"),
                               event_id=f"EvI{n}", user_auth=slack_user)
            payload["team_id"] = "T1"
            for auth in payload["authorizations"]:
                auth["team_id"] = "T1"
            await slack_events.handle_callback(payload, tokens, bus)
        assert sorted(e.user_id for e in bus.events) == sorted([a, b])  # the stranger produced nothing
        by_text = {e.user_id: e.payload["text"] for e in bus.events}
        assert by_text == {a: "hello", b: "hello"}
    finally:
        routing.set_slack_channel(None)
