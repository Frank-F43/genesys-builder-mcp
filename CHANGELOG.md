# Changelog

What changed per release, and what each one needs from you. `update.sh` prints
the entries that are new to your clone after updating.

Releases are named by date. Each is published as a single commit, which is why
`update.sh` rather than `git pull` brings them in.

## 2026-11-05 (2) — whoami, rate limits, speech engine on export

### What you need to do

Restart Cursor (Cmd+Q) after updating. For the full `whoami` the OAuth role
needs `oauth:client:view` and `authorization:role:view`; without them it still
reports org, region and client, and says why the roles are missing.

### Fixed

- **`export_flow` no longer switches a bot's speech recognition.** The export
  job names the speech-to-text engine of bot flows wrongly - a bot published
  with "Genesys Enhanced v2" came back as "Microsoft Azure", and publishing
  that file would have changed the engine. The engine of each language is now
  taken from the published configuration; the result lists any correction
  under `export_corrected`. If you re-exported a voice bot with the previous
  release, export it again.
- **Rate limits are waited out.** On a 429 the client waits the time Genesys
  announces and retries, up to four times. Exporting a whole repository used
  to fail after about ten flows.
- **`whoami` shows the OAuth client again**, and the permissions: a client
  credentials token carries none, so they now come from the client's roles.

### Changed

- `whoami` leads with the org name and the region, spelled out
  (`EU (Frankfurt) - mypurecloud.de`), lists the client's roles with their
  divisions, and counts permissions per domain. `permission_domain` lists one
  domain in full, e.g. `architect`.

## 2026-11-05 — No more Archy

Flows now travel through Genesys Cloud's Architect job API, which runs Archy on
the server side in its current version. Nothing needs installing any more
besides `uv` — no Archy binary, no Rosetta on Apple Silicon.

### What you need to do

1. **Run the update once by hand**, because your clone predates `update.sh`:

   ```bash
   cd ~/.genesys-builder-mcp && git fetch origin && git reset --hard origin/main
   ./update.sh /path/to/your-project
   ```

   From then on, `./update.sh /path/to/your-project` is the whole update.
2. **Move your credentials into the project.** The server no longer reads
   `~/.archy_config`; the three `GENESYSCLOUD_*` values belong in the `env`
   block of the project's `.cursor/mcp.json`. `update.sh` offers to copy them
   over without showing them.
3. **Add to the OAuth role** — the flow tools answer 403 without them, every
   other tool keeps working:
   `architect:job:create`, `architect:job:view`, `architect:jobExport:create`,
   `architect:jobExport:view`, `architect:flow:unlock`, and
   `architect:flow:delete` for the scratch copies validation publishes.
4. **Restart Cursor completely** (Cmd+Q). *Reload Window* is not enough.
5. Optionally: uninstall Archy, remove `./archy` and `.archy/bin/` from your flow
   repository, delete `~/.archy_config`, and add `.genesys-builder/` to the flow
   repository's `.gitignore`.

### Changed

- **`publish_flow`** uploads through the job API. A file that fails validation
  is rejected before anything changes — the existing flow keeps its version, no
  draft, no lock. A check-out held in Architect is released first, as Archy's
  `--forceUnlock` did, and reported in the result.
- **`validate_flow_file`** publishes a copy under `zz validate <name> <id>` and
  deletes it straight away, because the API validates only by publishing. The
  real flow is never touched. It now counts as a writing tool.
- **`export_flow`** leaves a file alone when it differs from the export only in
  block order and quoting, and says so in `file_rewritten`.
- **Exports are wordier than before.** The server-side exporter lists unbound
  data table columns and orders `referencedObjects` differently. Expect a
  one-off diff per flow on its first re-export; behaviour is unchanged.
- **Credentials** come only from `.cursor/mcp.json`: the project's file, the
  `env` block the editor passes to the server, or Cursor's global file, in that
  order. A shell exporting other values no longer redirects scripts run inside a
  project. `whoami` reports the source under `credentials_from`.
- **Terminal scripts** — `smoke_test.py`, the NLU probe, the flow audit — find
  the credentials in the project's `.cursor/mcp.json` when run from inside the
  project: `uv run --project ~/.genesys-builder-mcp/mcp python …/smoke_test.py`.
- **The flow repository** is any git repository. It no longer needs the
  `./archy` wrapper; the export state moves from `.archy/flow-state.json` to
  `.genesys-builder/flow-state.json` by itself.
- **`setup.sh`** takes the flow repository as an argument, reads the credentials
  from its `.cursor/mcp.json`, and no longer checks for Archy.

### Added

- **`update.sh`** — updates the clone across single-commit releases, keeps the
  previous state as a `backup/before-update-…` branch, refreshes installed
  skills, checks `.cursor/mcp.json` and `.gitignore`, and lists new tools.
- **`architect-catalog/`** — builds a catalog of every Architect action and
  menu, per flow type, as available in your org, from the official Architect
  Scripting SDK. The reference for writing flow YAML; refresh weekly.
- **Web messaging tools** — `list_messenger_configurations`,
  `list_web_deployments`, `copy_messenger_configuration` and
  `set_deployment_configuration`. They need the `webDeployments:*` permissions
  listed in `mcp/docs/oauth-role.md`.
- **`scripts/sync_derived_roles.py`** — maintains two roles computed from the
  org's permission catalog: *delta*, covering what the default roles miss, and
  *agent*, everything except the permissions that would let an automation's
  client grant itself more.

### Removed

- The Archy wrapper, the Archy check in `setup.sh`, the fallback to
  `~/.archy_config`, and the VS Code editor setup for Archy YAML schemas.

## 2026-09-03 — First published release

MCP server for flows, data actions, agent scripts, knowledge, routing, users,
data tables, external contacts and the Agent Copilot lifecycle; five Copilot
skills; the NLU probe; operational scripts; `SETUP.md` and `UPDATE.md`; and the
script that fetches the Genesys Cloud API specification.
