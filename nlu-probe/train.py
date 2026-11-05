#!/usr/bin/env python3
"""Add NLU training utterances and publish one version per assistant.

Dry-run by default. Aborts if any training sentence overlaps the test corpus
(messpunkte, holdout, grenzfaelle, negativ, intent test lists) — prevents
measuring memorization instead of generalization.

Example:
  toolkit/mcp/.venv/bin/python toolkit/nlu-probe/train.py \\
    --corpus my-corpus.json --additions my-additions.json \\
    --apply "My Copilot DE"

See README.md in this directory.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
MCP_SRC = REPO_ROOT / "toolkit" / "mcp" / "src"


def _bootstrap_mcp() -> tuple[object, dict]:
    if not MCP_SRC.is_dir():
        raise SystemExit(f"MCP source not found at {MCP_SRC}")
    sys.path.insert(0, str(MCP_SRC))
    from genesys_builder_mcp import server  # noqa: E402
    from genesys_builder_mcp.client import get_client  # noqa: E402

    tools = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    if "get_copilot_nlu" not in tools:
        raise SystemExit("MCP tool 'get_copilot_nlu' not registered.")
    return get_client(), tools


def _load_json(path: pathlib.Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".jsonc", ".json5"):
        text = re.sub(r"//.*?$|/\*.*?\*/", "", text, flags=re.MULTILINE | re.DOTALL)
    return json.loads(text)


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9äöüß]+", "", text.lower())


def utterance_texts(intent: dict) -> list[str]:
    return ["".join(s["text"] for s in u["segments"]) for u in intent.get("utterances") or []]


def latest_published(client, domain_id: str) -> dict:
    versions = [
        v
        for v in client.paginate(
            f"/api/v2/languageunderstanding/domains/{domain_id}/versions",
            params={"includeUtterances": "true"},
        )
        if v.get("published")
    ]
    versions.sort(key=lambda v: v.get("datePublished") or "", reverse=True)
    if not versions:
        raise SystemExit(f"No published NLU version for domain {domain_id}")
    return versions[0]


def carry_forward(intents: list[dict]) -> list[dict]:
    out = []
    for intent in intents:
        entry = {k: v for k, v in intent.items() if k not in ("id", "entityNameReferences")}
        entry["utterances"] = [
            {k: v for k, v in u.items() if k != "id"} for u in intent.get("utterances") or []
        ]
        out.append(entry)
    return out


def publish(client, domain_id: str, previous: dict, intents: list[dict]) -> str:
    base = f"/api/v2/languageunderstanding/domains/{domain_id}/versions"
    draft = client.request(
        "POST",
        base,
        json_body={
            "language": previous.get("language"),
            "intents": intents,
            "entityTypes": previous.get("entityTypes") or [],
            "entities": previous.get("entities") or [],
        },
    )
    vid = draft["id"]
    client.request("POST", f"{base}/{vid}/train")
    for _ in range(60):
        status = client.get(f"{base}/{vid}").get("trainingStatus")
        if status == "Trained":
            break
        if status == "Error":
            raise RuntimeError(f"Training der Version {vid} fehlgeschlagen.")
        time.sleep(2)
    else:
        raise RuntimeError(f"Version {vid} wurde nicht rechtzeitig trainiert.")
    client.request("POST", f"{base}/{vid}/publish")
    return vid


def testsaetze(gruppen: dict) -> set[str]:
    test: set[str] = set()
    for text in gruppen.get("grenzfaelle", []) + gruppen.get("ausserhalb", []):
        test.add(norm(text))
    for saetze in gruppen.get("intents", {}).values():
        for text in saetze:
            test.add(norm(text))
    for eintraege in gruppen.get("messpunkte", {}).values():
        for entry in eintraege:
            text = entry["text"] if isinstance(entry, dict) else entry
            test.add(norm(text))
    for entry in gruppen.get("negativ", []):
        test.add(norm(entry["text"]))
    return test


def parse_addition(value) -> tuple[list[str], str | None]:
    """Return (utterances, description) from additions entry."""
    if isinstance(value, list):
        return value, None
    if isinstance(value, dict):
        utterances = value.get("utterances") or value.get("saetze") or []
        description = value.get("description")
        return list(utterances), description
    raise SystemExit(f"Invalid additions entry: {value!r}")


def merge_corpus_test_sets(*corpora: dict, assistant: str) -> set[str]:
    merged: set[str] = set()
    for corpus in corpora:
        if assistant in corpus:
            merged |= testsaetze(corpus[assistant])
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=pathlib.Path, default=HERE / "example-corpus.jsonc")
    parser.add_argument("--test-corpus", type=pathlib.Path, default=None,
                        help="Extra corpus merged into test set (default: same as --corpus)")
    parser.add_argument("--additions", type=pathlib.Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Publish after validation")
    parser.add_argument("assistants", nargs="*", help="Filter to these assistant names")
    args = parser.parse_args()

    corpus_path = args.corpus if args.corpus.is_absolute() else (HERE / args.corpus)
    test_path = args.test_corpus or args.corpus
    test_path = test_path if test_path.is_absolute() else (HERE / test_path)
    additions_path = args.additions if args.additions.is_absolute() else (HERE / args.additions)

    korpus = _load_json(corpus_path)
    test_korpus = _load_json(test_path) if test_path != corpus_path else korpus
    zusatz = _load_json(additions_path)
    # Top-level keys are assistant names. Anything else is a note the author
    # left themselves, which should not crash the run as a missing assistant.
    zusatz = {k: v for k, v in zusatz.items() if isinstance(v, dict)}
    client, tools = _bootstrap_mcp()

    for assistent, gruppen in zusatz.items():
        test = merge_corpus_test_sets(korpus, test_korpus, assistant=assistent)
        for intent, raw in gruppen.items():
            saetze, _ = parse_addition(raw)
            kollision = [s for s in saetze if norm(s) in test]
            if kollision:
                raise SystemExit(
                    f"ABBRUCH - diese Trainingssaetze stehen auch im Testkorpus "
                    f"({assistent} / {intent}):\n  " + "\n  ".join(kollision)
                )
    print("Sperre ok: kein Testsatz im Trainingsmaterial.\n")

    for assistent, gruppen in zusatz.items():
        if args.assistants and assistent not in args.assistants:
            continue
        nlu = tools["get_copilot_nlu"].fn(assistant=assistent)
        domain_id = nlu["domain_id"]
        live = latest_published(client, domain_id)
        intents = carry_forward(live.get("intents") or [])

        print(f"{assistent}   Domain {domain_id}")
        bekannt = {e["name"] for e in intents}
        for entry in intents:
            raw = gruppen.get(entry["name"])
            if raw is None:
                print(f"  {entry['name']:<34} unveraendert ({len(entry['utterances'])})")
                continue
            neu, _ = parse_addition(raw)
            vorhanden = {norm(t) for t in utterance_texts(entry)}
            frisch = [s for s in neu if norm(s) not in vorhanden]
            vorher = len(entry["utterances"])
            entry["utterances"] += [
                {"source": "User", "segments": [{"text": s}]} for s in frisch
            ]
            print(
                f"  {entry['name']:<34} {vorher} -> {len(entry['utterances'])}"
                f"  (+{len(frisch)}, {len(neu) - len(frisch)} schon vorhanden)"
            )

        for name, raw in gruppen.items():
            if name in bekannt:
                continue
            neu, beschreibung = parse_addition(raw)
            if not beschreibung:
                raise SystemExit(
                    f"Neuer Intent {assistent} / {name} braucht 'description' im additions-Eintrag "
                    f"(dict mit utterances + description)."
                )
            intents.append(
                {
                    "name": name,
                    "description": beschreibung,
                    "utterances": [
                        {"source": "User", "segments": [{"text": s}]} for s in neu
                    ],
                }
            )
            print(f"  {name:<34} NEU ({len(neu)} Saetze)")

        if not args.apply:
            print("  [Probelauf - nichts veroeffentlicht]\n")
            continue

        vid = publish(client, domain_id, live, intents)
        pruef = tools["get_copilot_nlu"].fn(assistant=assistent)
        ok = pruef["live_version_id"] == vid
        print(f"  veroeffentlicht: {vid}  {'live bestaetigt' if ok else 'NICHT LIVE!'}\n")


if __name__ == "__main__":
    main()
