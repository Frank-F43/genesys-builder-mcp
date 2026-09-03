"""Who changed a flow, and did they do it in Architect or through this repo?

The version list cannot answer this on its own. `createdBy` is empty for
anything Archy published, because a client credentials grant has no user behind
it, and names a person otherwise - which is easy to misread in either direction.

The audit trail records both the user and the OAuth client. When the two ids are
equal, a client acted for itself, so it was automation. A user with no client is
somebody signed in and working in the UI. That distinction is the whole point of
this tool: an edit made in Architect is work that a publish from the repo can
silently revert.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..annotations import READ_ONLY
from ..client import ApiError, get_client

POLL_SECONDS = 3
POLL_ATTEMPTS = 40

# Actions that change a flow, as opposed to merely looking at one.
CHANGING = {"Publish", "Checkin", "Save", "Create", "Delete", "Update"}


def _run_query(client, interval: str) -> list[dict[str, Any]]:
    try:
        job = client.request(
            "POST",
            "/api/v2/audits/query",
            json_body={"interval": interval, "serviceName": "Architect"},
        )
    except ApiError as exc:
        if exc.status == 403:
            raise ToolError(
                "The audit trail needs the 'audits:audit:view' permission, which this "
                "OAuth role does not have. Add it to the role, or fall back to comparing "
                "flow versions with flow_status."
            ) from exc
        raise

    job_id = job["id"]
    for _ in range(POLL_ATTEMPTS):
        state = client.get(f"/api/v2/audits/query/{job_id}").get("state")
        if state == "Succeeded":
            break
        if state in ("Failed", "Cancelled"):
            raise ToolError(f"The audit query ended as {state}.")
        time.sleep(POLL_SECONDS)
    else:
        raise ToolError("The audit query did not finish in time.")

    entries: list[dict[str, Any]] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"pageSize": 200}
        if cursor:
            params["cursor"] = cursor
        page = client.get(f"/api/v2/audits/query/{job_id}/results", params=params)
        entries += page.get("entities") or []
        cursor = page.get("cursor")
        if not cursor:
            return entries


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def flow_change_history(
        flow: str | None = None,
        days: int = 7,
        changes_only: bool = True,
    ) -> dict[str, Any]:
        """Report who changed flows recently, separating Architect from Archy.

        Worth running before publishing something that has sat in the repo for a
        while, and whenever it matters whether a person has been in Architect
        since the last export. `flow_status` says *that* a flow moved on;
        this says *who* moved it and from where.

        Args:
            flow: Restrict to one flow by name. Matched case-insensitively as a
                substring, since the audit trail records the name as it was at
                the time, which may since have changed. All flows if omitted.
            days: How far back to look.
            changes_only: Skip Checkout and other read-like actions.
        """
        client = get_client()
        now = datetime.now(timezone.utc)
        interval = (
            f"{(now - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S.000Z')}/"
            f"{(now + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%S.000Z')}"
        )

        entries = _run_query(client, interval)
        names: dict[str, str] = {}

        def person(uid: str) -> str:
            if uid not in names:
                try:
                    names[uid] = client.get(f"/api/v2/users/{uid}").get("name") or uid
                except ApiError:
                    names[uid] = uid
            return names[uid]

        events = []
        for entry in entries:
            entity = entry.get("entity") or {}
            name = entity.get("name") or ""
            if flow and flow.strip().lower() not in name.lower():
                continue
            action = entry.get("action") or ""
            if changes_only and action not in CHANGING:
                continue

            user_id = (entry.get("user") or {}).get("id")
            client_id = (entry.get("client") or {}).get("id")

            if client_id and client_id == user_id:
                by, who = "automation", "Archy or the toolkit"
            elif user_id:
                by, who = "person_in_architect", person(user_id)
            else:
                by, who = "platform", "the platform itself"

            events.append({
                "when": entry.get("eventDate"),
                "action": action,
                "flow": name or None,
                "version": (entry.get("context") or {}).get("version"),
                "by": by,
                "who": who,
                "from_ip": (entry.get("remoteIp") or [None])[0],
            })

        events.sort(key=lambda e: e["when"] or "")
        in_architect = [e for e in events if e["by"] == "person_in_architect"]

        return {
            "window_days": days,
            "flow_filter": flow,
            "events": events,
            "changed_in_architect_by_a_person": len(in_architect),
            "people": sorted({e["who"] for e in in_architect}),
            "verdict": (
                "Only automation changed flows in this window."
                if not in_architect else
                "Somebody worked in Architect here. Re-export before publishing, "
                "or a publish from the repo may revert their changes."
            ),
        }
