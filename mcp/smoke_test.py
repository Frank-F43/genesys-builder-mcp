"""Call every read-only tool against the configured org.

Not a unit test: it checks that the tools work against a real org, which is
where the interesting failures live (pagination shape, name resolution,
permissions on the OAuth client). Run it after adding a tool group.

    PYTHONPATH=src .venv/bin/python smoke_test.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

logging.disable(logging.INFO)

from genesys_builder_mcp.client import get_client  # noqa: E402
from genesys_builder_mcp.server import mcp  # noqa: E402


async def call(name: str, args: dict | None = None):
    result = await mcp.call_tool(name, args or {})
    if result.is_error:
        raise RuntimeError(f"{name}: {result.content[0].text}")
    return (result.structured_content or {}).get("result")


async def main() -> int:
    failures: list[str] = []

    async def check(label: str, coro):
        try:
            value = await coro
            count = len(value) if isinstance(value, list) else 1
            print(f"  ok    {label} ({count})")
            return value
        except Exception as exc:  # noqa: BLE001 - the report is the point
            print(f"  FAIL  {label}: {exc}")
            failures.append(label)
            return None

    print("discovery")
    await check("whoami", call("whoami"))
    await check("list_divisions", call("list_divisions"))
    await check("list_integrations", call("list_integrations"))

    print("routing")
    queues = await check("list_queues", call("list_queues"))
    if queues:
        await check("get_queue by name", call("get_queue", {"queue": queues[0]["name"]}))
    await check("list_skills", call("list_skills"))
    await check("list_wrapup_codes", call("list_wrapup_codes"))
    await check("list_schedules", call("list_schedules"))

    print("users")
    await check("list_roles", call("list_roles"))
    users = await check("find_users", call("find_users", {"query": "a", "limit": 5}))
    if users:
        await check("get_user by name", call("get_user", {"user": users[0]["email"]}))

    print("data tables")
    tables = await check("list_data_tables", call("list_data_tables"))
    if tables:
        name = tables[0]["name"]
        await check("get_data_table_schema by name", call("get_data_table_schema", {"table": name}))
        await check("list_data_table_rows", call("list_data_table_rows", {"table": name, "limit": 5}))

    print("copilot")
    # Only assistants with a copilot configuration can answer the other calls,
    # so the first one carrying it decides what the rest run against. It also
    # has to have a name no other assistant shares: addressing by name is the
    # point of the exercise, and the resolver rightly refuses an ambiguous one.
    # Picking a duplicate would report four failures on a healthy install.
    assistants = await check("list_copilots", call("list_copilots"))
    seen: dict[str, int] = {}
    for a in assistants or []:
        seen[a["name"]] = seen.get(a["name"], 0) + 1
    configured = next(
        (a for a in assistants or [] if a["copilot_configured"] and seen[a["name"]] == 1),
        None,
    )
    if configured:
        name = configured["name"]
        await check("get_copilot by name", call("get_copilot", {"assistant": name}))
        await check("list_copilot_rules", call("list_copilot_rules", {"assistant": name}))
        await check("get_copilot_nlu", call("get_copilot_nlu", {"assistant": name}))
        await check("list_copilot_queues", call("list_copilot_queues", {"assistant": name}))
    elif any(a["copilot_configured"] for a in assistants or []):
        print("  skip  copilot by name: every configured assistant shares its name")

    print("flows")
    flows = await check("list_org_flows", call("list_org_flows", {"flow_type": "inboundcall"}))

    print("external contacts")
    contacts = await check("find_external_contact", call("find_external_contact", {"query": "a"}))
    # Checking that the tool answers is not enough. Its organization field read
    # as null for a while, because the search endpoint only returns the
    # organization when asked with an explicit expand - the tool looked healthy
    # and quietly dropped a field. Comparing a few results against the raw
    # records catches that class of omission without assuming any particular
    # contact exists in the org.
    for contact in (contacts or [])[:5]:
        raw = get_client().get(
            f"/api/v2/externalcontacts/contacts/{contact['id']}",
            params={"expand": "externalOrganization"},
        )
        expected = (raw.get("externalOrganization") or {}).get("name")
        if expected and not contact.get("organization"):
            print(f"  FAIL  find_external_contact drops organization {expected!r} for {contact['id']}")
            failures.append("find_external_contact organization")
            break
    else:
        if contacts:
            print("  ok    find_external_contact reports organization")

    print("execution history")
    # The history cannot be listed unfiltered, so it needs a flow to ask about.
    # A flow with no recent executions is a valid answer, not a failure.
    if flows:
        executions = await check(
            "find_flow_executions",
            call("find_flow_executions", {"flow_id": flows[0]["id"], "page_size": 5}),
        )
        found = (executions or {}).get("executions") or []
        if found:
            await check(
                "get_flow_execution",
                call("get_flow_execution", {"execution_id": found[0]["execution_id"]}),
            )

    print("content")
    await check("list_agent_scripts", call("list_agent_scripts"))
    await check("list_data_actions", call("list_data_actions"))
    bases = await check("list_knowledge_bases", call("list_knowledge_bases"))
    if bases:
        await check(
            "list_knowledge_articles",
            call("list_knowledge_articles", {"knowledge_base": bases[0]["name"], "limit": 3}),
        )

    print()
    if failures:
        print(f"{len(failures)} failed: {', '.join(failures)}")
        return 1
    print("all read tools ok")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
