# Checklist and Summary Prompt Guide

## Checklist items (`agentchecklists`)

| Field | Role |
| --- | --- |
| `name` | Agent-facing label — letters, digits, spaces, `_`, `-`, `'` only |
| `description` | **AI extraction/check prompt** — Copilot reads transcript and marks done when satisfied |
| `automatedCheckEnabled` | Must be `true` for AI checking |
| `exactPhraseMatch` | Usually `false` in demos |
| `important` | Highlights in UI |

Copilot evaluates checklists **on agent turns only** - customer turns never
tick an item, and there is no setting to change that. Phrase every item as
something the agent says.

**Formulation template:**

> Mark complete when the agent [asks / confirms / proposes …], e.g. "…".

A customer fact is covered by the agent saying it back ("So your income stays
the same"), not by waiting for the customer's answer.

## Summary settings (`conversations/summaries/settings`)

| Field | Role |
| --- | --- |
| `label` | Display name in summary UI |
| `description` | Extraction prompt — **max 120 characters** |

**Formulation template:**

> [Yes/No question with context]? Yes/No with quote.

MCP tools: `list_copilot_checklists`, `set_copilot_checklist`, `list_copilot_summary_settings`, `set_copilot_summary_setting`.

Checklist creation returns **202** — always verify via GET before retrying POST.
