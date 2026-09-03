"""Deletion, gated behind naming what is about to be deleted.

The OAuth client's role decides what may be deleted at all; this adds nothing to
that. What it adds is deliberateness. An agent working from "räum die alten
Testsachen weg" can resolve a name to the wrong object, and most of what lives
here has no version history to restore from: a queue, a data action or a
knowledge base that goes is rebuilt by hand.

So deleting takes two steps. The first call names the object and gets back what
that name currently resolves to; the second repeats that exact name back. The
gate is trivial to pass on purpose and hard to pass by accident, which is the
distinction worth drawing - a caller that has read the name has seen what it is
about to destroy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..annotations import DESTRUCTIVE, READ_ONLY
from ..client import ApiError, get_client
from ..resolve import LIST_LIMIT, paginate_lister, resolve
from ..write_guard import attach_write_impact, make_write_impact


@dataclass(frozen=True)
class Kind:
    label: str
    search: Callable[[Any, str], Iterable[dict[str, Any]]]
    delete_path: Callable[[str], str]
    detail_path: Callable[[str], str]
    display: Callable[[dict[str, Any]], str]
    warning: str = ""
    # Field an exact match is judged on. Contact search is fuzzy enough that
    # one email matches another sharing its prefix, so matching on the address
    # is what separates them.
    name_key: str = "name"
    list_available: Callable[[Any], Iterable[dict[str, Any]]] | None = None


def _named(item: dict[str, Any]) -> str:
    return item.get("name") or ""


def _contact_name(item: dict[str, Any]) -> str:
    full = " ".join(filter(None, [item.get("firstName"), item.get("lastName")]))
    return full or item.get("workEmail") or item.get("id", "")


KINDS: dict[str, Kind] = {
    "data_action": Kind(
        label="data action",
        search=lambda c, v: c.paginate("/api/v2/integrations/actions", params={"name": v}),
        delete_path=lambda i: f"/api/v2/integrations/actions/{i}",
        detail_path=lambda i: f"/api/v2/integrations/actions/{i}",
        display=_named,
        warning="Flows calling this action will fail at runtime once it is gone.",
        list_available=lambda c: paginate_lister(c, "/api/v2/integrations/actions")(),
    ),
    "agent_script": Kind(
        label="agent script",
        search=lambda c, v: [
            s for s in c.paginate("/api/v2/scripts") if v.lower() in (s.get("name") or "").lower()
        ],
        delete_path=lambda i: f"/api/v2/scripts/{i}",
        detail_path=lambda i: f"/api/v2/scripts/{i}",
        display=_named,
        warning="Queues pointing at this script will lose their screen pop.",
        list_available=lambda c: paginate_lister(c, "/api/v2/scripts")(),
    ),
    "knowledge_base": Kind(
        label="knowledge base",
        search=lambda c, v: [
            k
            for k in c.paginate("/api/v2/knowledge/knowledgebases")
            if v.lower() in (k.get("name") or "").lower()
        ],
        delete_path=lambda i: f"/api/v2/knowledge/knowledgebases/{i}",
        detail_path=lambda i: f"/api/v2/knowledge/knowledgebases/{i}",
        display=_named,
        warning="Every article in it goes too, and bots bound to it stop answering.",
        list_available=lambda c: paginate_lister(c, "/api/v2/knowledge/knowledgebases")(),
    ),
    "external_contact": Kind(
        label="external contact",
        search=lambda c, v: c.get("/api/v2/externalcontacts/contacts", params={"q": v}).get("entities", []),
        delete_path=lambda i: f"/api/v2/externalcontacts/contacts/{i}",
        detail_path=lambda i: f"/api/v2/externalcontacts/contacts/{i}",
        display=_contact_name,
        warning="To clear stitching history, use reset_external_contact instead - it recreates the contact.",
        name_key="workEmail",
        list_available=lambda c: [
            {"id": item.get("id"), "workEmail": item.get("workEmail")}
            for item in (c.get("/api/v2/externalcontacts/contacts", params={"pageSize": LIST_LIMIT}).get("entities") or [])
        ],
    ),
    "data_table": Kind(
        label="data table",
        search=lambda c, v: [
            d for d in c.paginate("/api/v2/flows/datatables") if v.lower() in (d.get("name") or "").lower()
        ],
        delete_path=lambda i: f"/api/v2/flows/datatables/{i}",
        detail_path=lambda i: f"/api/v2/flows/datatables/{i}",
        display=_named,
        warning="All rows go with it, and flows reading the table will fail.",
        list_available=lambda c: paginate_lister(c, "/api/v2/flows/datatables")(),
    ),
    "flow": Kind(
        label="flow",
        search=lambda c, v: c.paginate("/api/v2/flows", params={"name": v}),
        delete_path=lambda i: f"/api/v2/flows/{i}",
        detail_path=lambda i: f"/api/v2/flows/{i}",
        display=_named,
        warning="Check the YAML is in the repository first - this removes every published version.",
        list_available=lambda c: paginate_lister(c, "/api/v2/flows")(),
    ),
}


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def deletable_kinds() -> dict[str, str]:
        """List what delete_object can remove, and what each deletion takes with it."""
        return {name: f"{k.label}: {k.warning}" for name, k in KINDS.items()}

    @mcp.tool(annotations=DESTRUCTIVE)
    def delete_object(kind: str, target: str, confirm_name: str | None = None) -> dict[str, Any]:
        """Delete one object, after naming it back exactly.

        Call it once without confirm_name to see what target resolves to, then
        again passing that object's exact name. Nothing is deleted until the
        name matches, so a wrong guess costs a round trip rather than an object.

        Deleting is not undoable and most of these have no version history.
        Prefer reset_external_contact over deleting a contact, and check a
        flow's YAML is in the repository before deleting the flow.

        Args:
            kind: One of the keys from deletable_kinds, e.g. data_action.
            target: Name or id of the object.
            confirm_name: The object's exact current name. Omit to preview.
        """
        if kind not in KINDS:
            raise ToolError(f"Unknown kind {kind!r}. Available: {', '.join(sorted(KINDS))}")

        spec = KINDS[kind]
        client = get_client()
        object_id = resolve(
            target,
            label=spec.label,
            search=lambda v: spec.search(client, v),
            name_key=spec.name_key,
            list_available=(lambda: spec.list_available(client)) if spec.list_available else None,
        )
        detail = client.get(spec.detail_path(object_id))
        actual = spec.display(detail)

        if confirm_name is None:
            impact = make_write_impact(
                action="skipped_no_change",
                summary=f"Preview only — {spec.label} {actual!r} was not deleted.",
                diff={"kind": kind, "id": object_id, "name": actual, "consequence": spec.warning},
                confirmation_requested=False,
            )
            return attach_write_impact(
                {
                    "deleted": False,
                    "kind": kind,
                    "id": object_id,
                    "name": actual,
                    "consequence": spec.warning,
                    "next_step": f"Call again with confirm_name={actual!r} to delete it.",
                },
                impact,
            )

        if confirm_name.strip() != actual.strip():
            raise ToolError(
                f"confirm_name {confirm_name!r} does not match this {spec.label}'s name {actual!r}. "
                "Nothing was deleted."
            )

        try:
            client.request("DELETE", spec.delete_path(object_id))
        except ApiError as exc:
            raise _explained(exc) from exc
        impact = make_write_impact(
            action="applied",
            summary=f"Deleted {spec.label} {actual!r}. {spec.warning}",
            diff={"kind": kind, "id": object_id, "name": actual},
            previous_state={"id": object_id, "name": actual, "detail": detail},
            confirmation_requested=True,
            confirmation_accepted=True,
        )
        return attach_write_impact(
            {"deleted": True, "kind": kind, "id": object_id, "name": actual},
            impact,
        )


def _explained(exc: ApiError) -> Exception:
    """Turn a refusal caused by the stale dependency index into an actionable one.

    A 424 here says "status unknown", which reads as "something references this"
    when it actually means nothing can currently answer the question. Without
    this the error sends you looking for a reference that does not exist.
    """
    if exc.status == 424 and "status unknown" in str(exc):
        return RuntimeError(
            f"{exc}\n\n"
            "This usually means the org's dependency index is stale, not that the object is "
            "in use - publishing flows invalidates it, and the platform then refuses deletions "
            "it cannot check. Call dependency_tracking_status to confirm, then "
            "rebuild_dependency_tracking, then try again. The Genesys Cloud UI shows the same "
            "condition as 'Server Error Occurred'."
        )
    return exc
