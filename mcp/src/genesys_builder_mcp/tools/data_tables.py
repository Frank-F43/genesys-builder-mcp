"""Architect Data Table tools.

Data Tables are the lookup store flows read at runtime - branding per segment,
opening hours per market, product catalogues. In a demo they are usually the
thing that needs to change fastest.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError

from ..annotations import READ_ONLY, WRITES
from ..client import ApiError, get_client
from ..resolve import paginate_lister, resolve
from ..write_guard import attach_write_impact, elicit_write_confirmation, make_write_impact

# Row limit per read, so a large table cannot swamp the model's context.
DEFAULT_ROW_LIMIT = 200


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def list_data_tables(name_contains: str | None = None) -> list[dict[str, Any]]:
        """List Architect Data Tables."""
        client = get_client()
        results = []
        for table in client.paginate("/api/v2/flows/datatables"):
            name = table.get("name") or ""
            if name_contains and name_contains.lower() not in name.lower():
                continue
            results.append(
                {
                    "id": table.get("id"),
                    "name": name,
                    "description": table.get("description"),
                    "division": (table.get("division") or {}).get("name"),
                }
            )
        return results

    @mcp.tool(annotations=READ_ONLY)
    def get_data_table_schema(table: str) -> dict[str, Any]:
        """Show a Data Table's columns, their types and which one is the key.

        Args:
            table: Table name or id.
        """
        client = get_client()
        detail = client.get(f"/api/v2/flows/datatables/{_table_id(table)}", params={"expand": "schema"})
        schema = detail.get("schema") or {}
        properties = schema.get("properties") or {}
        key_column = _key_column(schema)
        return {
            "id": detail.get("id"),
            "name": detail.get("name"),
            "key_column": key_column,
            "key_column_label": (properties.get(key_column) or {}).get("title"),
            "columns": [
                {
                    "name": key,
                    "type": spec.get("type"),
                    "title": spec.get("title"),
                    "default": spec.get("default"),
                }
                for key, spec in properties.items()
            ],
        }

    @mcp.tool(annotations=READ_ONLY)
    def list_data_table_rows(table: str, limit: int = DEFAULT_ROW_LIMIT) -> list[dict[str, Any]]:
        """Read the rows of a Data Table.

        Args:
            table: Table name or id.
            limit: Maximum rows to return.
        """
        client = get_client()
        return list(
            client.paginate(
                f"/api/v2/flows/datatables/{_table_id(table)}/rows",
                params={"showbrief": "false"},
                max_items=limit,
            )
        )

    @mcp.tool(annotations=WRITES)
    async def upsert_data_table_row(
        table: str, row: dict[str, Any], ctx: Context | None = None
    ) -> dict[str, Any]:
        """Create or replace one row in a Data Table.

        The row must include the table's key column. If a row with that key
        already exists it is replaced, otherwise it is created — which is why
        the row has to be complete, not just the fields being changed.

        A row that cannot be written raises ``ToolError`` and writes nothing.

        Args:
            table: Table name or id.
            row: The full row, including the key column.
        """
        client = get_client()
        table_id = _table_id(table)

        detail = client.get(f"/api/v2/flows/datatables/{table_id}", params={"expand": "schema"})
        key_column = _key_column(detail.get("schema") or {})
        if key_column not in row:
            raise ToolError(
                f"Row is missing the key column {key_column!r}, so nothing was written. "
                f"Table {table_id}, columns given: {sorted(row)}."
            )

        key_value = row[key_column]
        if "/" in str(key_value):
            raise ToolError(
                f"Row key {key_value!r} contains '/', so nothing was written. The key becomes "
                "a path segment in /api/v2/flows/datatables/{id}/rows/{rowId}, so Genesys "
                "rejects it. Use another separator, such as '_'."
            )

        existing_row: dict[str, Any] | None = None
        try:
            # showbrief defaults to true and then returns the key and nothing else,
            # which would make previous_state useless for undoing, report every
            # non-key field as changed, and stop skipped_no_change from ever firing.
            existing_row = client.get(
                f"/api/v2/flows/datatables/{table_id}/rows/{key_value}",
                params={"showbrief": "false"},
            )
            exists = True
        except ApiError as exc:
            if exc.status != 404:
                raise ToolError(
                    f"Could not tell whether row {key_value!r} already exists, so nothing was "
                    f"written. {exc}"
                ) from exc
            exists = False

        if exists and existing_row == row:
            impact = make_write_impact(
                action="skipped_no_change",
                summary=f"Row {key_value!r} in table already matches — nothing was written.",
                diff={"table_id": table_id, "key": key_value},
                previous_state={"row": existing_row},
            )
            return attach_write_impact(
                {"table_id": table_id, "key": key_value, "action": "unchanged", "row": existing_row},
                impact,
            )

        diff: dict[str, Any] = {"table_id": table_id, "key": key_value, "exists": exists}
        previous_state: dict[str, Any] | None = None
        confirm_message = ""
        if exists and existing_row is not None:
            changed_fields = sorted(k for k in set(existing_row) | set(row) if existing_row.get(k) != row.get(k))
            diff["fields_changed"] = changed_fields
            diff["before"] = existing_row
            diff["after"] = row
            previous_state = {"row": existing_row}
            confirm_message = (
                f"Replace row {key_value!r} in data table? "
                f"Fields changing: {', '.join(changed_fields) or '(none)'}. Apply or cancel?"
            )

        if exists:
            outcome, unconfirmed = await elicit_write_confirmation(ctx, confirm_message)
            if outcome == "declined":
                impact = make_write_impact(
                    action="cancelled",
                    summary=f"Row {key_value!r} was not replaced — confirmation declined.",
                    diff=diff,
                    previous_state=previous_state,
                    confirmation_requested=True,
                    confirmation_accepted=False,
                )
                return attach_write_impact(
                    {"table_id": table_id, "key": key_value, "action": "cancelled"},
                    impact,
                )
            confirmation_requested = True
            confirmation_accepted = outcome == "confirmed"
        else:
            unconfirmed = None
            confirmation_requested = False
            confirmation_accepted = None

        if exists:
            result = client.request(
                "PUT", f"/api/v2/flows/datatables/{table_id}/rows/{key_value}", json_body=row
            )
            action = "replaced"
            summary = f"Replaced row {key_value!r} ({len(diff.get('fields_changed') or [])} field(s) changed)."
        else:
            result = client.request("POST", f"/api/v2/flows/datatables/{table_id}/rows", json_body=row)
            action = "created"
            summary = f"Created row {key_value!r}."
            diff["after"] = row

        impact = make_write_impact(
            action="applied",
            summary=summary,
            diff=diff,
            previous_state=previous_state,
            confirmation_requested=confirmation_requested,
            confirmation_accepted=confirmation_accepted,
            unconfirmed_reason=unconfirmed if exists else None,
        )
        return attach_write_impact(
            {"table_id": table_id, "key": key_value, "action": action, "row": result},
            impact,
        )

    @mcp.tool(annotations=WRITES)
    def create_data_table(
        name: str,
        key_column: str,
        columns: list[dict[str, Any]],
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create a Data Table.

        Like ``upsert_data_table_row``, an unusable argument raises
        ``ToolError`` and creates nothing.

        Args:
            name: Table name.
            key_column: Name of the key column. It is always a string and must
                also appear in ``columns``. Genesys stores every key column
                under the property ``key`` and keeps this name as its display
                label, so that is what gets created.
            columns: One dict per column: {"name": ..., "type": "string" |
                "integer" | "number" | "boolean", "default": optional}
            description: Optional description.
        """
        unnamed = [index for index, column in enumerate(columns) if not column.get("name")]
        if unnamed:
            raise ToolError(
                "Every column needs a 'name', so no table was created. Missing at "
                f"0-based position(s): {unnamed}."
            )

        names = [c["name"] for c in columns]
        if key_column not in names:
            raise ToolError(
                f"Key column {key_column!r} is not one of the columns, so no table was "
                f"created. The key column has to be listed in 'columns' as well. "
                f"Columns given: {names}."
            )

        if key_column != "key" and "key" in names:
            raise ToolError(
                f"Key column {key_column!r} would be stored under the property 'key', but "
                "there is already a separate column by that name, so no table was created. "
                "Rename the other column, or make it the key column."
            )

        # Genesys stores the schema exactly as sent - it does not rewrite it.
        # The key column always lives under the property `key`, carries the
        # caller's name as its display label, and has to be listed in
        # `required`; a schema without it is rejected. The length bounds are
        # what every table in the org carries and what Architect expects.
        ordered = [c for c in columns if c["name"] == key_column]
        ordered += [c for c in columns if c["name"] != key_column]

        properties: dict[str, Any] = {}
        for index, column in enumerate(ordered):
            is_key = column["name"] == key_column
            property_name = "key" if is_key else column["name"]
            column_type = "string" if is_key else column.get("type", "string")
            spec: dict[str, Any] = {
                "title": column["name"],
                "type": column_type,
                "$id": f"/properties/{property_name}",
                "displayOrder": index,
            }
            if column_type == "string":
                spec["minLength"] = 1 if is_key else 0
                spec["maxLength"] = 256 if is_key else 262144
            if "default" in column:
                spec["default"] = column["default"]
            properties[property_name] = spec

        body = {
            "name": name,
            "description": description or "",
            "schema": {
                "$schema": "http://json-schema.org/draft-04/schema#",
                "title": name,
                "description": description or "",
                "type": "object",
                "required": ["key"],
                "properties": properties,
                "additionalProperties": False,
            },
        }
        try:
            table = get_client().request("POST", "/api/v2/flows/datatables", json_body=body)
        except ApiError as exc:
            raise ToolError(
                f"Genesys rejected the table, so nothing was created. {exc} "
                f"Schema sent: {body['schema']}"
            ) from exc
        return {
            "id": table["id"],
            "name": table.get("name"),
            "key_column": "key",
            "key_column_label": key_column,
        }

    # --- schema reading ------------------------------------------------------

    def _key_column(schema: dict[str, Any]) -> str:
        """Name of the key column's property in a Data Table schema.

        Not ``schema["title"]`` - that is the table name. Genesys names the key
        property ``key`` and puts its display label in that property's own
        ``title``, so a table called "AgentScript Pages by Channel" with a key
        labelled "channel" still has its key under ``key``. Read it from
        ``required``, which Genesys populates with exactly the key column.
        """
        required = schema.get("required") or []
        properties = schema.get("properties") or {}
        for name in required:
            if name in properties:
                return str(name)
        return "key"

    # --- name resolution -----------------------------------------------------

    def _table_id(value: str) -> str:
        client = get_client()
        return resolve(
            value,
            label="data table",
            search=lambda v: [
                t
                for t in client.paginate("/api/v2/flows/datatables")
                if v.lower() in (t.get("name") or "").lower()
            ],
            list_available=paginate_lister(client, "/api/v2/flows/datatables"),
        )
