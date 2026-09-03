---
name: copilot-dispatch
description: >
  Dispatch to the correct step of the Agent Copilot demo lifecycle. Use when an author
  wants to create or improve Copilot NLU, rules, and optional checklists, summary settings,
  or knowledge coverage for a Genesys Cloud demo. Detects org context, lists existing
  Copilots, asks what is needed, and routes to design, build, measure or testplan skills. Always start here.
compatibility: genesys-builder
metadata:
  version: 1.0.0
  author: Genesys Cloud Demo Toolkit
license: MIT
---

# Agent Copilot Dispatcher

> **Lifecycle:** **dispatch** → [design](../copilot-design/SKILL.md) → [build](../copilot-build/SKILL.md) → [measure](../copilot-measure/SKILL.md) → [testplan](../copilot-testplan/SKILL.md)

Entry point for every Agent Copilot authoring session. Establish org context, understand what exists, and route to the right skill.

---

## Step 0: Read Local Workspace

Before calling APIs, check for local Copilot work artifacts (paths use `toolkit/` prefix in
the source repo; in the published toolkit, drop that prefix — see toolkit README):

| Path | Meaning |
| --- | --- |
| `.copilot-lifecycle/index.json` | Copilots worked on locally (optional; created by skills) |
| `docs/copilot-live-testplan.md` | Existing live test plan if the author keeps one in their project (not shipped with the toolkit) |
| `nlu-probe/*.json` | NLU corpus / measurement files |

If none exist, the workspace is fresh — proceed to Step 1.

---

## Step 1: Establish Intent

**Do not bulk-fetch every Copilot without a filter** — orgs can have many assistants.

If the author's message already states intent ("improve Claims Copilot NLU", "build checklist for the sales demo"), act on it. Only present the menu when genuinely ambiguous:

> What would you like to do?
>
> 1. **Design** a new or changed Copilot (intents, rules, and any checklist/summary/knowledge the demo needs) — starts with the demo script
> 2. **Build** — apply a completed design to Genesys Cloud
> 3. **Measure** — run the NLU probe stand and training loop
> 4. **Test plan** — generate or update the live demo checklist
> 5. **Browse** existing Copilots in this org

Route based on answer:

| Choice | Next skill |
| --- | --- |
| Design (new or change) | [copilot-design](../copilot-design/SKILL.md) |
| Build (design artifact ready) | [copilot-build](../copilot-build/SKILL.md) |
| Measure / train / probe NLU | [copilot-measure](../copilot-measure/SKILL.md) |
| Live test plan | [copilot-testplan](../copilot-testplan/SKILL.md) |
| Browse | `list_copilots` with name filter, then ask again |

If MCP connection fails: check `GENESYSCLOUD_*` credentials or `~/.archy_config`, then stop.

---

## Step 2: Locate the Copilot (when a specific assistant is involved)

Ask for the Copilot name or industry/language hint if not already given.

```
list_copilots(name="<fragment>")
```

Present: assistant name, language, intent confidence threshold, linked summary setting if visible via `get_copilot`.

For NLU work also note domain id via `get_copilot_nlu(assistant="<name>")`.

---

## Step 3: Classify the Task

| Author wants… | Route to | Prerequisite |
| --- | --- | --- |
| New demo Copilot or major rework | design | Demo script (or co-author one) |
| Tweaks to existing intents/rules/checklists | design (update mode) | What should change |
| Push design to org | build | Completed design artifact |
| Probe script lines / train NLU | measure | Corpus with messpunkte (skip if rules-only, no NLU) |
| Run sheet for live demo | testplan | Demo script; probe results when NLU is in scope |

**Critical gate:** If the author jumps straight to build or measure without a demo script, route to [design](../copilot-design/SKILL.md) first and explain why — without script lines there are no messpunkte, no participant roles, and no grounded checklist or summary prompts.

**Scope gate:** Ask what the demo actually uses — rules only, checklist, summary, knowledge fallback, agent script pages, or a subset. Do not assume every Copilot needs all four.

---

## Step 4: Hand Off

Carry forward:

- `assistant` name(s)
- `mode`: `new` | `update` | `measure-only` | `testplan-only`
- Demo script source (file path, pasted text, or "to be collected")
- Any existing design artifact path (`.copilot-lifecycle/<slug>/design-artifact.json`)

Report briefly what happens next in the target skill before routing.
