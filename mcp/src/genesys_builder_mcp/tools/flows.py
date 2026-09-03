"""Architect flow tools.

The point of these is the freshness check. Editing a flow YAML that is older
than what is live and publishing it silently reverts everything changed in
Architect since — the publish succeeds, so nothing announces the loss. So the
tools record the org version each time they export, and compare before writing.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .. import archy, flowdiff
from ..annotations import READ_ONLY, WRITES
from ..write_guard import attach_write_impact, make_write_impact
from ..client import get_client
from ..workspace import (
    LocalFlow,
    find_repo_root,
    iter_flow_files,
    load_state,
    read_local_flow,
    save_state,
)

# A flow that fails validation is a result to report, not a crash: 100 is what
# validateYaml exits with when the flow has errors, 1 when it only has warnings.
VALIDATE_EXIT_CODES = (0, 1, 100)

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
        org. Overwrites an existing file at the target path.

        Args:
            flow_name: Exact flow name in the org.
            flow_type: Archy flow type, e.g. inboundshortmessage, bot,
                commonmodule, inboundcall.
            output_dir: Directory for the export, relative to the repository
                root. Defaults to the directory of the existing file for this
                flow, so re-exporting updates in place.
        """
        root = find_repo_root()
        flow_type = flow_type.lower()

        target_dir = _resolve_output_dir(root, flow_name, flow_type, output_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        file_name = f"{flow_name}.yaml"

        archy.run(
            root,
            "export",
            [
                "--flowName", flow_name,
                "--flowType", flow_type,
                "--exportType", "yaml",
                "--outputDir", str(target_dir),
                "--exportFileName", file_name,
                "--force",
            ],
        )

        exported = target_dir / file_name
        if not exported.is_file():
            raise ToolError(f"Archy reported success but {exported} is missing.")

        record = _record_export(root, exported, flow_name, flow_type)
        return {
            "file": str(exported.relative_to(root)),
            "flow_name": flow_name,
            "flow_type": flow_type,
            "org_version": record.get("org_version"),
        }

    @mcp.tool(annotations=READ_ONLY)
    def validate_flow_file(flow_file: str) -> dict[str, Any]:
        """Check a flow YAML for errors without touching the org.

        Args:
            flow_file: Path to the YAML file.
        """
        root = find_repo_root()
        path = _absolute(root, flow_file)

        _, results = archy.run(
            root,
            "validateYaml",
            ["--file", str(path)],
            ok_exit_codes=VALIDATE_EXIT_CODES,
        )

        output = results.get("output") or {}
        traces = output.get("traces") or []
        return {
            "file": flow_file,
            "valid": bool(output.get("flowValid")),
            "errors": archy.errors(results),
            "warnings": [t.get("text") for t in traces if t.get("type") == "warning"],
        }

    @mcp.tool(annotations=WRITES)
    def publish_flow(flow_file: str, force: bool = False) -> dict[str, Any]:
        """Publish a flow YAML to the org, making it live.

        Refuses when the org has moved on since this file was exported, because
        publishing would silently revert those changes. Re-export and reapply
        the edits, or pass force to publish anyway.

        When the file has never been exported through these tools, the org is
        exported to a scratch directory and compared, so a first publish is
        checked rather than merely unverifiable.

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
            comparison = flowdiff.compare_with_org(root, local)
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

        archy.run(root, "publish", ["--file", str(path), "--forceUnlock"])

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
    org = _lookup_org_flow(local.name, local.api_type)

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


def _lookup_org_flow(name: str, api_type: str) -> dict[str, Any] | None:
    client = get_client()
    for flow in client.paginate("/api/v2/flows", params={"name": name, "type": api_type}):
        if (flow.get("name") or "").lower() == name.lower():
            return flow
    return None


def _record_export(root: Path, path: Path, flow_name: str, flow_type: str) -> dict[str, Any]:
    org = _lookup_org_flow(flow_name, flow_type.upper())
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
