# Genesys Cloud Demo Toolkit

Shareable tooling for building Genesys Cloud demos with an AI coding agent: MCP server,
Copilot lifecycle skills, NLU measurement scripts, and operational helpers.

Published separately from demo-specific flow configuration — colleagues install this
against their own org.

> **Paths in this file** are written for the published repository, where this file
> sits at the root next to `mcp/` and `skills/`. In the source repository the same
> tree lives under `toolkit/`, so prepend that prefix there: `mcp` becomes
> `toolkit/mcp`.

## Moved from an earlier clone?

The published repository used to contain the MCP server at its root. It now carries the
whole toolkit, so the server sits one level down in `mcp/`, alongside `skills/` and
`nlu-probe/`.

Nothing is lost, but an existing `.cursor/mcp.json` points at the old location and the
server will fail to start. Append `/mcp` to the `--directory` argument:

```jsonc
"args": ["run", "--directory", "/path/to/genesys-builder-mcp/mcp", "genesys-builder-mcp"]
```

Then pull and restart Cursor completely — toggling the MCP server off and on does not
replace the running process.

## Contents

| Path | Purpose |
| --- | --- |
| `mcp/` | genesys-builder MCP server (flows, Copilot, routing, knowledge, …) |
| `skills/` | Cursor Agent Skills for Agent Copilot demo work |
| `nlu-probe/` | NLU probe stand (`probe.py`, `train.py`) |
| `scripts/` | Operational scripts (data actions, agent script replace, flow resync, …) |
| `docs/` | Editor setup notes |

## MCP server setup

```bash
cd mcp && ./setup.sh
```

The script installs the dependencies, checks whether credentials already resolve,
looks for a flow repository and a working Archy, and then prints a ready-made
`.cursor/mcp.json` block with the absolute paths filled in. Paste it into your
project and restart the editor completely — toggling the server off and on does
not replace the running process.

Credentials come from `GENESYSCLOUD_REGION`, `GENESYSCLOUD_OAUTHCLIENT_ID` and
`GENESYSCLOUD_OAUTHCLIENT_SECRET`, or from `~/.archy_config` if you already use
Archy. See [mcp/docs/oauth-role.md](mcp/docs/oauth-role.md) for the permissions
the OAuth client needs.

Verify:

```bash
cd mcp && uv run python smoke_test.py
uv run python show_tools.py
```

**[SETUP.md](SETUP.md)** walks through all of this properly, including the OAuth
role, the optional flow tools and what to do when something does not work.
**[UPDATE.md](UPDATE.md)** covers picking up new tools later. See
[mcp/README.md](mcp/README.md) for the server itself.

## Copilot skills setup

Install them where Cursor loads agent skills:

**Personal (recommended for colleagues)** — available in all projects:

```bash
mkdir -p ~/.cursor/skills
cp -R skills/copilot-* ~/.cursor/skills/
```

**Project-local** — commit a `.cursor/skills/` copy in your demo repo if you want
the team to share identical instructions via git.

Each skill is a directory with `SKILL.md`. Cursor discovers them by the `name` and
`description` frontmatter fields.

### Lifecycle order

1. **copilot-dispatch** — entry point, lists Copilots, routes
2. **copilot-design** — asks for demo script first; derives intents, rules, and optional checklist/summary/knowledge
3. **copilot-build** — publishes to Genesys Cloud via MCP
4. **copilot-measure** — NLU probe stand, `measure_copilot_nlu`, or both
5. **copilot-testplan** — live demo run sheet (NLU scores when applicable)

Skills assume an **agent-assisted** demo (voice or messaging with Copilot on the agent desktop), one language per Copilot unless the author explicitly wants several. Scope is whatever the demo needs — rules-only Copilots without checklist, summary, or knowledge base are valid.

Tell the agent explicitly which skill to use, e.g. *"Use copilot-design for Support EN"* —
skills do not auto-run unless your Cursor settings enable skill invocation.

## NLU probe stand

See [nlu-probe/README.md](nlu-probe/README.md). Requires MCP credentials; read-only
with `probe.py`, writes NLU only with `train.py --apply`. For quick checks inside the
agent session, the MCP tool `measure_copilot_nlu` accepts the same corpus format or
inline sentences (`strict=True` for demo script lines).