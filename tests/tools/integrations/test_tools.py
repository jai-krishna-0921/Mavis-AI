import pytest

from mavis.domain.errors import ActionFailed, ConnectionRequired, IntegrationError
from mavis.domain.integrations import ConnectionState, ToolResult
from mavis.domain.policy import Capability, RiskClass
from mavis.tools.integrations.actions import ACTIONS, WORKSPACE_CAPABILITIES, MailSearchArgs
from mavis.tools.integrations.connections import ConnectionCache
from mavis.tools.integrations.tools import gated, register_integration_tools, tool_name
from mavis.tools.registry import ToolContext, ToolRegistry
from tests.tools.integrations.fakes import FakeProvider

CTX = ToolContext(user_id=1, timezone="Asia/Kolkata", task_id=1)


async def test_gated_returns_rendered_data_when_active(provider, cache):
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    provider.results["mail.search"] = ToolResult(ok=True, data={"messages": [{"subject": "Hi"}]})
    out = await gated(CTX, "mail.search", MailSearchArgs(query="x"), provider=provider, cache=cache)
    assert '"subject": "Hi"' in out
    assert provider.executed[0][2] == {"query": "x", "max_results": 10}


async def test_not_connected_raises_connection_required(provider, cache):
    with pytest.raises(ConnectionRequired) as exc:
        await gated(CTX, "mail.search", MailSearchArgs(), provider=provider, cache=cache)
    assert exc.value.capability is Capability.GMAIL
    assert exc.value.reason == "check and handle your email"
    assert exc.value.revoked is False
    assert provider.executed == []


async def test_failed_execute_with_revoked_status_raises_revoked(provider, cache):
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    await cache.status(1)                                     # cached as ACTIVE
    provider.set_state(1, Capability.GMAIL, ConnectionState.FAILED)  # token revoked upstream
    provider.results["mail.search"] = ToolResult(ok=False, error="Composio answered 400 for POST /x")
    with pytest.raises(ConnectionRequired) as exc:
        await gated(CTX, "mail.search", MailSearchArgs(), provider=provider, cache=cache)
    assert exc.value.revoked is True
    assert "expired" in exc.value.reason


async def test_failed_execute_while_still_active_reports_failure(provider, cache):
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    provider.results["mail.search"] = ToolResult(ok=False, error="quota exceeded")
    with pytest.raises(ActionFailed) as exc:
        await gated(CTX, "mail.search", MailSearchArgs(), provider=provider, cache=cache)
    assert str(exc.value) == "mail.search failed: quota exceeded"
    assert exc.value.reason == "quota exceeded"


async def test_integration_error_returns_sentence(cache):
    class Down(FakeProvider):
        async def status(self, user):
            raise IntegrationError("could not reach Composio: ConnectError")

    down = Down()
    dcache = ConnectionCache(down, ttl_s=60)
    with pytest.raises(ActionFailed) as exc:
        await gated(CTX, "mail.search", MailSearchArgs(), provider=down, cache=dcache)
    assert str(exc.value).startswith("Gmail is unreachable right now")
    assert exc.value.reason == "Gmail is unreachable right now"


def test_register_integration_tools():
    registry = ToolRegistry()
    names = register_integration_tools(registry)
    assert names == names_all(registry)  # flag off: no Workspace tools, and internal actions never
    assert tool_name("calendar.create_event") == "calendar_create_event" and "calendar_create_event" in names
    tool = registry.get("calendar_create_event")
    assert tool.requires is Capability.CALENDAR and tool.preview_needs_ctx is True
    assert tool.risk_fn is not None


async def test_failed_account_in_status_is_revoked(provider, cache):
    provider.set_state(1, Capability.GMAIL, ConnectionState.FAILED)
    with pytest.raises(ConnectionRequired) as exc:
        await gated(CTX, "mail.search", MailSearchArgs(), provider=provider, cache=cache)
    assert exc.value.revoked is True
    assert provider.executed == []


def test_chat_can_send_mail_with_approval():
    registry = ToolRegistry()
    register_integration_tools(registry)
    for name in ("mail_read", "mail_draft", "mail_send", "mail_reply", "calendar_free_slots"):
        assert "conversation" in registry.get(name).agents
    send = registry.get("mail_send")
    assert send.untrusted_output is True
    assert send.risk is RiskClass.OUTWARD and send.risk.needs_approval
    assert registry.get("mail_search").risk is RiskClass.READ
    assert all(registry.get(n).untrusted_output for n in names_all(registry))


def names_all(registry):
    return [tool_name(a) for a, spec in ACTIONS.items()
            if spec.agents and spec.capability not in WORKSPACE_CAPABILITIES]


def test_load_builtin_tools_registers_integration_tools():
    from mavis.tools import load_builtin_tools

    registry = ToolRegistry()
    load_builtin_tools(registry)
    assert "mail_send" in registry.names_for("conversation")
    assert "calendar_create_event" in registry.names_for("conversation")


async def test_model_driven_failure_returns_friendly_wrapped_text(provider, cache, monkeypatch):
    from mavis.tools.integrations import tools as tools_mod

    monkeypatch.setattr(tools_mod, "_deps", lambda p, c: (provider, cache))
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    provider.results["mail.search"] = ToolResult(ok=False, error="quota exceeded")
    registry = ToolRegistry()
    register_integration_tools(registry)
    tool = registry.get("mail_search")
    out = await registry.invoke(tool, 1, MailSearchArgs(query="x"))
    assert out == '<untrusted source="mail_search">\nmail.search failed: quota exceeded\n</untrusted>'
