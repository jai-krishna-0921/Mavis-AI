from __future__ import annotations

from mavis.domain.policy import Capability
from mavis.tools.integrations.actions import GOOGLE_ANCHOR, GOOGLE_CAPABILITIES
from mavis.tools.integrations.native import oauth, providers, tokens
from mavis.tools.integrations.native.base import NativeProvider


def test_three_builtin_specs_registered():
    assert {"google", "slack", "slack_bot"} <= set(providers.PROVIDERS)
    assert [s.id for s in providers.user_providers()][:2] == ["google", "slack"]
    assert providers.spec_of("slack_bot").internal is True


def test_google_spec_matches_the_old_constants():
    g = providers.spec_of(NativeProvider.GOOGLE)
    assert g.auth_url == oauth.GOOGLE_AUTH_URL and g.token_url == tokens.GOOGLE_TOKEN_URL
    assert g.revoke_url == oauth.GOOGLE_REVOKE_URL and g.pkce is True
    assert g.scope_params[0].scopes == oauth.GOOGLE_SCOPES
    assert g.extra_auth_params == {"access_type": "offline", "prompt": "consent",
                                   "include_granted_scopes": "true"}
    assert g.gone_codes == frozenset({"invalid_grant"})
    assert g.capabilities == GOOGLE_CAPABILITIES and g.anchor is GOOGLE_ANCHOR
    assert g.account_key({"email": "A@B.com"}) == "a@b.com"
    assert g.forget_prefixes == ("gmail:",)
    assert (g.client_id_setting, g.client_secret_setting) == ("google_oauth_client_id",
                                                              "google_oauth_client_secret")


def test_slack_spec_matches_the_old_constants():
    s = providers.spec_of(NativeProvider.SLACK)
    assert s.pkce is False and s.soft_errors is True and s.token_url == tokens.SLACK_TOKEN_URL
    assert [(p.name, p.sep) for p in s.scope_params] == [("scope", ","), ("user_scope", ",")]
    assert s.scope_params[0].scopes == oauth.SLACK_BOT_SCOPES
    assert s.scope_params[1].scopes == oauth.SLACK_USER_SCOPES
    assert s.account_key({"team_id": "T1", "user_id": "U1"}) == "T1/U1"
    assert s.account_key({"user_id": "U1"}) == "U1"
    assert s.capabilities == (Capability.SLACK,) and s.companions == ("slack_bot",)
    assert s.forget_prefixes == ("slack:",)


def test_slack_bot_shares_the_slack_account_key():
    bot = providers.spec_of(NativeProvider.SLACK_BOT)
    assert bot.account_key({"team_id": "T1", "user_id": "U1"}) == "T1/U1"
    assert bot.capabilities == ()
