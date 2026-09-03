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
| A repository with Architect YAML | the flow tools only | yes |
| The Archy binary | the flow tools only | yes |

The last two go together and are genuinely optional. Without them you lose the
handful of flow tools and keep the other sixty-odd. Set them up later if and
when you need them — see [Flow tools](#flow-tools-optional) below.

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

If you already use Archy, its client in `~/.archy_config` is often enough to
start with — but it will not carry the Copilot permissions unless you add them.

## 2. Clone and run the setup script

```bash
git clone https://github.com/Frank-F43/genesys-builder-mcp.git
cd genesys-builder-mcp/mcp && ./setup.sh
```

The script installs dependencies and then reports on four things. It does not
change anything outside its own virtual environment; the configuration it prints
is for you to paste.

**Checking for uv** — if missing, it prints the install command and stops.

**Checking credentials** — it asks the server itself to resolve them, so what it
reports is exactly what the server will see at runtime. Credentials come from
`GENESYSCLOUD_REGION`, `GENESYSCLOUD_OAUTHCLIENT_ID` and
`GENESYSCLOUD_OAUTHCLIENT_SECRET`, or from `~/.archy_config`. If neither is
present the script leaves placeholders in the printed configuration for you to
fill in.

Keep the secret out of any repository. `~/.archy_config` or your editor's MCP
configuration in your home directory are both fine; a JSON file inside a project
you might later push is not.

**Locating your flow repository and Archy** — see below. A warning here is
expected on a fresh install and costs you only the flow tools.

**Editor configuration** — a ready-made JSON block with absolute paths already
filled in. In Cursor it goes into `.cursor/mcp.json` in your project root; other
editors use the same shape.

## 3. Restart and verify

Restart the editor **completely**. Toggling the MCP server off and on does not
replace the running process, which is the single most common reason a fresh
setup appears not to work.

Then check, in rising order of confidence:

```bash
cd mcp
uv run python show_tools.py     # lists every tool the code defines
uv run python smoke_test.py     # calls the org and reports what came back
```

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
`validate_flow_file` and the rest — need two things that are not part of this
repository:

1. **A repository holding your Architect YAML**, with an `./archy` wrapper at
   its root. The wrapper resolves the binary relative to itself, so the
   repository works from any clone location.
2. **The Archy binary**, a 75 MB download deliberately kept out of git. Get it
   from [developer.genesys.cloud/devapps/archy](https://developer.genesys.cloud/devapps/archy/),
   put it in `.archy/bin/` in that repository and make it executable.

`setup.sh` looks for the wrapper above itself and, when it finds one, actually
runs `archy --version` rather than merely checking that a file exists — on macOS
a freshly downloaded binary is often quarantined, and that only shows on
execution. If it is, clear the flag:

```bash
xattr -d com.apple.quarantine .archy/bin/archy-macos-*
```

Point `GENESYS_BUILDER_REPO` at that repository. Pointing it at a directory
without the wrapper fails every flow tool, so leaving it unset is the safer of
the two — unset merely makes them unavailable and nothing else notices.

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

**Flow tools fail while everything else works.** Almost always Archy: either not
downloaded, not executable, or quarantined by macOS. Run `./archy --version` in
your flow repository — it should print a version number.
