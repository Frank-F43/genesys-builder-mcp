# Architect catalog

Which Architect actions and menus exist, per flow type, in *your* org, and which
properties each one has. It is the reference for writing flow YAML by hand or
by agent, especially with an action nobody in the repository has used yet.

```bash
cd /path/to/your-flow-repo
/path/to/genesys-builder-mcp/architect-catalog/refresh.sh
```

Needs Node 22 or newer. The script installs the newest
[Architect Scripting SDK](https://mypurecloud.github.io/purecloud-flow-scripting-api-sdk-javascript/)
and writes `reference/architect/catalog.json` next to this directory. Both are
gitignored: the catalog is a reproducible download, and it describes one org.

Run it from inside your project so it finds the credentials in
`.cursor/mcp.json`, or export the three `GENESYSCLOUD_*` variables. It only
reads.

## Why the SDK and not a schema file

Genesys adds and changes Architect actions almost weekly and ships the SDK just
as often. Archy's YAML is the SDK's object model written out, so the SDK is the
source the YAML follows, and asking it live answers what a static schema
cannot: which actions *this* org offers in *this* flow type, given its licensed
products.

The mapping is mechanical. The YAML key is the class name without `ArchAction`
or `ArchMenu`, lower-camel-cased — `ArchActionTransferToAcd` becomes
`transferToAcd` — and the property names are the SDK's.

## Reading it

The file is around 400 KB, too much for an agent's context. Query it rather
than reading it whole:

```bash
jq '.flowTypes.inboundcall.actions' reference/architect/catalog.json
jq '.actions.transferToAcd' reference/architect/catalog.json
jq '.flowTypes | map_values(.available)' reference/architect/catalog.json
```

A flow type with `"available": false` has no actions listed. That is the org,
not the catalog: the type is not enabled there. In a typical org that applies to
`voice` (the newer Voice Flow, a bot-style flow type — not the Inbound Call Flow
that handles phone calls), `emailsend` and `businessprocess`. They fill in on
the next refresh once enabled.

The catalog says *what exists*. For the shape of a value, look at a flow that
already uses the action, or build one in Architect and export it. Whether a file
is right is decided by `validate_flow_file`, which runs the real thing on the
server.
