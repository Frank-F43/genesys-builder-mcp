"""Architect flow execution history.

Analytics say a flow ended in a TRANSFER; they never say why. When a flow errors,
its error handling still transfers the conversation to a queue, so a crash and a
successful route look identical from the outside — the interaction simply arrives
somewhere unexpected, with no skill and no explanation.

Execution history is the only place that distinguishes the two. It records the
action-by-action path, the value written to every variable, and the error reason
when one fires.

Two things make it awkward to reach by hand, and both are handled here. The query
criteria are named ``key``/``value`` rather than the ``criteriaKey``/``values``
that ``/flows/instances/querycapabilities`` implies, and a wrong shape is rejected
as "bad.request - Unauthorized", which reads like a permissions problem and is
not. And the detail is not returned directly: it has to be requested as a job,
polled, then downloaded from a signed URL.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..annotations import READ_ONLY
from ..client import get_client

QUERY_PATH = "/api/v2/flows/instances/query"
INSTANCE_PATH = "/api/v2/flows/instances"

JOB_POLL_SECONDS = 0.5
JOB_POLL_ATTEMPTS = 40

# Events that only bracket the interesting ones. Kept out of the rendered trace
# unless something inside them failed, so the path stays readable.
STRUCTURAL_EVENTS = {"startedState", "endedState", "startedTask", "endedTask"}


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def find_flow_executions(
        conversation_id: str | None = None,
        flow_id: str | None = None,
        error_reason: str | None = None,
        errors_only: bool = False,
        page_size: int = 25,
    ) -> dict[str, Any]:
        """Find recorded flow executions, newest first.

        Returns one entry per flow run with its error reason, which is usually
        enough on its own: a run that ended in the error handler carries a
        reason like Error.Expression.Value.NotAllowed.NotSet, while a clean run
        reports NONE. Pass the returned id to get_flow_execution for the
        action-by-action path.

        Genesys keeps execution data for several days, and only records it while
        the org-level setting is on. An empty result for a conversation that
        definitely ran means one of those two, not that the flow did not run.

        Args:
            conversation_id: The conversation whose flow run to look up.
            flow_id: Restrict to one flow, by id rather than name.
            error_reason: Match one reason exactly, e.g.
                Error.Expression.Value.NotAllowed.NotSet.
            errors_only: Keep only runs that recorded an error. Applied after
                the query, so it combines with the other filters.
            page_size: Maximum runs to return.
        """
        criteria = []
        if conversation_id:
            criteria.append({"key": "ConversationId", "operator": "eq", "value": conversation_id})
        if flow_id:
            criteria.append({"key": "FlowId", "operator": "eq", "value": flow_id})
        if error_reason:
            criteria.append({"key": "FlowErrorReason", "operator": "eq", "value": error_reason})

        if not criteria:
            raise ToolError(
                "Give at least one of conversation_id, flow_id or error_reason - "
                "execution history cannot be listed unfiltered."
            )

        client = get_client()
        payload = client.request(
            "POST",
            QUERY_PATH,
            # Deliberately without indexOnly: despite the name, it strips each
            # entry down to an id and conversation id, dropping the flow name
            # and error reason that make the result worth reading.
            params={"pageSize": page_size},
            # The query is a list of criteria groups that are AND'd together, so
            # each criterion goes in its own group rather than into one group's
            # "and" list.
            json_body={"query": [{"criteria": c} for c in criteria]},
        )

        entities = payload.get("entities") or []
        if errors_only:
            entities = [e for e in entities if _has_error(e)]

        return {
            "total": payload.get("total", len(entities)),
            "executions": [
                {
                    "id": e.get("id"),
                    "flow_name": e.get("flowName"),
                    "flow_version": e.get("flowVersion"),
                    "flow_type": e.get("flowType"),
                    "conversation_id": e.get("conversationId"),
                    "started": e.get("startDateTime"),
                    "ended": e.get("endDateTime"),
                    "error_reason": e.get("flowErrorReason"),
                    "warning_reason": e.get("flowWarningReason"),
                }
                for e in entities
            ],
        }

    @mcp.tool(annotations=READ_ONLY)
    def get_flow_execution(execution_id: str) -> dict[str, Any]:
        """Get the action-by-action path of one flow execution.

        The trace is flattened into indented lines in execution order, each
        naming the action, the output path it took, and any variable it wrote.
        The action immediately before an `eventError` line is the one that
        failed — Architect stops the task there and hands over to the flow's
        error handling, which is what makes the failure look like a normal
        transfer afterwards.

        Args:
            execution_id: Id from find_flow_executions.
        """
        client = get_client()

        job = client.request("GET", f"{INSTANCE_PATH}/{execution_id}")
        job_id = job.get("id")
        if not job_id:
            raise ToolError(f"No execution data job was started for {execution_id}.")

        entity = _await_job(client, job_id)
        download_uri = entity.get("downloadUri")
        if not download_uri:
            raise ToolError(
                f"Execution data job {job_id} finished without a download link "
                f"(status {entity.get('statusCode')}). The data may have aged out."
            )

        response = httpx.get(download_uri, timeout=60.0)
        response.raise_for_status()
        flow = response.json().get("flow") or {}

        lines: list[str] = []
        _render(flow.get("execution") or [], lines, 0)

        return {
            "flow_name": flow.get("flowName"),
            "flow_version": flow.get("flowVersion"),
            "flow_type": flow.get("flowType"),
            "conversation_id": flow.get("conversationId"),
            "message_type": flow.get("messageType"),
            "started": flow.get("startDateTime"),
            "ended": flow.get("endDateTime"),
            "truncated": flow.get("isTruncated"),
            "trace": lines,
        }


def _await_job(client: Any, job_id: str) -> dict[str, Any]:
    """Poll until the execution data is rendered and downloadable."""
    for _ in range(JOB_POLL_ATTEMPTS):
        status = client.request("GET", f"{INSTANCE_PATH}/jobs/{job_id}")
        state = status.get("jobState")
        if state == "Success":
            entities = status.get("entities") or []
            if not entities:
                raise ToolError(f"Execution data job {job_id} returned no data.")
            entity = entities[0]
            if entity.get("failed"):
                raise ToolError(
                    f"Execution data job {job_id} failed with status {entity.get('statusCode')}."
                )
            return entity
        if state == "Failed":
            raise ToolError(f"Execution data job {job_id} failed.")
        time.sleep(JOB_POLL_SECONDS)

    raise ToolError(
        f"Execution data job {job_id} was still {state!r} after "
        f"{JOB_POLL_ATTEMPTS * JOB_POLL_SECONDS:.0f}s."
    )


def _has_error(entity: dict[str, Any]) -> bool:
    reason = entity.get("flowErrorReason")
    return bool(reason) and reason != "NONE"


def _render(events: list[Any], lines: list[str], depth: int) -> None:
    """Flatten the nested event tree into indented, readable lines."""
    for event in events:
        if not isinstance(event, dict):
            continue
        for kind, body in event.items():
            if not isinstance(body, dict):
                lines.append("  " * depth + kind)
                continue

            label = body.get("actionName") or body.get("taskName") or ""
            parts = [f"{kind}" + (f" {label!r}" if label else "")]

            if body.get("outputPathName"):
                parts.append(f"-> {body['outputPathName']}")
            for key in ("flowExitReason", "errorReason", "errorMessage", "languageTag"):
                if body.get(key):
                    parts.append(f"{key}={body[key]}")

            lines.append("  " * depth + " ".join(parts))

            for statement in body.get("statements") or []:
                if isinstance(statement, dict):
                    lines.append(
                        "  " * (depth + 1)
                        + f". {statement.get('variableName')} = {statement.get('value')!r}"
                    )

            nested = body.get("execution")
            # Structural events wrap their contents rather than adding a step of
            # their own, so their children stay at the same indent.
            _render(nested or [], lines, depth if kind in STRUCTURAL_EVENTS else depth + 1)
