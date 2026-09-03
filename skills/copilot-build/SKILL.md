---
name: copilot-build
description: >
  Build and publish Agent Copilot configuration in Genesys Cloud from a design artifact.
  Creates or updates NLU intents, rule engine rules, and optional checklists and summary
  settings via genesys-builder MCP tools. Use after copilot-design. Validates API pitfalls before publish.
compatibility: genesys-builder
metadata:
  version: 1.0.0
  author: Genesys Cloud Demo Toolkit
license: MIT
---

# Agent Copilot Build

> **Lifecycle:** [dispatch](../copilot-dispatch/SKILL.md) → [design](../copilot-design/SKILL.md) → **build** → [measure](../copilot-measure/SKILL.md) → [testplan](../copilot-testplan/SKILL.md)

Applies a design artifact to Genesys Cloud. Prefer MCP tools over raw API calls; use `genesys_api_call` only for gaps.

---

## Step 1: Read Design Artifact

Path: `.copilot-lifecycle/<slug>/design-artifact.json`

If missing → route to [design](../copilot-design/SKILL.md), stop.

Validate non-empty: `assistant`, and either `intents`/`script_lines`/`corpus_messpunkte` for NLU scope or `rules` for rules-only scope.

**Before any write:** confirm with author if live demos are running — org changes affect production Copilot behavior.

---

## Step 2: Pre-Flight Checks

Read [api-pitfalls.md](references/api-pitfalls.md) — especially:

1. **NLU new version replaces entire intent list** — always read live version with `includeUtterances: true`, merge changes, write back full list.
2. **Never publish training that includes messpunkte text** — run `train.py` dry-run first; collision guard must pass.
3. **Rule page ids** — resolve script page names; stale ids after `replace_agent_script` show empty panels.
4. **Wrap-up codes** — need division + queue attachment for Copilot prediction (see `create_wrapup_code` docstring).

Verify each rule target exists:

```
list_copilot_rules(assistant="<name>")
list_agent_scripts()
get_copilot_checklist(checklist="<checklist>")
```

---

## Step 3: Checklists (skip if not in design artifact)

Create or update via `set_copilot_checklist`:

- POST returns **202** — list before retry
- Item `description` holds the AI prompt (from design artifact)
- Note returned checklist id for rules

---

## Step 4: Summary Settings (skip if not in design artifact)

Create or update via `set_copilot_summary_setting`:

- Trim every `customEntities[].description` to ≤ 120 chars
- Bind setting on assistant through `get_copilot` / PUT copilot config if needed

---

## Step 5: NLU Domain (skip if rules-only, no intents)

Use `get_copilot_nlu(assistant="<name>")` for domain id and live version.

Workflow:

1. GET latest published version with utterances
2. Merge design training utterances (not messpunkte)
3. POST new version → train → poll → publish
4. Confirm `live_version_id` updated

For intent CRUD helpers: `set_copilot_intent`, `delete_copilot_intent` when sufficient; else full version POST.

**Catch-all intent:** add utterances, **no** matching rule.

---

## Step 6: Rule Engine

Use `set_copilot_rule` / inspect with `list_copilot_rules`:

| Field | Source |
| --- | --- |
| `intent` | Design artifact |
| `actionType` | Script / Checklist / CannedResponse |
| `participantRoles` | From design — **do not widen** without author approval |
| `attributes.checklistId` | From step 3 |
| Script page | Name resolution via agent script |

Remove stale `rule.id` values on bulk PUT via `genesys_api_call` if needed.

---

## Step 7: Hand Off

Update artifact `_meta.local_status` → `built`.

If NLU is in scope, route to [measure](../copilot-measure/SKILL.md):

> Configuration published. Run probe on messpunkte before live demo.

Report: assistant, NLU version id (if any), rules changed, checklist/summary ids (if any).
