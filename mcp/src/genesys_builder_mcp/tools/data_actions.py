"""Data Action tools.

A Data Action folder in this repository holds one file per part of the action:

    inputschema.json      JSON Schema (draft-04) for the input contract
    successschema.json    JSON Schema (draft-04) for the success output
    requesttemplate.vm    Velocity template for the request body
    successtemplate.vm    Velocity template for the success response
    translationmap.json          optional, JSONPath output extraction
    translationmapdefaults.json  optional
    action.json           written back after publishing

That layout mirrors what the Integrations UI builds by hand.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..annotations import READ_ONLY, WRITES
from ..client import get_client
from ..resolve import paginate_lister, resolve

REQUIRED_FILES = ["inputschema.json", "successschema.json", "requesttemplate.vm", "successtemplate.vm"]

# Actions that call the org's own Public API extract the created record's id by
# default. Override with a translationmap.json for anything nested.
DEFAULT_TRANSLATION_MAP = {"RECORD_ID": "$.id"}


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def get_data_action(action: str) -> dict[str, Any]:
        """Fetch a Data Action's full published definition, including its config.

        Args:
            action: Data Action name or id.
        """
        client = get_client()
        action_id = resolve(
            action,
            label="data action",
            search=lambda v: client.paginate("/api/v2/integrations/actions", params={"name": v}),
            list_available=paginate_lister(client, "/api/v2/integrations/actions"),
        )
        return client.get(f"/api/v2/integrations/actions/{action_id}", params={"includeConfig": "true"})

    @mcp.tool(annotations=WRITES)
    def create_data_action(
        folder: str,
        name: str,
        category: str,
        integration: str,
        request_url_template: str,
        request_type: str,
    ) -> dict[str, Any]:
        """Create and publish a Data Action from a local folder.

        Does the full round trip that the Integrations UI does: create draft,
        validate, publish, then write the published definition back into the
        folder as action.json.

        Publishing needs the draft's version number in the body. Sending an
        empty POST fails with an unhelpful "must not be null", which is why this
        is worth doing through a tool rather than by hand.

        Args:
            folder: Directory holding the action's files. See the module docs
                for the expected file names.
            name: Action name as it will appear in Architect.
            category: Grouping category in the Integrations UI.
            integration: Name or id of the integration this action belongs to.
            request_url_template: Target URL, e.g. /api/v2/externalcontacts/contacts
            request_type: HTTP method for the outbound request.
        """
        client = get_client()
        integration_id = resolve(
            integration,
            label="integration",
            search=lambda v: [
                i
                for i in client.paginate("/api/v2/integrations")
                if v.lower() in ((i.get("name") or "")).lower()
            ],
            list_available=paginate_lister(client, "/api/v2/integrations"),
        )
        base = Path(folder)
        if not base.is_dir():
            raise ToolError(f"Data Action folder not found: {folder}")

        missing = [f for f in REQUIRED_FILES if not (base / f).is_file()]
        if missing:
            raise ToolError(f"{folder} is missing required file(s): {', '.join(missing)}")

        input_schema = json.loads((base / "inputschema.json").read_text())
        success_schema = json.loads((base / "successschema.json").read_text())
        request_template = (base / "requesttemplate.vm").read_text()
        success_template = (base / "successtemplate.vm").read_text()

        translation_map = _optional_json(base / "translationmap.json", DEFAULT_TRANSLATION_MAP)
        translation_map_defaults = _optional_json(base / "translationmapdefaults.json", {})

        body = {
            "name": name,
            "category": category,
            "integrationId": integration_id,
            "secure": False,
            "contract": {
                "input": {"inputSchema": input_schema},
                "output": {"successSchema": success_schema},
            },
            "config": {
                "request": {
                    "requestUrlTemplate": request_url_template,
                    "requestType": request_type.upper(),
                    "requestTemplate": request_template,
                    "headers": {
                        "Content-Type": "application/json",
                        "UserAgent": "PureCloudIntegrations/1.0",
                    },
                },
                "response": {
                    "translationMap": translation_map,
                    "translationMapDefaults": translation_map_defaults,
                    "successTemplate": success_template,
                },
            },
        }

        draft = client.request("POST", "/api/v2/integrations/actions/drafts", json_body=body)
        action_id = draft["id"]

        validation = client.get(f"/api/v2/integrations/actions/{action_id}/draft/validation")
        if not validation.get("valid"):
            raise ToolError(
                f"Draft {action_id} did not validate, so it was left unpublished: "
                f"{json.dumps(validation)[:1500]}"
            )

        client.request(
            "POST",
            f"/api/v2/integrations/actions/{action_id}/draft/publish",
            json_body={"version": draft["version"]},
        )

        published = client.get(f"/api/v2/integrations/actions/{action_id}", params={"includeConfig": "true"})
        action_file = base / "action.json"
        action_file.write_text(json.dumps(published, indent=2) + "\n")

        return {
            "action_id": action_id,
            "name": name,
            "category": category,
            "version": published.get("version"),
            "wrote": str(action_file),
        }


def _optional_json(path: Path, fallback: Any) -> Any:
    if not path.is_file():
        return fallback
    return json.loads(path.read_text())
