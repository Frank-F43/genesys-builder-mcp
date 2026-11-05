# Live Test Plan Template

Use this skeleton per Copilot assistant. Replace placeholders. Write in the demo language.

---

# {Assistant Name}

**Assistant:** {name} · NLU threshold: **{threshold}** · Checklist: **{checklist name or —}** · Summary: **{summary setting name or —}**

## Watch points

Document demo-specific watch items (double welcome, checklist timing, etc.).

## Flow — lines with expected Copilot reaction

| Nr | Speaker | Wording (exact) | Expected reaction | Conf. | Gap | ☐ | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | **Agent** | *(conversation start)* | **CR** "Welcome …" | — | — | ☐ | |
| … | **Customer** | … | **Intent** … → **Script** … | 0.00 | +0.00 | ☐ | |

## Lines without expected Copilot reaction

| Speaker | Wording | Note |
| --- | --- | --- |
| Agent | … | Not a trained messpunkt |

## Must NOT happen

| ☐ | Forbidden behavior |
| --- | --- |
| ☐ | Wrong script page opens |
| ☐ | Off-topic sentence triggers a use-case rule |
| ☐ | Agent-only intent fires on customer line |

## Checklist — when items must be checked (omit if no checklist)

| Item | Latest after step | ☐ |
| --- | --- | --- |
| … | Nr … | ☐ |

## Summary — expected fields (omit if no summary)

| Field | Expected content | ☐ | Actual / note |
| --- | --- | --- | --- |
| Reason for contact | … | ☐ | |
| **Custom entity** | **Yes/No** (with quote) | ☐ | |

---

*Measurement source:* `nlu-probe/probe.py --demo` · *Config as of:* {date}
