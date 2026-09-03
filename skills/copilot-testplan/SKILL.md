---
name: copilot-testplan
description: >
  Generate a live Agent Copilot demo test plan from the demo script and NLU probe results.
  Produces line-by-line run sheets with expected reactions, measured confidence values when
  NLU applies, negative tests, and optional checklist and summary expectations. Use before
  customer-facing demos.
compatibility: genesys-builder
metadata:
  version: 1.0.0
  author: Genesys Cloud Demo Toolkit
license: MIT
---

# Agent Copilot Live Test Plan

> **Lifecycle:** [dispatch](../copilot-dispatch/SKILL.md) → … → [measure](../copilot-measure/SKILL.md) → **testplan**

Produces a document agents read **during** the demo — second screen or printout. Write it in the demo language unless the author asks otherwise.

---

## Prerequisites

1. Demo script with speaker per line ([design](../copilot-design/SKILL.md))
2. Probe results with `--demo` or `measure_copilot_nlu(..., strict=True)` when NLU is in scope
3. Copilot names; checklist and summary names only if the demo uses them (`get_copilot`, `list_copilot_checklists`)

---

## Step 1: Gather Inputs

| Source | Data |
| --- | --- |
| Design artifact / script | Ordered lines, speaker, exact wording |
| `probe.py` output JSON | `p_ziel`, `abstand` per messpunkt (when NLU applies) |
| Live config | Threshold, checklist id, summary fields (when used) |

---

## Step 2: Document Structure

Follow [live-testplan-template.md](references/live-testplan-template.md). One section per Copilot assistant.

Required sections:

1. **Header** — assistant name, NLU threshold (if NLU), checklist + summary names (if used)
2. **Watch points** — demo-specific items (double welcome CR, checklist timing at a validation step, etc.)
3. **Flow table** — columns: Nr, Speaker, Wording, Expected reaction, Conf., Gap, ☐, Note
4. **Lines without expected Copilot reaction** — narrative lines, checklist-AI-only, summary-only
5. **Must NOT happen** — negative tests (wrong page, off-topic intent, agent-only intent on customer speech)
6. **Checklist** — when each item must be AI-checked (omit section if no checklist)
7. **Summary** — predefined + custom entity expectations (omit section if no summary)

**Reaction shorthand** (legend):

| Code | Meaning |
| --- | --- |
| CR | Canned response in Copilot panel |
| Script | Agent script page switch |
| Checklist | Checklist opens/stays visible |
| Checklist ✓ | AI should mark item (after agent+customer exchange) |
| Summary | Verify after wrap-up |
| — | No Copilot rule expected |

---

## Step 3: Fill Measured Values

From probe JSON for each messpunkt line:

```json
"p_ziel": 0.78,
"abstand": 0.73
```

Round to two decimals. Note: live UI does not show scores — values document **reserve**, not a live readout.

If a line fails probe thresholds, flag in Note column and link to [measure](../copilot-measure/SKILL.md) before demo.

Skip Conf./Gap columns for rules-only lines or knowledge fallback rows.

---

## Step 4: Negative and Holdout Tests

Include at least:

- Off-topic customer sentence → catch-all intent, **no** script page
- Removed/wrong use case (e.g. a retired script page must not open)
- Agent-only rule must not fire on customer messpunkt (manual live check)

Optional: reference holdout probe file and date in header footer.

---

## Step 5: Write Output

Default path: `docs/copilot-live-testplan.md` in the author's project, or any path they choose (not shipped with the toolkit).

Add footer:

> *Measurement source:* `nlu-probe/probe.py --demo` or `measure_copilot_nlu(..., strict=True)` · *As of:* \<date\>

Update `_meta.local_status` → `testplan-complete` in design artifact if present.

---

## Example Reference

A complete live test plan lists messpunkte per intent, threshold values from probe output when NLU applies, and role-specific checks (Agent vs Customer). Use your org's assistant names and measured values — do not copy IDs from another org.
