"""Routing configuration: queues, skills, wrap-up codes and schedules."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..annotations import READ_ONLY, WRITES
from ..client import get_client
from ..resolve import paginate_lister, resolve, resolve_async
from ..write_guard import (
    attach_write_impact,
    confirmation_block_reason,
    elicit_write_confirmation,
    make_write_impact,
)

NEXT_STEP_HINT = "next_step_hint"


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def list_queues(name_contains: str | None = None) -> list[dict[str, Any]]:
        """List routing queues, optionally filtered by a substring of the name."""
        client = get_client()
        params = {"name": f"*{name_contains}*"} if name_contains else None
        return [
            {
                "id": q.get("id"),
                "name": q.get("name"),
                "division": (q.get("division") or {}).get("name"),
                "member_count": q.get("memberCount"),
                "description": q.get("description"),
            }
            for q in client.paginate("/api/v2/routing/queues", params=params)
        ]

    @mcp.tool(annotations=READ_ONLY)
    async def get_queue(queue: str, ctx: Context | None = None) -> dict[str, Any]:
        """Fetch a queue's full configuration by name or id."""
        client = get_client()
        queue_id = await _queue_id_async(queue, ctx)
        return client.get(f"/api/v2/routing/queues/{queue_id}")

    @mcp.tool(annotations=WRITES)
    def create_queue(
        name: str,
        description: str | None = None,
        division: str | None = None,
        wrapup_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a routing queue.

        Args:
            name: Queue name.
            description: Optional description.
            division: Division name or id. Defaults to the org's home division.
            wrapup_codes: Wrap-up code names or ids to attach to the queue.
        """
        client = get_client()
        body: dict[str, Any] = {"name": name}
        if description:
            body["description"] = description
        if division:
            body["division"] = {"id": _division_id(division)}

        queue = client.request("POST", "/api/v2/routing/queues", json_body=body)

        if wrapup_codes:
            client.request(
                "POST",
                f"/api/v2/routing/queues/{queue['id']}/wrapupcodes",
                json_body=[{"id": _wrapup_code_id(code)} for code in wrapup_codes],
            )

        return {
            "id": queue["id"],
            "name": queue.get("name"),
            "division": (queue.get("division") or {}).get("name"),
            "wrapup_codes_attached": len(wrapup_codes or []),
        }

    @mcp.tool(annotations=READ_ONLY)
    def list_skills(name_contains: str | None = None) -> list[dict[str, Any]]:
        """List ACD routing skills."""
        client = get_client()
        params = {"name": name_contains} if name_contains else None
        return [
            {"id": s.get("id"), "name": s.get("name"), "state": s.get("state")}
            for s in client.paginate("/api/v2/routing/skills", params=params)
        ]

    @mcp.tool(annotations=WRITES)
    def create_skill(name: str) -> dict[str, Any]:
        """Create an ACD routing skill."""
        skill = get_client().request("POST", "/api/v2/routing/skills", json_body={"name": name})
        return {"id": skill["id"], "name": skill.get("name")}

    @mcp.tool(annotations=READ_ONLY)
    def list_wrapup_codes(name_contains: str | None = None) -> list[dict[str, Any]]:
        """List wrap-up codes."""
        client = get_client()
        results = []
        for code in client.paginate("/api/v2/routing/wrapupcodes"):
            name = code.get("name") or ""
            if name_contains and name_contains.lower() not in name.lower():
                continue
            results.append({"id": code.get("id"), "name": name})
        return results

    @mcp.tool(annotations=WRITES)
    def create_wrapup_code(
        name: str,
        description: str,
        division: str | None = None,
    ) -> dict[str, Any]:
        """Create a wrap-up code.

        Agent Copilot **wrap-up prediction** matches codes by **name and description**.
        A code without a description appears in the queue list but is almost never
        suggested automatically — the description is how the model decides when a code
        applies. ``create_wrapup_code`` therefore requires a non-empty description and
        creates nothing without one.

        Write a description in two parts:

        1. **When to use** — the concrete trigger in the conversation (what the agent
           did and what the customer agreed to or confirmed).
        2. **When not to use** — near-miss situations that must *not* get this code,
           e.g. interest without commitment, requests for time to decide, or rejection.
           This boundary is what stops the model from firing the code on an incomplete
           outcome.

        Example shape (generic):

            Used when the customer has confirmed a binding change during the call:
            the agent presented the offer and the customer agreed. Do not use when the
            customer only expressed interest, asked for time to decide, or declined.

        To fix a description on an existing code, use ``set_wrapup_code`` — the API
        PUT requires the full body (``name`` and ``division`` included), not a partial
        patch.

        Args:
            name: Wrap-up code name.
            description: Required. Copilot prediction prompt; must be non-empty after
                trimming whitespace.
            division: Division name or id. Without this, the code lands on division ``*``
                instead of your home division — usually wrong for demo orgs.

        Copilot wrap-up **prediction** only suggests codes attached to the conversation's
        queue. Creating a code here does not attach it anywhere: also add it to the relevant
        queue via ``POST /api/v2/routing/queues/{queueId}/wrapupcodes`` (JSON array body)
        or ``create_queue(..., wrapup_codes=[...])``. The divisions API cannot attach
        wrap-up codes to queues; the code's ``name`` must be sent again on PUT if you move
        divisions later.
        """
        trimmed = description.strip()
        if not trimmed:
            raise ToolError(
                "Wrap-up code needs a non-empty description, so nothing was created. "
                "Copilot prediction matches codes by name and description; without one "
                "the code is dead weight. Describe when to use the code and when not to "
                "(near-miss situations that must not get this code)."
            )

        body: dict[str, Any] = {"name": name, "description": trimmed}
        if division:
            body["division"] = {"id": _division_id(division)}
        code = get_client().request("POST", "/api/v2/routing/wrapupcodes", json_body=body)
        code_id = code["id"]
        code_name = code.get("name")
        return {
            "id": code_id,
            "name": code_name,
            "description": code.get("description"),
            "division": (code.get("division") or {}).get("name"),
            "queue_attachment_required": True,
            NEXT_STEP_HINT: (
                f"Attach wrap-up code {code_name!r} to every queue where agents should "
                f"receive Copilot suggestions for it — prediction only considers codes "
                f"bound to the active queue. POST /api/v2/routing/queues/{{queueId}}/wrapupcodes "
                f'with [{{"id": "{code_id}"}}], or pass wrapup_codes=[{code_name!r}] when '
                f"creating the queue."
            ),
        }

    @mcp.tool(annotations=WRITES)
    async def set_wrapup_code(
        wrapup_code: str, description: str, ctx: Context | None = None
    ) -> dict[str, Any]:
        """Update a wrap-up code's description.

        Agent Copilot matches wrap-up codes by **name and description**. Use this
        when a code exists but has no description or a weak one — the same two-part
        pattern as ``create_wrapup_code`` (when to use, then when not to use).

        Replacing a description that is already there asks for confirmation first,
        because the wording is what Copilot matches on and the old text is not
        recoverable from anywhere else. Filling in an empty one just writes.

        The PUT endpoint requires the **full** resource body: ``name`` and ``division``
        cannot be omitted. This tool reads the live object first and sends a complete
        PUT so you do not have to assemble the body by hand.

        Args:
            wrapup_code: Wrap-up code name or id.
            description: New description. Must be non-empty after trimming whitespace.
        """
        trimmed = description.strip()
        if not trimmed:
            raise ToolError(
                "Wrap-up code description must be non-empty, so nothing was updated. "
                "Describe when to use the code and when not to (near-miss situations)."
            )

        client = get_client()
        code_id = _wrapup_code_id(wrapup_code)
        existing = client.get(f"/api/v2/routing/wrapupcodes/{code_id}")
        code_name = existing.get("name") or wrapup_code
        before = (existing.get("description") or "").strip()

        base_result = {
            "id": code_id,
            "name": code_name,
            "division": (existing.get("division") or {}).get("name"),
        }

        if before == trimmed:
            impact = make_write_impact(
                action="skipped_no_change",
                summary=f"Wrap-up code {code_name!r} already has that description — nothing was written.",
                diff={"wrapup_code_id": code_id, "name": code_name},
                previous_state={"description": before},
            )
            return attach_write_impact(
                {**base_result, "description": before, "action": "unchanged"}, impact
            )

        diff = {
            "wrapup_code_id": code_id,
            "name": code_name,
            "had_description": bool(before),
            "before": before,
            "after": trimmed,
        }

        confirmation_requested = False
        confirmation_accepted: bool | None = None
        unconfirmed: str | None = None
        if before:
            confirmation_requested = True
            outcome, unconfirmed = await elicit_write_confirmation(
                ctx,
                f"Replace the description on wrap-up code {code_name!r}? "
                f"Copilot matches on this wording. Current text: {before!r}. Apply or cancel?",
            )
            if outcome == "declined":
                impact = make_write_impact(
                    action="cancelled",
                    summary=(
                        f"Wrap-up code {code_name!r} was not changed — "
                        f"{confirmation_block_reason(unconfirmed)}."
                    ),
                    diff=diff,
                    previous_state={"description": before},
                    confirmation_requested=True,
                    confirmation_accepted=False,
                    unconfirmed_reason=unconfirmed,
                )
                return attach_write_impact(
                    {**base_result, "description": before, "action": "cancelled"}, impact
                )
            confirmation_accepted = outcome == "confirmed"

        updated = client.request(
            "PUT",
            f"/api/v2/routing/wrapupcodes/{code_id}",
            json_body={
                "name": existing["name"],
                "description": trimmed,
                "division": existing.get("division"),
            },
        )

        impact = make_write_impact(
            action="applied",
            summary=(
                f"Replaced the description on wrap-up code {code_name!r}."
                if before
                else f"Set a description on wrap-up code {code_name!r}, which had none."
            ),
            diff=diff,
            previous_state={"description": before},
            confirmation_requested=confirmation_requested,
            confirmation_accepted=confirmation_accepted,
            unconfirmed_reason=unconfirmed,
        )
        return attach_write_impact(
            {
                "id": updated["id"],
                "name": updated.get("name"),
                "description": updated.get("description"),
                "division": (updated.get("division") or {}).get("name"),
                NEXT_STEP_HINT: (
                    f"If Copilot still does not suggest {updated.get('name')!r}, confirm the "
                    f"code is attached to the conversation queue — list_wrapup_codes shows "
                    f"existence, not queue binding."
                ),
            },
            impact,
        )

    @mcp.tool(annotations=READ_ONLY)
    def list_schedules(name_contains: str | None = None) -> list[dict[str, Any]]:
        """List Architect schedules (business hours, holidays)."""
        client = get_client()
        results = []
        for item in client.paginate("/api/v2/architect/schedules"):
            name = item.get("name") or ""
            if name_contains and name_contains.lower() not in name.lower():
                continue
            results.append(
                {
                    "id": item.get("id"),
                    "name": name,
                    "start": item.get("start"),
                    "end": item.get("end"),
                    "rrule": item.get("rrule"),
                }
            )
        return results

    # --- name resolution -----------------------------------------------------

    def _queue_search(client: Any, term: str):
        return client.paginate("/api/v2/routing/queues", params={"name": f"*{term}*"})

    def _queue_list_available(client: Any):
        return paginate_lister(client, "/api/v2/routing/queues")

    def _queue_id(value: str) -> str:
        client = get_client()
        return resolve(
            value,
            label="queue",
            search=lambda v: _queue_search(client, v),
            list_available=_queue_list_available(client),
        )

    async def _queue_id_async(value: str, ctx: Context | None) -> str:
        client = get_client()
        return await resolve_async(
            value,
            ctx=ctx,
            label="queue",
            search=lambda v: _queue_search(client, v),
            list_available=_queue_list_available(client),
        )

    def _division_id(value: str) -> str:
        client = get_client()
        return resolve(
            value,
            label="division",
            search=lambda v: [
                d
                for d in client.paginate("/api/v2/authorization/divisions")
                if v.lower() in (d.get("name") or "").lower()
            ],
            list_available=paginate_lister(client, "/api/v2/authorization/divisions"),
        )

    def _wrapup_code_id(value: str) -> str:
        client = get_client()
        return resolve(
            value,
            label="wrap-up code",
            search=lambda v: [
                c
                for c in client.paginate("/api/v2/routing/wrapupcodes")
                if v.lower() in (c.get("name") or "").lower()
            ],
            list_available=paginate_lister(client, "/api/v2/routing/wrapupcodes"),
        )
