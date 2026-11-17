"""Architect flow tools.

The point of these is the freshness check. Editing a flow YAML that is older
than what is live and publishing it silently reverts everything changed in
Architect since — the publish succeeds, so nothing announces the loss. So the
tools record the org version each time they export, and compare before writing.

YAML moves in and out through Architect jobs (see architect_jobs), which run
Archy inside Genesys Cloud.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .. import architect_jobs, flowdiff
from ..annotations import READ_ONLY, WRITES
from ..write_guard import attach_write_impact, make_write_impact
from ..client import ApiError, get_client
from ..workspace import (
    LocalFlow,
    find_repo_root,
    iter_flow_files,
    load_state,
    read_local_flow,
    save_state,
    with_flow_name,
)

# Sorts scratch copies to the end of Architect's flow list and marks them as
# disposable should a cleanup ever fail.
SCRATCH_PREFIX = "zz validate"

IN_SYNC = "in_sync"
UNTRACKED = "untracked"
LOCAL_CHANGES = "local_changes"
ORG_AHEAD = "org_ahead"
DIVERGED = "diverged"
NOT_IN_ORG = "not_in_org"

SAFE_TO_PUBLISH = {IN_SYNC, LOCAL_CHANGES, NOT_IN_ORG}

VERDICT_ADVICE = {
    IN_SYNC: "Local file matches the published version.",
    UNTRACKED: "Never exported through these tools, so the local file's age is unknown. Export before editing.",
    LOCAL_CHANGES: "Local edits on top of the published version. Safe to publish.",
    ORG_AHEAD: "The flow was changed in Architect after this file was exported. Re-export before editing.",
    DIVERGED: "Changed both locally and in Architect. Publishing would discard the Architect changes.",
    NOT_IN_ORG: "No flow with this name and type exists in the org yet. Publishing creates it.",
}


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def list_org_flows(flow_type: str | None = None, name_contains: str | None = None) -> list[dict[str, Any]]:
        """List Architect flows in the org with their published versions.

        Args:
            flow_type: Restrict to one type, e.g. inboundshortmessage, bot,
                commonmodule.
            name_contains: Substring of the flow name.
        """
        client = get_client()
        params: dict[str, Any] = {}
        if flow_type:
            params["type"] = flow_type.upper()
        if name_contains:
            params["name"] = f"*{name_contains}*"

        results = []
        for flow in client.paginate("/api/v2/flows", params=params or None):
            published = flow.get("publishedVersion") or {}
            results.append(
                {
                    "id": flow.get("id"),
                    "name": flow.get("name"),
                    "type": flow.get("type"),
                    "division": (flow.get("division") or {}).get("name"),
                    "published_version": published.get("id"),
                    "published_at": published.get("datePublished"),
                    "checked_out_by": ((flow.get("lockedUser") or {}).get("name")),
                }
            )
        return results

    @mcp.tool(annotations=READ_ONLY)
    def flow_status(flow_file: str | None = None) -> list[dict[str, Any]]:
        """Compare local flow YAML files against what is published in the org.

        Answers the question that matters before editing: is this file still
        current? Each entry gets one of:

            in_sync        local file matches the published version
            local_changes  edited locally, org unchanged - safe to publish
            org_ahead      changed in Architect since export - re-export first
            diverged       changed in both places - publishing loses org changes
            untracked      never exported through these tools, age unknown
            not_in_org     no such flow in the org yet

        It compares against published versions, so a draft somebody saved in
        Architect without publishing is invisible here and still reads
        in_sync, while publishing would discard it. flow_change_history shows
        saves as well as publishes and closes that gap.

        Args:
            flow_file: A single file to check. Defaults to every flow YAML in
                the repository.
        """
        root = find_repo_root()
        state = load_state(root)
        paths = [Path(flow_file)] if flow_file else list(iter_flow_files(root))

        results = []
        for path in paths:
            resolved = path if path.is_absolute() else root / path
            try:
                local = read_local_flow(resolved)
            except Exception as exc:  # noqa: BLE001 - a bad file should not hide the rest
                results.append({"file": str(path), "verdict": "unreadable", "detail": str(exc)})
                continue
            results.append(_status_for(root, state, local))

        return results

    @mcp.tool(annotations=WRITES)
    def export_flow(flow_name: str, flow_type: str, output_dir: str | None = None) -> dict[str, Any]:
        """Export a flow from the org to a local YAML file.

        Records the exported version so later edits can be checked against the
        org. Overwrites an existing file at the target path, unless that file
        differs from the export only in the order of its blocks and quoting -
        the exporter's own churn, which is not worth a diff.

        Args:
            flow_name: Exact flow name in the org.
            flow_type: Flow type, e.g. inboundshortmessage, bot,
                commonmodule, inboundcall.
            output_dir: Directory for the export, relative to the repository
                root. Defaults to the directory of the existing file for this
                flow, so re-exporting updates in place.
        """
        root = find_repo_root()
        flow_type = flow_type.lower()

        org = architect_jobs.find_flow(flow_name, flow_type.upper())
        if org is None:
            raise ToolError(f"No {flow_type} flow named {flow_name!r} in the org.")

        target_dir = _resolve_output_dir(root, flow_name, flow_type, output_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        exported = target_dir / f"{flow_name}.yaml"

        text = architect_jobs.export_yaml(org["id"])
        corrections: list[str] = []
        published_version = (org.get("publishedVersion") or {}).get("id")
        if published_version:
            text, corrections = architect_jobs.correct_speech_engines(text, org["id"], published_version)
        unchanged = exported.is_file() and flowdiff.same_content(exported.read_text(encoding="utf-8"), text)
        if not unchanged:
            exported.write_text(text, encoding="utf-8")

        record = _record_export(root, exported, flow_name, flow_type)
        result = {
            "file": str(exported.relative_to(root)),
            "flow_name": flow_name,
            "flow_type": flow_type,
            "org_version": record.get("org_version"),
            "file_rewritten": not unchanged,
        }
        if corrections:
            result["export_corrected"] = corrections
        return result

    @mcp.tool(annotations=WRITES)
    def validate_flow_file(flow_file: str) -> dict[str, Any]:
        """Check a flow YAML for errors without changing the flow it describes.

        Genesys Cloud only validates a file by trying to publish it, so this
        publishes a copy under a scratch name ("zz validate ...") and deletes
        that copy straight away. The real flow is never touched. An invalid
        file is rejected before anything is created. A bot flow's copy briefly
        trains its NLU, which makes validating one take longer.

        Args:
            flow_file: Path to the YAML file.
        """
        root = find_repo_root()
        path = _absolute(root, flow_file)
        local = read_local_flow(path)

        scratch_name = f"{SCRATCH_PREFIX} {local.name} {uuid.uuid4().hex[:6]}"
        copy = with_flow_name(path.read_text(encoding="utf-8"), scratch_name)
        result = architect_jobs.publish_yaml(copy.encode("utf-8"))

        scratch_id = result.flow_id
        if scratch_id is None:
            scratch_id = (architect_jobs.find_flow(scratch_name, local.api_type) or {}).get("id")
        cleanup_error = None
        if scratch_id:
            try:
                architect_jobs.delete_flow(scratch_id)
            except ApiError as exc:
                cleanup_error = f"Could not delete the scratch copy {scratch_name!r} ({scratch_id}): {exc}"

        response: dict[str, Any] = {
            "file": flow_file,
            "valid": result.succeeded,
            "errors": result.errors,
            "warnings": result.warnings,
        }
        if cleanup_error:
            response["cleanup_error"] = cleanup_error
        return response

    @mcp.tool(annotations=WRITES)
    def publish_flow(flow_file: str, force: bool = False) -> dict[str, Any]:
        """Publish a flow YAML to the org, making it live.

        Refuses when the org has moved on since this file was exported, because
        publishing would silently revert those changes. Re-export and reapply
        the edits, or pass force to publish anyway.

        When the file has never been exported through these tools, the org is
        exported and compared, so a first publish is checked rather than
        merely unverifiable. A check-out held in Architect is released first,
        as it would otherwise block the publish.

        That check only sees published versions. An unpublished draft in
        Architect passes it and is lost on publish, so when a flow may have
        been open in the UI, ask flow_change_history who last touched it.

        Args:
            flow_file: Path to the YAML file.
            force: Publish even when the flow changed in Architect since export.
        """
        root = find_repo_root()
        path = _absolute(root, flow_file)
        local = read_local_flow(path)
        status = _status_for(root, load_state(root), local)
        verdict = status["verdict"]

        # Without a recorded export there is nothing to compare against, but the
        # org can be asked directly. A guard that always fires on the first
        # publish only teaches people to pass force.
        comparison = None
        if verdict == UNTRACKED:
            comparison = flowdiff.compare_with_org(local)
            # Nothing can be lost when the org holds nothing this file lacks,
            # whether the two are identical or the file only adds to them.
            if not comparison["only_in_org_count"]:
                verdict = IN_SYNC if comparison["identical"] else LOCAL_CHANGES

        if verdict not in SAFE_TO_PUBLISH and not force:
            return {
                "published": False,
                "verdict": verdict,
                "detail": VERDICT_ADVICE[verdict],
                "org_version": status.get("org_version"),
                "exported_version": status.get("exported_version"),
                "org_has_that_this_file_does_not": (comparison or {}).get("only_in_org"),
                "hint": "Re-export and reapply your edits, or call again with force=true.",
            }

        org = architect_jobs.find_flow(local.name, local.api_type)
        unlocked_from = ((org or {}).get("lockedUser") or {}).get("name")
        if unlocked_from:
            architect_jobs.unlock_flow(org["id"])

        result = architect_jobs.publish_yaml(path.read_bytes())
        if not result.succeeded:
            raise ToolError(
                f"Publishing {local.name!r} failed; the org was not changed.\n" + "\n".join(result.errors)
            )

        record = _record_export(root, path, local.name, local.flow_type)
        org_loss = (comparison or {}).get("only_in_org_count", 0) if comparison else 0
        impact = make_write_impact(
            action="applied",
            summary=(
                f"Published flow {local.name!r} ({local.flow_type}) to the org"
                + (f"; overwrites {org_loss} org-only element(s)" if org_loss else "")
                + (" (forced)" if force and verdict not in SAFE_TO_PUBLISH else "")
                + "."
            ),
            diff={
                "flow_name": local.name,
                "flow_type": local.flow_type,
                "verdict_before_publish": verdict,
                "org_version_before": status.get("org_version"),
                "org_only_elements": (comparison or {}).get("only_in_org_count"),
                "was_forced": force and verdict not in SAFE_TO_PUBLISH,
                "unlocked_from": unlocked_from,
            },
            previous_state={"org_version": status.get("org_version")},
            confirmation_requested=False,
        )
        return attach_write_impact(
            {
                "published": True,
                "flow_name": local.name,
                "flow_type": local.flow_type,
                "org_version": record.get("org_version"),
                "was_forced": force and verdict not in SAFE_TO_PUBLISH,
            },
            impact,
        )


# --- status ------------------------------------------------------------------


def _status_for(root: Path, state: dict[str, Any], local: LocalFlow) -> dict[str, Any]:
    key = str(local.path.relative_to(root))
    record = state.get(key) or {}
    # A record from a different org says nothing about this one.
    if record.get("org_id") != get_client().org_id:
        record = {}
    org = architect_jobs.find_flow(local.name, local.api_type)

    org_version = (org.get("publishedVersion") or {}).get("id") if org else None
    exported_version = record.get("org_version")
    local_changed = record.get("content_sha256") != _hash(local.path) if record else None

    if org is None:
        verdict = NOT_IN_ORG
    elif not record:
        verdict = UNTRACKED
    elif org_version != exported_version:
        verdict = DIVERGED if local_changed else ORG_AHEAD
    else:
        verdict = LOCAL_CHANGES if local_changed else IN_SYNC

    return {
        "file": key,
        "flow_name": local.name,
        "flow_type": local.flow_type,
        "verdict": verdict,
        "detail": VERDICT_ADVICE[verdict],
        "org_version": org_version,
        "exported_version": exported_version,
        "locally_modified": local_changed,
        "checked_out_by": ((org or {}).get("lockedUser") or {}).get("name"),
    }


def _record_export(root: Path, path: Path, flow_name: str, flow_type: str) -> dict[str, Any]:
    org = architect_jobs.find_flow(flow_name, flow_type.upper())
    record = {
        "org_id": get_client().org_id,
        "flow_id": (org or {}).get("id"),
        "flow_name": flow_name,
        "flow_type": flow_type,
        "org_version": ((org or {}).get("publishedVersion") or {}).get("id"),
        "content_sha256": _hash(path),
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    state = load_state(root)
    state[str(path.relative_to(root))] = record
    save_state(root, state)
    return record


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _absolute(root: Path, flow_file: str) -> Path:
    path = Path(flow_file)
    resolved = path if path.is_absolute() else root / path
    if not resolved.is_file():
        raise ToolError(f"Flow file not found: {flow_file}")
    return resolved


def _resolve_output_dir(root: Path, flow_name: str, flow_type: str, output_dir: str | None) -> Path:
    if output_dir:
        path = Path(output_dir)
        return path if path.is_absolute() else root / path

    for candidate in iter_flow_files(root):
        try:
            existing = read_local_flow(candidate)
        except Exception:  # noqa: BLE001 - unreadable files just do not match
            continue
        if existing.name == flow_name and existing.flow_type == flow_type:
            return candidate.parent

    raise ToolError(
        f"No existing file found for {flow_name!r} ({flow_type}), so there is no "
        "obvious place to put it. Pass output_dir."
    )
