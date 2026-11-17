---
name: copilot-build
description: >
  Build and publish Agent Copilot configuration in Genesys Cloud from a design artifact.
  Creates the copilot if needed, then knowledge, checklists, summary settings, wrap-up codes,
  NLU intents and rules via genesys-builder MCP tools. Use after copilot-design. Validates API
  pitfalls before publish.
compatibility: genesys-builder
metadata:
  version: 1.1.0
  author: Genesys Cloud Demo Toolkit
license: MIT
---

# Agent Copilot Build

> **Lifecycle:** [dispatch](../copilot-dispatch/SKILL.md) → [design](../copilot-design/SKILL.md) → **build** → [measure](../copilot-measure/SKILL.md) → [testplan](../copilot-testplan/SKILL.md)

Applies a design artifact to Genesys Cloud. Every step has an MCP tool; `genesys_api_call`
is not needed for a standard copilot. If a step seems to need it, the tool is missing —
say so rather than working around it.

Build in this order: each step creates what a later one points at.

---

## Step 1: Read Design Artifact

Path: `.copilot-lifecycle/<slug>/design-artifact.json`, or wherever the project keeps it.

If missing → route to [design](../copilot-design/SKILL.md), stop.

Validate non-empty: `assistant`, and either `intents`/`script_lines`/`corpus_messpunkte` for NLU scope or `rules` for rules-only scope.

**Before any write:** confirm with author if live demos are running — org changes affect production Copilot behavior.

Read [api-pitfalls.md](references/api-pitfalls.md).

---

## Step 2: Knowledge Base (skip if the demo uses no knowledge)

```
create_knowledge_base(name="<kb>", core_language="en-US")
import_knowledge_articles(knowledge_base="<kb>", articles=[...])
knowledge_health(knowledge_base="<kb>")
```

Articles go in through the import job only — a raw document POST never indexes.
Numbers in articles must match the agent script the demo shows.

---

## Step 3: Checklist (skip if not in design artifact)

`set_copilot_checklist(checklist="<name>", items=[...], language="en-us")`

- Creation returns **202** — `get_copilot_checklist` before retrying
- Item `description` is the AI prompt from the design artifact; no `&` in names

---

## Step 4: Summary Setting (skip if not in design artifact)

`set_copilot_summary_setting(setting="<name>", language="en-us", custom_entities=[...], participant_labels={...})`

Passing `language` creates it when no setting has exactly that name. Entity
descriptions over 120 characters are refused.

---

## Step 5: Assistant

If `get_copilot(assistant="<name>")` finds nothing:

```
create_copilot(name="<name>", language="en-US", template="<similar copilot>",
               knowledge_bases=["<kb>"], summary_setting="<setting>")
```

`template` copies settings only, never rules, intents, knowledge or summary. The NLU
domain is new and empty. For an existing copilot, change bindings with
`set_copilot_settings`.

---

## Step 6: Wrap-up Codes (skip if not in design artifact)

```
create_wrapup_code(name, description, division="<division>", queues=["<queue>"])
set_copilot_settings(assistant="<name>", wrapup_prediction=True)
```

Existing codes: `attach_wrapup_codes(queue, wrapup_codes=[...])`.

---

## Step 7: NLU Intents (skip if rules-only)

`set_copilot_intent(assistant, intent, utterances, description)` once per intent,
catch-all included. The first call on a new domain trains its first version; each
later call carries every other intent forward.

- Training utterances only — **never** a messpunkt (script line)
- For bulk changes on an existing domain, `nlu-probe/train.py` dry-run first; its collision guard must pass

**Catch-all intent:** utterances, **no** rule.

---

## Step 8: Rules

`set_copilot_rule(assistant, intent, ..., participant_roles=[...])`

| Design artifact | Tool argument |
| --- | --- |
| One action | `action` plus `script`/`page`, `checklist`, `response_id` or `knowledge_base`/`article` |
| Several actions on one intent | `actions=[{"action": "Script", ...}, {"action": "Checklist", ...}]` |
| `participantRoles` | `participant_roles` — **do not widen** without author approval |

Knowledge fallback comes from `create_copilot` when a knowledge base is bound.
Check wiring with `list_copilot_rules`.

---

## Step 9: Go Live and Hand Off

```
assign_copilot_queue(assistant="<name>", queue="<queue>")
get_copilot(assistant="<name>")      # readiness.gaps must be empty
```

Update artifact `_meta.local_status` → `built`.

If NLU is in scope, route to [measure](../copilot-measure/SKILL.md):

> Configuration published. Run probe on messpunkte before live demo.

Report: assistant, NLU version id (if any), rules changed, checklist/summary/knowledge ids, wrap-up codes (if any).
