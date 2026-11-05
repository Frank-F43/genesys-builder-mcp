# Setup

Getting the `genesys-builder` MCP server and the Copilot skills running against
your own Genesys Cloud org. Allow about twenty minutes, most of it waiting for a
role to be assembled in the admin UI.

Paths here are written for the published repository, where this file sits at the
root next to `mcp/` and `skills/`.

## What you need first

| | Needed for | Optional? |
| --- | --- | --- |
| [uv](https://docs.astral.sh/uv/) | running the server | no |
| An OAuth client, Client Credentials grant | everything | no |
| A role for that client | everything | no |
| A git repository with Architect YAML | the flow tools only | yes |

The repository is genuinely optional. Without it you lose the handful of flow
tools and keep the other sixty-odd. Set it up later if and when you need it —
see [Flow tools](#flow-tools-optional) below. Nothing else needs installing:
flows travel through Genesys Cloud's Architect job API, so there is no Archy
binary and no Rosetta.

## 1. The OAuth client and its role

The server authenticates as an OAuth client using the Client Credentials grant,
so it acts as the org rather than as a person. **The role you give that client
is the real security boundary** — the server can do exactly what the role
permits and nothing else. A role that is too small is harmless: the affected
tools return a `403` and change nothing.

In **Admin → Integrations → OAuth → Add Client**:

- Grant type: **Client Credentials**
- Assign the role from the next step
- Note the Client ID and Secret

Then build the role in **Admin → People & Permissions → Roles**. The complete
list of permissions, grouped by what each one unlocks, is in
**[mcp/docs/oauth-role.md](mcp/docs/oauth-role.md)**. Every string there is
checked against the published OpenAPI specification, so the admin search box
will find what is written.

Two traps worth knowing before you start:

- Agent Copilot reaches across four permission families — `assistants:*`,
  `languageUnderstanding:*`, `aiStudio:summaryConfig:*` and `knowledge:*`. Miss
  one and the rest keeps working, so a build gets most of the way and then
  stops, which is hard to diagnose.
- Summary settings live under **`aiStudio`**, not `assistants`. A role assembled
  by searching for "assistant" will not have them, and summaries then fail while
  everything else is fine.

An OAuth client you already set up for Archy is often enough to start with —
but it will not carry the Copilot permissions unless you add them, and its
values have to be copied into the project configuration (step 2), because the
server no longer reads `~/.archy_config`.

## 2. Clone and run the setup script

```bash
git clone https://github.com/Frank-F43/genesys-builder-mcp.git
cd genesys-builder-mcp/mcp && ./setup.sh /path/to/your-flow-repo
```

The argument is optional — leave it out if you have no flow repository yet. The
script installs dependencies and then reports on four things. It does not
change anything outside its own virtual environment; the configuration it prints
is for you to paste.

**Checking for uv** — if missing, it prints the install command and stops.

**Locating your flow repository** — see below. A warning here is expected on a
fresh install and costs you only the flow tools.

**Checking credentials** — it asks the server itself to resolve them, with the
project's `.cursor/mcp.json` applied, so what it reports is exactly what the
server will see at runtime. Credentials are the three values
`GENESYSCLOUD_REGION`, `GENESYSCLOUD_OAUTHCLIENT_ID` and
`GENESYSCLOUD_OAUTHCLIENT_SECRET` in the `env` block of that file. They make the
org a property of the project: a second project for another org gets its own
file, and there is deliberately no machine-wide fallback that could send one
project's changes into another's org.

Keep the secret out of git: add `.cursor/mcp.json` to the project's
`.gitignore` before you fill it in.

**Editor configuration** — a ready-made JSON block with absolute paths already
filled in. In Cursor it goes into `.cursor/mcp.json` in your project root; other
editors use the same shape. Scripts you run from a terminal inside the project —
`smoke_test.py`, the NLU probe — read the same file, so they talk to the same
org as the editor.

## 3. Restart and verify

Restart the editor **completely** — in Cursor that is Cmd+Q and reopen.
Neither toggling the MCP server off and on nor *Reload Window* replaces the
running server process, which is the single most common reason a fresh setup
appears not to work. Also check in Cursor's MCP settings that `genesys-builder`
is enabled; a disabled server looks exactly like a missing one.

Then check, in rising order of confidence:

```bash
cd /path/to/your-project          # where .cursor/mcp.json lives
uv run --project ~/genesys-builder-mcp/mcp python ~/genesys-builder-mcp/mcp/show_tools.py
uv run --project ~/genesys-builder-mcp/mcp python ~/genesys-builder-mcp/mcp/smoke_test.py
```

The first lists every tool the code defines; the second calls the org and
reports what came back. Run them from the project — `--project`, unlike
`--directory`, keeps you there, which is how the smoke test finds the
credentials. Adjust `~/genesys-builder-mcp` to wherever you cloned it.

Finally ask the agent something only a working connection can answer, such as
*"which queues exist in my org?"* or *"run whoami"*. `whoami` reports which org
you are connected to and with which permissions, which is also the quickest way
to spot a role that is missing something.

Before you let an agent change anything, it is worth knowing what stands between
it and your org: tools that replace or remove something ask you to confirm
first, the rest do not, and every write reports what it did.
[mcp/docs/write-safety.md](mcp/docs/write-safety.md) says which is which.

## 4. The Copilot skills

Five skills lead through designing, building, measuring and test-planning an
Agent Copilot. Install them where Cursor looks for agent skills:

```bash
mkdir -p ~/.cursor/skills
cp -R skills/copilot-* ~/.cursor/skills/
```

That makes them available in every project. If you would rather have a team
share identical instructions through git, copy them into `.cursor/skills/` in
your demo repository instead.

Skills do not run by themselves unless your Cursor settings allow it. Name the
one you want: *"Use copilot-design for Support EN"*. The intended order is
`copilot-dispatch` → `design` → `build` → `measure` → `testplan`.

## The API specification (optional)

Nothing here needs it, and most work never touches it. But when an agent has to
get a **Data Action's** request and response shape exactly right, or reach an
endpoint through `genesys_api_call` that no tool covers, having the spec locally
beats guessing:

```bash
python3 scripts/fetch_api_spec.py
```

That lands about 22 MB in `reference/swagger/`, gitignored because it is a
reproducible download rather than source. Tell your agent it is there — and that
it must be **searched, not read whole**, because it does not fit in a context
window. [scripts/README.md](scripts/README.md) has the detail.

## Flow tools (optional)

The flow tools — `export_flow`, `publish_flow`, `flow_status`,
`validate_flow_file` and the rest — need one thing that is not part of this
repository: **a git repository holding your Architect YAML**. Point
`GENESYS_BUILDER_REPO` at it, or pass it to `setup.sh`, which fills it in.

Flows move through Genesys Cloud's Architect job API, which runs Archy on the
server side in its current version. Three consequences worth knowing:

- **Validation publishes a copy.** The API validates a file only by trying to
  publish it, so `validate_flow_file` publishes it under a scratch name
  (`zz validate …`) and deletes that copy straight away. The real flow is never
  touched. An invalid file is rejected before anything is created.
- **A rejected publish changes nothing.** An invalid file leaves the existing
  flow exactly as it was — same version, no draft, no lock.
- **The first export after switching is wordier.** The server-side exporter
  writes out unbound data table columns and orders some blocks differently from
  older local Archy versions. That is a one-off diff per flow, not a change in
  behaviour.

The tools record what they exported in `.genesys-builder/flow-state.json` in
that repository; add `.genesys-builder/` to its `.gitignore`. An older
`.archy/flow-state.json` is moved there automatically.

Which actions exist per flow type, and their properties, comes from the
Architect Scripting SDK — see [architect-catalog/](architect-catalog/) for how
to build the catalog.

## When something does not work

**Tools do not appear at all.** The editor was not restarted completely, or
`--directory` in the configuration points somewhere else than the `mcp/`
directory. Compare it against what `setup.sh` printed.

**A single tool is missing.** You are running an older process than your
checkout. `uv run python show_tools.py` lists what the code defines; if a tool is
there but not in your editor, restart it. See [UPDATE.md](UPDATE.md).

**A tool returns 403.** The role is missing that permission. `whoami` shows what
the token carries; [mcp/docs/oauth-role.md](mcp/docs/oauth-role.md) says which
permission the tool needs and why.

**Credentials do not resolve.** Run the same check the script runs and read the
error rather than guessing:

```bash
uv run --directory mcp python -c "from genesys_builder_mcp.config import load_config; print(load_config())"
```

**Flow tools fail while everything else works.** Usually `GENESYS_BUILDER_REPO`
is unset or points somewhere without your flows, or the role lacks the
Architect job permissions — `architect:job:*`, `architect:jobExport:*` and
`architect:flow:delete` for the validation copies. See
[mcp/docs/oauth-role.md](mcp/docs/oauth-role.md).
