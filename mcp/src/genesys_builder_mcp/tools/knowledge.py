"""Knowledge base tools.

Articles go in through the Import Job flow (request an upload URL, PUT the
payload, start an import job) rather than the raw document POST endpoint. The
raw endpoint accepts documents but does not index them in every org, so they
silently never show up in search. The import job path is the reliable one.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..annotations import READ_ONLY, WRITES
from ..client import get_client
from ..resolve import paginate_lister, resolve

IMPORT_POLL_ATTEMPTS = 30
IMPORT_POLL_SECONDS = 2
TERMINAL_JOB_STATES = {"Completed", "Failed", "PartialCompleted", "ValidationFailed"}

KB_DOCUMENT_SEARCH_PATH = "/api/v2/knowledge/knowledgebases/{kb_id}/documents/search"

KB_DEFAULT_MIN_CONFIDENCE = 0.50
KB_STRICT_MIN_CONFIDENCE = 0.65
KB_DEFAULT_MIN_MARGIN = 0.05
KB_STRICT_MIN_MARGIN = 0.10
KB_TIGHT_MARGIN = 0.03

# Health-check defaults. Twelve titles spread across the base is enough to see
# whether retrieval works at all without turning a check into a long crawl.
KB_HEALTH_SIGNAL_SAMPLE = 12

# Questions no business knowledge base should be able to answer. Whatever
# confidence these reach is the floor below which a score means nothing — a
# confidence threshold set under it can never stay silent. Deliberately about
# everyday life rather than any industry, so the same probes work anywhere.
KB_NOISE_PROBES = {
    "en": [
        "How long should an egg boil to stay soft?",
        "When does the next train to the coast leave?",
        "My dog has stopped eating, what should I do?",
        "Who won the football match last night?",
        "What is the weather going to be like at the weekend?",
    ],
    "de": [
        "Wie lange muss ein Ei kochen, damit es weich bleibt?",
        "Wann faehrt der naechste Zug an die Kueste?",
        "Mein Hund frisst nicht mehr, was soll ich tun?",
        "Wer hat gestern Abend das Fussballspiel gewonnen?",
        "Wie wird das Wetter am Wochenende?",
    ],
}

# A threshold needs breathing room on both sides; sitting exactly on the noise
# ceiling is not meaningfully better than sitting under it.
KB_THRESHOLD_HEADROOM = 0.05

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _resolve_knowledge_base_id(value: str) -> str:
    client = get_client()
    return resolve(
        value,
        label="knowledge base",
        search=lambda v: [
            kb
            for kb in client.paginate("/api/v2/knowledge/knowledgebases")
            if v.lower() in (kb.get("name") or "").lower()
        ],
        list_available=paginate_lister(client, "/api/v2/knowledge/knowledgebases"),
    )


def _kb_pass_thresholds(
    strict: bool,
    min_confidence: float | None,
    min_margin: float | None,
) -> tuple[float, float]:
    if min_confidence is not None and min_margin is not None:
        return min_confidence, min_margin
    if strict:
        return (
            min_confidence if min_confidence is not None else KB_STRICT_MIN_CONFIDENCE,
            min_margin if min_margin is not None else KB_STRICT_MIN_MARGIN,
        )
    return (
        min_confidence if min_confidence is not None else KB_DEFAULT_MIN_CONFIDENCE,
        min_margin if min_margin is not None else KB_DEFAULT_MIN_MARGIN,
    )


def _fetch_knowledge_base_record(knowledge_base_id: str) -> dict[str, Any]:
    return get_client().get(f"/api/v2/knowledge/knowledgebases/{knowledge_base_id}")


def _validate_knowledge_base_for_search(record: dict[str, Any]) -> None:
    name = record.get("name") or record.get("id")
    if not record.get("published"):
        raise ToolError(
            f"Knowledge base {name!r} is not published. Publish it before measuring search."
        )
    article_count = record.get("articleCount") or 0
    faq_count = record.get("faqCount") or 0
    if article_count + faq_count == 0:
        raise ToolError(
            f"Knowledge base {name!r} has no published articles or FAQs to search."
        )
    if record.get("contentSearchEnabled") is False:
        raise ToolError(
            f"Knowledge base {name!r} has content search disabled (contentSearchEnabled=false)."
        )


def _resolve_assistant_id(value: str) -> str:
    client = get_client()
    return resolve(
        value,
        label="assistant",
        search=lambda v: [
            item
            for item in client.paginate("/api/v2/assistants")
            if v.lower() in (item.get("name") or "").lower()
        ],
        list_available=paginate_lister(client, "/api/v2/assistants"),
    )


def _copilot_search_alignment(
    client: Any,
    assistant_id: str,
    knowledge_base_id: str,
) -> dict[str, Any]:
    """Derive search request fields from a Copilot assistant's live configuration."""
    record = client.get(f"/api/v2/assistants/{assistant_id}")
    config = client.get(f"/api/v2/assistants/{assistant_id}/copilot") or {}
    suggestion = record.get("knowledgeSuggestionConfig") or {}

    kb_entry = next(
        (
            kb
            for kb in (suggestion.get("knowledgeBases") or [])
            if kb.get("id") == knowledge_base_id
        ),
        None,
    )

    query_processing = (config.get("queryProcessingConfig") or {}).get(
        "knowledgeQueryProcessing"
    )
    auto_search = (config.get("autoSearchConfig") or {}).get("type")
    fallback = ((config.get("ruleEngineConfig") or {}).get("fallback")) or {}
    fallback_actions = fallback.get("actions") or []
    uses_knowledge_fallback = any(
        action.get("actionType") == "KnowledgeSearch" for action in fallback_actions
    )

    body: dict[str, Any] = {
        "queryType": "AutoSearch",
        "preprocessQuery": query_processing == "ClassificationAndReformulation",
        "application": {
            "type": "Assistant",
            "assistant": {"id": assistant_id},
        },
    }
    if auto_search == "AnswerGeneration":
        body["answerMode"] = ["AnswerGeneration"]

    confidence_threshold = (kb_entry or {}).get("confidenceThreshold")
    if confidence_threshold is not None:
        body["confidenceThreshold"] = confidence_threshold

    notes: list[str] = []
    if not kb_entry:
        notes.append(
            "The assistant's knowledgeSuggestionConfig does not list this knowledge base; "
            "confidenceThreshold and language binding were not copied from the assistant."
        )
    if not uses_knowledge_fallback:
        notes.append(
            "The assistant fallback rule is not KnowledgeSearch, so live auto-search "
            "behaviour may differ from what you measure here."
        )
    notes.append(
        "This tool calls POST .../documents/search on the knowledge base directly. "
        "Genesys also exposes POST /api/v2/knowledge/search (knowledge-setting scoped) "
        "and chunk search; Copilot's internal call chain is not documented publicly, "
        "but the documents/search endpoint accepts Assistant application context, "
        "AutoSearch, preprocessQuery, answerMode and confidenceThreshold — the same "
        "fields visible in Architect and the OpenAPI spec for agent knowledge lookup."
    )

    return {
        "assistant_id": assistant_id,
        "assistant_name": record.get("name"),
        "confidence_threshold": confidence_threshold,
        "query_processing": query_processing,
        "auto_search": auto_search,
        "knowledge_base_linked": kb_entry is not None,
        "uses_knowledge_fallback": uses_knowledge_fallback,
        "request_body": body,
        "alignment_notes": notes,
    }


def _article_lookup(client: Any, knowledge_base_id: str) -> dict[str, dict[str, str]]:
    by_id: dict[str, str] = {}
    by_title: dict[str, str] = {}
    for doc in client.paginate(
        f"/api/v2/knowledge/knowledgebases/{knowledge_base_id}/documents"
    ):
        doc_id = doc.get("id")
        title = doc.get("title")
        if not doc_id or not title:
            continue
        by_id[doc_id.lower()] = title
        by_title[title.strip().lower()] = doc_id
    return {"by_id": by_id, "by_title": by_title}


def _resolve_expected_article(
    expected: str,
    lookup: dict[str, dict[str, str]],
) -> tuple[str, str]:
    needle = expected.strip()
    if _UUID_RE.match(needle):
        doc_id = needle.lower()
        title = lookup["by_id"].get(doc_id)
        if not title:
            raise ToolError(f"Expected article id {needle!r} is not in this knowledge base.")
        return doc_id, title

    key = needle.lower()
    if key in lookup["by_title"]:
        doc_id = lookup["by_title"][key]
        return doc_id.lower(), lookup["by_id"][doc_id.lower()]

    partial = [
        (doc_id, title)
        for doc_id, title in lookup["by_id"].items()
        if key in title.lower()
    ]
    if len(partial) == 1:
        doc_id, title = partial[0]
        return doc_id, title
    if len(partial) > 1:
        titles = ", ".join(sorted(title for _, title in partial[:5]))
        raise ToolError(
            f"Expected article {needle!r} is ambiguous ({len(partial)} title matches: {titles}). "
            "Pass the article id instead."
        )
    raise ToolError(f"Expected article {needle!r} was not found in this knowledge base.")


def _knowledge_document_search(
    client: Any,
    knowledge_base_id: str,
    query: str,
    *,
    page_size: int,
    search_body: dict[str, Any],
) -> dict[str, Any]:
    if len(query.strip()) < 3:
        raise ToolError(f"Search query must be at least 3 characters: {query!r}")

    body = {
        **search_body,
        "query": query,
        "pageSize": page_size,
        "pageNumber": 1,
    }
    return client.request(
        "POST",
        KB_DOCUMENT_SEARCH_PATH.format(kb_id=knowledge_base_id),
        json_body=body,
    )


def _normalize_search_hits(response: dict[str, Any]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for rank, item in enumerate(response.get("results") or [], start=1):
        doc = item.get("document") or {}
        hits.append(
            {
                "rank": rank,
                "article_id": doc.get("id"),
                "title": doc.get("title"),
                "confidence": round(float(item.get("confidence") or 0.0), 3),
                "category": ((doc.get("category") or {}).get("name")),
                "state": doc.get("state"),
            }
        )
    return hits


def _score_knowledge_case(
    hits: list[dict[str, Any]],
    *,
    expected_article_id: str | None,
    expected_article_title: str | None,
    min_confidence: float,
    min_margin: float,
    server_confidence_threshold: float | None,
) -> dict[str, Any]:
    if not hits:
        return {
            "detected_article_id": None,
            "detected_title": None,
            "confidence": 0.0,
            "margin": 0.0,
            "runner_up_title": None,
            "runner_up_confidence": 0.0,
            "expected_rank": None,
            "expected_found": False,
            "verdict": "no_results",
            "passed": False if expected_article_id else None,
        }

    top = hits[0]
    runner = hits[1] if len(hits) > 1 else None
    confidence = top["confidence"]
    runner_up_confidence = runner["confidence"] if runner else 0.0
    margin = round(confidence - runner_up_confidence, 3)

    expected_rank = None
    expected_found = False
    if expected_article_id:
        for hit in hits:
            if hit.get("article_id", "").lower() == expected_article_id:
                expected_rank = hit["rank"]
                expected_found = True
                break

    if expected_article_id is None:
        verdict = "exploratory"
        passed = None
    elif not expected_found:
        if server_confidence_threshold is not None and confidence < server_confidence_threshold:
            verdict = "filtered_by_threshold"
        else:
            verdict = "not_in_results"
        passed = False
    elif top.get("article_id", "").lower() != expected_article_id:
        verdict = "wrong_article"
        passed = False
    elif confidence < min_confidence:
        verdict = "low_confidence"
        passed = False
    elif margin < KB_TIGHT_MARGIN:
        verdict = "tight_margin"
        passed = False
    elif margin < min_margin:
        verdict = "below_margin"
        passed = False
    else:
        verdict = "confident"
        passed = True

    return {
        "detected_article_id": top.get("article_id"),
        "detected_title": top.get("title"),
        "confidence": confidence,
        "margin": margin,
        "runner_up_title": runner.get("title") if runner else None,
        "runner_up_confidence": runner_up_confidence,
        "expected_rank": expected_rank,
        "expected_found": expected_found,
        "verdict": verdict,
        "passed": passed,
    }


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=WRITES)
    def create_knowledge_base(name: str, core_language: str, description: str = "") -> dict[str, Any]:
        """Create a knowledge base.

        Creating the base works through the plain API; its articles do not, and
        have to go in with import_knowledge_articles.

        A bot binds its default language's base under settingsKnowledge and any
        further languages under supportedLanguages, so wiring one up in a
        multilingual bot flow means editing two different places.

        Args:
            name: Display name.
            core_language: Locale such as de-DE or en-US.
            description: Optional description.
        """
        kb = get_client().request(
            "POST",
            "/api/v2/knowledge/knowledgebases",
            json_body={
                "name": name,
                "coreLanguage": core_language,
                "description": description,
                "contentSearchEnabled": True,
            },
        )
        return {"id": kb["id"], "name": kb.get("name"), "core_language": kb.get("coreLanguage")}

    @mcp.tool(annotations=READ_ONLY)
    def list_knowledge_articles(knowledge_base: str, limit: int = 200) -> list[dict[str, Any]]:
        """List the articles in a knowledge base with their categories.

        Watch ``state``. Editing an article with
        ``PATCH .../documents/{documentId}`` only writes a draft — search keeps
        serving the previously published text, so a fix appears to have no
        effect. Publish the draft with
        ``POST .../documents/{documentId}/versions`` (empty body) before
        measuring again.

        Args:
            knowledge_base: Knowledge base name or id.
            limit: Maximum number of articles to return.
        """
        client = get_client()
        base_id = _resolve_knowledge_base_id(knowledge_base)

        # The document list gives each category as an id and a selfUri but no
        # name, so without this every article reports no category at all.
        category_names = {
            c["id"]: c.get("name")
            for c in client.paginate(f"/api/v2/knowledge/knowledgebases/{base_id}/categories")
        }

        results = []
        for doc in client.paginate(
            f"/api/v2/knowledge/knowledgebases/{base_id}/documents", max_items=limit
        ):
            category = doc.get("category") or {}
            results.append(
                {
                    "id": doc.get("id"),
                    "title": doc.get("title"),
                    "category": category.get("name") or category_names.get(category.get("id")),
                    "visible": doc.get("visible"),
                    "state": doc.get("state"),
                }
            )
        return results

    @mcp.tool(annotations=WRITES)
    def import_knowledge_articles(knowledge_base: str, articles: list[dict[str, Any]]) -> dict[str, Any]:
        """Bulk-create knowledge articles through the Import Job flow.

        This is the only way to create articles that works. Do not fall back to
        POST /api/v2/knowledge/knowledgebases/{id}/documents: it answers 200 and
        raises the article count, but the documents are never retrievable
        afterwards, by id or in any list call under any state filter, and the
        request has no state field to publish them with. Waiting does not help,
        so it is a failure of that write path rather than eventual consistency.

        Args:
            knowledge_base: Knowledge base name or id.
            articles: The articles to create.

        Each article is a dict:

            title:        str   the question, as a customer would ask it
            category:     str   grouping; created automatically if new
            alternatives: list[str]  optional phrasings for search matching
            body:         list  ordered content blocks, each one of
                                {"paragraph": "..."} or
                                {"bullets": [{"lead": "...", "text": "..."}]}
                                where "lead" is an optional bold prefix

        Categories are collected from the articles, so they do not need to
        exist beforehand.
        """
        if not articles:
            raise ToolError("No articles given.")

        client = get_client()
        knowledge_base_id = _resolve_knowledge_base_id(knowledge_base)
        documents = [_build_document(a) for a in articles]
        categories = list(dict.fromkeys(a.get("category") for a in articles if a.get("category")))

        payload = {
            "version": 2,
            "knowledgeBase": {"id": knowledge_base_id},
            "documents": documents,
            "categories": [{"name": c} for c in categories],
            "contexts": [],
        }
        payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        upload = client.request(
            "POST",
            "/api/v2/knowledge/documentuploads",
            json_body={"fileName": "import.json", "contentType": "application/json"},
        )
        client.put_raw(
            upload["url"],
            content=payload_bytes,
            headers={**(upload.get("headers") or {}), "Content-Type": "application/json"},
        )

        job = client.request(
            "POST",
            f"/api/v2/knowledge/knowledgebases/{knowledge_base_id}/import/jobs",
            json_body={"uploadKey": upload["uploadKey"], "fileType": "Json", "skipConfirmationStep": True},
        )
        job_id = job["id"]

        for _ in range(IMPORT_POLL_ATTEMPTS):
            job = client.get(f"/api/v2/knowledge/knowledgebases/{knowledge_base_id}/import/jobs/{job_id}")
            if job.get("status") in TERMINAL_JOB_STATES:
                break
            time.sleep(IMPORT_POLL_SECONDS)
        else:
            raise ToolError(f"Import job {job_id} did not finish in time; check it in the Knowledge UI.")

        report = job.get("report") or {}
        return {
            "job_id": job_id,
            "status": job.get("status"),
            "categories": categories,
            "statistics": report.get("statistics", {}),
            "errors": report.get("errors", []),
        }

    @mcp.tool(annotations=READ_ONLY)
    def measure_knowledge_search(
        knowledge_base: str,
        cases: list[dict[str, Any]],
        assistant: str | None = None,
        min_confidence: float | None = None,
        min_margin: float | None = None,
        strict: bool = False,
        top_k: int = 5,
        preprocess_query: bool | None = None,
        confidence_threshold: float | None = None,
    ) -> dict[str, Any]:
        """Measure knowledge-base search quality against test utterances.

        Posts each utterance to the live document search endpoint for the
        knowledge base and reports which articles come back, in rank order, with
        confidence and separation from the runner-up. When ``expected_article``
        is given on a case, checks whether that article wins at rank one.

        **Endpoint:** ``POST /api/v2/knowledge/knowledgebases/{id}/documents/search``.
        This is the per-base document search API. It accepts the same request
        fields Copilot exposes in Architect (``queryType`` AutoSearch,
        ``preprocessQuery``, ``answerMode``, ``confidenceThreshold``,
        ``application.type=Assistant``). Genesys also offers
        ``POST /api/v2/knowledge/search`` (scoped by knowledge *setting* id)
        and chunk search; the Copilot runtime does not document which HTTP call
        it makes internally. Passing ``assistant`` copies that assistant's
        Copilot knowledge settings onto the request so results align with a
        KnowledgeSearch fallback rule as closely as the public API allows.

        Default pass thresholds: top-hit confidence >= 0.50 and margin >= 0.05.
        ``strict=True`` raises them to 0.65 / 0.10 (closer to typical Copilot
        ``confidenceThreshold`` values). ``min_confidence`` and ``min_margin``
        override either mode.

        Two things explain most disappointing results. The assistant's own
        threshold — ``knowledge_bases[].confidence_threshold`` in
        ``get_copilot`` — may sit above the scores measured here, in which case
        the agent sees nothing even though search finds the article. And an
        article edited via ``PATCH .../documents/{documentId}`` stays a draft
        until ``POST .../documents/{documentId}/versions`` publishes it, so a
        rewrite that should have fixed a gap can measure exactly as before.

        Args:
            knowledge_base: Knowledge base name or id, e.g. "Support EN".
            cases: Test rows, each with ``text``. Optional ``expected_article``
                (title or id) turns the row into a pass/fail check; omit it to
                only report what search returns.
            assistant: Optional Copilot assistant name or id. When given, copies
                ``preprocessQuery``, ``answerMode``, ``confidenceThreshold`` and
                ``application`` from that assistant's live configuration.
            min_confidence: Minimum confidence on the top hit to pass.
            min_margin: Minimum gap between top hit and runner-up to pass.
            strict: Stricter thresholds (0.65 confidence, 0.10 margin).
            top_k: Number of ranked hits to return per case (1-30).
            preprocess_query: Override query preprocessing (reformulation).
            confidence_threshold: Server-side filter applied by the search API
                (Copilot often sets this on the linked knowledge base).
        """
        if not cases:
            raise ToolError("Pass cases — nothing to measure.")

        if top_k < 1 or top_k > 30:
            raise ToolError("top_k must be between 1 and 30.")

        resolved_conf, resolved_margin = _kb_pass_thresholds(
            strict, min_confidence, min_margin
        )

        client = get_client()
        knowledge_base_id = _resolve_knowledge_base_id(knowledge_base)
        kb_record = _fetch_knowledge_base_record(knowledge_base_id)
        _validate_knowledge_base_for_search(kb_record)

        search_body: dict[str, Any] = {"queryType": "AutoSearch"}
        alignment: dict[str, Any] | None = None
        server_threshold = confidence_threshold

        if assistant:
            assistant_id = _resolve_assistant_id(assistant)
            alignment = _copilot_search_alignment(client, assistant_id, knowledge_base_id)
            search_body = dict(alignment["request_body"])
            if server_threshold is None:
                server_threshold = alignment.get("confidence_threshold")
        else:
            if preprocess_query is None:
                search_body["preprocessQuery"] = True
            if server_threshold is not None:
                search_body["confidenceThreshold"] = server_threshold

        if preprocess_query is not None:
            search_body["preprocessQuery"] = preprocess_query
        if confidence_threshold is not None:
            search_body["confidenceThreshold"] = confidence_threshold

        needs_lookup = any(row.get("expected_article") for row in cases)
        lookup = _article_lookup(client, knowledge_base_id) if needs_lookup else None

        measured: list[dict[str, Any]] = []
        for row in cases:
            text = row.get("text")
            if not text:
                raise ToolError("Each case needs text.")
            expected_raw = row.get("expected_article")
            expected_id: str | None = None
            expected_title: str | None = None
            if expected_raw:
                if lookup is None:
                    lookup = _article_lookup(client, knowledge_base_id)
                expected_id, expected_title = _resolve_expected_article(str(expected_raw), lookup)

            response = _knowledge_document_search(
                client,
                knowledge_base_id,
                text,
                page_size=top_k,
                search_body=search_body,
            )
            hits = _normalize_search_hits(response)
            scored = _score_knowledge_case(
                hits,
                expected_article_id=expected_id,
                expected_article_title=expected_title,
                min_confidence=resolved_conf,
                min_margin=resolved_margin,
                server_confidence_threshold=search_body.get("confidenceThreshold"),
            )
            entry: dict[str, Any] = {
                "text": text,
                "total_hits": response.get("total") or 0,
                "results": hits,
                "detected_title": scored["detected_title"],
                "detected_article_id": scored["detected_article_id"],
                "confidence": scored["confidence"],
                "margin": scored["margin"],
                "runner_up_title": scored["runner_up_title"],
                "runner_up_confidence": scored["runner_up_confidence"],
                "verdict": scored["verdict"],
                "passed": scored["passed"],
            }
            if expected_raw:
                entry["expected_article"] = expected_raw
                entry["expected_title"] = expected_title
                entry["expected_rank"] = scored["expected_rank"]
                entry["expected_found"] = scored["expected_found"]
            measured.append(entry)

        scored_cases = [c for c in measured if c["passed"] is not None]
        passed_cases = sum(1 for c in scored_cases if c["passed"])
        total_scored = len(scored_cases)
        expected_at_rank_1 = sum(
            1
            for c in scored_cases
            if c.get("expected_rank") == 1
        )
        expected_in_results = sum(
            1 for c in scored_cases if c.get("expected_found")
        )
        no_results = sum(1 for c in measured if c["verdict"] == "no_results")
        wrong_top = sum(
            1
            for c in scored_cases
            if c.get("expected_found") and c.get("expected_rank") != 1
        )
        below_conf = sum(
            1 for c in scored_cases if c["confidence"] < resolved_conf
        )
        below_margin = sum(
            1 for c in scored_cases if c["margin"] < resolved_margin
        )
        hit_rate = round(passed_cases / total_scored, 3) if total_scored else None

        problem_cases = [
            c
            for c in measured
            if c["verdict"] in {"no_results", "wrong_article", "not_in_results", "filtered_by_threshold"}
        ]

        weakest = sorted(
            [c for c in scored_cases if not c["passed"]],
            key=lambda c: (c["margin"], c["confidence"]),
        )[:10]
        if len(weakest) < 10:
            borderline = sorted(scored_cases, key=lambda c: (c["margin"], c["confidence"]))[:10]
            seen = {c["text"] for c in weakest}
            for c in borderline:
                if c["text"] not in seen:
                    weakest.append(c)
                if len(weakest) >= 10:
                    break

        all_passed = total_scored == 0 or passed_cases == total_scored

        verdict_parts: list[str] = []
        if total_scored:
            if passed_cases == total_scored:
                verdict_parts.append(f"{passed_cases}/{total_scored} cases passed")
            else:
                failed_count = total_scored - passed_cases
                verdict_parts.append(f"{failed_count}/{total_scored} cases failed")
        if no_results:
            verdict_parts.append(f"{no_results} with no results")
        if wrong_top:
            verdict_parts.append(f"{wrong_top} with wrong top hit")

        if all_passed and not no_results:
            verdict = "PASSED" + (f": {'; '.join(verdict_parts)}" if verdict_parts else "")
        else:
            reasons: list[str] = []
            for c in weakest:
                if c["passed"] is False:
                    snippet = c["text"][:60] + ("..." if len(c["text"]) > 60 else "")
                    label = c.get("expected_title") or c.get("expected_article") or "?"
                    reasons.append(
                        f"{label!r} -> {c['detected_title']!r} "
                        f"({c['confidence']:.2f}, margin {c['margin']:+.2f}): {snippet!r}"
                    )
                    if len(reasons) >= 5:
                        break
            for c in problem_cases:
                if c["verdict"] == "no_results" and c not in weakest:
                    snippet = c["text"][:50] + ("..." if len(c["text"]) > 50 else "")
                    reasons.append(f"no results: {snippet!r}")
                    if len(reasons) >= 5:
                        break
            verdict = "FAILED: " + "; ".join(verdict_parts + reasons[:5])

        result: dict[str, Any] = {
            "knowledge_base": kb_record.get("name"),
            "knowledge_base_id": knowledge_base_id,
            "core_language": kb_record.get("coreLanguage"),
            "published": kb_record.get("published"),
            "article_count": kb_record.get("articleCount"),
            "search_endpoint": KB_DOCUMENT_SEARCH_PATH.format(kb_id=knowledge_base_id),
            "search_request": search_body,
            "confidence_threshold": search_body.get("confidenceThreshold"),
            "thresholds": {
                "min_confidence": resolved_conf,
                "min_margin": resolved_margin,
                "strict": strict,
            },
            "passed": all_passed and no_results == 0,
            "verdict": verdict,
            "summary": {
                "total_cases": len(measured),
                "scored_cases": total_scored,
                "passed_cases": passed_cases,
                "hit_rate": hit_rate,
                "expected_at_rank_1": expected_at_rank_1,
                "expected_in_results": expected_in_results,
                "no_results": no_results,
                "wrong_top_result": wrong_top,
                "below_confidence_threshold": below_conf,
                "below_margin_threshold": below_margin,
                "exploratory_cases": len(measured) - total_scored,
            },
            "cases": measured,
            "weakest_cases": weakest[:10],
            "problem_cases": problem_cases,
        }
        if assistant:
            result["assistant"] = alignment.get("assistant_name") if alignment else assistant
            result["copilot_alignment"] = alignment
        return result

    @mcp.tool(annotations=READ_ONLY)
    def knowledge_health(
        knowledge_base: str,
        assistant: str | None = None,
        signal_sample: int = KB_HEALTH_SIGNAL_SAMPLE,
        noise_probes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Check whether a knowledge base is fit to answer, without a test corpus.

        ``measure_knowledge_search`` grades questions somebody wrote down. This
        grades the base itself, so it says something useful before anyone has
        written a single test case — the counterpart to the readiness checks on
        ``get_copilot`` rather than to ``measure_copilot_nlu``.

        The base is made to test itself. Each sampled article is looked up using
        its **own title** as the query: a healthy base returns that article
        first, with high confidence. That is the signal level — and an optimistic
        one, since a title is the easiest question an article will ever be asked.
        Then questions no business knowledge base could answer are sent in, and
        whatever they score is the noise level.

        Those two numbers are what make a confidence threshold judgeable. It has
        to sit **above the noise** or every stray remark surfaces an article, and
        **below the signal** or even an exact match stays hidden. When noise
        reaches signal, no threshold works and the content is the problem.

        Reported gaps:

        - ``threshold_below_noise`` — off-topic questions clear the assistant's
          threshold. Agents get a confident suggestion for anything said.
        - ``threshold_above_signal`` — articles do not clear their own threshold
          even when asked by their exact title.
        - ``no_workable_threshold`` — noise scores as high as signal.
        - ``draft_articles`` — edited via PATCH and never published, so search
          still serves the old text.
        - ``weak_self_retrieval`` — articles do not come back first for their own
          title, which usually means near-duplicates competing.
        - ``duplicate_titles`` — same title twice, splitting confidence.
        - ``language_mismatch`` — base language differs from the assistant's.
        - ``no_published_articles`` — nothing to search.

        Hidden articles (``visible: false``) are reported as a fact rather than a
        fault, since excluding content is often deliberate. Worth knowing anyway:
        search still answers questions in those areas, just from a neighbouring
        article.

        Args:
            knowledge_base: Knowledge base name or id.
            assistant: Copilot assistant name or id. Supplies the live
                confidence threshold and search settings — without it the
                threshold verdicts are skipped, since there is nothing to judge.
            signal_sample: How many article titles to look up. Higher is more
                thorough and slower.
            noise_probes: Override the off-topic questions. Use this when the
                defaults are close to the subject matter, which would understate
                the noise level.
        """
        client = get_client()
        knowledge_base_id = _resolve_knowledge_base_id(knowledge_base)
        kb_record = _fetch_knowledge_base_record(knowledge_base_id)
        kb_name = kb_record.get("name") or knowledge_base_id

        documents = list(
            client.paginate(f"/api/v2/knowledge/knowledgebases/{knowledge_base_id}/documents")
        )

        published = [d for d in documents if d.get("state") == "Published"]
        drafts = [d for d in documents if d.get("state") == "Draft"]
        hidden = [d for d in published if d.get("visible") is False]
        searchable = [d for d in published if d.get("visible") is not False]

        gaps: list[dict[str, Any]] = []
        notes: list[str] = []

        if not published:
            gaps.append(
                {
                    "gap": "no_published_articles",
                    "detail": f"{kb_name!r} has no published articles — there is nothing to search.",
                }
            )
        if drafts:
            titles = ", ".join(repr(d.get("title")) for d in drafts[:5])
            gaps.append(
                {
                    "gap": "draft_articles",
                    "count": len(drafts),
                    "detail": (
                        f"{len(drafts)} article(s) sit in Draft, so search still serves the "
                        f"previously published text: {titles}. Publish with "
                        "POST .../documents/{id}/versions."
                    ),
                }
            )

        by_title: dict[str, list[str]] = {}
        for doc in published:
            by_title.setdefault(_normalise_title(doc.get("title") or ""), []).append(
                doc.get("title") or ""
            )
        duplicates = {key: names for key, names in by_title.items() if len(names) > 1}
        if duplicates:
            gaps.append(
                {
                    "gap": "duplicate_titles",
                    "count": len(duplicates),
                    "detail": (
                        f"{len(duplicates)} title(s) appear more than once, which splits "
                        f"confidence between them: {', '.join(sorted(duplicates)[:5])}."
                    ),
                }
            )

        if hidden:
            notes.append(
                f"{len(hidden)} of {len(published)} published articles are hidden "
                "(visible: false) and never returned by search. Questions in those "
                "areas are still answered, from whatever article ranks next."
            )

        # Match the live Copilot request where possible; without an assistant
        # there is no threshold to judge, only retrieval.
        search_body: dict[str, Any] = {"queryType": "AutoSearch", "preprocessQuery": True}
        alignment: dict[str, Any] | None = None
        threshold: float | None = None
        if assistant:
            assistant_id = _resolve_assistant_id(assistant)
            alignment = _copilot_search_alignment(client, assistant_id, knowledge_base_id)
            search_body = dict(alignment["request_body"])
            threshold = alignment.get("confidence_threshold")

            assistant_language = (
                (client.get(f"/api/v2/assistants/{assistant_id}") or {}).get("language")
                or ""
            )
            kb_language = kb_record.get("coreLanguage") or ""
            if (
                assistant_language
                and kb_language
                and assistant_language.split("-")[0].lower()
                != kb_language.split("-")[0].lower()
            ):
                gaps.append(
                    {
                        "gap": "language_mismatch",
                        "detail": (
                            f"Assistant speaks {assistant_language}, knowledge base is "
                            f"{kb_language}."
                        ),
                    }
                )

        # The threshold is a server-side filter. Leaving it on would hide the
        # very scores this check exists to measure.
        probe_body = {k: v for k, v in search_body.items() if k != "confidenceThreshold"}

        signal: list[dict[str, Any]] = []
        for doc in _spread_sample(searchable, max(1, signal_sample)):
            title = doc.get("title") or ""
            if len(title.strip()) < 3:
                continue
            response = _knowledge_document_search(
                client, knowledge_base_id, title, page_size=3, search_body=probe_body
            )
            hits = _normalize_search_hits(response)
            top = hits[0] if hits else None
            signal.append(
                {
                    "title": title,
                    "found_itself_first": bool(top and top["article_id"] == doc.get("id")),
                    "confidence": top["confidence"] if top else 0.0,
                    "returned_instead": (
                        top["title"] if top and top["article_id"] != doc.get("id") else None
                    ),
                }
            )

        language = (kb_record.get("coreLanguage") or "en").split("-")[0].lower()
        probes = noise_probes or KB_NOISE_PROBES.get(language) or KB_NOISE_PROBES["en"]
        noise: list[dict[str, Any]] = []
        for probe in probes:
            response = _knowledge_document_search(
                client, knowledge_base_id, probe, page_size=2, search_body=probe_body
            )
            hits = _normalize_search_hits(response)
            top = hits[0] if hits else None
            noise.append(
                {
                    "probe": probe,
                    "top_title": top["title"] if top else None,
                    "confidence": top["confidence"] if top else 0.0,
                }
            )

        self_first = [s for s in signal if s["found_itself_first"]]
        # The weakest true match is what a threshold has to stay below, so the
        # minimum matters here and the average would flatter the base.
        signal_floor = min((s["confidence"] for s in self_first), default=None)
        noise_ceiling = max((n["confidence"] for n in noise), default=None)

        if signal and len(self_first) < len(signal):
            missed = [s for s in signal if not s["found_itself_first"]]
            gaps.append(
                {
                    "gap": "weak_self_retrieval",
                    "count": len(missed),
                    "detail": (
                        f"{len(missed)} of {len(signal)} articles did not come back first for "
                        f"their own title, e.g. {missed[0]['title']!r} returned "
                        f"{missed[0]['returned_instead']!r}."
                    ),
                }
            )

        if signal_floor is not None and noise_ceiling is not None:
            if noise_ceiling >= signal_floor:
                gaps.append(
                    {
                        "gap": "no_workable_threshold",
                        "detail": (
                            f"Off-topic questions reach {noise_ceiling}, as high as the weakest "
                            f"true match at {signal_floor}. No threshold separates them — the "
                            "articles are too alike or too thin to tell apart."
                        ),
                    }
                )
            if threshold is not None:
                if threshold <= noise_ceiling:
                    gaps.append(
                        {
                            "gap": "threshold_below_noise",
                            "detail": (
                                f"The threshold of {threshold} sits at or under the noise level "
                                f"of {noise_ceiling} — off-topic questions clear it, so agents "
                                "get a confident suggestion for anything the customer says."
                            ),
                        }
                    )
                elif threshold - noise_ceiling < KB_THRESHOLD_HEADROOM:
                    notes.append(
                        f"The threshold of {threshold} clears the noise level of "
                        f"{noise_ceiling} by only {round(threshold - noise_ceiling, 3)}. "
                        "That is inside the margin these probes can shift by, so treat it "
                        "as unsettled rather than sound — a paraphrased question can score "
                        "under the noise it is meant to beat."
                    )
                if threshold > signal_floor:
                    gaps.append(
                        {
                            "gap": "threshold_above_signal",
                            "detail": (
                                f"The threshold of {threshold} is above the weakest true match "
                                f"at {signal_floor} — some articles stay hidden even when asked "
                                "for by their own title."
                            ),
                        }
                    )

        # Deliberately just clear of the noise rather than midway to the signal.
        # Looking an article up by its own title is the easiest question it will
        # ever get, so the signal level here is a ceiling, not what real
        # paraphrased questions score. Splitting the difference would set the
        # bar above genuine matches and trade noisy suggestions for silence.
        suggested_minimum = None
        if noise_ceiling is not None and (
            signal_floor is None or noise_ceiling < signal_floor
        ):
            suggested_minimum = round(noise_ceiling + KB_THRESHOLD_HEADROOM, 2)

        result: dict[str, Any] = {
            "knowledge_base": kb_name,
            "knowledge_base_id": knowledge_base_id,
            "core_language": kb_record.get("coreLanguage"),
            "published": kb_record.get("published"),
            "healthy": not gaps,
            "gaps": gaps,
            "notes": notes,
            "articles": {
                "total": len(documents),
                "published": len(published),
                "draft": len(drafts),
                "hidden_from_search": len(hidden),
                "searchable": len(searchable),
            },
            "signal": {
                "tested": len(signal),
                "found_itself_first": len(self_first),
                "weakest_true_match": signal_floor,
                "cases": signal,
            },
            "noise": {
                "probes": len(noise),
                "highest_off_topic_score": noise_ceiling,
                "cases": noise,
            },
            "threshold": {
                "configured": threshold,
                "suggested_minimum": suggested_minimum,
                "noise_ceiling": noise_ceiling,
                "exact_match_ceiling": signal_floor,
                "reading": (
                    "Set the threshold at or above suggested_minimum to stop off-topic "
                    "questions surfacing articles. exact_match_ceiling comes from asking "
                    "for articles by their own title, which is the easiest question they "
                    "will ever get — real paraphrased questions score below it, so treat "
                    "it as a ceiling and raise the threshold towards it only with "
                    "measure_knowledge_search to back you up."
                ),
            },
        }
        if alignment:
            result["assistant"] = alignment.get("assistant_name")
            result["copilot_alignment"] = alignment
        if not assistant:
            result["threshold"]["note"] = (
                "Pass assistant= to judge the confidence threshold; on its own this "
                "reports retrieval and content only."
            )
        return result


def _normalise_title(title: str) -> str:
    """Strip punctuation and case so near-duplicate titles collide."""
    return re.sub(r"[^\w\s]", "", (title or "").lower()).strip()


def _spread_sample(items: list[Any], count: int) -> list[Any]:
    """Take ``count`` items spread across the list rather than the first few.

    Knowledge bases are usually listed in creation order, so the first dozen
    articles tend to come from one import and one topic. Sampling across the
    whole list is the difference between checking the base and checking a
    corner of it.
    """
    if count >= len(items):
        return list(items)
    step = len(items) / count
    return [items[int(index * step)] for index in range(count)]


def _build_document(article: dict[str, Any]) -> dict[str, Any]:
    title = article.get("title")
    if not title:
        raise ToolError(f"Article without a title: {article}")

    blocks = [_build_block(entry, title) for entry in article.get("body") or []]
    if not blocks:
        raise ToolError(f"Article {title!r} has no body content.")

    return {
        "published": {
            "title": title,
            "alternatives": [{"phrase": p, "autocomplete": True} for p in article.get("alternatives") or []],
            "category": {"name": article.get("category")} if article.get("category") else None,
            "visible": True,
            "variations": [{"name": "Default", "priority": 1, "body": {"blocks": blocks}}],
        }
    }


def _build_block(entry: dict[str, Any], title: str) -> dict[str, Any]:
    if "paragraph" in entry:
        return {
            "type": "Paragraph",
            "paragraph": {
                "blocks": [{"type": "Text", "text": {"text": entry["paragraph"]}}],
                "properties": {"fontType": "Paragraph"},
            },
        }

    if "bullets" in entry:
        items = []
        for bullet in entry["bullets"]:
            inner: list[dict[str, Any]] = []
            if bullet.get("lead"):
                inner.append({"type": "Text", "text": {"text": bullet["lead"] + ": ", "marks": ["Bold"]}})
            inner.append({"type": "Text", "text": {"text": bullet["text"]}})
            items.append({"type": "ListItem", "blocks": inner})
        return {"type": "UnorderedList", "list": {"blocks": items}}

    raise ToolError(f"Article {title!r} has a body entry that is neither 'paragraph' nor 'bullets': {entry}")
