"""MCP server entry point."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from .tools import (
    agent_scripts,
    audit,
    conversation_audit,
    copilot,
    data_actions,
    data_tables,
    deletion,
    dependency_tracking,
    discovery,
    execution_history,
    external_contacts,
    flows,
    knowledge,
    raw,
    routing,
    users,
)

mcp = MCPServer("genesys-builder")

discovery.register(mcp)
audit.register(mcp)
flows.register(mcp)
execution_history.register(mcp)
routing.register(mcp)
users.register(mcp)
data_tables.register(mcp)
data_actions.register(mcp)
agent_scripts.register(mcp)
copilot.register(mcp)
conversation_audit.register(mcp)
external_contacts.register(mcp)
knowledge.register(mcp)
deletion.register(mcp)
dependency_tracking.register(mcp)
raw.register(mcp)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
