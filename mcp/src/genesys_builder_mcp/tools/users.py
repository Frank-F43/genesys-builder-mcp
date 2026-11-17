"""User configuration: lookup, skills, roles and queue membership."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..annotations import READ_ONLY, WRITES
from ..client import get_client
from ..resolve import LIST_LIMIT, paginate_lister, resolve

# Users are the one collection large enough to be worth capping by default.
USER_SEARCH_LIMIT = 50


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def find_users(query: str, limit: int = USER_SEARCH_LIMIT) -> list[dict[str, Any]]:
        """Search users by name or email.

        Args:
            query: Part of a name or email address.
            limit: Maximum number of results.
        """
        client = get_client()
        payload = client.request(
            "POST",
            "/api/v2/users/search",
            json_body={
                "pageSize": min(limit, 100),
                "query": [{"type": "CONTAINS", "fields": ["name", "email"], "value": query}],
            },
        )
        return [_summarize(u) for u in (payload.get("results") or [])[:limit]]

    @mcp.tool(annotations=READ_ONLY)
    def get_user(user: str) -> dict[str, Any]:
        """Fetch a user with their skills, roles and queue membership.

        Args:
            user: Name, email or id.
        """
        client = get_client()
        user_id = _user_id(user)
        detail = client.get(
            f"/api/v2/users/{user_id}",
            params={"expand": "skills,languages,authorization,groups"},
        )
        queues = list(client.paginate(f"/api/v2/users/{user_id}/queues"))

        summary = _summarize(detail)
        summary["skills"] = [
            {"name": s.get("name"), "proficiency": s.get("proficiency")} for s in detail.get("skills") or []
        ]
        summary["roles"] = [r.get("name") for r in (detail.get("authorization") or {}).get("roles") or []]
        summary["queues"] = [q.get("name") for q in queues]
        return summary

    @mcp.tool(annotations=WRITES)
    def assign_user_skill(user: str, skill: str, proficiency: float = 5.0) -> dict[str, Any]:
        """Give a user an ACD routing skill.

        Args:
            user: Name, email or id.
            skill: Skill name or id.
            proficiency: 0 to 5, higher routes work to this user first.
        """
        if not 0 <= proficiency <= 5:
            raise ToolError(f"Proficiency must be between 0 and 5, got {proficiency}.")

        client = get_client()
        user_id = _user_id(user)
        skill_id = _skill_id(skill)
        client.request(
            "POST",
            f"/api/v2/users/{user_id}/routingskills",
            json_body={"id": skill_id, "proficiency": proficiency},
        )
        return {"user_id": user_id, "skill_id": skill_id, "proficiency": proficiency}

    @mcp.tool(annotations=WRITES)
    def add_user_to_queue(user: str, queue: str) -> dict[str, Any]:
        """Make a user a member of a routing queue.

        Args:
            user: Name, email or id.
            queue: Queue name or id.
        """
        client = get_client()
        user_id = _user_id(user)
        queue_id = _queue_id(queue)
        client.request(
            "POST",
            f"/api/v2/routing/queues/{queue_id}/members",
            json_body=[{"id": user_id}],
        )
        return {"user_id": user_id, "queue_id": queue_id}

    @mcp.tool(annotations=READ_ONLY)
    def list_roles(name_contains: str | None = None) -> list[dict[str, Any]]:
        """List authorization roles."""
        client = get_client()
        params = {"name": name_contains} if name_contains else None
        return [
            {"id": r.get("id"), "name": r.get("name"), "description": r.get("description")}
            for r in client.paginate("/api/v2/authorization/roles", params=params)
        ]

    # --- name resolution -----------------------------------------------------

    def _user_list_available(client):
        def list_available() -> list[dict[str, Any]]:
            payload = client.request(
                "POST",
                "/api/v2/users/search",
                json_body={"pageSize": LIST_LIMIT},
            )
            return payload.get("results") or []

        return list_available

    def _user_id(value: str) -> str:
        client = get_client()

        def search(v: str) -> list[dict[str, Any]]:
            payload = client.request(
                "POST",
                "/api/v2/users/search",
                json_body={
                    "pageSize": 25,
                    "query": [{"type": "CONTAINS", "fields": ["name", "email"], "value": v}],
                },
            )
            return payload.get("results") or []

        return resolve(
            value,
            label="user",
            search=search,
            list_available=_user_list_available(client),
        )

    def _skill_id(value: str) -> str:
        client = get_client()
        return resolve(
            value,
            label="skill",
            search=lambda v: client.paginate("/api/v2/routing/skills", params={"name": v}),
            list_available=paginate_lister(client, "/api/v2/routing/skills"),
        )

    def _queue_id(value: str) -> str:
        client = get_client()
        return resolve(
            value,
            label="queue",
            search=lambda v: client.paginate("/api/v2/routing/queues", params={"name": f"*{v}*"}),
            list_available=paginate_lister(client, "/api/v2/routing/queues"),
        )


def _summarize(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user.get("id"),
        "name": user.get("name"),
        "email": user.get("email"),
        "title": user.get("title"),
        "department": user.get("department"),
        "state": user.get("state"),
        "division": (user.get("division") or {}).get("name"),
    }
