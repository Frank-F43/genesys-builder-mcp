"""Read-only orientation tools.

These answer "what am I connected to and what is already there", which is what
an agent needs before it can safely change anything.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from ..annotations import READ_ONLY
from ..client import ApiError, GenesysClient, get_client
from ..config import REGION_NAMES


def _client_permissions(client: GenesysClient, client_id: str | None, domain: str | None) -> dict[str, Any]:
    """Roles and permissions of the OAuth client, or why they cannot be read."""
    if not client_id:
        return {"roles": None, "permissions_note": "The token names no OAuth client."}
    try:
        grants = client.get(f"/api/v2/oauth/clients/{client_id}").get("roleDivisions") or []
        division_names = {d["id"]: d.get("name") for d in client.paginate("/api/v2/authorization/divisions")}
        roles: dict[str, dict[str, Any]] = {}
        for grant in grants:
            if grant["roleId"] not in roles:
                roles[grant["roleId"]] = client.get(f"/api/v2/authorization/roles/{grant['roleId']}")
    except ApiError as exc:
        if exc.status != 403:
            raise
        return {
            "roles": None,
            "permissions_note": f"Not readable with this client ({exc.path}); "
            "needs oauth:client:view and authorization:role:view.",
        }

    permissions = sorted(
        {
            f"{policy['domain']}:{policy['entityName']}:{action}"
            for role in roles.values()
            for policy in role.get("permissionPolicies") or []
            for action in policy.get("actionSet") or []
        }
    )
    per_domain: dict[str, int] = {}
    for permission in permissions:
        name = permission.split(":", 1)[0]
        per_domain[name] = per_domain.get(name, 0) + 1

    result: dict[str, Any] = {
        "roles": [
            {
                "name": roles[grant["roleId"]].get("name"),
                "division": "all divisions" if grant.get("divisionId") == "*" else division_names.get(grant.get("divisionId"), grant.get("divisionId")),
            }
            for grant in grants
        ],
        "permission_count": len(permissions),
    }
    if domain:
        result["permissions"] = [p for p in permissions if p.split(":", 1)[0].lower() == domain.lower()]
    else:
        result["permissions_per_domain"] = dict(sorted(per_domain.items()))
    return result


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def whoami(permission_domain: str | None = None) -> dict[str, Any]:
        """Show which Genesys Cloud org and region the server is connected to, and with which permissions.

        Run this first in a new session, or whenever a call fails with 403, to
        confirm the org, the OAuth client and its roles. A client credentials
        token carries no permission list, so permissions come from the
        client's roles - a few thousand of them, hence counted per domain
        unless one domain is asked for.

        Args:
            permission_domain: List the permissions of this domain in full,
                e.g. architect or routing.
        """
        client = get_client()
        org = client.get("/api/v2/organizations/me")
        token = client.get("/api/v2/tokens/me")
        oauth_client = token.get("OAuthClient") or {}

        result: dict[str, Any] = {
            "organization": org.get("name"),
            "region": f"{REGION_NAMES.get(client.config.region, client.config.region)} - {client.config.region}",
            "organization_id": org.get("id"),
            "default_language": org.get("defaultLanguage"),
            "oauth_client": {"name": oauth_client.get("name"), "id": oauth_client.get("id")},
            "credentials_from": client.config.source,
        }
        result.update(_client_permissions(client, oauth_client.get("id"), permission_domain))
        return result

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
