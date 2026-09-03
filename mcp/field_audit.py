"""Report fields that every result leaves empty.

A tool that asks the API for a field it does not return, or reads a nested
object without expanding it, still answers successfully. Nothing raises, the
call looks healthy, and the field is simply absent from every row. That is the
one failure mode the smoke test cannot see, because the smoke test asks whether
a tool answers, not whether the answer is complete.

Both bugs found this way looked like facts about the org rather than bugs:
knowledge bases that appeared to hold no documents, and contacts that appeared
to belong to no organization.

An empty column is a signal, not a verdict. Plenty of fields are legitimately
empty across a whole org - a flow nobody has checked out, a queue with no
description. Check a hit against the raw API before treating it as a defect.

The probes only cover tools that return lists, since a single object has no
comparison set to make "always empty" mean anything.

    PYTHONPATH=src .venv/bin/python field_audit.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

logging.disable(logging.INFO)

from genesys_builder_mcp.server import mcp  # noqa: E402

PROBES: list[tuple[str, dict]] = [
    ("list_divisions", {}),
    ("list_integrations", {}),
    ("list_queues", {}),
    ("list_skills", {}),
    ("list_wrapup_codes", {}),
    ("list_schedules", {}),
    ("list_roles", {}),
    ("find_users", {"query": "a", "limit": 10}),
    ("list_data_tables", {}),
    ("list_copilots", {}),
    ("list_agent_scripts", {}),
    ("list_data_actions", {}),
    ("list_knowledge_bases", {}),
    ("list_org_flows", {"flow_type": "inboundcall"}),
    ("find_external_contact", {"query": "a"}),
]

EMPTY = (None, "", [], {})


async def call(name: str, args: dict) -> object:
    result = await mcp.call_tool(name, args)
    if result.is_error:
        raise RuntimeError(result.content[0].text)
    return (result.structured_content or {}).get("result")


async def main() -> int:
    hits = 0
    for tool, args in PROBES:
        try:
            rows = await call(tool, args)
        except Exception as exc:  # noqa: BLE001 - the report is the point
            print(f"  ??    {tool}: could not probe - {exc}")
            continue

        if not isinstance(rows, list) or not rows:
            print(f"  --    {tool}: no rows to judge")
            continue

        dicts = [row for row in rows if isinstance(row, dict)]
        keys = {key for row in dicts for key in row}
        empty = sorted(key for key in keys if all(row.get(key) in EMPTY for row in dicts))

        if empty:
            hits += 1
            print(f"  check {tool} ({len(dicts)} rows): always empty - {', '.join(empty)}")
        else:
            print(f"  ok    {tool} ({len(dicts)} rows)")

    print()
    if hits:
        print(f"{hits} tool(s) worth checking against the raw API.")
    else:
        print("every probed tool fills every field it reports")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
