# genesys-builder MCP server

Lets an AI coding agent read and change Genesys Cloud configuration: data
actions, agent scripts, external contacts, knowledge, queues, users, data tables,
Messenger configurations, web deployments and Architect flows.

Nothing here is specific to one org. It reads whichever org your OAuth client
belongs to.

## Why one server for flows and everything around them

A flow is rarely the whole change. The data action it calls, the queue it
transfers to, the agent script that pops on the desktop, the knowledge base it
searches — all of that lives behind the Platform API, next to the Architect job
API that flows themselves travel through. This server covers both, so one agent
session can work across all of it, with nothing to install besides `uv`.

## Setup

```bash
./setup.sh /path/to/your-flow-repo
```

It checks for [uv](https://docs.astral.sh/uv/), installs dependencies, reports
whether your credentials resolve, and prints the editor configuration with the
absolute paths already filled in.

Credentials are `GENESYSCLOUD_REGION`, `GENESYSCLOUD_OAUTHCLIENT_ID` and
`GENESYSCLOUD_OAUTHCLIENT_SECRET`, resolved in this order:

1. the project's `.cursor/mcp.json`, found from `GENESYS_BUILDER_REPO` or the
   working directory upwards — so scripts run from a terminal inside the
   project reach the same org as the editor, whatever the shell exports
2. the environment — which is how the editor passes the `env` block of the
   configuration that started the server
3. `~/.cursor/mcp.json`, Cursor's global configuration, last because it may
   describe another org than the project at hand

`~/.archy_config` is not read. `whoami` reports under `credentials_from` which
of these the running server used.

The OAuth client needs the Client Credentials grant. See
[docs/oauth-role.md](docs/oauth-role.md) for the permissions.

To check it against your org without an editor, from the project that holds
`.cursor/mcp.json` (`--project` keeps the working directory, so the credentials
are found):

```bash
uv run --project /path/to/genesys-builder-mcp/mcp python /path/to/genesys-builder-mcp/mcp/smoke_test.py
```

That calls every read-only tool and changes nothing.

A tool can answer and still be wrong. Asking for a field the API does not
return, or reading a nested object without expanding it, leaves that field empty
in every row while the call itself looks healthy. To catch that:

```bash
uv run --project /path/to/genesys-builder-mcp/mcp python /path/to/genesys-builder-mcp/mcp/field_audit.py
```

It reports fields that are empty across all results. Some legitimately are, so
check a hit against the raw API before treating it as a defect.

### After changing a tool

The package is installed editable, so the source is what runs - but a server
already started keeps the code it loaded. **Restart it in your editor after
editing a tool**, or you will be calling yesterday's version.

Editors cache the tool list on top of that, for the length of a session. A
description can therefore still read as it did before your edit even once the
server is serving the new one, which looks like the restart failed. To see what
is really being served rather than what your editor remembers:

```bash
uv run python show_tools.py            # every tool, one line each
uv run python show_tools.py knowledge  # full text of matching tools
```

## Addressing things by name

Queues, flows, users, skills, data tables and knowledge bases are all addressed
by name. Ask for "the Support EN queue", not a GUID. Where a name is ambiguous
the tool says so and lists the candidates rather than guessing.

## Working on flows

Flows need `GENESYS_BUILDER_REPO` pointing at the git repository that holds your
Architect YAML. Without it the other tools still work.

```
flow_status          is my copy still current?
export_flow          pull the live version down
  ... edit ...
validate_flow_file   check it without changing the flow
publish_flow         make it live
```

All of it goes through the Architect job API, which runs Archy inside Genesys
Cloud in its current version. That API validates a file only by publishing it,
so `validate_flow_file` publishes a copy under a scratch name (`zz validate …`)
and deletes it straight away; the real flow is never touched. A file that fails
validation is rejected before anything is created or changed — publishing an
invalid file over an existing flow leaves that flow as it was.

`export_flow` leaves a file alone when it differs from the export only in block
order and quoting, so re-exporting does not produce noise diffs. Which actions
exist per flow type comes from [../architect-catalog/](../architect-catalog/).

`flow_status` is the one that earns its keep. A YAML file on disk can be older
than what is live, and publishing a stale one silently reverts whatever changed
in Architect since — the publish succeeds, so nothing tells you. Export records
the version and a content hash; `publish_flow` refuses when they no longer match
and shows what the org has that your file does not.

`flow_change_history` answers the follow-up question: not whether a flow moved
on, but who moved it. The version list will not tell you — it leaves `createdBy`
empty for everything the toolkit published, since a client credentials grant
has no user behind it. The audit trail distinguishes an OAuth client acting for itself
from a person signed in to Architect, and a person is the case worth knowing
about, because their work is what a stale publish would revert. It needs
`audits:audit:view` on the role.

## Before a write

Tools that change the org return a `write_impact` block saying what happened and
what the previous state was, and the ones that replace or remove something ask
you first. [docs/write-safety.md](docs/write-safety.md) lists exactly which
tools prompt, when, and what happens on a client that cannot show a prompt —
worth reading before relying on it.

## Deleting

The OAuth client's role decides what can be deleted at all, and nothing here
adds to that. What `delete_object` adds is deliberateness: it takes two calls.

```
delete_object(kind="data_action", target="Old Lookup")
  -> resolves to "Old Lookup", says what breaks if it goes

delete_object(kind="data_action", target="Old Lookup",
              confirm_name="Old Lookup")
  -> deleted
```

Passing a name that does not match deletes nothing. The gate is easy to pass on
purpose and hard to pass by accident, which is the useful distinction — most of
what lives here has no version history, so a queue or knowledge base that goes
is rebuilt by hand.

`deletable_kinds` lists what it covers. For endpoints it does not,
`genesys_api_call` accepts `DELETE` when `confirm_delete_path` repeats the path
exactly.

Prefer `reset_external_contact` over deleting a contact: it deletes and
recreates, which is how a contact's identity-stitching history gets cleared
without losing the contact.

A deletion refused with `424 ... status unknown`, or `Server Error Occurred` in
the UI, almost never means the object is in use. Architect's dependency index
goes stale whenever flows are published, and the platform refuses deletions it
can no longer check. `dependency_tracking_status` confirms it and
`rebuild_dependency_tracking` fixes it in a couple of minutes; `delete_object`
recognises this case and points at them.
