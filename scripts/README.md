# API Tools

Small operational scripts that act on a live Genesys Cloud org, using the same
client-credentials configuration as `archy` (`~/.archy_config`) or the
`GENESYSCLOUD_*` environment variables. Region and credentials come from there,
so the scripts run against whichever org you are configured for.

These are day-to-day helpers, not Architect or Scripter build tooling. The first
one is the odd one out: it downloads a reference file and touches no org at all.

## fetch_api_spec.py

Downloads the public API v2 specification into `reference/swagger/`, where an
agent can search it:

```
python3 scripts/fetch_api_spec.py                      # region from your configuration
python3 scripts/fetch_api_spec.py --region mypurecloud.com
```

No credentials needed — `/api/v2/docs/swagger` is public. The region still
matters: it decides the `host` recorded inside the spec, and example URLs get
built from that later.

**One region is an exception.** Measured across the whole region list, every
region serves this endpoint except **`euc2.pure.cloud`**, which answers `403`.
That is not a permission problem and no role fixes it. The contract shapes are
the same everywhere and only the recorded `host` differs, so fetch from another
region — the script says as much when it hits the 403.

**Roughly 22 MB, 2100+ paths, 4900+ definitions — gitignored on purpose.** It is
a reproducible download, so a committed copy would be stale within weeks and
would weigh down every clone. Fetch it when you want it.

**Do not read it whole.** It does not fit in a context window, and an agent that
tries loses several minutes discovering that. Search it for the path or
definition you need.

**You will not need it most of the time.** The MCP tools already carry the
contracts that matter, and their descriptions say more about local quirks than
the spec does. Two situations make it worth having:

- Building a **Data Action**, where the request and response shapes have to be
  exact.
- Reaching an endpoint through `genesys_api_call` that no tool covers.

And one warning, spelled out under `replace_script.py` below: **absence from the
spec does not prove absence of an endpoint.** Some routes live on the app domain
rather than the api domain and appear nowhere in it.

## reset_external_contact.py

Worth binding to a short standing phrase in your own rules, so the agent runs it
without being asked twice — "reset `<email>`" or whatever you prefer:

```
python3 scripts/reset_external_contact.py <email>
```

**What it does and why:** Genesys Cloud External Contacts accumulate a
`mergeSet` entry every time a new visitor identity (typically a browser
cookie) gets stitched onto the same contact via matching email/phone.
Repeated demo runs against the same test contact inflate that list fast —
a demo is usually rehearsed against a handful of fixed identities — and the
platform limits how many identities can be stitched to one contact.
Once hit, identity stitching starts failing with an error.

The fix: read the contact's full data (core fields + custom schema fields
via its `schema` reference + all `identifiers`), delete it, and recreate it
from scratch with the same core field values. This resets the merge/
identity-stitching count to zero while the contact — same name, same
email/phone, same custom fields — keeps working exactly as before for
anything that looks it up by email or phone. The contact gets a new `id`, so
check first that nothing in your flows or scripts references External Contact
IDs directly rather than looking them up by email or phone at runtime.

**Requires:** the OAuth client's assigned role needs
`externalContacts:contact` `[view, delete, add]`. The `Architect for Archy`
role carries this already, so a client set up for Archy needs no extra
grant. (An earlier `403` during testing
was just an expired cached token, not a missing permission — always pull a
fresh token before concluding a permission is actually missing.)

**Excluded from the rebuild** (system-generated or merge-history, would
defeat the purpose of resetting): `id`, `division`, `modifyDate`,
`createDate`, `type`, `canonicalContact`, `mergeSet`, `mergedFrom`,
`mergedTo`, `mergeOperation`, `selfUri`, `identifiers`.

## replace_script.py

**Automates the Scripter UI's Import → Replace, end to end — no manual UI
step needed anymore.** Run:

```
python3 scripts/replace_script.py <path-to.script> <scriptId> [scriptName]
```

Example: `python3 scripts/replace_script.py "scripts/Master Agent Script.script" 00000000-0000-4000-8000-000000000001`

**Do not conclude this is impossible from the Swagger spec.** The upload
endpoint is undocumented: it is absent from the public `/api/v2` spec and
therefore also from the Genesys Cloud CLI, which is generated from it.
Searching the spec alone leads to the wrong answer that a manual UI import
is the only route.

The path used here was verified end-to-end against a disposable script
before being trusted — create it, replace its content via
`scriptIdToReplace`, confirm the change in a fresh export, publish it,
confirm `publishedDate` was set, delete it. Worth repeating that check if
Genesys changes the endpoint, since no official contract covers it.

**The three-step flow** (all three needed — the first two alone leave
agents seeing the old published version):
1. `POST https://apps.<region>/uploads/v2/scripter` (note: **app domain**,
   not `api.<region>` — this is why it's not in the public API/CLI, which
   only ever talk to the api domain) — `multipart/form-data` with fields
   `file` (the `.script` JSON), `scriptName`, and `scriptIdToReplace` (the
   id of the existing script to overwrite in place — omit it and you get a
   new script instead of a replace). Returns `{"correlationId": "..."}` —
   confirmed empirically: that correlationId **is** the uploadId for the
   next step, despite the name.
2. `GET /api/v2/scripts/uploads/{uploadId}/status?longPoll=false` — poll
   until `succeeded` appears. **Gotcha found while testing:** the very
   first poll right after the upload can 404 (the upload record isn't
   queryable yet) — not a real failure, the tool retries through it.
3. `POST /api/v2/scripts/published` with `{"scriptId": "..."}` — actually
   publishes the just-replaced version; without this step the replace
   succeeds but agents keep seeing the previously published version.

Uses the same `~/.archy_config` client-credentials as every other tool
here — no extra OAuth scope or role needed beyond what `Archy` already has
(confirmed by the successful test).

## create_data_action.py

Creates (or republishes) a Genesys Cloud Data Action from a local folder of
files — the automated equivalent of building an action by hand in the
Integrations UI. Run:

```
python3 scripts/create_data_action.py <folder> <name> <category> <integrationId> <requestUrlTemplate> <requestType>
```

`<folder>` must contain `inputschema.json`, `successschema.json`,
`requesttemplate.vm`, `successtemplate.vm`. For a `GET` action the request
body is just `${input.rawRequest}`, the standard empty-body Velocity
template. Optionally drop a
`translationmap.json` and/or `translationmapdefaults.json` in the folder to
control the response `translationMap`/`translationMapDefaults` (JSONPath →
output-field extraction, e.g. `"KEY_VALUE_1": "$.customAttributes.item_id"`
for a nested field); without one, it defaults to the single-field
`{"RECORD_ID": "$.id"}` map used by the write-style actions.

Does the full round trip: create draft (`POST
/api/v2/integrations/actions/drafts`), validate (`GET
.../draft/validation`), publish (`POST .../draft/publish` — **note:** needs
`{"version": <draft version>}` in the body, not an empty POST, or it 400s
with an unhelpful `"must not be null"`), then writes the published
definition back into the folder as `action.json`.

Only wired up for the built-in `purecloud-data-actions` integration type
(the one that calls the org's own Public API using the org's own
credentials — no external endpoint/auth config needed, matches this
your org's existing "Demo Genesys Cloud Data Actions" integration,
`00000000-0000-4000-8000-000000000002`). A Data Action calling an external
service would need more config (auth, base URL) not covered here.
