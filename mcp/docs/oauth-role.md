# OAuth client and role

The server authenticates as an OAuth client using the **Client Credentials**
grant, so it acts as the org itself rather than as a person. Everything it can
do is bounded by the role you give that client — that role is the real safety
boundary, more than anything in the code.

## Creating the client

In **Admin → Integrations → OAuth → Add Client**:

- Grant type: **Client Credentials**
- Assign the role described below
- Note the Client ID and Secret

If you already use Archy, its client is usually enough — the server falls back to
`~/.archy_config`, so you may be configured without doing anything.

## Permissions

Grant only what you intend the agent to touch. The list below is what the tools
call; a smaller role simply means some tools return a 403 rather than breaking
anything.

Every permission string below is checked against the published OpenAPI
specification, so what is written here is what the admin search box will find.

`whoami`, `list_divisions` and the token check need no permission at all — those
endpoints declare none.

### Reading configuration

| Permission | For |
| --- | --- |
| `routing:queue:view`, `routing:skill:view`, `routing:wrapupCode:view` | queues, skills, wrap-up codes |
| `routing:schedule:view` | schedules — under `routing`, not `architect` |
| `directory:user:view` | user lookup |
| `authorization:role:view` | roles |
| `integrations:integration:view` | listing integrations |
| `architect:flow:view` | listing flows and their published versions |
| `audits:audit:view` | who published a version, and whether from Architect or Archy |

### Changing configuration

| Permission | For | Note |
| --- | --- | --- |
| `architect:flow:add`, `:edit`, `:publish` | exporting and publishing flows | via Archy |
| `architect:flowInstance:search` | flow execution history | |
| `architect:dependencyTracking:rebuild` | rebuilding the dependency index | |
| `architect:datatable:view`, `:add`, `:edit` | the tables themselves | |
| `architect:datatableRow:view`, `:add`, `:edit` | their rows | a separate family from the table |
| `integrations:action:view`, `:add`, `:edit` | data actions | note the plural `integrations` |
| `scripter:script:view` | agent scripts | |
| `scripter:publishedScript:view`, `:add` | publishing a script | publishing hangs off `publishedScript` |
| `routing:queue:add`, `:edit`, `routing:queueMember:manage` | queues and their members | |
| `routing:skill:assign`, `routing:wrapupCode:edit` | assigning skills, wrap-up codes on queues | |
| `knowledge:knowledgebase:view`, `:add`, `:search` | knowledge bases; `:search` drives `knowledge_health` and `measure_knowledge_search` | |
| `knowledge:category:view` | article categories | |
| `knowledge:document:view`, `:add`, `:edit`, `:upload` | knowledge articles | |
| `knowledge:importJob:add`, `:view` | the import path articles actually arrive through | |
| `knowledge:documentVersion:add` | publishing an edited article | a `PATCH` alone only writes a draft |
| `externalContacts:contact:view`, `:add`, `:delete` | external contacts | delete only for the reset tool |

For flows specifically, Genesys publishes a ready-made **"Architect for Archy"**
role that covers the Architect permissions above.

Several of these endpoints accept an alternative permission — `dialog:bot:view`
for the NLU domain, `relate:contact:view` for contacts, `bridge:actions:view`
for data actions. Either satisfies the call; the names above are the current
ones.

### Auditing a conversation

`audit_conversation` reads a transcript and what Copilot surfaced against it,
which reaches outside the configuration APIs.

| Permission | For |
| --- | --- |
| `analytics:conversationDetail:view` | finding the conversation |
| `conversation:message:view`, `conversation:webmessaging:view` | reading the transcript |
| `responses:response:view` | resolving canned responses named in rules |

### Agent Copilot

The Copilot tools reach across four separate permission families, which is easy
to underestimate: the assistant record, the Copilot configuration on it, the NLU
domain behind the intents, and the summary settings. Missing one of them fails
only part of the lifecycle, so a build can get most of the way and then stop.

| Permission | For |
| --- | --- |
| `assistants:assistant:view`, `:add`, `:edit` | the assistant itself, and the knowledge confidence threshold on it |
| `assistants:copilot:view`, `:edit` | Copilot configuration: rules, NLU binding, summary binding |
| `assistants:agentchecklist:view`, `:add`, `:edit` | agent checklists |
| `assistants:queue:view`, `:edit`, `:delete` | switching Copilot on and off for a queue |
| `assistants:queueUserAssignment:view`, `:add`, `:delete` | per-user assignment on a manual queue |
| `languageUnderstanding:nluDomain:view`, `:edit` | the NLU domain |
| `languageUnderstanding:nluDomainVersion:view`, `:add` | training intents, publishing a version, and `measure_copilot_nlu` |
| `aiStudio:summaryConfig:view`, `:add`, `:edit` | conversation summary settings |
| `analytics:agentCopilotAggregate:view` | what Copilot actually surfaced in a past conversation |

Note the last one: summary settings live under `aiStudio`, not `assistants`. A
role assembled by searching for "assistant" will miss it, and summaries then
fail while everything else works.

### Deleting

Grant these only if you want an agent able to remove things. Leaving them out is
a sound default: `delete_object` then reports a 403 and nothing else changes.

| Permission | Removes |
| --- | --- |
| `externalContacts:contact:delete` | external contacts, and needed by `reset_external_contact` |
| `integrations:action:delete` | data actions |
| `knowledge:knowledgebase:delete` | knowledge bases, with every article in them |
| `architect:datatable:delete` | data tables and their rows |
| `architect:flow:delete` | flows, including every published version |

Agent scripts are missing from that list on purpose: the API offers no delete
endpoint and no permission for it, so no role can grant it.

`externalContacts:contact:delete` is the one most likely to be wanted anyway,
because the contact reset needs it: clearing a contact's identity-stitching
history means deleting and recreating it.

## The role is the boundary

The server asks a caller to name what it is deleting before deleting it, which
guards against a misresolved name. It is not a security control — anything the
client is permitted to do, it can be talked into doing.

If an agent should not be able to remove something, the answer is to withhold
the permission, not to rely on the confirmation step.
