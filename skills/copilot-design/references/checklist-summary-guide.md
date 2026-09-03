# Checklist and Summary Prompt Guide

## Checklist items (`agentchecklists`)

| Field | Role |
| --- | --- |
| `name` | Agent-facing label — letters, digits, spaces, `_`, `-`, `'` only |
| `description` | **AI extraction/check prompt** — Copilot reads transcript and marks done when satisfied |
| `automatedCheckEnabled` | Must be `true` for AI checking |
| `exactPhraseMatch` | Usually `false` in demos |
| `important` | Highlights in UI |

**Formulation template:**

> Mark complete when [agent action with example question] and [customer responds as …], e.g. "…".

Include **who speaks first** for agent-triggered steps (a "Verify Identity" checklist opens on the agent line, not the customer line).

## Summary settings (`conversations/summaries/settings`)

| Field | Role |
| --- | --- |
| `label` | Display name in summary UI |
| `description` | Extraction prompt — **max 120 characters** |

**Formulation template:**

> [Yes/No question with context]? Yes/No with quote.

MCP tools: `list_copilot_checklists`, `set_copilot_checklist`, `list_copilot_summary_settings`, `set_copilot_summary_setting`.

Checklist creation returns **202** — always verify via GET before retrying POST.
