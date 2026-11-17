---
name: copilot-measure
description: >
  Measure Agent Copilot NLU against demo script lines and training quality. Runs the
  toolkit NLU probe stand (probe.py, train.py) or measure_copilot_nlu via MCP, enforces
  train/test separation, checks confidence and intent separation thresholds, and guides
  the training loop. Use after build or when tuning NLU before a demo. Skip when the
  Copilot is rules-only with no NLU domain.
compatibility: genesys-builder
metadata:
  version: 1.0.0
  author: Genesys Cloud Demo Toolkit
license: MIT
---

# Agent Copilot Measure

> **Lifecycle:** [dispatch](../copilot-dispatch/SKILL.md) → [design](../copilot-design/SKILL.md) → [build](../copilot-build/SKILL.md) → **measure** → [testplan](../copilot-testplan/SKILL.md)

Objective NLU quality gate before live demos. Script lines are **messpunkte** (targets); training uses **other** phrasings only. Not needed when the Copilot has no NLU intents.

---

## Prerequisites

- Demo script captured in corpus `messpunkte` (from [design](../copilot-design/SKILL.md))
- MCP credentials configured (`mcp/.venv` after setup, or `uv run` from `mcp/`)
- Corpus file under `nlu-probe/` or a project path the author chooses

See [nlu-probe/README.md](../../nlu-probe/README.md) for command reference. Paths below use the `toolkit/` prefix from the source repo; in the published toolkit, drop that prefix.

---

## Two ways to measure

| Method | When |
| --- | --- |
| **`measure_copilot_nlu`** (MCP) | Quick check after one training round; pass `corpus_file` or inline `cases`; `strict=True` for demo script thresholds (0.70 / 0.40) |
| **`probe.py`** | Full corpus baseline, holdout runs, JSON files for the test plan |
| **`measure_knowledge_search`** (MCP) | Same idea for the knowledge base: scores each question against the assistant's knowledge confidence threshold |
| **`knowledge_health`** (MCP) | Before any of the above — checks the knowledge base itself, no test cases needed |
| **`audit_conversation`** (MCP) | After a rehearsal — grades the Copilot against a conversation that really happened |

All of these use the same detect and search endpoints as the live Copilot, and
the same thresholds.

Start with `knowledge_health`. Everything else asks questions somebody had to
write down first, which means a base can measure well on the handful of
questions in the corpus while being unusable on everything else. `knowledge_health`
needs no corpus: it looks each article up by its own title to establish what a
true match scores, sends deliberately off-topic questions to establish what
noise scores, and reports whether the confidence threshold sits between the two.

A threshold under the noise level is the common failure and the hardest to spot
by hand, because nothing looks broken — the agent simply gets a suggestion for
every single thing the customer says, including "good morning". Note that noise
levels differ by language on identical content, so a threshold copied from one
assistant to its translation is a guess, not a setting.

---

## Step 1: Baseline Probe

From the toolkit root (or repo root with `toolkit/` prefix):

```bash
mcp/.venv/bin/python nlu-probe/probe.py \
  --corpus nlu-probe/example-corpus.jsonc \
  --demo "My Assistant"
```

Flags:

| Flag | Effect |
| --- | --- |
| `--demo` | Stricter thresholds: 0.70 confidence, 0.40 gap |
| `--corpus PATH` | Corpus file (JSON or JSONC) |
| `--output-dir PATH` | Where to write `results*.json` |
| trailing names | Filter to listed assistant names |

**Interpret output:**

| Verdict | Meaning |
| --- | --- |
| `OK` / demotauglich | Meets demo thresholds |
| `FAIL` | Below threshold or wrong winner |
| `verwechselt` | Second intent beats target |
| `knapp` | Gap < 0.25 to runner-up |

Record per-intent confusion (which foreign intent appears as runner-up).

Or via MCP:

```
measure_copilot_nlu(assistant="My Assistant", corpus_file="nlu-probe/my-corpus.jsonc", strict=True)
```

---

## Step 2: Training Loop

1. Author new **training** sentences in additions file — paraphrases only, never messpunkt text.
2. Dry-run:

```bash
mcp/.venv/bin/python nlu-probe/train.py \
  --corpus nlu-probe/my-corpus.json \
  --additions nlu-probe/my-additions.json \
  "My Assistant"
```

3. If **Sperre ok** → re-run with `--apply` to publish one NLU version per assistant.
4. Re-run **probe.py --demo** (or `measure_copilot_nlu` with `strict=True`) on full corpus.
5. Measure **all intents** — not only the one you trained.
6. Run **holdout** corpus (sentences never used for train/tune):

```bash
mcp/.venv/bin/python nlu-probe/probe.py \
  --corpus nlu-probe/my-holdout.json \
  --demo "My Assistant"
```

Holdout catches overfitting; in-sample numbers alone are optimistic.

---

## Step 3: Threshold Policy

| Metric | Demo target | Minimum |
| --- | --- | --- |
| Confidence (p_ziel) | ≥ 0.70 | ≥ 0.50 |
| Gap to 2nd intent | ≥ 0.40 | ≥ 0.30 |

Also check:

- Catch-all (`Sonstiges`/`Other` or your label) wins on off-topic `ausserhalb` lines without firing rules
- Agent-only intents do **not** fire on customer-only messpunkte (role check is config, but probe can flag wrong intent on customer text)
- `negativ` section: forbidden intent stays below assistant threshold

---

## Step 4: Grade a Real Rehearsal

A corpus drifts towards the intents it was written to test, because whoever
wrote it already knew them. Once a rehearsal has been run, grade against that
instead:

```
audit_conversation(queue="My Demo Queue")
```

It reads the transcript, replays every utterance through the live NLU domain and
knowledge base, and sets that against what Copilot actually surfaced.

Two halves, at different resolutions. `turns` is per utterance: winning intent,
confidence, margin. `copilot_activity` is conversation-wide only — the analytics
endpoint leaves `messageId` empty, so a suggestion cannot be traced back to the
sentence that caused it. What it does carry is `Suggested` against `Accepted`,
and `acceptance_rate` is the number to watch: suggestions nobody takes are worse
than none at all.

Findings are deliberately few, so a short list means a healthy setup. Act on
`near_miss_intent` first — those are training gaps within reach of one round.

Requires `analytics:agentCopilotAggregate:view` on the OAuth client. Without it
the replay still runs, and the report says so rather than reporting silence as
health. Message conversations only; voice transcripts sit behind a different API.

---

## Step 5: When to Stop

Stop tuning when:

- All messpunkte pass `--demo` (or `strict=True`)
- Holdout stable
- No regressions on sibling intents
- Negative tests pass

Route to [testplan](../copilot-testplan/SKILL.md) to embed measured values in the live run sheet.

---

## Anti-Patterns (refuse)

| Request | Response |
| --- | --- |
| Add script line to training | Explain messpunkt vs training separation; refuse publish |
| Train on grenzfaelle used for tuning | Move to holdout-only after first fix |
| Optimize bot-only use case in Copilot NLU | Route to design — verify Copilot scope first |

The train script **aborts** if additions overlap test corpus — do not bypass.
