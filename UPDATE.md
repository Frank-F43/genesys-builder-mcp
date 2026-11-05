# Updating

Picking up new or changed tools after this repository moves on. One script and
a restart.

```bash
cd genesys-builder-mcp
./update.sh /path/to/your-project
```

Then **restart the editor completely** — in Cursor, Cmd+Q and reopen.

That last step is not optional and not a formality. Neither toggling the MCP
server off and on nor *Reload Window* replaces the running process, so the
editor keeps talking to the code as it was when it started. Almost every "the
new tool is not there" report comes down to this.

## What the script does

**Brings the clone up to date.** Each release is published as a single commit
without history, so a plain `git pull` refuses with *"refusing to merge
unrelated histories"*. The script jumps to the new release instead and keeps
the previous state as a `backup/before-update-…` branch. It stops before
touching anything if you have local changes to tracked files — commit or stash
them first.

**Installs dependencies** (`uv sync`). Skipping that works right up until a
release adds a library, and then the server fails to start with an import error
that says nothing about the update.

**Refreshes the skills you installed.** Skills are copied out of the repository,
so a pull alone does not touch your installed copies. The script updates every
skill it finds in `~/.cursor/skills/` and lists the ones you do not have.

**Checks your configuration** — the project's `.cursor/mcp.json`, or
`~/.cursor/mcp.json` without an argument:

- that `--directory` points at this clone's `mcp/`,
- that the `env` block carries the three `GENESYSCLOUD_*` values — and if it
  does not but `~/.archy_config` exists, it offers to copy them over, without
  showing them,
- that the flow repository ignores `.cursor/mcp.json` and `.genesys-builder/`,
- and what Archy left behind that can go.

**Shows which tools are new or gone**, and reminds you to restart.

It is safe to re-run at any time. Apart from this clone and your installed
skills it changes nothing, and a project's `.cursor/mcp.json` only after you
answer yes.

## The switch away from Archy

The release that introduced `update.sh` also dropped the Archy CLI. Flows now
travel through Genesys Cloud's Architect job API, which runs Archy on the
server side. What that means for an existing setup:

- **Credentials move into the project.** The server no longer reads
  `~/.archy_config`. The script offers to copy the values into the `env` block;
  afterwards that file can go.
- **The role needs the job permissions:** `architect:job:create` and `:view`,
  `architect:jobExport:create` and `:view`, `architect:flow:unlock`, and
  `architect:flow:delete` for the scratch copies `validate_flow_file` publishes.
  Without them the flow tools answer 403 and everything else keeps working.
- **The flow repository no longer needs `./archy` or `.archy/bin/`.** The
  export state moves from `.archy/flow-state.json` to
  `.genesys-builder/flow-state.json` by itself the first time a flow tool runs.
- **Archy itself can be uninstalled**, and with it the need for Rosetta.
- **The first export of each flow is wordier** than before — the server-side
  exporter writes out unbound data table columns and orders some blocks
  differently. A one-off diff, not a change in behaviour.

## Checking that it worked

`show_tools.py` lists what the code defines, which is the ground truth:

```bash
cd mcp && uv run python show_tools.py
```

To find out whether your **editor** caught up — a different question from what
the code defines — ask the agent to list its `genesys-builder` tools and compare
the count. A mismatch means the process is older than the checkout: restart
again. This is worth doing after any update that announced a new tool, because
the failure is silent. The tool is simply absent, and an agent asked to use it
will improvise with something else rather than tell you it is missing.

## When an update needs more than a restart

Some changes reach outside this repository, and the release notes say so.

**New permissions.** A new tool may call an API your OAuth role does not cover
yet. The symptom is a `403` from that one tool while everything else works.
[mcp/docs/oauth-role.md](mcp/docs/oauth-role.md) lists every permission and what
needs it; add the missing one to the role.

**A moved server path.** Then `--directory` in your MCP configuration points at
the old place and the server fails to start. `update.sh` notices; `mcp/setup.sh`
prints a corrected block.

## Rolling back

Nothing here writes to your org during an update, so going back is just git:

```bash
git branch --list 'backup/*'
git reset --hard backup/before-update-<timestamp>
cd mcp && uv sync
```

Restart the editor afterwards, for the same reason as above.
