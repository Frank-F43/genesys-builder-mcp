# NLU Probe Stand

Read-only measurement and controlled training for **Agent Copilot** NLU domains. Uses the same OAuth credentials as `toolkit/mcp` — no dependency on `.scratch/`.

Pair with the Copilot skills under `toolkit/skills/copilot-*` — especially [copilot-design](../skills/copilot-design/SKILL.md) (script-first corpus design) and [copilot-measure](../skills/copilot-measure/SKILL.md) (training loop).

## Setup

```bash
cd toolkit/mcp && ./setup.sh
```

Credentials: `GENESYSCLOUD_*` or `~/.archy_config`.

## Corpus format

Top-level keys = **Copilot assistant names** (as returned by `list_copilots`).

| Section | Purpose |
| --- | --- |
| `messpunkte` | Demo script lines — **measure only**, never train |
| `intents` | Paraphrases for training quality checks |
| `grenzfaelle` | Ambiguous sentences — check separation after each train round |
| `ausserhalb` | Off-topic — should hit catch-all intent (`Sonstiges`/`Other`) |
| `negativ` | `{text, soll_nicht}` — forbidden intent must stay below threshold |

See `example-corpus.jsonc` (comments explain each section). The bundled examples are **industry-neutral English** templates — generic intents (appointment, status, complaint, identity check, catch-all) so colleagues in any sector can copy the structure and swap in their own demo script. They are not tied to a specific org or vertical.

**Train/test separation:** `train.py` refuses to publish if any addition sentence normalizes to the same string as anything in the test corpus (messpunkte, intents test lists, grenzfaelle, ausserhalb, negativ). This blocks measuring rote memorization.

## Probe (read-only)

```bash
# From repository root
toolkit/mcp/.venv/bin/python toolkit/nlu-probe/probe.py \
  --corpus toolkit/nlu-probe/example-corpus.jsonc \
  --demo "Support EN"
```

| Flag | Meaning |
| --- | --- |
| `--demo` | Stricter pass criteria: confidence ≥ 0.70, gap ≥ 0.40 |
| `--output-dir DIR` | Write `results.json` / `results_demo.json` / `results_holdout.json` |
| `--output FILE` | Explicit output path |

Default thresholds without `--demo`: 0.50 / 0.30.

## Train (writes NLU — use with care)

Dry-run first (default):

```bash
toolkit/mcp/.venv/bin/python toolkit/nlu-probe/train.py \
  --corpus my-corpus.json \
  --additions my-additions.json \
  "Support EN"
```

Publish one trained version per assistant:

```bash
toolkit/mcp/.venv/bin/python toolkit/nlu-probe/train.py \
  --corpus my-corpus.json \
  --additions my-additions.json \
  --apply "Support EN"
```

Additions format per intent:

- Simple: `"Intent Name": ["sentence one", "sentence two"]`
- New intent: `"Intent Name": {"description": "…", "utterances": ["…"]}`

Optional `--test-corpus` merges an extra file (e.g. holdout) into the collision check.

## Recommended workflow

1. Capture demo script → `messpunkte` with `role` per line ([copilot-design](../skills/copilot-design/SKILL.md))
2. `probe.py --demo` — baseline
3. Add paraphrases in additions file — **not** script lines
4. `train.py` dry-run → `--apply` → `probe.py --demo` again
5. Holdout file never used in additions → probe separately
6. [copilot-testplan](../skills/copilot-testplan/SKILL.md) — embed scores in live checklist

## Output files

| File | When |
| --- | --- |
| `results_demo.json` | `--demo` or corpus name contains `demo` |
| `results_holdout.json` | corpus name contains `holdout` |
| `results.json` | otherwise |

Results include per-line confidence, gap to second intent, and `demotauglich` flag.
