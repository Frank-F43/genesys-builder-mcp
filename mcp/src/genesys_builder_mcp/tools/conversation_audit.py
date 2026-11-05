"""Grade a Copilot against a conversation that actually happened.

Every other measurement tool here works from a corpus somebody wrote down. A
corpus drifts towards the intents it is meant to test, because the person
writing it already knows them. A real rehearsal does not, which makes it the
harder and more honest test.

Two sources are combined, and they sit at different resolutions.

The agent copilot analytics report what Copilot really did, but only for the
conversation as a whole: ``messageId`` and ``utteranceId`` come back empty from
that endpoint, so a suggestion cannot be pinned to the sentence that caused it.
What it does carry is the split between ``Suggested`` and ``Accepted``, which
answers a question nothing else here can — did the agent actually use what
Copilot offered.

Replaying each utterance through NLU detection and knowledge search fills in the
per-turn detail: which intent won, by how much, and whether a rule could have
applied to that speaker at all.

Findings are deliberately narrow. Reporting every turn where no rule fired would
bury the two or three that matter, since most turns in any conversation are
small talk that should trigger nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..annotations import READ_ONLY
from ..client import ApiError, get_client
from ..resolve import paginate_lister, resolve
from .copilot import _nlu_detect

# The analytics conversation query needs a bounded window; a demo rehearsal is
# always recent, and a wide window costs a slow query for nothing.
DEFAULT_LOOKBACK_HOURS = 24

# The bulk message endpoint rejects oversized batches.
MESSAGE_BATCH_SIZE = 100

COPILOT_AGGREGATE_PATH = "/api/v2/analytics/agentcopilots/aggregates/query"
COPILOT_AGGREGATE_PERMISSION = "analytics:agentCopilotAggregate:view"

# ``messageId`` and ``utteranceId`` are accepted by the endpoint but always come
# back empty, so grouping by them only fragments the result. These four are the
# dimensions that actually carry values.
AGGREGATE_GROUP_BY = [
    "suggestionType",
    "scriptPageId",
    "triggerType",
    "state",
]

AGGREGATE_METRICS = ["nDistinctSuggestions"]

# A suggestion that lost to its threshold by a hair is a training gap worth
# fixing; one that lost by a mile was never the right intent to begin with, and
# reporting it as a near miss would send the reader off after nothing.
NEAR_MISS_BAND = 0.15

# Short turns carry no intent to speak of. A bare postcode or account number
# will still classify as something, and reporting it as a training gap sends the
# reader off to write phrases for "4890".
MIN_WORDS_FOR_INTENT = 4

# Knowledge needs a higher bar again: an agreement of a few words ("that's not
# necessary, I agree") is long enough to be a sentence but is not a question,
# and grading article coverage against it manufactures failures.
MIN_WORDS_FOR_KNOWLEDGE = 8


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse_moment(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _speaker_of(purpose: str | None) -> str | None:
    """Map a participant purpose onto the roles Copilot rules are scoped by."""
    if purpose == "customer":
        return "Customer"
    if purpose in ("agent", "user"):
        return "Agent"
    return None


def register(mcp: MCPServer) -> None:
    client_factory = get_client

    def _queue_id(value: str) -> str:
        client = client_factory()
        return resolve(
            value,
            label="queue",
            search=lambda v: list(
                client.paginate("/api/v2/routing/queues", params={"name": f"*{v}*"})
            ),
            list_available=paginate_lister(client, "/api/v2/routing/queues"),
        )

    def _assistant_id(value: str) -> str:
        client = client_factory()
        return resolve(
            value,
            label="assistant",
            search=lambda v: [
                a
                for a in client.paginate("/api/v2/assistants")
                if v.lower() in (a.get("name") or "").lower()
            ],
            list_available=paginate_lister(client, "/api/v2/assistants"),
        )

    def _latest_conversation_on_queue(queue_id: str, lookback_hours: int) -> str:
        client = client_factory()
        now = datetime.now(timezone.utc)
        window = f"{_iso(now - timedelta(hours=lookback_hours))}/{_iso(now)}"
        response = client.request(
            "POST",
            "/api/v2/analytics/conversations/details/query",
            json_body={
                "interval": window,
                "order": "desc",
                "paging": {"pageSize": 25, "pageNumber": 1},
                "segmentFilters": [
                    {
                        "type": "and",
                        "predicates": [
                            {"dimension": "queueId", "value": queue_id}
                        ],
                    }
                ],
            },
        )
        conversations = (response or {}).get("conversations") or []
        if not conversations:
            raise ToolError(
                f"No conversation on that queue in the last {lookback_hours} hours. "
                "Raise lookback_hours, or pass a conversation id directly."
            )
        return conversations[0]["conversationId"]

    def _transcript(conversation_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Ordered utterances plus the conversation facts worth reporting."""
        client = client_factory()
        record = client.get(f"/api/v2/conversations/messages/{conversation_id}")

        wanted: dict[str, dict[str, Any]] = {}
        facts: dict[str, Any] = {
            "queue_id": None,
            "agent_user_id": None,
            "wrapup_code_id": None,
            "started": None,
            "ended": None,
        }

        for participant in record.get("participants") or []:
            purpose = participant.get("purpose")
            speaker = _speaker_of(purpose)
            if purpose == "customer":
                facts["queue_id"] = (participant.get("queue") or {}).get("id")
                facts["started"] = participant.get("startTime")
                facts["ended"] = participant.get("endTime")
            if purpose == "agent":
                facts["agent_user_id"] = (participant.get("user") or {}).get("id")
                facts["wrapup_code_id"] = (participant.get("wrapup") or {}).get("code")
            if speaker is None:
                continue
            for message in participant.get("messages") or []:
                metadata = message.get("messageMetadata") or {}
                if metadata.get("type") != "Text":
                    continue
                message_id = message.get("messageId")
                if message_id:
                    wanted[message_id] = {
                        "message_id": message_id,
                        "speaker": speaker,
                        "time": message.get("messageTime"),
                    }

        if not wanted:
            raise ToolError(
                "No text messages on this conversation. Voice conversations are not "
                "supported yet — their transcript lives behind a different API."
            )

        ids = list(wanted)
        for start in range(0, len(ids), MESSAGE_BATCH_SIZE):
            batch = ids[start : start + MESSAGE_BATCH_SIZE]
            response = client.request(
                "POST",
                f"/api/v2/conversations/messages/{conversation_id}/messages/bulk",
                json_body=batch,
            )
            for entity in (response or {}).get("entities") or []:
                normalized = entity.get("normalizedMessage") or {}
                target = wanted.get(entity.get("id"))
                if target is not None:
                    target["text"] = normalized.get("text") or ""

        turns = [t for t in wanted.values() if t.get("text")]
        turns.sort(key=lambda t: t.get("time") or "")
        for index, turn in enumerate(turns, start=1):
            turn["turn"] = index
        return turns, facts

    def _actual_activity(
        conversation_id: str, started: str | None, ended: str | None
    ) -> tuple[dict[str, Any], str | None]:
        """What Copilot really surfaced, for the conversation as a whole.

        Per-message attribution is not on offer: the endpoint leaves
        ``messageId`` empty. What it does give is ``Suggested`` against
        ``Accepted``, so the report can say whether the agent used any of it.
        """
        client = client_factory()
        begin = _parse_moment(started) or (datetime.now(timezone.utc) - timedelta(hours=DEFAULT_LOOKBACK_HOURS))
        finish = _parse_moment(ended) or datetime.now(timezone.utc)
        window = f"{_iso(begin - timedelta(minutes=5))}/{_iso(finish + timedelta(minutes=30))}"

        try:
            response = client.request(
                "POST",
                COPILOT_AGGREGATE_PATH,
                json_body={
                    "interval": window,
                    "groupBy": AGGREGATE_GROUP_BY,
                    "filter": {
                        "type": "and",
                        "predicates": [
                            {
                                "type": "dimension",
                                "dimension": "conversationId",
                                "operator": "matches",
                                "value": conversation_id,
                            }
                        ],
                    },
                    "metrics": AGGREGATE_METRICS,
                },
            )
        except ApiError as exc:
            if exc.status == 403:
                return {}, (
                    f"Copilot analytics unavailable: the OAuth client lacks "
                    f"{COPILOT_AGGREGATE_PERMISSION}. Only the replay is reported, "
                    "so this says what Copilot should have done, not what it did."
                )
            raise

        def _bucket() -> dict[str, int]:
            return {"suggested": 0, "accepted": 0}

        by_trigger: dict[str, dict[str, int]] = {}
        by_type: dict[str, dict[str, int]] = {}
        pages: dict[str, dict[str, int]] = {}
        totals = _bucket()

        for result in (response or {}).get("results") or []:
            group = result.get("group") or {}
            state = group.get("state")
            # Every suggestion is reported once as Suggested and, if the agent
            # used it, a second time as Accepted. Anything else is a state this
            # tool does not know how to count.
            key = {"Suggested": "suggested", "Accepted": "accepted"}.get(state)
            if key is None:
                continue

            count = 0
            for datum in result.get("data") or []:
                for metric in datum.get("metrics") or []:
                    if metric.get("metric") == "nDistinctSuggestions":
                        count += int((metric.get("stats") or {}).get("count") or 0)
            if not count:
                continue

            totals[key] += count
            by_trigger.setdefault(group.get("triggerType") or "unknown", _bucket())[key] += count
            by_type.setdefault(group.get("suggestionType") or "unknown", _bucket())[key] += count
            page_id = group.get("scriptPageId")
            if page_id:
                pages.setdefault(page_id, _bucket())[key] += count

        if totals["suggested"]:
            totals["acceptance_rate"] = round(totals["accepted"] / totals["suggested"], 2)

        return (
            {
                "totals": totals,
                "by_trigger": by_trigger,
                "by_suggestion_type": by_type,
                "script_pages": pages,
            },
            None,
        )

    def _assistant_for_queue(queue_id: str | None) -> str | None:
        if not queue_id:
            return None
        try:
            record = client_factory().get(f"/api/v2/routing/queues/{queue_id}/assistant")
        except ApiError:
            return None
        # The response is the queue-assistant association: its own ``id`` is the
        # queue, and the assistant sits one level down.
        return ((record or {}).get("assistant") or {}).get("id")

    @mcp.tool(annotations=READ_ONLY)
    def audit_conversation(
        conversation: str | None = None,
        queue: str | None = None,
        assistant: str | None = None,
        lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
        replay_nlu: bool = True,
        replay_knowledge: bool = True,
    ) -> dict[str, Any]:
        """Audit a Copilot against a conversation that actually took place.

        Reads the transcript of a past conversation, reports what Copilot
        actually surfaced and how much of it the agent used, and replays every
        utterance through the live NLU domain so a quiet turn can be explained
        rather than guessed at.

        This is the counterpart to ``measure_copilot_nlu``. That one grades a
        corpus somebody wrote; this one grades a rehearsal, including the
        off-script sentences that a corpus never contains and that break demos.

        ``copilot_activity`` is the measured half, and it is conversation-wide
        rather than per turn — the analytics endpoint does not attribute a
        suggestion to a message. Its ``acceptance_rate`` is the number worth
        watching: suggestions nobody accepts are worse than none at all.

        ``turns`` is the replayed half, per utterance: winning intent, its
        confidence and margin, and whether a rule could have applied.

        Findings are kept narrow, so that a short list means a healthy setup:

        - ``page_never_surfaced`` — the replay says a script page was due, and
          the analytics say it never opened.
        - ``page_unexpected`` — a page opened that no utterance accounts for.
        - ``near_miss_intent`` — the right intent won but landed just under the
          Copilot threshold. These are the training gaps worth closing first.
        - ``role_blocked`` — an intent cleared the threshold, but its only rule
          is scoped to the other speaker, so it could never have fired.
        - ``knowledge_hidden`` — a real question, with an article just under the
          knowledge threshold, so the agent saw nothing.
        - ``greeting_repeated`` — the opening rule fired more than once.
        - ``suggestions_ignored`` — a suggestion type the agent never accepted.

        Voice conversations are not supported yet; their transcript sits behind
        the speech-and-text-analytics API rather than the messaging one.

        Args:
            conversation: Conversation id. Omit it and pass ``queue`` to take
                the most recent conversation there.
            queue: Queue name or id, to pick up the latest rehearsal on it.
            assistant: Assistant name or id. Inferred from the queue when omitted.
            lookback_hours: How far back to look when selecting by queue.
            replay_nlu: Replay each utterance through NLU detection.
            replay_knowledge: Search knowledge for customer turns that no rule
                covers, and compare against the assistant's threshold.
        """
        client = client_factory()

        if not conversation and not queue:
            raise ToolError("Pass a conversation id, or a queue to take the latest one from.")

        conversation_id = conversation or _latest_conversation_on_queue(
            _queue_id(queue), lookback_hours
        )

        turns, facts = _transcript(conversation_id)
        activity, analytics_note = _actual_activity(
            conversation_id, facts["started"], facts["ended"]
        )

        assistant_id = (
            _assistant_id(assistant) if assistant else _assistant_for_queue(facts["queue_id"])
        )
        if not assistant_id:
            raise ToolError(
                "No assistant found for this conversation's queue. Pass assistant= explicitly."
            )

        assistant_record = client.get(f"/api/v2/assistants/{assistant_id}")
        config = client.get(f"/api/v2/assistants/{assistant_id}/copilot") or {}
        nlu = config.get("nluConfig") or {}
        domain_id = (nlu.get("domain") or {}).get("id")
        copilot_threshold = float(nlu.get("intentConfidenceThreshold") or 0.0)
        rules = ((config.get("ruleEngineConfig") or {}).get("rules")) or []

        version_id = None
        if replay_nlu and domain_id:
            versions = [
                v
                for v in client.paginate(
                    f"/api/v2/languageunderstanding/domains/{domain_id}/versions",
                    params={"includeUtterances": "false"},
                )
                if v.get("datePublished")
            ]
            if versions:
                version_id = max(versions, key=lambda v: v["datePublished"])["id"]

        knowledge_bases = (
            (assistant_record.get("knowledgeSuggestionConfig") or {}).get("knowledgeBases")
        ) or []
        knowledge_base_id = knowledge_bases[0].get("id") if knowledge_bases else None
        knowledge_threshold = (
            float(knowledge_bases[0].get("confidenceThreshold") or 0.0)
            if knowledge_bases
            else None
        )

        # An intent may be wired to several rules; a rule only fires for the
        # speakers it is scoped to, which is what separates "no rule" from
        # "rule that could never apply here".
        rules_by_intent: dict[str, list[dict[str, Any]]] = {}
        for entry in rules:
            for condition in ((entry.get("rule") or {}).get("conditions") or []):
                if condition.get("conditionType") != "Intent":
                    continue
                for intent in condition.get("conditionValues") or []:
                    rules_by_intent.setdefault(intent, []).append(entry)

        # A page id in the analytics means nothing to a reader; the intent that
        # opens it does.
        page_labels: dict[str, str] = {}
        for intent, entries in rules_by_intent.items():
            for entry in entries:
                for action in ((entry.get("rule") or {}).get("actions") or []):
                    page_id = (action.get("attributes") or {}).get("pageId")
                    if page_id:
                        page_labels[page_id] = intent

        findings: list[dict[str, Any]] = []
        expected_pages: dict[str, int] = {}

        # Role scoping can only be judged across the whole conversation. A rule
        # scoped to the customer will look blocked on every agent turn that
        # happens to mention the same topic, which is the rule working, not
        # failing. It is only a defect if the intent never once landed on a
        # speaker the rule accepts.
        intent_applied: set[str] = set()
        intent_blocked: dict[str, dict[str, Any]] = {}

        for turn in turns:
            if replay_nlu and domain_id and version_id:
                probs = _nlu_detect(client, domain_id, version_id, turn["text"])
                real = {k: v for k, v in probs.items() if k != "None"}
                top_intent, top_confidence = max(
                    real.items(), key=lambda kv: kv[1], default=("-", 0.0)
                )
                runner_up = sorted(real.values(), reverse=True)
                turn["top_intent"] = top_intent
                turn["confidence"] = top_confidence
                turn["margin"] = round(
                    top_confidence - (runner_up[1] if len(runner_up) > 1 else 0.0), 3
                )
                turn["above_copilot_threshold"] = top_confidence >= copilot_threshold

                matching = rules_by_intent.get(top_intent) or []
                applicable = [
                    entry
                    for entry in matching
                    if not entry.get("participantRoles")
                    or turn["speaker"] in entry["participantRoles"]
                ]
                turn["rules_for_intent"] = len(matching)
                turn["rules_applicable_to_speaker"] = len(applicable)

                if turn["above_copilot_threshold"]:
                    if matching and not applicable:
                        intent_blocked.setdefault(
                            top_intent,
                            {
                                "turn": turn["turn"],
                                "speaker": turn["speaker"],
                                "confidence": top_confidence,
                            },
                        )
                    elif applicable:
                        intent_applied.add(top_intent)
                    for entry in applicable:
                        for action in ((entry.get("rule") or {}).get("actions") or []):
                            page_id = (action.get("attributes") or {}).get("pageId")
                            if page_id:
                                expected_pages[page_id] = turn["turn"]
                elif (
                    applicable
                    and top_confidence >= copilot_threshold - NEAR_MISS_BAND
                    and len(turn["text"].split()) >= MIN_WORDS_FOR_INTENT
                ):
                    findings.append(
                        {
                            "turn": turn["turn"],
                            "issue": "near_miss_intent",
                            "detail": (
                                f"{top_intent!r} reached {top_confidence}, just under the "
                                f"threshold of {copilot_threshold}. A few more training "
                                f"phrases like this one would carry it over."
                            ),
                        }
                    )

            if (
                replay_knowledge
                and knowledge_base_id
                and turn["speaker"] == "Customer"
                and not turn.get("rules_applicable_to_speaker")
                and len(turn["text"].split()) >= MIN_WORDS_FOR_KNOWLEDGE
            ):
                try:
                    response = client.request(
                        "POST",
                        f"/api/v2/knowledge/knowledgebases/{knowledge_base_id}/documents/search",
                        json_body={
                            "query": turn["text"],
                            "pageSize": 3,
                            "pageNumber": 1,
                        },
                    )
                except ApiError:
                    response = {}
                results = (response or {}).get("results") or []
                if results:
                    best = results[0]
                    confidence = round(float(best.get("confidence") or 0.0), 3)
                    title = (best.get("document") or {}).get("title")
                    turn["knowledge_top_hit"] = {"title": title, "confidence": confidence}
                    if (
                        knowledge_threshold
                        and confidence < knowledge_threshold
                        and confidence >= knowledge_threshold - NEAR_MISS_BAND
                    ):
                        findings.append(
                            {
                                "turn": turn["turn"],
                                "issue": "knowledge_hidden",
                                "detail": (
                                    f"{title!r} matches at {confidence}, just under the "
                                    f"knowledge threshold of {knowledge_threshold} — close "
                                    "enough that the agent should have seen it."
                                ),
                            }
                        )

        # The remaining checks compare the two halves against each other, so
        # they can only run once every turn has been replayed.
        for intent, blocked in intent_blocked.items():
            if intent in intent_applied:
                continue
            findings.append(
                {
                    "turn": blocked["turn"],
                    "issue": "role_blocked",
                    "detail": (
                        f"{intent!r} cleared the threshold at {blocked['confidence']} on a "
                        f"{blocked['speaker']} turn, and never once on a speaker its rule "
                        "accepts — so the rule sat out the whole conversation."
                    ),
                }
            )

        pages_shown = set(activity.get("script_pages") or {})
        if activity:
            for page_id, turn_number in expected_pages.items():
                if page_id not in pages_shown:
                    findings.append(
                        {
                            "turn": turn_number,
                            "issue": "page_never_surfaced",
                            "detail": (
                                f"{page_labels.get(page_id, page_id)!r} was due on this turn, "
                                "but the analytics show that page never opened."
                            ),
                        }
                    )
            for page_id in pages_shown - set(expected_pages):
                findings.append(
                    {
                        "issue": "page_unexpected",
                        "detail": (
                            f"Page {page_labels.get(page_id, page_id)!r} opened, but no "
                            "utterance in this conversation accounts for it."
                        ),
                    }
                )

            opening = (activity.get("by_trigger") or {}).get("ConversationStart") or {}
            if opening.get("suggested", 0) > 1:
                findings.append(
                    {
                        "issue": "greeting_repeated",
                        "detail": (
                            f"The opening rule fired {opening['suggested']} times; agents see "
                            "the greeting more than once."
                        ),
                    }
                )

            for kind, counts in (activity.get("by_suggestion_type") or {}).items():
                if counts.get("suggested", 0) >= 3 and not counts.get("accepted"):
                    findings.append(
                        {
                            "issue": "suggestions_ignored",
                            "detail": (
                                f"{counts['suggested']} {kind} suggestions, none accepted. "
                                "Either they missed the point, or the agent never saw them."
                            ),
                        }
                    )

        report: dict[str, Any] = {
            "conversation_id": conversation_id,
            "assistant": assistant_record.get("name"),
            "assistant_id": assistant_id,
            "intent_confidence_threshold": copilot_threshold,
            "knowledge_confidence_threshold": knowledge_threshold,
            "wrapup_code_id": facts["wrapup_code_id"],
            "copilot_activity": activity,
            "turns": turns,
            "findings": findings,
            "scorecard": {
                "turns": len(turns),
                "customer_turns": sum(1 for t in turns if t["speaker"] == "Customer"),
                "agent_turns": sum(1 for t in turns if t["speaker"] == "Agent"),
                "suggestions_offered": (activity.get("totals") or {}).get("suggested", 0),
                "suggestions_accepted": (activity.get("totals") or {}).get("accepted", 0),
                "acceptance_rate": (activity.get("totals") or {}).get("acceptance_rate"),
                "findings": len(findings),
                "issues_by_type": {
                    issue: sum(1 for f in findings if f["issue"] == issue)
                    for issue in sorted({f["issue"] for f in findings})
                },
            },
        }
        if analytics_note:
            report["analytics_note"] = analytics_note
        return report
