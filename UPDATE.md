# Updating

Picking up new or changed tools after this repository moves on. Usually three
commands and a restart.

```bash
cd genesys-builder-mcp
git pull
cd mcp && uv sync
```

Then **restart the editor completely**.

That last step is not optional and not a formality. Toggling the MCP server off
and on in the settings does not replace the running process, so the editor keeps
talking to the code as it was when it started. Almost every "the new tool is not
there" report comes down to this.

## Why each step

**`git pull`** brings the new code, and also new skills and documentation.

**`uv sync`** installs dependencies that the new code may need. Skipping it
works right up until a release adds a library, and then the server fails to
start with an import error that says nothing about the update.

**Restarting** is what makes the editor launch the new process. Nothing short of
a full restart does it.

## Checking that it worked

`show_tools.py` lists what the code defines, which is the ground truth:

```bash
cd mcp && uv run python show_tools.py
```

The last line reports a count. To see exactly what an update brought, take that
list before and after:

```bash
cd mcp
uv run python show_tools.py | sed -n 's/^  \([a-z_]*\) .*/\1/p' | sort > /tmp/tools-before.txt
cd .. && git pull && cd mcp && uv sync
uv run python show_tools.py | sed -n 's/^  \([a-z_]*\) .*/\1/p' | sort > /tmp/tools-after.txt
comm -13 /tmp/tools-before.txt /tmp/tools-after.txt      # new
comm -23 /tmp/tools-before.txt /tmp/tools-after.txt      # gone
```

To find out whether your **editor** caught up — a different question from what
the code defines — ask the agent to list its `genesys-builder` tools and compare
the count. A mismatch means the process is older than the checkout: restart
again. This is worth doing after any update that announced a new tool, because
the failure is silent. The tool is simply absent, and an agent asked to use it
will improvise with something else rather than tell you it is missing.

## Updating the skills

Skills are copied out of the repository, so `git pull` alone does not touch your
installed copies. Copy them again:

```bash
cp -R skills/copilot-* ~/.cursor/skills/
```

Or, if you installed them project-locally, into that project's
`.cursor/skills/`. Skills are read per session, so a new chat picks them up — no
restart needed.

## When the server path changes

Rarely, a release moves where the server lives inside the repository. Then
`--directory` in your MCP configuration points at the old place and the server
fails to start. Re-running the setup script prints a corrected block:

```bash
cd mcp && ./setup.sh
```

It is safe to re-run at any time: it installs dependencies, reports what it
finds and prints configuration. It changes nothing outside its own virtual
environment and never touches your credentials.

## When an update needs more than a restart

Some changes reach outside this repository, and the release notes say so. Two
kinds come up:

**New permissions.** A new tool may call an API your OAuth role does not cover
yet. The symptom is a `403` from that one tool while everything else works.
[mcp/docs/oauth-role.md](mcp/docs/oauth-role.md) lists every permission and what
needs it; add the missing one to the role.

**Archy.** The flow tools wrap the Archy CLI. If a release requires a newer
Archy, drop the new binary into `.archy/bin/` in your flow repository — the
wrapper picks the highest version present, so upgrading is a matter of adding
the new one and deleting the old.

## Rolling back

Nothing here writes to your org during an update, so going back is just git:

```bash
git log --oneline -10
git checkout <commit>
cd mcp && uv sync
```

Restart the editor afterwards, for the same reason as above.
