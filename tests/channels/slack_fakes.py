"""Shared doubles for the Slack chat tests: a lookup of users, bots and DMs, and a recording channel."""

from __future__ import annotations

from mavis.channels.fake import FakeChannel

TEAM, BOT_UID = "T0TEAM001", "U0BOT0001"
ME, ALICE, STRANGER = "U0ME000001", "U0ALICE001", "U0STRANGE1"
MY_DM, ALICE_DM = "D0MYDM0001", "D0ALICE001"


class Lookup:
    """Slack user -> Mavis user, the workspace bot, and which Mavis user owns which bot DM."""

    def __init__(self, users=None, dms=None, bot=True):
        self.users = users if users is not None else {(TEAM, ME): 7, (TEAM, ALICE): 8}
        self.dms = dms if dms is not None else {(TEAM, MY_DM): 7, (TEAM, ALICE_DM): 8}
        self.bot = {"bot_user_id": BOT_UID, "team_id": TEAM} if bot else None

    async def user_for_slack(self, team_id, slack_user_id):
        return self.users.get((team_id, slack_user_id))

    async def bot_account(self, team_id):
        return self.bot if team_id == TEAM else None

    async def bot_dm_owner(self, team_id, channel):
        return self.dms.get((team_id, channel))


def callback(event, *, event_id="Ev0001", bot_auth=True, user_auth=ME):
    auths = []
    if bot_auth:
        auths.append({"team_id": TEAM, "user_id": BOT_UID, "is_bot": True})
    if user_auth:
        auths.append({"team_id": TEAM, "user_id": user_auth, "is_bot": False})
    return {"type": "event_callback", "team_id": TEAM, "event_id": event_id, "event_time": 1760000100,
            "authorizations": auths, "event": event}


def dm_message(text="what is on my calendar?", *, user=ME, channel=MY_DM, ts="1760000100.000100", **kw):
    return {"type": "message", "channel": channel, "channel_type": "im", "user": user, "text": text,
            "ts": ts, "event_ts": ts, **kw}


def mention(text=f"<@{BOT_UID}> summarise the thread", *, user=ME, channel="C0CHAN001",
            ts="1760000200.000100", **kw):
    return {"type": "app_mention", "channel": channel, "user": user, "text": text, "ts": ts,
            "event_ts": ts, **kw}


class SlackFake(FakeChannel):
    """FakeChannel plus open_dm, standing in for the process Slack channel."""

    async def open_dm(self, team, slack_user):
        return {ME: MY_DM, ALICE: ALICE_DM}[slack_user]
