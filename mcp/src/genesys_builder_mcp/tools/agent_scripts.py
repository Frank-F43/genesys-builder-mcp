"""Agent Script (Scripter) tools."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError

from ..annotations import READ_ONLY, WRITES
from ..client import ApiError, get_client
from ..resolve import paginate_lister, resolve
from ..write_guard import (
    attach_write_impact,
    elicit_write_confirmation,
    make_write_impact,
)

# The upload record is not queryable for a moment right after the POST, so the
# first status poll can 404. That is not a failure, just not ready yet.
UPLOAD_POLL_ATTEMPTS = 15
UPLOAD_POLL_SECONDS = 2


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def list_agent_scripts(name_contains: str | None = None) -> list[dict[str, Any]]:
        """List Agent Scripts (Scripter) in the org."""
        client = get_client()
        results = []
        for item in client.paginate("/api/v2/scripts"):
            name = item.get("name") or ""
            if name_contains and name_contains.lower() not in name.lower():
                continue
            results.append(
                {
                    "id": item.get("id"),
                    "name": name,
                    "published_date": item.get("publishedDate"),
                    "modified_date": item.get("modifiedDate"),
                }
            )
        return results

    @mcp.tool(annotations=WRITES)
    async def replace_agent_script(
        script_path: str,
        script: str,
        script_name: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Replace an existing Agent Script with a local .script file and publish it.

        This is the automated equivalent of the Scripter UI's Import -> Replace,
        so never hand script work back to be imported by hand. That the public
        API spec suggests otherwise is misleading: the upload endpoint lives on
        the app domain rather than under /api/v2, which is why it appears in
        neither the Swagger nor the Genesys CLI, and why Archy cannot do it.

        All three steps are required. Doing only the upload leaves agents seeing
        the previously published version, because the replace and the publish
        are separate operations.

        Args:
            script_path: Path to the .script file (JSON).
            script: Name or id of the existing script to overwrite in place.
                Without it the upload would create a new script instead of
                replacing.
            script_name: Defaults to the "name" field inside the file. Pass it
                only to rename the script while replacing, which is rare.
        """
        client = get_client()
        script_id = _script_id(script)
        path = Path(script_path)
        if not path.is_file():
            raise ToolError(f"Script file not found: {script_path}")

        content = path.read_bytes()
        if script_name is None:
            try:
                script_name = json.loads(content).get("name")
            except json.JSONDecodeError as exc:
                raise ToolError(f"{script_path} is not valid JSON: {exc}") from exc
        if not script_name:
            raise ToolError("Could not determine the script name from the file; pass script_name explicitly.")

        try:
            live_before = client.get(f"/api/v2/scripts/published/{script_id}")
        except ApiError:
            live_before = client.get(f"/api/v2/scripts/{script_id}") or {}

        before_name = live_before.get("name") or script
        before_version = live_before.get("versionId")
        before_published = live_before.get("publishedDate")
        diff = {
            "script_id": script_id,
            "script_name": before_name,
            "previous_version_id": before_version,
            "previous_published_date": before_published,
            "replacing_with_file": str(path),
        }
        confirm_message = (
            f"Replace and publish agent script {before_name!r}? "
            f"The live published version ({before_version or 'unknown'}) will be overwritten "
            f"by {path.name} — agents see the new script immediately after publish."
        )

        outcome, unconfirmed = await elicit_write_confirmation(ctx, confirm_message)
        if outcome == "declined":
            impact = make_write_impact(
                action="cancelled",
                summary=f"Agent script {before_name!r} was not replaced — confirmation declined.",
                diff=diff,
                previous_state={
                    "version_id": before_version,
                    "published_date": before_published,
                },
                confirmation_requested=True,
                confirmation_accepted=False,
            )
            return attach_write_impact(
                {
                    "script_id": script_id,
                    "script_name": before_name,
                    "verified_live": False,
                },
                impact,
            )

        upload = client.post_multipart(
            "/uploads/v2/scripter",
            fields={"scriptName": script_name, "scriptIdToReplace": script_id},
            file=("upload.script", content),
            base=client.config.apps_base,
        )
        # Named correlationId, but it is the uploadId the status endpoint wants.
        upload_id = upload["correlationId"]

        status: dict[str, Any] | None = None
        for _ in range(UPLOAD_POLL_ATTEMPTS):
            try:
                status = client.get(
                    f"/api/v2/scripts/uploads/{upload_id}/status",
                    params={"longPoll": "false"},
                )
            except ApiError as exc:
                if exc.status == 404:
                    time.sleep(UPLOAD_POLL_SECONDS)
                    continue
                raise
            if status and "succeeded" in status:
                break
            time.sleep(UPLOAD_POLL_SECONDS)
        else:
            raise ToolError(f"Timed out waiting for upload {upload_id} to settle.")

        if not (status or {}).get("succeeded"):
            raise ToolError(f"Script upload did not succeed: {status}")

        published = client.request("POST", "/api/v2/scripts/published", json_body={"scriptId": script_id})

        # Read the published copy back rather than trusting the publish call.
        # Note the endpoint: /api/v2/scripts/{id} returns the *draft*, whose
        # publishedDate stays null, so checking there makes a good publish look
        # like a failed one.
        live = client.get(f"/api/v2/scripts/published/{script_id}")
        if live.get("versionId") != published.get("versionId"):
            raise ToolError(
                f"Published version {published.get('versionId')} but agents are served "
                f"{live.get('versionId')}. The upload succeeded, so the script may need "
                "publishing again."
            )

        return attach_write_impact(
            {
                "script_id": script_id,
                "script_name": script_name,
                "upload_message": (status or {}).get("message"),
                "published_version_id": published.get("versionId"),
                "published_date": published.get("publishedDate"),
                "verified_live": True,
            },
            make_write_impact(
                action="applied",
                summary=(
                    f"Replaced and published agent script {script_name!r} "
                    f"({before_version or 'no prior version'} → {published.get('versionId')})."
                ),
                diff=diff,
                previous_state={
                    "version_id": before_version,
                    "published_date": before_published,
                },
                confirmation_requested=True,
                confirmation_accepted=(outcome == "confirmed"),
                unconfirmed_reason=unconfirmed,
            ),
        )

    def _script_id(value: str) -> str:
        client = get_client()
        return resolve(
            value,
            label="agent script",
            search=lambda v: [
                s for s in client.paginate("/api/v2/scripts") if v.lower() in (s.get("name") or "").lower()
            ],
            list_available=paginate_lister(client, "/api/v2/scripts"),
        )
