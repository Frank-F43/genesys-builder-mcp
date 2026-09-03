"""Read-only orientation tools.

These answer "what am I connected to and what is already there", which is what
an agent needs before it can safely change anything.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from ..annotations import READ_ONLY
from ..client import get_client


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def whoami() -> dict[str, Any]:
        """Show which Genesys Cloud org the server is connected to and with which permissions.

        Run this first in a new session, or whenever a call fails with 403, to
        confirm the org and the OAuth client's granted permissions.
        """
        client = get_client()
        org = client.get("/api/v2/organizations/me")
        token = client.get("/api/v2/tokens/me")

        return {
            "region": client.config.region,
            "credentials_from": client.config.source,
            "organization": {
                "name": org.get("name"),
                "id": org.get("id"),
                "default_language": org.get("defaultLanguage"),
            },
            "oauth_client": {
                "name": (token.get("oAuthClient") or {}).get("name"),
                "id": (token.get("oAuthClient") or {}).get("id"),
            },
            "permission_count": len(token.get("permissions") or []),
            "permissions": sorted(token.get("permissions") or []),
        }

    @mcp.tool(annotations=READ_ONLY)
    def list_divisions() -> list[dict[str, Any]]:
        """List the org's divisions."""
        client = get_client()
        return [
            {"id": d.get("id"), "name": d.get("name"), "is_home": d.get("homeDivision", False)}
            for d in client.paginate("/api/v2/authorization/divisions")
        ]

    @mcp.tool(annotations=READ_ONLY)
    def list_integrations() -> list[dict[str, Any]]:
        """List configured integrations, including the Data Actions integrations.

        Creating a Data Action requires the id of the integration it belongs to,
        which is what this is normally used for.
        """
        client = get_client()
        results = []
        for item in client.paginate("/api/v2/integrations"):
            results.append(
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "type": (item.get("integrationType") or {}).get("id"),
                    "state": item.get("intendedState"),
                }
            )
        return results

    @mcp.tool(annotations=READ_ONLY)
    def list_data_actions(category: str | None = None, name_contains: str | None = None) -> list[dict[str, Any]]:
        """List Data Actions, optionally filtered by category or a substring of the name."""
        client = get_client()
        params: dict[str, Any] = {}
        if category:
            params["category"] = category

        results = []
        for item in client.paginate("/api/v2/integrations/actions", params=params):
            name = item.get("name") or ""
            if name_contains and name_contains.lower() not in name.lower():
                continue
            results.append(
                {
                    "id": item.get("id"),
                    "name": name,
                    "category": item.get("category"),
                    "integration_id": item.get("integrationId"),
                    "secure": item.get("secure"),
                }
            )
        return results

    @mcp.tool(annotations=READ_ONLY)
    def list_knowledge_bases() -> list[dict[str, Any]]:
        """List knowledge bases with their language and how much they hold.

        The counts separate the two document kinds the API tracks: articles and
        FAQs. A base can be published and still hold nothing, which is worth
        seeing before searching it for content that was never imported.
        """
        client = get_client()
        results = []
        for item in client.paginate("/api/v2/knowledge/knowledgebases"):
            results.append(
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "core_language": item.get("coreLanguage"),
                    "article_count": item.get("articleCount"),
                    "faq_count": item.get("faqCount"),
                    "published": item.get("published"),
                }
            )
        return results
