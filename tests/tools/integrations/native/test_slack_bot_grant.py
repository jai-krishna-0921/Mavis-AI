"""The Slack bot token travels beside the user token: sealed, per team, removed with the connection."""

import httpx
from sqlalchemy import select

from mavis.store import db as dbm
from mavis.store.models import NativeGrant
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.oauth import SLACK_OPEN_DM_URL, SLACK_REVOKE_URL
from mavis.tools.integrations.native.tokens import SLACK_TOKEN_URL

from .conftest import *  # noqa: F403 - fixtures
from .test_oauth import query

S, B = NativeProvider.SLACK, NativeProvider.SLACK_BOT
BOT_TOKEN = "xoxb-bot-secret-token"


def install(vendor, *, bot=True, dm="D0DM00001"):
    body = {"ok": True, "team": {"id": "T1", "name": "Kripya"},
            "authed_user": {"id": "U1", "scope": "channels:history,chat:write", "access_token": "xoxp-user"}}
    if bot:
        body |= {"access_token": BOT_TOKEN, "bot_user_id": "UBOT01", "scope": "chat:write,im:history"}
    vendor.routes[SLACK_TOKEN_URL] = lambda r: httpx.Response(200, json=body)
    vendor.routes[SLACK_OPEN_DM_URL] = lambda r: httpx.Response(200, json={"ok": True, "channel": {"id": dm}})
    vendor.routes[SLACK_REVOKE_URL] = lambda r: httpx.Response(200, json={"ok": True})


async def connect(oauth, user_id=5):
    state = query(await oauth.authorize_url(user_id, S))["state"]
    return await oauth.complete(state, "code", S)


async def test_bot_token_is_saved_sealed_and_keyed_by_team(oauth, tokens, vendor):
    install(vendor)
    done = await connect(oauth)
    assert "_bot" not in done.account and "token" not in str(done.account)
    assert await tokens.bot_token_for_team("T1") == BOT_TOKEN
    account = await tokens.bot_account("T1")
    assert account["bot_user_id"] == "UBOT01" and account["dm"] == "D0DM00001" and account["team_id"] == "T1"
    async with dbm.Session() as s:
        row = await s.scalar(select(NativeGrant).where(NativeGrant.provider == B.value))
    assert row.account_key == "T1/U1" and BOT_TOKEN not in row.access_token  # sealed at rest
    assert BOT_TOKEN not in str(row.account)


async def test_other_teams_have_no_bot(oauth, tokens, vendor):
    install(vendor)
    await connect(oauth)
    assert await tokens.bot_token_for_team("T2") is None and await tokens.bot_account("") is None


async def test_dm_channel_names_its_owner(oauth, tokens, vendor):
    install(vendor)
    await connect(oauth, user_id=5)
    assert await tokens.bot_dm_owner("T1", "D0DM00001") == 5
    assert await tokens.bot_dm_owner("T1", "D0OTHER") is None
    assert await tokens.bot_dm_owner("T2", "D0DM00001") is None


async def test_install_without_a_bot_still_connects_reading(oauth, tokens, vendor):
    install(vendor, bot=False)
    await connect(oauth)
    assert await tokens.bot_token_for_team("T1") is None
    assert (await tokens.grant(5, S)).status == "ACTIVE"


async def test_a_failed_dm_open_keeps_the_bot_token(oauth, tokens, vendor):
    install(vendor)
    vendor.routes[SLACK_OPEN_DM_URL] = lambda r: httpx.Response(500, text="boom")
    await connect(oauth)
    assert await tokens.bot_token_for_team("T1") == BOT_TOKEN
    assert "dm" not in await tokens.bot_account("T1")


async def test_disconnecting_slack_removes_the_bot_grant(oauth, tokens, vendor):
    install(vendor)
    await connect(oauth)
    await oauth.revoke(5, S)
    assert await tokens.grant(5, S) is None and await tokens.grant(5, B) is None
    assert await tokens.bot_token_for_team("T1") is None
