# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
# Portions adapted from Hermes Agent (https://github.com/NousResearch/hermes-agent), Copyright (c) 2025 Nous
# Research, MIT License.
"""A small static catalog of hosted (remote) MCP servers a user can connect with one click (spec D11).

The endpoints come from Hermes Agent's `optional-mcps/*/manifest.yaml` (http transport only, vendor-hosted).
The descriptions are ours. Entries are the ones that need no client secret: either no sign-in (`none`) or
OAuth with dynamic client registration (`oauth`). Asana (its remote server needs a user-owned OAuth app with a
client secret) and Cloudflare (a query string in the URL, full API access) are left out. An entry could also
pre-register a `client_id`; none needs one today.

`allowed_hosts` names hosts, beyond the issuer's own site, where a server's authorization endpoints may live
(see `mavis.mcp.oauth`). Each entry should be checked against the live server before it is switched on in
production: vendors move their endpoints."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    name: str
    description: str  # one line, our words
    url: str
    auth: str  # "none" | "oauth"
    client_id: str | None = None  # a pre-registered OAuth client, when the vendor requires one
    allowed_hosts: tuple[str, ...] = ()


CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry("notion", "Notion", "Search and read your pages and databases.",
                 "https://mcp.notion.com/mcp", "oauth"),
    CatalogEntry("linear", "Linear", "Issues, projects and comments in your Linear workspace.",
                 "https://mcp.linear.app/mcp", "oauth"),
    CatalogEntry("atlassian", "Atlassian", "Jira issues and Confluence pages.",
                 "https://mcp.atlassian.com/v1/mcp/authv2", "oauth"),
    CatalogEntry("sentry", "Sentry", "Errors, stack traces and the context around them.",
                 "https://mcp.sentry.dev/mcp", "oauth"),
    CatalogEntry("vercel", "Vercel", "Projects, deployments and build logs.",
                 "https://mcp.vercel.com", "oauth"),
    CatalogEntry("stripe", "Stripe", "Customers, payments and invoices.",
                 "https://mcp.stripe.com", "oauth"),
    CatalogEntry("supabase", "Supabase", "Your Supabase projects: database, auth and storage.",
                 "https://mcp.supabase.com/mcp", "oauth"),
    CatalogEntry("hugging_face", "Hugging Face", "Models, datasets, Spaces and papers on the Hub.",
                 "https://huggingface.co/mcp", "oauth"),
    CatalogEntry("context7", "Context7", "Current documentation and code examples for libraries.",
                 "https://mcp.context7.com/mcp", "none"),
    CatalogEntry("deepwiki", "DeepWiki", "Ask questions about any public GitHub repository.",
                 "https://mcp.deepwiki.com/mcp", "none"),
    CatalogEntry("microsoft_learn", "Microsoft Learn", "Official Microsoft, Azure and .NET documentation.",
                 "https://learn.microsoft.com/api/mcp", "none"),
    CatalogEntry("aws_knowledge", "AWS Knowledge", "AWS documentation, API references and best practices.",
                 "https://knowledge-mcp.global.api.aws", "none"),
)

_BY_ID = {e.id: e for e in CATALOG}


def get(catalog_id: str) -> CatalogEntry | None:
    return _BY_ID.get(catalog_id)


def allowed_hosts_for_url(url: str) -> tuple[str, ...]:
    """The extra hosts the catalog declares for a server at `url` (empty for a custom server)."""
    return next((e.allowed_hosts for e in CATALOG if e.url == url), ())


def as_dicts() -> list[dict]:
    """The catalog as the API sends it. A pre-registered client id is not shown."""
    return [{"id": e.id, "name": e.name, "description": e.description, "auth": e.auth, "url": e.url}
            for e in CATALOG]
