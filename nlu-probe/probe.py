#!/usr/bin/env python3
"""Probe Copilot NLU against demo script lines and paraphrase test sets.

Measures intent confidence and separation (gap to second-best intent) without changing
the org. Requires genesys-builder MCP credentials (same as toolkit/mcp).

Example:
  toolkit/mcp/.venv/bin/python toolkit/nlu-probe/probe.py \\
    --corpus toolkit/nlu-probe/example-corpus.jsonc \\
    --demo "My Copilot DE"

See README.md in this directory.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
MCP_SRC = REPO_ROOT / "toolkit" / "mcp" / "src"

DEFAULT_MIN_CONF = 0.50
DEFAULT_MIN_ABSTAND = 0.30
DEMO_MIN_CONF = 0.70
DEMO_MIN_ABSTAND = 0.40
KNAPP_ABSTAND = 0.25


def _bootstrap_mcp() -> tuple[object, dict]:
    if not MCP_SRC.is_dir():
        raise SystemExit(f"MCP source not found at {MCP_SRC}")
    sys.path.insert(0, str(MCP_SRC))
    from genesys_builder_mcp import server  # noqa: E402
    from genesys_builder_mcp.client import get_client  # noqa: E402

    tools = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    for required in ("get_copilot_nlu", "get_copilot"):
        if required not in tools:
            raise SystemExit(f"MCP tool {required!r} not registered.")
    return get_client(), tools


def _load_corpus(path: pathlib.Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".jsonc", ".json5"):
        text = re.sub(r"//.*?$|/\*.*?\*/", "", text, flags=re.MULTILINE | re.DOTALL)
    return json.loads(text)


def detect(client, domain: str, version: str, text: str) -> dict[str, float]:
    r = client.request(
        "POST",
        f"/api/v2/languageunderstanding/domains/{domain}/versions/{version}/detect",
        json_body={"input": {"text": text}},
    )
    return {
        i["name"]: round(i.get("probability", 0.0), 3)
        for i in (r.get("output") or {}).get("intents") or []
    }


def bewerte(probs: dict[str, float], ziel: str, schwelle: float, demo_mode: bool) -> dict:
    echt = {k: v for k, v in probs.items() if k != "None"}
    p_ziel = echt.get(ziel, 0.0)
    andere = {k: v for k, v in echt.items() if k != ziel}
    zweiter, p_zweiter = max(andere.items(), key=lambda kv: kv[1], default=("-", 0.0))
    p_none = probs.get("None", 0.0)
    abstand = round(p_ziel - p_zweiter, 3)

    if p_zweiter > p_ziel:
        urteil = "verwechselt"
    elif p_ziel < schwelle:
        urteil = "nicht erkannt"
    elif abstand < KNAPP_ABSTAND:
        urteil = "knapp"
    else:
        urteil = "sicher"

    min_conf = DEMO_MIN_CONF if demo_mode else DEFAULT_MIN_CONF
    min_abst = DEMO_MIN_ABSTAND if demo_mode else DEFAULT_MIN_ABSTAND

    return {
        "p_ziel": p_ziel,
        "zweiter": zweiter,
        "p_zweiter": p_zweiter,
        "abstand": abstand,
        "p_none": p_none,
        "urteil": urteil,
        "demotauglich": p_ziel >= min_conf and abstand >= min_abst and p_ziel > p_none,
    }


def probe_assistant(
    client,
    tools: dict,
    assistent: str,
    gruppen: dict,
    demo_mode: bool,
) -> dict:
    nlu = tools["get_copilot_nlu"].fn(assistant=assistent)
    domain, version = nlu["domain_id"], nlu["live_version_id"]
    schwelle = tools["get_copilot"].fn(assistant=assistent)["intent_confidence_threshold"]

    print(f"\n{'=' * 78}\n{assistent}   Schwelle {schwelle}\n{'=' * 78}")
    ergebnis: dict = {
        "schwelle": schwelle,
        "domain_id": domain,
        "live_version_id": version,
        "messpunkte": [],
        "intents": {},
        "grenzfaelle": [],
        "ausserhalb": [],
        "negativ": [],
    }

    if "messpunkte" in gruppen:
        print("\n  Demo-Skriptzeilen (Messpunkte):")
        for ziel, eintraege in gruppen["messpunkte"].items():
            for entry in eintraege:
                text = entry["text"] if isinstance(entry, dict) else entry
                role = entry.get("role") if isinstance(entry, dict) else "?"
                probs = detect(client, domain, version, text)
                zeile = {
                    "role": role,
                    "text": text,
                    "ziel": ziel,
                    **bewerte(probs, ziel, schwelle, demo_mode),
                }
                ergebnis["messpunkte"].append(zeile)
                flag = "OK" if zeile["demotauglich"] else "FAIL"
                snippet = text[:70] + ("..." if len(text) > 70 else "")
                print(
                    f"    {flag} [{role}] {zeile['p_ziel']:.2f} ab={zeile['abstand']:+.2f}"
                    f"  ({ziel})  \"{snippet}\""
                )

    for ziel, saetze in gruppen.get("intents", {}).items():
        zeilen = []
        for text in saetze:
            probs = detect(client, domain, version, text)
            zeilen.append({"text": text, **bewerte(probs, ziel, schwelle, demo_mode)})
        ergebnis["intents"][ziel] = zeilen

        treffer = sum(z["urteil"] in ("sicher", "knapp") for z in zeilen)
        demo = sum(z["demotauglich"] for z in zeilen)
        abst = sorted(z["abstand"] for z in zeilen)
        print(f"\n  {ziel}")
        print(
            f"    {treffer}/{len(zeilen)} erkannt, davon {demo} demotauglich"
            f"   Abstand min {abst[0]:+.2f} / median {abst[len(abst) // 2]:+.2f}"
        )

    for ziel, zeilen in ergebnis["intents"].items():
        zaehler: dict[str, int] = {}
        for z in zeilen:
            zaehler[z["zweiter"]] = zaehler.get(z["zweiter"], 0) + 1
        rang = sorted(zaehler.items(), key=lambda kv: -kv[1])
        hoechst = max(z["p_zweiter"] for z in zeilen)
        wie = ", ".join(f"{k} {v}x" for k, v in rang[:2])
        print(f"    {ziel:<26} {wie:<44} staerkster Fremdwert {hoechst:.2f}")

    for text in gruppen.get("grenzfaelle", []):
        probs = detect(client, domain, version, text)
        ergebnis["grenzfaelle"].append({"text": text, "probs": probs})

    for text in gruppen.get("ausserhalb", []):
        probs = detect(client, domain, version, text)
        echt = {k: v for k, v in probs.items() if k != "None"}
        top, p = max(echt.items(), key=lambda kv: kv[1], default=("-", 0.0))
        ergebnis["ausserhalb"].append({"text": text, "probs": probs, "top": top, "p": p})

    for entry in gruppen.get("negativ", []):
        text = entry["text"]
        verboten = entry["soll_nicht"]
        probs = detect(client, domain, version, text)
        p = probs.get(verboten, 0.0)
        ergebnis["negativ"].append({"text": text, "verboten": verboten, "p": p, "probs": probs})
        flag = "OK" if p < schwelle else "LOEST AUS"
        snippet = text[:60] + ("..." if len(text) > 60 else "")
        print(f"\n  Negativ [{flag}] {verboten} {p:.2f}  \"{snippet}\"")

    return ergebnis


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        type=pathlib.Path,
        default=HERE / "example-corpus.jsonc",
        help="Corpus JSON/JSONC (top-level keys = Copilot assistant names)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Use demo thresholds (0.70 confidence, 0.40 gap)",
    )
    parser.add_argument(
        "--output-dir",
        type=pathlib.Path,
        default=HERE,
        help="Directory for results JSON",
    )
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        default=None,
        help="Explicit results file (overrides --output-dir naming)",
    )
    parser.add_argument("assistants", nargs="*", help="Filter to these assistant names")
    args = parser.parse_args()

    corpus_path = args.corpus if args.corpus.is_absolute() else (HERE / args.corpus)
    korpus = _load_corpus(corpus_path)
    client, tools = _bootstrap_mcp()
    alles = {}

    for assistent, gruppen in korpus.items():
        if args.assistants and assistent not in args.assistants:
            continue
        alles[assistent] = probe_assistant(client, tools, assistent, gruppen, args.demo)

    if args.output:
        ausgabe = args.output
    elif "holdout" in corpus_path.name.lower():
        ausgabe = args.output_dir / "results_holdout.json"
    elif args.demo or "demo" in corpus_path.name.lower():
        ausgabe = args.output_dir / "results_demo.json"
    else:
        ausgabe = args.output_dir / "results.json"

    ausgabe.parent.mkdir(parents=True, exist_ok=True)
    ausgabe.write_text(json.dumps(alles, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nDetails in {ausgabe}")


if __name__ == "__main__":
    main()
