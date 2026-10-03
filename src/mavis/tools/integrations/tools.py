"""Integration actions exposed as Mavis tools.

Missing/revoked connection -> ConnectionRequired. Phase 4's ToolRegistry checks the capability before
approval (so the user connects first, then approves), and the orchestrator's connect_gate turns the
exception into a {"type": "connect"} interrupt that ConnectFlow (Task 8) answers.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from mavis.domain.errors import ActionFailed, ConnectionRequired, IntegrationError
from mavis.domain.integrations import ToolResult, UserRef
from mavis.tools.integrations.actions import (
    ACTIONS,
    CAPABILITY_PURPOSE,
    WORKSPACE_CAPABILITIES,
    ActionSpec,
    display_name,
    localize,
    workspace_enabled,
)
from mavis.tools.integrations.base import IntegrationProvider, render_result
from mavis.tools.integrations.connections import ConnectionCache
from mavis.tools.integrations.mail_render import RENDERERS
from mavis.tools.integrations.workspace_render import RENDERERS as WORKSPACE_RENDERERS
from mavis.tools.registry import MavisTool, TaintPolicy, ToolContext, ToolRegistry, contextual

REVOKED_REASON = "access expired or was revoked"


def tool_name(action: str) -> str:
    return action.replace(".", "_")


def _deps(provider: IntegrationProvider | None, cache: ConnectionCache | None):
    if provider is None or cache is None:
        from mavis.tools.integrations import get_connection_cache, get_provider

        provider = provider or get_provider()
        cache = cache or get_connection_cache()
    return provider, cache


async def call_action(
    ctx: ToolContext, action: str, args: BaseModel, *, provider: IntegrationProvider, cache: ConnectionCache
) -> ToolResult:
    spec = ACTIONS[action]
    await cache.ensure(ctx.user_id, spec.capability, CAPABILITY_PURPOSE[spec.capability])
    result = await provider.execute(UserRef(user_id=ctx.user_id), action, args.model_dump(mode="json"))
    if not result.ok and not await cache.is_active(ctx.user_id, spec.capability, fresh=True):
        raise ConnectionRequired(spec.capability, REVOKED_REASON, revoked=True)
    return result


async def gated(
    ctx: ToolContext,
    action: str,
    args: BaseModel,
    *,
    provider: IntegrationProvider | None = None,
    cache: ConnectionCache | None = None,
    render: Callable[[Any], Any] | None = None,
) -> Any:
    """Run one integration action. Raises ConnectionRequired, or ActionFailed when it did not happen.

    `render` turns successful data into model-facing text (default: truncated JSON).
    """
    provider, cache = _deps(provider, cache)
    name = display_name(ACTIONS[action].capability)
    try:
        result = await call_action(ctx, action, args, provider=provider, cache=cache)
    except IntegrationError as exc:
        raise ActionFailed(
            f"{name} is unreachable right now ({exc}). Tell the user and offer to try again later.",
            reason=f"{name} is unreachable right now",
        ) from exc
    if not result.ok:
        raise ActionFailed(f"{action} failed: {result.error}", reason=str(result.error or f"{action} failed"))
    if render is not None:
        return render(result.data)
    return render_result(result)


async def action_data(
    ctx: ToolContext,
    action: str,
    args: BaseModel,
    *,
    provider: IntegrationProvider | None = None,
    cache: ConnectionCache | None = None,
) -> Any:
    """Like `gated`, but returns the provider's raw data: for actions that need several calls and for
    pre-steps (file metadata). Same errors as `gated`."""
    return await gated(ctx, action, args, provider=provider, cache=cache, render=lambda data: data)


def _make_tool(spec: ActionSpec) -> MavisTool:
    from mavis.tools.integrations import workspace_tools  # lazy: it imports this module

    custom = workspace_tools.CUSTOM_FNS.get(spec.name)
    render = RENDERERS.get(spec.name) or WORKSPACE_RENDERERS.get(spec.name)

    async def fn(ctx: ToolContext, args: BaseModel) -> str:
        localized = localize(args, ctx.timezone)
        if custom is not None:
            return await custom(ctx, localized)
        return await gated(ctx, spec.name, localized, render=render)

    def preview(args: BaseModel, ctx: ToolContext) -> str:
        localized = localize(args, ctx.timezone)
        if spec.preview:
            return spec.preview(localized, ctx.timezone)
        return f"{spec.name}: {localized.model_dump()}"

    risk_fn = (lambda a, _s=spec: _s.risk_for(a)) if spec.risk_fn else None
    return MavisTool(
        name=tool_name(spec.name),
        description=spec.description,
        args_model=spec.args_model,
        risk=spec.risk,
        fn=contextual(fn),
        agents=spec.agents,
        requires=spec.capability,
        preview=preview,
        preview_needs_ctx=True,
        untrusted_output=True,  # email bodies and event text are third-party content
        risk_fn=risk_fn,
        priority=spec.priority,
        on_taint=TaintPolicy.APPROVE if spec.taint_approve else TaintPolicy.ALLOW,
        prepare=workspace_tools.PREPARES.get(spec.name),
        identity=spec.identity,
        action_time=spec.action_time,
    )


def register_integration_tools(registry: ToolRegistry) -> list[str]:
    names = []
    workspace = workspace_enabled()
    for spec in ACTIONS.values():
        if not spec.agents:
            continue  # internal: used by other actions and polls, never offered to a model
        if spec.capability in WORKSPACE_CAPABILITIES and not workspace:
            continue  # GOOGLE_WORKSPACE_ENABLED=false: the catalog is exactly the pre-Workspace one
        tool = _make_tool(spec)
        registry.register(tool)
        names.append(tool.name)
    return names
