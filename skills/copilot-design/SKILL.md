---
name: copilot-design
description: >
  Design Agent Copilot configuration from a demo script. ALWAYS asks for the demo script
  or demo story first, then derives intents, participant roles, rules, and optional
  checklist items, summary fields, and knowledge coverage. Use before building or training NLU. Works in new and update mode.
compatibility: genesys-builder
metadata:
  version: 1.0.0
  author: Genesys Cloud Demo Toolkit
license: MIT
---

# Agent Copilot Designer

> **Lifecycle:** [dispatch](../copilot-dispatch/SKILL.md) → **design** → [build](../copilot-build/SKILL.md) → [measure](../copilot-measure/SKILL.md) → [testplan](../copilot-testplan/SKILL.md)

Collect and validate everything needed before touching Genesys Cloud. **The demo script is the source of truth** — not existing NLU names, not bot flows, not agent script page titles alone.

## Interaction Protocol

- **Demo script first.** Before proposing intents, ask whether the author has a line-by-line demo script (speaker + exact wording). If none exists, offer to draft one together from the demo story — but do not invent intents until script lines exist.
- **One block per turn** when collecting script lines (e.g. confirm opening, then validation block) — do not dump a full Copilot design in one shot unless the author pasted a complete script.
- **Never fabricate** script lines. Suggest examples labelled as suggestions; only treat lines the author confirmed as messpunkte.
- **Harvest what's given** — if the author pasted a script, acknowledge it and proceed to analysis instead of re-asking.
- **Confirm scope early** — ask which Copilot features the demo uses: NLU rules, agent script pages, checklist, conversation summary, knowledge fallback. A minimal Copilot may be rules-only.

---

## Step 1: Obtain the Demo Script

Required columns per line:

| Field | Purpose |
| --- | --- |
| **Nr** | Order in the demo |
| **Speaker** | `Agent` or `Customer` |
| **Wortlaut** | Exact or near-exact wording used in the demo (column name is German from the probe format; content follows the demo language) |

If the author only has a narrative ("customer asks for claim status, agent verifies identity"), convert it to numbered lines **with the author** — do not finalize alone.

**Output:** `script_lines[]` saved in the design artifact.

---

## Step 2: Map Script Lines to Copilot Use Cases

For each line where the Copilot should react (script page, checklist, canned response):

1. **Name the intent** — short, matches agent script page names where the demo uses script pages (e.g. `Appointment Status`, `Verify Identity`).
2. **Set participant role** — this is not optional:
   - Customer concern → `participantRoles: ["Customer"]` on the rule (or both roles only when customer speech must trigger).
   - Agent phrasing → `participantRoles: ["Agent"]` only.
   - **Why:** A rule open to both roles can fire when the agent mentions the topic in wrap-up — e.g. in a telco demo, a product name fired twice because the agent repeated it in closing summary while the rule listened to both Agent and Customer.
3. **Verify the use case exists for Copilot** — not only in a digital bot. Self-service flows (online form, IVR bot) are often bot-only; agent-assisted validation during the live demo is Copilot scope. Building rules to script pages that never appear in the agent demo wastes time.

Lines with **no** expected Copilot reaction → mark as `—` in the test plan (checklist AI, summary only, or nothing).

Add a catch-all intent (`Sonstiges` / `Other` or equivalent in the demo language) for off-topic customer speech — **no rule** on it. Train it; do not rely on NLU `None` (not trainable). Without it, broad intents steal matches (e.g. "address" at 0.45 on "I want to cancel my policy").

A knowledge question in the script must **not** win an intent: the fallback only
searches when no rule matched. Check its score stays below the threshold.

---

## Step 3: Derive NLU Intents and Training Policy

| Concept | Rule |
| --- | --- |
| **Messpunkt** | Exact script line — used only in `messpunkte` section of corpus, never as training utterance |
| **Training utterances** | Different phrasings of the **same concern** — paraphrases, synonyms, shorter/longer variants |
| **Holdout** | Fresh sentences neither trained nor used for tuning — separate corpus section or file |
| **Grenzfall** | Ambiguous between two intents — measure separation after each training round |

Document target metrics:

- **Demo script lines:** confidence ≥ **0.70**, gap to second intent ≥ **0.40**
- **Minimum acceptable:** 0.50 / 0.30

After every training round, measure **all intents against each other** — sharpening one intent can dilute another.

---

## Step 4: Design Rule Engine Actions

Per intent that needs Copilot UI action:

| Action | When |
| --- | --- |
| `Script` | Open agent script page — verify page name resolves via `list_agent_scripts` / live script |
| `Checklist` | Open checklist — link checklist id from `list_copilot_checklists` |
| `CannedResponse` | ConversationStart welcome |
| `KnowledgeArticle` | Surface one specific article for a known intent |
| *(fallback)* | Free customer questions — see Step 4b |
| *(none)* | Catch-all intent only |

One rule per intent. If a line should open a script page *and* a checklist, that is one rule with both actions.

---

## Step 4b: Knowledge Coverage (if the demo uses it)

Copilot can combine **knowledge search**, **checklist**, and **script page** actions — ask the author which apply. A line can need none, one, or several.

Knowledge is easy to forget at design time because no intent rule points at it. Free customer questions ("What does my policy cover?", "How long does processing take?") are answered by the **fallback rule**, not by an intent rule:

- Fallback action `KnowledgeSearch`, `participantRoles: ["Customer"]` — agent speech must not trigger searches.
- The assistant's `auto_search` setting decides between raw article hits and generated answers; confirm with the author which mode the demo expects.

Confirm with the author, per knowledge line in the script:

1. **Which knowledge base** backs this Copilot, and is it bound to the assistant?
2. **Does an article actually cover the question?** A fallback that finds nothing looks broken on stage. Check with `list_knowledge_articles` before the demo, not during.
3. **Is content in the demo language?** A question in French against an English-only base returns nothing useful.

Knowledge questions are **not** NLU messpunkte — they never reach a threshold and must be marked `—` in the test plan, with the expected article named instead.

---

## Step 5: Checklist Items (if the demo uses checklists)

Each item:

- `name` — display label (no `&`; spell out "Terms and Conditions")
- `description` — **AI check prompt**, not a use-case title

Copilot evaluates checklists **on agent turns only**. What the customer says
never ticks an item, so a prompt that waits for the customer's answer stays
open for the whole call. Every item must be met by something the agent says:
asking, confirming, summarising or proposing.

**Good prompt pattern:** *Mark complete when the agent [asks / confirms / proposes …], e.g. '…'.*
For a customer fact, let the agent say it back: *"So your income stays the same"*.

**Bad:** *The customer agreed to the terms.* (customer turn, never evaluated)
**Bad:** *… when the agent asks for the date of birth and the customer answers.* (waits for a customer turn)

The same holds for the demo script: each checklist item needs an **agent** line
that meets it, and the checklist only opens with its rule - an agent line
before that moment does not count.

Set `automatedCheckEnabled: true`, `exactPhraseMatch: false` for demos.

See [checklist-summary-guide.md](references/checklist-summary-guide.md).

---

## Step 6: Summary Settings (if the demo uses conversation summary)

Bound to the assistant by `create_copilot(summary_setting=...)` or `set_copilot_settings`.

- `predefinedInsights`: ReasonForCall, Resolution, ActionItems
- `customEntities[]`: `label` + `description` (max **120 characters** — API returns 400 if longer)

Predefined insights carry **no prompt**. Their wording comes from the model and
cannot be steered, so anything the demo must state precisely belongs in a custom
entity, not in ActionItems.

A description is an instruction, and it has to pin the answer format down. "Yes/No
with quote" permits both a bare `yes` and a bare quote, and the model will produce
each of them in different runs.

**Good:** *Did the customer agree to the terms after being asked? Answer exactly Yes - "&lt;quote&gt;" or No, never the quote alone.*

**Bad:** *Did the customer agree to the terms? Yes/No with quote.* (two valid shapes)

**Bad:** *The customer agreed.* (statement, no format)

Name examples in a description only when you want them in the output — the model
treats them as candidates, so listing "appointment link" invites an appointment
link into the summary whether or not one was promised.

---

## Step 6b: Wrap-up Codes (if the demo shows wrap-up prediction)

Copilot suggests a code by matching the conversation against code **name and
description**, and only among the codes on the conversation's queue.

- Each description says **when to use** (what was agreed) and **when not** (near misses: interest without commitment, only informed, declined).
- Draw the boundaries so the demo's ending has exactly one winning code. Two codes that both fit — "offer requested" and "meeting booked" when the meeting is about the offer — need an explicit tie-break in one of the descriptions.
- Prefix codes per demo (e.g. `Claims - …`); the org shares one code list.

---

## Step 7: Save Design Artifact

Write `.copilot-lifecycle/<slug>/design-artifact.json`:

```json
{
  "_meta": { "slug": "claims-en", "assistant": "Claims EN", "local_status": "design-complete" },
  "script_lines": [],
  "intents": [],
  "rules": [],
  "checklist": { "name": "", "items": [] },
  "knowledge": { "base_name": "", "auto_search": "", "expected_questions": [] },
  "summary": { "setting_name": "", "custom_entities": [] },
  "wrapup_codes": [{ "name": "", "description": "" }],
  "corpus_messpunkte": {},
  "training_utterances": {},
  "catch_all_intent": "Other"
}
```

Show diff summary; confirm before handoff to [build](../copilot-build/SKILL.md) or [measure](../copilot-measure/SKILL.md).

---

## Additional Resources

- [checklist-summary-guide.md](references/checklist-summary-guide.md) — prompt patterns and API fields
- [api-pitfalls.md](../copilot-build/references/api-pitfalls.md) — NLU version replace, wrap-up codes
