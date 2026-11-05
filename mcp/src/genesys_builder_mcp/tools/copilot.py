"""Agent Copilot: assistants, their settings, rules, NLU and queue assignments.

Copilot spreads over three unrelated API families that have to be stitched
together to answer even simple questions:

- the assistant itself, ``/api/v2/assistants/{id}``, holding state, knowledge
  suggestion and transcription settings,
- its copilot configuration, ``/api/v2/assistants/{id}/copilot``, holding the
  rule engine, the NLU binding and the summarisation settings,
- the NLU domain it points at, under ``/api/v2/languageunderstanding``, which
  is a separate resource with its own version history.

Nothing in the API joins those for you, and almost every id in a rule points at
something in yet another service - a script page, an agent checklist, a canned
response. The tools here resolve those to names, because a bare
``pageId: 786e5cda-...`` tells nobody whether the rule is wired correctly.

Writing has one governing constraint. There is no endpoint for a single rule:
the whole copilot configuration goes back in one PUT, so every write is a
read-modify-write of the live object. It must be the live object rather than
one rebuilt from the API spec, because the spec lags the service - it does not
mention ``participantRoles`` on a rule, nor ``voiceTranscriptionConfig`` and
``queryProcessingConfig`` on the configuration, all of which are really there
and would be dropped by a well-meaning reconstruction.
"""

from __future__ import annotations

import asyncio
import copy
import json
import pathlib
import re
import time
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from ..annotations import DESTRUCTIVE, READ_ONLY, WRITES
from ..client import ApiError, get_client
from ..resolve import LIST_LIMIT, paginate_lister, resolve, resolve_async
from ..write_guard import (
    attach_write_impact,
    confirmation_block_reason,
    elicit_write_confirmation,
    format_sample_list,
    list_set_diff,
    make_write_impact,
)

# Rejected by the PUT if sent back: readOnly on the configuration itself, and
# per-rule ids that the service assigns.
READ_ONLY_CONFIG_FIELDS = ("enabled", "selfUri")

# Training is asynchronous, and a domain with a few hundred utterances takes
# well under a minute in practice. The ceiling is a backstop, not an estimate.
TRAINING_POLL_ATTEMPTS = 60
TRAINING_POLL_SECONDS = 3

# Switching a queue's assignment mode is a job, though it has settled by the
# time the POST answers in every observed case.
JOB_POLL_ATTEMPTS = 20
JOB_POLL_SECONDS = 2

# NLU measurement thresholds — aligned with toolkit/nlu-probe/probe.py defaults.
NLU_DEFAULT_MIN_CONFIDENCE = 0.50
NLU_DEFAULT_MIN_MARGIN = 0.30
NLU_STRICT_MIN_CONFIDENCE = 0.70
NLU_STRICT_MIN_MARGIN = 0.40
NLU_TIGHT_MARGIN = 0.25
CATCH_ALL_INTENT_NAMES = ("Sonstiges", "Other")

# Returned by every copilot write tool to steer the agent through the lifecycle.
NEXT_STEP_HINT = "next_step_hint"

# Elicitation backstop when the client does not answer (seconds).
ELICITATION_TIMEOUT_SECONDS = 30

# Stable readiness gap codes — derived only from live configuration.
READINESS_GAP_NO_NLU_DOMAIN = "no_nlu_domain"
READINESS_GAP_NO_NLU_INTENTS = "no_nlu_intents"
READINESS_GAP_NO_RULES = "no_rules"
READINESS_GAP_NO_CHECKLIST_RULE = "no_checklist_rule"
READINESS_GAP_NO_SUMMARY_SETTING = "no_summary_setting"
READINESS_GAP_SUMMARY_ENABLED_UNBOUND = "summarisation_enabled_but_no_setting"
READINESS_GAP_NO_QUEUE = "no_queue_assignment"
READINESS_GAP_MANUAL_QUEUE_NO_USERS = "manual_queue_without_users"
READINESS_GAP_COPILOT_DISABLED = "copilot_disabled"
READINESS_GAP_UNRESTRICTED_ROLES = "rules_without_restricted_participant_roles"
READINESS_GAP_UNKNOWN_INTENT_RULES = "rules_reference_unknown_intents"
READINESS_GAP_INVALID_ACTIONS = "rules_with_invalid_actions"
READINESS_GAP_KNOWLEDGE_WITHOUT_BASE = "knowledge_search_without_knowledge_base"
READINESS_GAP_KNOWLEDGE_BASE_UNUSED = "knowledge_base_attached_but_never_searched"
READINESS_GAP_KNOWLEDGE_BASE_UNPUBLISHED = "knowledge_base_unpublished"
READINESS_GAP_KNOWLEDGE_BASE_EMPTY = "knowledge_base_empty"
READINESS_GAP_KNOWLEDGE_LANGUAGE_MISMATCH = "knowledge_base_language_mismatch"


def _nlu_pass_thresholds(
    strict: bool,
    min_confidence: float | None,
    min_margin: float | None,
) -> tuple[float, float]:
    if min_confidence is not None and min_margin is not None:
        return min_confidence, min_margin
    if strict:
        return (
            min_confidence if min_confidence is not None else NLU_STRICT_MIN_CONFIDENCE,
            min_margin if min_margin is not None else NLU_STRICT_MIN_MARGIN,
        )
    return (
        min_confidence if min_confidence is not None else NLU_DEFAULT_MIN_CONFIDENCE,
        min_margin if min_margin is not None else NLU_DEFAULT_MIN_MARGIN,
    )


def _load_nlu_corpus_section(corpus_file: str, assistant: str) -> dict[str, Any]:
    path = pathlib.Path(corpus_file).expanduser()
    if not path.is_file():
        raise ToolError(f"Corpus file not found: {corpus_file}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".jsonc", ".json5"):
        text = re.sub(r"//.*?$|/\*.*?\*/", "", text, flags=re.MULTILINE | re.DOTALL)
    try:
        corpus = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ToolError(f"Corpus file is not valid JSON: {exc}") from exc
    if assistant not in corpus:
        keys = ", ".join(sorted(corpus)) or "(empty)"
        raise ToolError(
            f"No section for assistant {assistant!r} in {corpus_file}. Top-level keys: {keys}."
        )
    return corpus[assistant]


def _cases_from_corpus_section(section: dict[str, Any]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for intent, entries in (section.get("messpunkte") or {}).items():
        for entry in entries:
            text = entry["text"] if isinstance(entry, dict) else entry
            row: dict[str, Any] = {"text": text, "expected_intent": intent}
            if isinstance(entry, dict) and entry.get("role"):
                row["role"] = entry["role"]
            cases.append(row)
    for intent, sentences in (section.get("intents") or {}).items():
        for text in sentences:
            cases.append({"text": text, "expected_intent": intent})
    return cases


def _nlu_detect(client: Any, domain_id: str, version_id: str, text: str) -> dict[str, float]:
    response = client.request(
        "POST",
        f"/api/v2/languageunderstanding/domains/{domain_id}/versions/{version_id}/detect",
        json_body={"input": {"text": text}},
    )
    return {
        i["name"]: round(i.get("probability", 0.0), 3)
        for i in (response.get("output") or {}).get("intents") or []
        if i.get("name")
    }


def _score_nlu_case(
    probs: dict[str, float],
    expected_intent: str,
    min_confidence: float,
    min_margin: float,
) -> dict[str, Any]:
    real = {k: v for k, v in probs.items() if k != "None"}
    confidence = real.get(expected_intent, 0.0)
    others = {k: v for k, v in real.items() if k != expected_intent}
    runner_up_intent, runner_up_confidence = max(
        others.items(), key=lambda kv: kv[1], default=("-", 0.0)
    )
    none_confidence = probs.get("None", 0.0)
    margin = round(confidence - runner_up_confidence, 3)
    detected_intent, _ = max(real.items(), key=lambda kv: kv[1], default=("-", 0.0))

    if runner_up_confidence > confidence:
        verdict = "confused"
    elif confidence < min_confidence:
        verdict = "low_confidence"
    elif margin < NLU_TIGHT_MARGIN:
        verdict = "tight_margin"
    elif margin < min_margin:
        verdict = "below_margin"
    else:
        verdict = "confident"

    passed = (
        detected_intent == expected_intent
        and confidence >= min_confidence
        and margin >= min_margin
        and confidence > none_confidence
    )
    return {
        "detected_intent": detected_intent,
        "confidence": confidence,
        "margin": margin,
        "runner_up_intent": runner_up_intent,
        "runner_up_confidence": runner_up_confidence,
        "none_confidence": none_confidence,
        "verdict": verdict,
        "passed": passed,
    }


def _infer_catch_all_intent(intent_names: list[str]) -> str | None:
    for name in CATCH_ALL_INTENT_NAMES:
        if name in intent_names:
            return name
    return None


def _score_off_topic(
    probs: dict[str, float],
    use_case_intents: list[str],
    catch_all: str | None,
    copilot_threshold: float,
) -> dict[str, Any]:
    real = {k: v for k, v in probs.items() if k != "None"}
    top_intent, top_confidence = max(real.items(), key=lambda kv: kv[1], default=("-", 0.0))
    triggered = [
        {"intent": name, "confidence": round(probs.get(name, 0.0), 3)}
        for name in use_case_intents
        if probs.get(name, 0.0) >= copilot_threshold
    ]
    false_alarm = bool(triggered)
    if not false_alarm and top_confidence >= copilot_threshold:
        allowed = {catch_all} if catch_all else set()
        allowed.update(CATCH_ALL_INTENT_NAMES)
        if top_intent not in allowed:
            false_alarm = True
    return {
        "top_intent": top_intent,
        "top_confidence": top_confidence,
        "triggered_use_case_intents": triggered,
        "false_alarm": false_alarm,
        "passed": not false_alarm,
    }


class _IntentPlanningChoice(BaseModel):
    """Elicitation schema for the first-intent fork in set_copilot_intent."""

    intent_planning: Literal["from_demo_story", "self_defined"] = Field(
        description="How to build the initial intent set for this copilot",
    )


def _default_participant_roles() -> list[str]:
    return ["Agent", "Customer"]


def _rule_is_intent_triggered(entry: dict[str, Any]) -> bool:
    """Whether the rule fires on an utterance rather than on a lifecycle event.

    Only utterance-triggered rules can meaningfully be restricted to a speaker.
    A ConversationStart rule has no speaker, so ``participantRoles`` is null on
    it by design and must not be reported as a gap.
    """
    for condition in ((entry.get("rule") or {}).get("conditions") or []):
        if condition.get("conditionType") == "Intent":
            return True
    return False


def _rule_participant_roles_unrestricted(entry: dict[str, Any]) -> bool:
    if not _rule_is_intent_triggered(entry):
        return False
    roles = entry.get("participantRoles")
    if not roles:
        return True
    return sorted(roles) == sorted(_default_participant_roles())


def _has_checklist_rule(rules: list[dict[str, Any]]) -> bool:
    for entry in rules:
        for action in ((entry.get("rule") or {}).get("actions") or []):
            if action.get("actionType") == "Checklist":
                attrs = action.get("attributes") or {}
                if attrs.get("checklistId"):
                    return True
    return False


def _rule_intent_names(rules: list[dict[str, Any]]) -> list[str]:
    found: list[str] = []
    for entry in rules:
        for condition in ((entry.get("rule") or {}).get("conditions") or []):
            if condition.get("conditionType") == "Intent":
                found.extend(condition.get("conditionValues") or [])
    return found


def _unknown_intents_in_rules(rules: list[dict[str, Any]], live_intents: set[str]) -> list[str]:
    unknown = []
    for intent in _rule_intent_names(rules):
        if intent not in live_intents:
            unknown.append(intent)
    return sorted(set(unknown))


def _invalid_rule_actions(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for index, entry in enumerate(rules):
        intents = _rule_intent_names([entry])
        for action in ((entry.get("rule") or {}).get("actions") or []):
            action_type = action.get("actionType")
            attrs = action.get("attributes") or {}
            problem = None
            if action_type == "Script" and (
                not attrs.get("scriptId") or not attrs.get("pageId")
            ):
                problem = "script_action_missing_target"
            elif action_type == "Checklist" and not attrs.get("checklistId"):
                problem = "checklist_action_missing_target"
            elif action_type == "CannedResponse" and not attrs.get("responseId"):
                problem = "canned_response_action_missing_target"
            elif not action_type:
                problem = "action_missing_type"
            if problem:
                issues.append(
                    {
                        "rule_index": index,
                        "intents": intents,
                        "action_type": action_type,
                        "issue": problem,
                    }
                )
    return issues


def _searches_knowledge(config: dict[str, Any]) -> bool:
    """Whether any rule or the fallback performs a knowledge search."""
    rule_engine = config.get("ruleEngineConfig") or {}
    action_lists = [((rule_engine.get("fallback") or {}).get("actions")) or []]
    for entry in (rule_engine.get("rules") or []):
        action_lists.append(((entry.get("rule") or {}).get("actions")) or [])
    return any(
        action.get("actionType") == "KnowledgeSearch"
        for actions in action_lists
        for action in actions
    )


def _knowledge_readiness(
    config: dict[str, Any],
    knowledge_bases: list[dict[str, Any]],
) -> tuple[list[str], dict[str, Any]]:
    """Knowledge gaps visible from configuration, without running a search.

    Whether the confidence threshold is set sensibly cannot be answered here —
    that needs live scores from ``knowledge_health``. These are the failures
    that make the threshold irrelevant because nothing reaches it anyway.
    """
    gaps: list[str] = []
    details: dict[str, Any] = {}
    searches = _searches_knowledge(config)

    if searches and not knowledge_bases:
        gaps.append(READINESS_GAP_KNOWLEDGE_WITHOUT_BASE)
    if knowledge_bases and not searches:
        gaps.append(READINESS_GAP_KNOWLEDGE_BASE_UNUSED)

    unpublished = [kb["name"] for kb in knowledge_bases if kb.get("published") is False]
    if unpublished:
        gaps.append(READINESS_GAP_KNOWLEDGE_BASE_UNPUBLISHED)
        details["unpublished_knowledge_bases"] = unpublished

    empty = [kb["name"] for kb in knowledge_bases if kb.get("article_count") == 0]
    if empty:
        gaps.append(READINESS_GAP_KNOWLEDGE_BASE_EMPTY)
        details["empty_knowledge_bases"] = empty

    # Compare the base language of the assistant against each attachment; a
    # German base wired to an English copilot answers, just never usefully.
    assistant_language = (config.get("defaultLanguage") or "").split("-")[0].lower()
    if assistant_language:
        mismatched = [
            f"{kb['name']} ({kb.get('language')})"
            for kb in knowledge_bases
            if (kb.get("language") or "").split("-")[0].lower() not in ("", assistant_language)
        ]
        if mismatched:
            gaps.append(READINESS_GAP_KNOWLEDGE_LANGUAGE_MISMATCH)
            details["language_mismatched_knowledge_bases"] = mismatched

    return gaps, details


def _readiness_gaps_from_config(config: dict[str, Any]) -> list[str]:
    """Gaps derivable from the copilot configuration alone (cheap)."""
    gaps: list[str] = []
    if not config.get("enabled"):
        gaps.append(READINESS_GAP_COPILOT_DISABLED)

    nlu = config.get("nluConfig") or {}
    if not (nlu.get("domain") or {}).get("id"):
        gaps.append(READINESS_GAP_NO_NLU_DOMAIN)

    rules = ((config.get("ruleEngineConfig") or {}).get("rules")) or []
    if not rules:
        gaps.append(READINESS_GAP_NO_RULES)
    elif any(_rule_participant_roles_unrestricted(entry) for entry in rules):
        gaps.append(READINESS_GAP_UNRESTRICTED_ROLES)
    if not _has_checklist_rule(rules):
        gaps.append(READINESS_GAP_NO_CHECKLIST_RULE)

    summary = config.get("summaryGenerationConfig") or {}
    setting_id = (summary.get("summarySetting") or {}).get("id")
    if summary.get("enabled") and not setting_id:
        gaps.append(READINESS_GAP_SUMMARY_ENABLED_UNBOUND)
    elif not setting_id:
        gaps.append(READINESS_GAP_NO_SUMMARY_SETTING)

    return gaps


def _readiness_assessment(
    config: dict[str, Any],
    *,
    live_intent_names: list[str] | None = None,
    queue_entries: list[dict[str, Any]] | None = None,
    knowledge_bases: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Full readiness for one copilot; extra API data fills in the expensive checks."""
    gaps = _readiness_gaps_from_config(config)
    rules = ((config.get("ruleEngineConfig") or {}).get("rules")) or []
    details: dict[str, Any] = {
        "rule_count": len(rules),
        "intent_count": len(live_intent_names) if live_intent_names is not None else None,
        "queue_count": len(queue_entries) if queue_entries is not None else None,
    }

    if READINESS_GAP_UNRESTRICTED_ROLES in gaps:
        details["rules_without_restricted_roles"] = sorted(
            {
                intent
                for entry in rules
                if _rule_participant_roles_unrestricted(entry)
                for intent in _rule_intent_names([entry])
            }
        )

    if live_intent_names is not None:
        if not live_intent_names:
            if READINESS_GAP_NO_NLU_DOMAIN not in gaps:
                gaps.append(READINESS_GAP_NO_NLU_INTENTS)
        unknown = _unknown_intents_in_rules(rules, set(live_intent_names))
        if unknown:
            gaps.append(READINESS_GAP_UNKNOWN_INTENT_RULES)
            details["unknown_intent_rules"] = unknown

    invalid_actions = _invalid_rule_actions(rules)
    if invalid_actions:
        gaps.append(READINESS_GAP_INVALID_ACTIONS)
        details["invalid_actions"] = invalid_actions

    if knowledge_bases is not None:
        knowledge_gaps, knowledge_details = _knowledge_readiness(config, knowledge_bases)
        gaps.extend(knowledge_gaps)
        details.update(knowledge_details)
        details["knowledge_base_count"] = len(knowledge_bases)

    if queue_entries is not None:
        if not queue_entries:
            gaps.append(READINESS_GAP_NO_QUEUE)
        elif any(
            entry.get("assignmentMode") == "Manual" and not entry.get("assignedUsersCount")
            for entry in queue_entries
        ):
            gaps.append(READINESS_GAP_MANUAL_QUEUE_NO_USERS)
            details["manual_queues_without_users"] = [
                entry.get("id")
                for entry in queue_entries
                if entry.get("assignmentMode") == "Manual" and not entry.get("assignedUsersCount")
            ]

    return {"gaps": sorted(set(gaps)), "details": details}


def _copilot_snapshot(
    client: Any, assistant_id: str, assistant_record: dict[str, Any]
) -> tuple[bool, dict[str, Any] | None]:
    """Whether a copilot configuration exists, and the config when it does."""
    if assistant_record.get("copilotContext"):
        try:
            config = client.get(f"/api/v2/assistants/{assistant_id}/copilot") or {}
        except ApiError:
            return True, None
        return True, config
    try:
        config = client.get(f"/api/v2/assistants/{assistant_id}/copilot") or {}
    except ApiError:
        return False, None
    configured = bool(
        config.get("enabled")
        or (config.get("nluConfig") or {}).get("domain")
        or ((config.get("ruleEngineConfig") or {}).get("rules"))
    )
    return configured, config if configured else None


async def _elicit_intent_planning(ctx: Context | None) -> Literal["from_demo_story", "self_defined"] | None:
    """Ask how to bootstrap intents; None when elicitation is unavailable or declined."""
    if ctx is None:
        return None
    try:
        result = await asyncio.wait_for(
            ctx.elicit(
                message=(
                    "This copilot has no NLU intents yet. How should the initial intent set be built?"
                ),
                schema=_IntentPlanningChoice,
            ),
            timeout=ELICITATION_TIMEOUT_SECONDS,
        )
    except (asyncio.TimeoutError, ValueError, ApiError, ToolError, OSError):
        return None
    except Exception:
        return None

    if result.action != "accept" or result.data is None:
        return None
    return result.data.intent_planning


def _hint_measure_nlu(
    assistant: str,
    *,
    intent: str | None = None,
    strict: bool = True,
    extra: str = "",
) -> str:
    strict_flag = ", strict=True" if strict else ""
    intent_clause = ""
    if intent:
        intent_clause = (
            f" with cases like {{'text': '<typical customer phrase>', "
            f"'expected_intent': '{intent}'}}"
        )
    base = (
        f"Call measure_copilot_nlu(assistant={assistant!r}{intent_clause}{strict_flag}) "
        "using sentences from your demo or evaluation corpus before treating the change as done. "
        "Intents that win below the margin threshold (default 0.30, 0.40 in strict mode) "
        "are easily confused with the runner-up and will misfire in live conversations."
    )
    return f"{base} {extra}".strip()


def _utterance_texts(intent: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for utterance in intent.get("utterances") or []:
        segments = utterance.get("segments") or []
        text = "".join(segment.get("text") or "" for segment in segments)
        if text:
            texts.append(text)
    return texts


def _find_intent(intents: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for entry in intents:
        if entry.get("name") == name:
            return entry
    return None


def _rule_compare_key(entry: dict[str, Any]) -> dict[str, Any]:
    rule = entry.get("rule") or {}
    actions = []
    for action in rule.get("actions") or []:
        actions.append(
            {
                "actionType": action.get("actionType"),
                "attributes": dict(action.get("attributes") or {}),
            }
        )
    conditions = []
    for condition in rule.get("conditions") or []:
        conditions.append(
            {
                "conditionType": condition.get("conditionType"),
                "conditionValues": sorted(condition.get("conditionValues") or []),
            }
        )
    return {
        "enabled": entry.get("enabled"),
        "participantRoles": sorted(entry.get("participantRoles") or []),
        "conditions": conditions,
        "actions": actions,
    }


async def _confirmation_gate(
    ctx: Context | None,
    *,
    message: str,
    request_confirmation: bool,
    removes_configuration: bool = False,
) -> tuple[bool, bool | None, str | None]:
    """Return (proceed, accepted, unconfirmed_reason). proceed=False when declined.

    An unanswered prompt, or a client that cannot be asked at all, normally lets
    the write through: a tool that hangs on a question nobody sees is worse than
    one that acts. Where the write *removes* configuration that bargain turns
    around, because the cost of guessing wrong is asymmetric — a write that did
    not happen is repeated in seconds, a rule or queue assignment that quietly
    vanished is noticed days later, in a demo. So those callers pass
    ``removes_configuration`` and stop instead.
    """
    if not request_confirmation:
        return True, None, None
    outcome, reason = await elicit_write_confirmation(ctx, message)
    if outcome == "declined":
        return False, False, None
    if outcome == "confirmed":
        return True, True, None
    return not removes_configuration, False, reason


def _queue_user_rows(client: Any, assistant_id: str, queue_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in client.paginate(f"/api/v2/assistants/{assistant_id}/queues/{queue_id}/users"):
        user_id = entry.get("id")
        if not user_id:
            continue
        label = user_id
        try:
            user = client.get(f"/api/v2/users/{user_id}")
            label = user.get("email") or user.get("name") or user_id
        except ApiError:
            pass
        rows.append({"id": user_id, "label": label})
    return rows


def _checklist_items_signature(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": item.get("id"),
            "name": item.get("name"),
            "description": item.get("description"),
            "automatedCheckEnabled": item.get("automatedCheckEnabled"),
            "exactPhraseMatch": item.get("exactPhraseMatch"),
            "important": item.get("important"),
        }
        for item in items
    ]


def _hint_set_copilot_intent(
    assistant: str,
    intent: str,
    *,
    planning: Literal["from_demo_story", "self_defined"] | None = None,
) -> str:
    if planning == "from_demo_story":
        return (
            f"Derive the remaining intents and training utterances from your demo story or "
            f"script outline, then add each with set_copilot_intent(assistant={assistant!r}, "
            f"intent='<name>', utterances=[...]). "
            + _hint_measure_nlu(
                assistant,
                intent=intent,
                extra=(
                    "Pass corpus_file='<path-to-your-corpus.jsonc>' when you have a structured "
                    "demo corpus. After all intents pass, wire each with set_copilot_rule(...)."
                ),
            )
        )
    if planning == "self_defined":
        return (
            f"Add any further intents with set_copilot_intent(assistant={assistant!r}, "
            f"intent='<name>', utterances=[...]) — pass the full utterance list each time. "
            + _hint_measure_nlu(
                assistant,
                intent=intent,
                extra=(
                    f"Then add or update the rule with set_copilot_rule(assistant={assistant!r}, "
                    f"intent={intent!r}, action='Script'|'Checklist'|..., participant_roles=['Agent'] "
                    "when the intent is agent-facing)."
                ),
            )
        )
    return (
        f"This intent is live but unverified. "
        + _hint_measure_nlu(
            assistant,
            intent=intent,
            extra=(
                f"Then ensure a rule exists: set_copilot_rule(assistant={assistant!r}, "
                f"intent={intent!r}, action='Script'|'Checklist'|..., "
                "participant_roles=['Agent'] for agent-only triggers)."
            ),
        )
    )


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def list_copilots(name_contains: str | None = None) -> list[dict[str, Any]]:
        """List Agent Copilot assistants.

        Shared demo orgs accumulate these quickly — some hold well over fifty,
        many of them near-duplicates differing only by language suffix. Filter
        by name rather than reading the whole list.

        ``copilot_configured`` distinguishes an assistant that merely exists
        from one that actually has a copilot configuration behind it.

        ``readiness_gaps`` lists configuration gaps derivable from the copilot
        config alone (no NLU or queue roundtrips). Call ``get_copilot`` for the
        full readiness picture including intents, queues and broken rules.

        Args:
            name_contains: Case-insensitive substring of the assistant name.
        """
        client = get_client()
        results = []
        for item in client.paginate("/api/v2/assistants"):
            name = item.get("name") or ""
            if name_contains and name_contains.lower() not in name.lower():
                continue
            assistant_id = item.get("id")
            configured, config = _copilot_snapshot(client, assistant_id, item)
            entry: dict[str, Any] = {
                "id": assistant_id,
                "name": name,
                "state": item.get("state"),
                "copilot_configured": configured,
                "date_modified": item.get("dateModified"),
            }
            if config is not None:
                entry["readiness_gaps"] = sorted(set(_readiness_gaps_from_config(config)))
            elif configured:
                entry["readiness_gaps"] = []
            results.append(entry)
        return results

    @mcp.tool(annotations=READ_ONLY)
    def get_copilot(assistant: str) -> dict[str, Any]:
        """Fetch an assistant's Copilot settings by name or id.

        Combines the assistant record and its copilot configuration, which are
        two separate GETs. Covers the settings only - use ``list_copilot_rules``
        for the rule engine, ``get_copilot_nlu`` for the intents actually in
        force, and ``list_copilot_queues`` for where it is switched on.

        ``readiness`` reports configuration gaps derived from the live copilot
        config, NLU intents, queue assignments and attached knowledge — no
        measurement history is stored or inferred.

        ``knowledge_bases`` carries each attached base with its
        ``confidence_threshold``. That threshold decides whether a knowledge hit
        is shown to the agent at all, and it lives on the assistant record
        rather than the copilot config, so it is easy to miss when knowledge
        seems to return nothing.

        The knowledge checks here are the structural ones — a base that is
        empty, unpublished, in the wrong language, attached but never searched,
        or searched for without being attached. Whether the threshold itself is
        set sensibly cannot be answered from configuration: it depends on the
        scores the content actually produces. ``knowledge_health`` measures
        those and judges the threshold against them.

        Args:
            assistant: Assistant name or id, for example "Support DE".
        """
        client = get_client()
        assistant_id = _assistant_id(assistant)
        record = client.get(f"/api/v2/assistants/{assistant_id}")
        config = _copilot_config(assistant_id)

        nlu = config.get("nluConfig") or {}
        domain = nlu.get("domain") or {}
        summary = config.get("summaryGenerationConfig") or {}
        rules = (config.get("ruleEngineConfig") or {}).get("rules") or []

        live_intent_names: list[str] | None = None
        domain_id = domain.get("id")
        if domain_id:
            try:
                live = _latest_published_version(domain_id, with_utterances=False)
                live_intent_names = [
                    i.get("name") for i in (live.get("intents") or []) if i.get("name")
                ]
            except ToolError:
                live_intent_names = []

        queue_entries = list(client.paginate(f"/api/v2/assistants/{assistant_id}/queues"))

        attached_bases = ((record.get("knowledgeSuggestionConfig") or {}).get("knowledgeBases")) or []
        knowledge_bases: list[dict[str, Any]] = []
        if attached_bases:
            base_records = {
                kb.get("id"): kb
                for kb in client.paginate("/api/v2/knowledge/knowledgebases")
            }
            for kb in attached_bases:
                base = base_records.get(kb.get("id")) or {}
                knowledge_bases.append(
                    {
                        "id": kb.get("id"),
                        "name": base.get("name"),
                        "language": kb.get("languageCode"),
                        "confidence_threshold": kb.get("confidenceThreshold"),
                        "published": base.get("published"),
                        "article_count": (base.get("articleCount") or 0)
                        + (base.get("faqCount") or 0),
                    }
                )

        readiness = _readiness_assessment(
            config,
            live_intent_names=live_intent_names,
            queue_entries=queue_entries,
            knowledge_bases=knowledge_bases,
        )

        return {
            "id": assistant_id,
            "name": record.get("name"),
            "state": record.get("state"),
            "enabled": config.get("enabled"),
            "live_on_queue": config.get("liveOnQueue"),
            "default_language": config.get("defaultLanguage"),
            "nlu_engine": config.get("nluEngineType"),
            "nlu_domain_id": domain_id,
            "nlu_uses_latest_version": domain.get("useLatestVersion"),
            "intent_confidence_threshold": nlu.get("intentConfidenceThreshold"),
            "rule_count": len(rules),
            "summarisation_enabled": summary.get("enabled"),
            "summary_setting_id": (summary.get("summarySetting") or {}).get("id"),
            "wrapup_code_prediction": (config.get("wrapupCodePredictionConfig") or {}).get("enabled"),
            "voice_transcription_engine": (config.get("voiceTranscriptionConfig") or {}).get("engine"),
            "auto_search": (config.get("autoSearchConfig") or {}).get("type"),
            "knowledge_query_processing": (config.get("queryProcessingConfig") or {}).get(
                "knowledgeQueryProcessing"
            ),
            "knowledge_bases": knowledge_bases,
            "readiness": readiness,
        }

    @mcp.tool(annotations=READ_ONLY)
    def list_copilot_rules(assistant: str) -> dict[str, Any]:
        """List an assistant's rule engine rules, with ids resolved to names.

        Each rule maps a condition - usually an NLU intent, sometimes
        ConversationStart - to an action that surfaces something to the agent: a
        script page, an agent checklist, a canned response, a knowledge search.

        Two things are worth checking here and are easy to miss. A rule can name
        an intent that no longer exists in the published NLU version, in which
        case it simply never fires; compare against ``get_copilot_nlu``.
        And ``participant_roles`` decides who can trigger it - an agent-facing
        intent restricted to ``["Agent"]`` will never fire on customer speech.

        Args:
            assistant: Assistant name or id.
        """
        config = _copilot_config(_assistant_id(assistant))
        engine = config.get("ruleEngineConfig") or {}
        # Rules of one assistant nearly always point into the same script, so
        # resolving per rule would refetch it once per rule.
        cache: dict[str, dict[str, Any]] = {"scripts": {}, "pages": {}}

        rules = [
            {
                "enabled": entry.get("enabled"),
                "conditions": [
                    {
                        "type": condition.get("conditionType"),
                        "values": condition.get("conditionValues") or [],
                    }
                    for condition in ((entry.get("rule") or {}).get("conditions") or [])
                ],
                "actions": [
                    _describe_action(action, cache)
                    for action in ((entry.get("rule") or {}).get("actions") or [])
                ],
                "participant_roles": entry.get("participantRoles"),
            }
            for entry in (engine.get("rules") or [])
        ]

        fallback = engine.get("fallback") or {}
        return {
            "rules": rules,
            "fallback": {
                "enabled": fallback.get("enabled"),
                "actions": [_describe_action(a, cache) for a in (fallback.get("actions") or [])],
                "participant_roles": fallback.get("participantRoles"),
            },
        }

    @mcp.tool(annotations=READ_ONLY)
    def get_copilot_nlu(assistant: str, include_all_versions: bool = False) -> dict[str, Any]:
        """Show the NLU intents an assistant actually runs on.

        Answering "which intents are live" is not as direct as it looks. Every
        version of a domain reports ``published: true``, the list comes back
        unordered, and older versions keep their flag forever — some orgs have
        domains with ten versions that all claim to be published. The one in
        force is simply the most recently published, which is what this returns,
        provided the assistant's domain binding says ``useLatestVersion``.

        Because a new version replaces the whole intent list rather than merging
        into it, comparing the live intents against the previous version is the
        quickest way to spot intents that were dropped by accident.

        Args:
            assistant: Assistant name or id.
            include_all_versions: Also return the version history, newest first,
                with each version's intent names. Useful to see what a
                republish added or silently removed.
        """
        client = get_client()
        config = _copilot_config(_assistant_id(assistant))
        domain = ((config.get("nluConfig") or {}).get("domain")) or {}
        domain_id = domain.get("id")
        if not domain_id:
            raise ToolError(f"{assistant!r} has no NLU domain configured.")

        versions = [
            v
            for v in client.paginate(
                f"/api/v2/languageunderstanding/domains/{domain_id}/versions",
                params={"includeUtterances": "false"},
            )
            if v.get("published")
        ]
        if not versions:
            raise ToolError(f"NLU domain {domain_id} has no published version.")

        versions.sort(key=lambda v: v.get("datePublished") or "", reverse=True)
        live = versions[0]

        result: dict[str, Any] = {
            "domain_id": domain_id,
            "uses_latest_version": domain.get("useLatestVersion"),
            "live_version_id": live.get("id"),
            "language": live.get("language"),
            "date_published": live.get("datePublished"),
            "training_status": live.get("trainingStatus"),
            "published_version_count": len(versions),
            "intents": [
                {"name": i.get("name"), "description": i.get("description")}
                for i in (live.get("intents") or [])
            ],
        }
        if include_all_versions:
            result["versions"] = [
                {
                    "id": v.get("id"),
                    "date_published": v.get("datePublished"),
                    "intents": [i.get("name") for i in (v.get("intents") or [])],
                }
                for v in versions
            ]
        return result

    @mcp.tool(annotations=READ_ONLY)
    def measure_copilot_nlu(
        assistant: str,
        cases: list[dict[str, Any]] | None = None,
        corpus_file: str | None = None,
        off_topic: list[str] | None = None,
        use_case_intents: list[str] | None = None,
        catch_all_intent: str | None = None,
        min_confidence: float | None = None,
        min_margin: float | None = None,
        strict: bool = False,
    ) -> dict[str, Any]:
        """Measure Copilot NLU quality against test sentences — run after every intent or rule change.

        Call this after ``set_copilot_intent``, ``delete_copilot_intent`` or
        ``set_copilot_rule`` before treating the change as done. It posts each
        sentence to the live NLU domain's detect endpoint and reports whether
        the expected intent wins with enough confidence and separation from the
        runner-up. Optionally checks off-topic sentences for false alarms on
        use-case intents.

        Pass ``cases`` inline (typical for a quick check after one training
        round) or ``corpus_file`` pointing at a JSON/JSONC corpus in the
        nlu-probe format (top-level key = assistant name, sections
        ``messpunkte``, ``intents``, ``ausserhalb``, ``negativ``). At least one
        of ``cases`` or ``corpus_file`` is required unless only ``off_topic`` is
        given.

        Default pass thresholds match the NLU probe stand: confidence ≥ 0.50
        and margin ≥ 0.30. ``strict=True`` raises them to 0.70 / 0.40 for demo
        script lines. ``min_confidence`` and ``min_margin`` override either mode.

        Args:
            assistant: Copilot assistant name or id, e.g. "Support DE".
            cases: Inline test rows, each with ``text`` and ``expected_intent``.
                Optional ``role`` (Agent/Customer) is echoed back for script lines.
            corpus_file: Path to a corpus JSON/JSONC file. The section for
                ``assistant`` is merged with any inline ``cases``.
            off_topic: Sentences outside the use case that should not trigger
                use-case intents. Loaded from corpus ``ausserhalb`` when omitted.
            use_case_intents: Intent names treated as in-scope for false-alarm
                checks. Defaults to all live intents except the catch-all.
            catch_all_intent: Catch-all intent name (``Sonstiges`` / ``Other``).
                Auto-detected from live intents when omitted.
            min_confidence: Minimum probability on the expected intent to pass.
            min_margin: Minimum gap between expected intent and runner-up to pass.
            strict: Demo thresholds (0.70 confidence, 0.40 margin).
        """
        if not cases and not corpus_file and not off_topic:
            raise ToolError("Pass cases, corpus_file, or off_topic — nothing to measure.")

        resolved_conf, resolved_margin = _nlu_pass_thresholds(
            strict, min_confidence, min_margin
        )
        inline_cases: list[dict[str, Any]] = list(cases or [])
        corpus_off_topic: list[str] = []
        negativ_cases: list[dict[str, Any]] = []

        if corpus_file:
            section = _load_nlu_corpus_section(corpus_file, assistant)
            inline_cases.extend(_cases_from_corpus_section(section))
            corpus_off_topic = list(section.get("ausserhalb") or [])
            for key in ("negativ", "negativ_adresse"):
                negativ_cases.extend(section.get(key) or [])

        off_topic_sentences = list(off_topic or corpus_off_topic)

        client = get_client()
        assistant_id = _assistant_id(assistant)
        config = _copilot_config(assistant_id)
        copilot_threshold = ((config.get("nluConfig") or {}).get("intentConfidenceThreshold")) or 0.0

        domain_id = (((config.get("nluConfig") or {}).get("domain")) or {}).get("id")
        if not domain_id:
            raise ToolError(f"{assistant!r} has no NLU domain configured.")

        live = _latest_published_version(domain_id, with_utterances=False)
        version_id = live["id"]
        live_intent_names = [i.get("name") for i in (live.get("intents") or []) if i.get("name")]

        catch_all = catch_all_intent or _infer_catch_all_intent(live_intent_names)
        in_scope = use_case_intents or [
            name for name in live_intent_names if name not in CATCH_ALL_INTENT_NAMES
        ]

        measured: list[dict[str, Any]] = []
        for row in inline_cases:
            text = row.get("text")
            expected = row.get("expected_intent")
            if not text or not expected:
                raise ToolError("Each case needs text and expected_intent.")
            probs = _nlu_detect(client, domain_id, version_id, text)
            scored = _score_nlu_case(probs, expected, resolved_conf, resolved_margin)
            entry: dict[str, Any] = {
                "text": text,
                "expected_intent": expected,
                "detected_intent": scored["detected_intent"],
                "confidence": scored["confidence"],
                "margin": scored["margin"],
                "runner_up_intent": scored["runner_up_intent"],
                "runner_up_confidence": scored["runner_up_confidence"],
                "verdict": scored["verdict"],
                "passed": scored["passed"],
            }
            if row.get("role"):
                entry["role"] = row["role"]
            measured.append(entry)

        off_topic_results: list[dict[str, Any]] = []
        false_alarms: list[dict[str, Any]] = []
        for text in off_topic_sentences:
            probs = _nlu_detect(client, domain_id, version_id, text)
            check = _score_off_topic(
                probs, in_scope, catch_all, copilot_threshold
            )
            row_result = {"text": text, **check}
            off_topic_results.append(row_result)
            if check["false_alarm"]:
                false_alarms.append(row_result)

        negativ_results: list[dict[str, Any]] = []
        for entry in negativ_cases:
            text = entry.get("text")
            forbidden = entry.get("soll_nicht") or entry.get("forbidden_intent")
            if not text or not forbidden:
                raise ToolError("Each negativ entry needs text and soll_nicht/forbidden_intent.")
            probs = _nlu_detect(client, domain_id, version_id, text)
            prob_forbidden = round(probs.get(forbidden, 0.0), 3)
            triggered = prob_forbidden >= copilot_threshold
            negativ_results.append(
                {
                    "text": text,
                    "forbidden_intent": forbidden,
                    "confidence": prob_forbidden,
                    "copilot_threshold": copilot_threshold,
                    "passed": not triggered,
                    "false_alarm": triggered,
                }
            )
            if triggered:
                false_alarms.append(
                    {
                        "text": text,
                        "kind": "negativ",
                        "triggered_intent": forbidden,
                        "confidence": prob_forbidden,
                        "copilot_threshold": copilot_threshold,
                    }
                )

        passed_cases = sum(1 for c in measured if c["passed"])
        total_cases = len(measured)
        below_conf = sum(1 for c in measured if c["confidence"] < resolved_conf)
        below_margin = sum(1 for c in measured if c["margin"] < resolved_margin)
        hit_rate = round(passed_cases / total_cases, 3) if total_cases else None

        weakest = sorted(
            [c for c in measured if not c["passed"]],
            key=lambda c: (c["margin"], c["confidence"]),
        )[:10]
        if len(weakest) < 10:
            borderline = sorted(measured, key=lambda c: (c["margin"], c["confidence"]))[:10]
            seen = {c["text"] for c in weakest}
            for c in borderline:
                if c["text"] not in seen:
                    weakest.append(c)
                if len(weakest) >= 10:
                    break

        negativ_failed = sum(1 for n in negativ_results if not n["passed"])
        all_passed = (
            (total_cases == 0 or passed_cases == total_cases)
            and not false_alarms
            and negativ_failed == 0
        )

        verdict_parts: list[str] = []
        if total_cases:
            if passed_cases == total_cases:
                verdict_parts.append(f"{passed_cases}/{total_cases} cases passed")
            else:
                failed_count = total_cases - passed_cases
                verdict_parts.append(f"{failed_count}/{total_cases} cases failed")
        if false_alarms:
            verdict_parts.append(f"{len(false_alarms)} false alarm(s)")
        if negativ_failed:
            verdict_parts.append(f"{negativ_failed} negativ case(s) triggered forbidden intent")

        if all_passed:
            verdict = "PASSED" + (f": {'; '.join(verdict_parts)}" if verdict_parts else "")
        else:
            reasons: list[str] = []
            for c in weakest:
                if not c["passed"]:
                    snippet = c["text"][:60] + ("..." if len(c["text"]) > 60 else "")
                    reasons.append(
                        f"{c['expected_intent']!r} ({c['confidence']:.2f}, margin {c['margin']:+.2f}): "
                        f"{snippet!r}"
                    )
                    if len(reasons) >= 5:
                        break
            for alarm in false_alarms[:3]:
                if alarm.get("kind") == "negativ":
                    reasons.append(
                        f"negativ {alarm['triggered_intent']!r} {alarm['confidence']:.2f}: "
                        f"{alarm['text'][:50]!r}"
                    )
                else:
                    reasons.append(
                        f"off-topic {alarm.get('triggered_intent', alarm.get('top_intent'))!r} "
                        f"{alarm.get('confidence', alarm.get('top_confidence', 0)):.2f}: "
                        f"{alarm['text'][:50]!r}"
                    )
            verdict = "FAILED: " + "; ".join(verdict_parts + reasons[:5])

        return {
            "assistant": assistant,
            "domain_id": domain_id,
            "live_version_id": version_id,
            "copilot_threshold": copilot_threshold,
            "thresholds": {
                "min_confidence": resolved_conf,
                "min_margin": resolved_margin,
                "strict": strict,
            },
            "passed": all_passed,
            "verdict": verdict,
            "summary": {
                "total_cases": total_cases,
                "passed_cases": passed_cases,
                "hit_rate": hit_rate,
                "below_confidence_threshold": below_conf,
                "below_margin_threshold": below_margin,
                "off_topic_total": len(off_topic_results),
                "off_topic_false_alarms": len([r for r in off_topic_results if r["false_alarm"]]),
                "negativ_total": len(negativ_results),
                "negativ_failed": negativ_failed,
            },
            "cases": measured,
            "weakest_cases": weakest[:10],
            "off_topic": off_topic_results,
            "negativ": negativ_results,
            "false_alarms": false_alarms,
        }

    @mcp.tool(annotations=READ_ONLY)
    def list_copilot_queues(assistant: str) -> list[dict[str, Any]]:
        """List the queues an assistant is switched on for, by queue name.

        The API returns queue ids only, so a raw call leaves you unable to tell
        which queue is meant without a second lookup per entry; the names are
        resolved here.

        ``assignment_mode`` is the one to read carefully: with "Manual" the
        copilot only reaches the users explicitly assigned to that queue, so a
        queue can appear here and still leave most of its agents without it.

        Args:
            assistant: Assistant name or id.
        """
        client = get_client()
        assistant_id = _assistant_id(assistant)
        results = []
        for entry in client.paginate(f"/api/v2/assistants/{assistant_id}/queues"):
            queue_id = entry.get("id")
            results.append(
                {
                    "queue_id": queue_id,
                    "queue_name": _queue_name(queue_id),
                    "media_types": entry.get("mediaTypes"),
                    "assignment_mode": entry.get("assignmentMode"),
                    "users_assigned": entry.get("usersAssigned"),
                    "assigned_users_count": entry.get("assignedUsersCount"),
                }
            )
        return results

    @mcp.tool(annotations=WRITES)
    async def set_copilot_rule(
        assistant: str,
        intent: str,
        action: str,
        script: str | None = None,
        page: str | None = None,
        checklist: str | None = None,
        response_id: str | None = None,
        participant_roles: list[str] | None = None,
        enabled: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add or replace the rule that fires on an intent.

        Keyed on the intent, so calling it twice with the same intent updates
        the rule rather than adding a second one that shadows the first.

        Every other rule and every unrelated setting is carried over untouched,
        because the API only accepts the whole configuration at once. The result
        is read back and checked, since a PUT that quietly dropped the rule
        would otherwise look like a success.

        Args:
            assistant: Assistant name or id.
            intent: NLU intent name the rule fires on. It is not required to
                exist yet, but the answer reports whether it does - a rule on an
                intent absent from the published NLU version never fires, and
                that mismatch is the most common reason a copilot looks dead.
            action: "Script", "Checklist", "CannedResponse" or "KnowledgeArticle".
            script: Script name or id, for a Script action.
            page: Page name or id within that script.
            checklist: Agent checklist name or id, for a Checklist action.
            response_id: Canned response id, for a CannedResponse action.
                An id rather than a name: responses are only searchable within a
                library, so there is no unambiguous org-wide name to resolve.
            participant_roles: Who may trigger it, "Agent" and/or "Customer".
                Defaults to both. An agent-facing intent belongs on ["Agent"].
            enabled: Whether the rule is active.
        """
        assistant_id = _assistant_id(assistant)
        attributes = _action_attributes(action, script, page, checklist, response_id)
        new_rule = {
            "enabled": enabled,
            "rule": {
                "conditions": [{"conditionType": "Intent", "conditionValues": [intent]}],
                "actions": [{"actionType": action, "attributes": attributes}],
            },
            "participantRoles": participant_roles or ["Agent", "Customer"],
        }

        config = _copilot_config(assistant_id)
        rules = list(((config.get("ruleEngineConfig") or {}).get("rules")) or [])
        existing = _index_of_intent_rule(rules, intent)
        cache: dict[str, dict[str, Any]] = {"scripts": {}, "pages": {}}
        previous_rule = rules[existing] if existing is not None else None
        replacing = previous_rule is not None

        if replacing and _rule_compare_key(previous_rule) == _rule_compare_key(new_rule):
            impact = make_write_impact(
                action="skipped_no_change",
                summary=(
                    f"Rule on intent {intent!r} already matches the requested action "
                    f"({action}), targets and participant roles."
                ),
                diff={"intent": intent, "action": action},
            )
            return attach_write_impact(
                {
                    "assistant_id": assistant_id,
                    "intent": intent,
                    "replaced_existing": False,
                    "rule_count": len(rules),
                    "intent_in_live_nlu": _intent_is_live(config, intent),
                    NEXT_STEP_HINT: (
                        f"No change was written. Confirm with list_copilot_rules(assistant={assistant!r})."
                    ),
                },
                impact,
            )

        previous_summary: dict[str, Any] | None = None
        if previous_rule is not None:
            prev_actions = [
                _describe_action(a, cache)
                for a in ((previous_rule.get("rule") or {}).get("actions") or [])
            ]
            new_actions = [
                _describe_action(a, cache)
                for a in ((new_rule.get("rule") or {}).get("actions") or [])
            ]
            previous_summary = {
                "enabled": previous_rule.get("enabled"),
                "participant_roles": previous_rule.get("participantRoles"),
                "actions": prev_actions,
            }
            diff = {
                "intent": intent,
                "before": previous_summary,
                "after": {
                    "enabled": new_rule.get("enabled"),
                    "participant_roles": new_rule.get("participantRoles"),
                    "actions": new_actions,
                },
            }
            before_target = prev_actions[0] if prev_actions else {}
            after_target = new_actions[0] if new_actions else {}
            confirm_message = (
                f"Intent {intent!r} already has a rule pointing to {before_target!r}. "
                f"This call would replace it with {after_target!r} "
                f"(participant roles {previous_rule.get('participantRoles')} → "
                f"{new_rule.get('participantRoles')}). Apply or cancel?"
            )
        else:
            diff = {
                "intent": intent,
                "action": action,
                "new_target": _describe_action(
                    {"actionType": action, "attributes": attributes}, cache
                ),
            }
            confirm_message = ""

        proceed, accepted, unconfirmed = await _confirmation_gate(
            ctx,
            message=confirm_message,
            request_confirmation=replacing,
        )
        if not proceed:
            impact = make_write_impact(
                action="cancelled",
                summary=f"Rule on intent {intent!r} was not changed — confirmation declined.",
                diff=diff,
                previous_state=previous_summary,
                confirmation_requested=True,
                confirmation_accepted=False,
            )
            return attach_write_impact(
                {
                    "assistant_id": assistant_id,
                    "intent": intent,
                    "replaced_existing": False,
                    "rule_count": len(rules),
                    NEXT_STEP_HINT: "No change was written.",
                },
                impact,
            )

        if existing is None:
            rules.append(new_rule)
        else:
            rules[existing] = new_rule

        updated = _put_rules(assistant_id, config, rules)
        live = _index_of_intent_rule(
            ((updated.get("ruleEngineConfig") or {}).get("rules")) or [], intent
        )
        if live is None:
            raise ToolError(
                f"The PUT succeeded but no rule for intent {intent!r} came back. "
                "The configuration was not changed the way it was asked for."
            )

        summary = (
            f"Replaced rule on intent {intent!r}."
            if replacing
            else f"Added rule on intent {intent!r} ({action})."
        )
        impact = make_write_impact(
            action="applied",
            summary=summary,
            diff=diff,
            previous_state=previous_summary,
            confirmation_requested=replacing,
            confirmation_accepted=accepted,
            unconfirmed_reason=unconfirmed,
        )
        return attach_write_impact(
            {
                "assistant_id": assistant_id,
                "intent": intent,
                "replaced_existing": replacing,
                "rule_count": len(rules),
                "intent_in_live_nlu": _intent_is_live(config, intent),
                NEXT_STEP_HINT: (
                    f"The rule is saved but untested. "
                    + _hint_measure_nlu(
                        assistant,
                        intent=intent,
                        extra=(
                            f"If participant_roles was left at the default ['Agent', 'Customer'], "
                            f"agent utterances during wrap-up can trigger this rule — re-run with "
                            f"participant_roles=['Agent'] or ['Customer'] when the intent is "
                            f"one-sided. Confirm wiring with list_copilot_rules(assistant={assistant!r}). "
                            f"If the copilot is not live yet, assign_copilot_queue(assistant={assistant!r}, "
                            f"queue='<queue name>') switches it on for agents."
                        ),
                    )
                ),
            },
            impact,
        )

    @mcp.tool(annotations=DESTRUCTIVE)
    async def delete_copilot_rule(assistant: str, intent: str, ctx: Context | None = None) -> dict[str, Any]:
        """Remove the rule that fires on an intent.

        Removes the rule only. The intent stays in the NLU domain, so the
        copilot keeps recognising it and simply stops acting on it.

        Args:
            assistant: Assistant name or id.
            intent: NLU intent name whose rule should go.
        """
        assistant_id = _assistant_id(assistant)
        config = _copilot_config(assistant_id)
        rules = list(((config.get("ruleEngineConfig") or {}).get("rules")) or [])

        index = _index_of_intent_rule(rules, intent)
        if index is None:
            raise ToolError(
                f"No rule on intent {intent!r}. Existing rules fire on: "
                f"{', '.join(_intents_of(rules)) or 'no intent at all'}."
            )

        cache: dict[str, dict[str, Any]] = {"scripts": {}, "pages": {}}
        removed_rule = rules[index]
        removed_summary = {
            "enabled": removed_rule.get("enabled"),
            "participant_roles": removed_rule.get("participantRoles"),
            "actions": [
                _describe_action(a, cache)
                for a in ((removed_rule.get("rule") or {}).get("actions") or [])
            ],
        }
        diff = {"intent": intent, "rule_removed": removed_summary}
        confirm_message = (
            f"Remove the rule on intent {intent!r}? It currently surfaces "
            f"{removed_summary['actions']!r} to participants "
            f"{removed_rule.get('participantRoles')!r}. The NLU intent stays — only the action wiring goes."
        )

        proceed, accepted, unconfirmed = await _confirmation_gate(
            ctx,
            message=confirm_message,
            request_confirmation=True,
            removes_configuration=True,
        )
        if not proceed:
            impact = make_write_impact(
                action="cancelled",
                summary=(
                    f"Rule on intent {intent!r} was not removed — "
                    f"{confirmation_block_reason(unconfirmed)}."
                ),
                diff=diff,
                previous_state={"rule": removed_rule},
                confirmation_requested=True,
                confirmation_accepted=False,
                unconfirmed_reason=unconfirmed,
            )
            return attach_write_impact(
                {
                    "assistant_id": assistant_id,
                    "intent": intent,
                    "rule_count": len(rules),
                    NEXT_STEP_HINT: "No change was written.",
                },
                impact,
            )

        rules.pop(index)

        updated = _put_rules(assistant_id, config, rules)
        remaining = ((updated.get("ruleEngineConfig") or {}).get("rules")) or []
        if _index_of_intent_rule(remaining, intent) is not None:
            raise ToolError(f"The rule on {intent!r} is still there after the PUT.")

        impact = make_write_impact(
            action="applied",
            summary=f"Removed rule on intent {intent!r} ({removed_summary['actions']!r}).",
            diff=diff,
            previous_state={"rule": removed_rule},
            confirmation_requested=True,
            confirmation_accepted=accepted,
            unconfirmed_reason=unconfirmed,
        )
        return attach_write_impact(
            {
                "assistant_id": assistant_id,
                "intent": intent,
                "rule_count": len(remaining),
                NEXT_STEP_HINT: (
                    f"The intent {intent!r} may still exist in NLU and keep firing without an action. "
                    f"Call get_copilot_nlu(assistant={assistant!r}) to confirm, then either "
                    f"delete_copilot_intent(assistant={assistant!r}, intent={intent!r}) to drop it or "
                    f"set_copilot_rule(assistant={assistant!r}, intent={intent!r}, ...) to replace the "
                    f"rule. Run measure_copilot_nlu(assistant={assistant!r}, strict=True) on your demo "
                    f"sentences so removed rules did not leave recognition gaps."
                ),
            },
            impact,
        )

    @mcp.tool(annotations=WRITES)
    async def set_copilot_intent(
        assistant: str,
        intent: str,
        utterances: list[str],
        description: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add or retrain one NLU intent, keeping every other intent intact.

        There is no endpoint that edits an intent. A domain is changed by
        publishing a whole new version, and that version becomes the entire
        intent list - anything left out is gone. This carries the published
        intents forward and changes only the named one.

        Doing that by hand has cost training data before, because the trap is
        doubled: reading the current version does not return utterances unless
        asked, so intents copied forward from a default read arrive empty and
        publish away every phrase they were trained on. This always reads with
        utterances included.

        Runs the full sequence - create version, train, wait, publish - and
        verifies the result, because an untrained or unpublished version changes
        nothing while looking like it worked.

        When the copilot's NLU domain has no intents yet, the tool may ask
        (via MCP elicitation) whether to derive intents from a demo story or
        define them manually. If elicitation is unavailable or declined, it
        completes normally with a general next-step hint.

        Args:
            assistant: Assistant name or id.
            intent: Intent name. An existing one is retrained with exactly the
                utterances given here, so pass the full set rather than
                additions only.
            utterances: Training phrases. Fewer than a handful trains badly.
            description: Optional intent description.
        """
        if not utterances:
            raise ToolError("An intent needs utterances to train on.")

        domain_id, live = _live_nlu_version(_assistant_id(assistant))
        intents = _carry_forward(live.get("intents") or [])
        had_no_intents = len(intents) == 0
        planning: Literal["from_demo_story", "self_defined"] | None = None
        if had_no_intents:
            planning = await _elicit_intent_planning(ctx)

        existing_intent = _find_intent(intents, intent)
        replaced = existing_intent is not None
        before_utterances = _utterance_texts(existing_intent) if existing_intent else []
        before_description = (existing_intent or {}).get("description")
        after_utterances = list(utterances)
        utterance_diff = list_set_diff(before_utterances, after_utterances)
        description_changed = (description or None) != (before_description or None)

        if replaced and not description_changed and not utterance_diff["removed_all"] and not utterance_diff["added_all"]:
            impact = make_write_impact(
                action="skipped_no_change",
                summary=(
                    f"Intent {intent!r} already has the same {len(after_utterances)} training "
                    "utterances and description — no NLU version was published."
                ),
                diff={
                    "intent": intent,
                    "utterances": utterance_diff,
                    "description_changed": False,
                },
                previous_state={
                    "utterances": before_utterances,
                    "description": before_description,
                },
            )
            return attach_write_impact(
                {
                    "domain_id": domain_id,
                    "intent": intent,
                    "replaced_existing": True,
                    "utterance_count": len(after_utterances),
                    "intents_now_live": sorted(i["name"] for i in intents),
                    NEXT_STEP_HINT: _hint_set_copilot_intent(assistant, intent, planning=planning),
                },
                impact,
            )

        diff: dict[str, Any] = {
            "intent": intent,
            "utterances": utterance_diff,
            "description_changed": description_changed,
            "replaced_existing": replaced,
        }
        previous_state: dict[str, Any] | None = None
        confirm_message = ""
        request_confirmation = replaced and bool(utterance_diff["removed_all"])

        if replaced:
            previous_state = {
                "utterances": before_utterances,
                "description": before_description,
            }
            removed = utterance_diff["removed_all"]
            if request_confirmation:
                sample = format_sample_list(removed)
                confirm_message = (
                    f"Intent {intent!r} currently has {utterance_diff['before_count']} training "
                    f"utterances; this call would replace them with {utterance_diff['after_count']} "
                    f"and permanently drop {len(removed)}"
                )
                if sample:
                    confirm_message += f", including: {sample}"
                confirm_message += ". Apply or cancel?"
            else:
                diff["note"] = "Retraining adds utterances only; none removed."

        proceed, accepted, unconfirmed = await _confirmation_gate(
            ctx,
            message=confirm_message,
            request_confirmation=request_confirmation,
        )
        if not proceed:
            impact = make_write_impact(
                action="cancelled",
                summary=f"Intent {intent!r} was not retrained — confirmation declined.",
                diff=diff,
                previous_state=previous_state,
                confirmation_requested=True,
                confirmation_accepted=False,
            )
            return attach_write_impact(
                {
                    "domain_id": domain_id,
                    "intent": intent,
                    "replaced_existing": replaced,
                    "utterance_count": len(after_utterances),
                    NEXT_STEP_HINT: "No NLU version was published.",
                },
                impact,
            )

        entry: dict[str, Any] = {
            "name": intent,
            "utterances": [{"source": "User", "segments": [{"text": u}]} for u in utterances],
        }
        if description:
            entry["description"] = description
        intents = [i for i in intents if i["name"] != intent] + [entry]

        published = _publish_nlu_version(domain_id, live, intents)
        summary = (
            f"Retrained intent {intent!r}: {utterance_diff['before_count']} → "
            f"{utterance_diff['after_count']} utterances"
            + (f" ({len(utterance_diff['removed_all'])} removed)" if utterance_diff["removed_all"] else "")
            + "."
            if replaced
            else f"Added intent {intent!r} with {len(after_utterances)} training utterances."
        )
        impact = make_write_impact(
            action="applied",
            summary=summary,
            diff=diff,
            previous_state=previous_state,
            confirmation_requested=request_confirmation,
            confirmation_accepted=accepted,
            unconfirmed_reason=unconfirmed,
        )
        return attach_write_impact(
            {
                "domain_id": domain_id,
                "intent": intent,
                "replaced_existing": replaced,
                "utterance_count": len(utterances),
                "intents_now_live": sorted(i["name"] for i in intents),
                "version_id": published["id"],
                "date_published": published.get("datePublished"),
                NEXT_STEP_HINT: _hint_set_copilot_intent(assistant, intent, planning=planning),
            },
            impact,
        )

    @mcp.tool(annotations=DESTRUCTIVE)
    async def delete_copilot_intent(assistant: str, intent: str, ctx: Context | None = None) -> dict[str, Any]:
        """Remove an NLU intent by publishing a version without it.

        Its utterances go with it and are not recoverable from the new version,
        though the previous version keeps them and stays readable through
        ``get_copilot_nlu`` with ``include_all_versions``.

        Any copilot rule on this intent is left alone and simply stops firing;
        ``delete_copilot_rule`` removes that too.

        Args:
            assistant: Assistant name or id.
            intent: Intent name to drop.
        """
        assistant_id = _assistant_id(assistant)
        domain_id, live = _live_nlu_version(assistant_id)
        intents = _carry_forward(live.get("intents") or [])
        target = _find_intent(intents, intent)
        remaining = [i for i in intents if i["name"] != intent]
        if target is None:
            raise ToolError(
                f"No intent {intent!r} in the published version. It has: "
                f"{', '.join(sorted(i['name'] for i in intents)) or 'no intents at all'}."
            )
        if not remaining:
            raise ToolError(
                f"{intent!r} is the only intent in the domain, and a version with none "
                "cannot be trained. Delete the assistant's NLU binding instead."
            )

        utterance_count = len(_utterance_texts(target))
        config = _copilot_config(assistant_id)
        rules = ((config.get("ruleEngineConfig") or {}).get("rules")) or []
        has_rule = _index_of_intent_rule(rules, intent) is not None
        diff = {
            "intent": intent,
            "utterances_lost": utterance_count,
            "intents_after": sorted(i["name"] for i in remaining),
            "orphaned_rule_remains": has_rule,
        }
        previous_state = {
            "utterances": _utterance_texts(target),
            "description": target.get("description"),
        }
        confirm_message = (
            f"Remove NLU intent {intent!r} ({utterance_count} training utterances)? "
            f"Live intents afterward: {', '.join(diff['intents_after'])}."
        )
        if has_rule:
            confirm_message += f" A copilot rule on {intent!r} would be left orphaned."

        proceed, accepted, unconfirmed = await _confirmation_gate(
            ctx,
            message=confirm_message,
            request_confirmation=True,
            removes_configuration=True,
        )
        if not proceed:
            impact = make_write_impact(
                action="cancelled",
                summary=(
                    f"Intent {intent!r} was not removed — "
                    f"{confirmation_block_reason(unconfirmed)}."
                ),
                diff=diff,
                previous_state=previous_state,
                confirmation_requested=True,
                confirmation_accepted=False,
                unconfirmed_reason=unconfirmed,
            )
            return attach_write_impact(
                {
                    "domain_id": domain_id,
                    "intent": intent,
                    NEXT_STEP_HINT: "No NLU version was published.",
                },
                impact,
            )

        published = _publish_nlu_version(domain_id, live, remaining)
        impact = make_write_impact(
            action="applied",
            summary=(
                f"Removed intent {intent!r} ({utterance_count} training utterances). "
                f"{len(remaining)} intent(s) remain live."
            ),
            diff=diff,
            previous_state=previous_state,
            confirmation_requested=True,
            confirmation_accepted=accepted,
            unconfirmed_reason=unconfirmed,
        )
        return attach_write_impact(
            {
                "domain_id": domain_id,
                "intent": intent,
                "intents_now_live": sorted(i["name"] for i in remaining),
                "version_id": published["id"],
                "date_published": published.get("datePublished"),
                NEXT_STEP_HINT: (
                    f"Remove the orphaned rule with delete_copilot_rule(assistant={assistant!r}, "
                    f"intent={intent!r}) if one still exists — list_copilot_rules(assistant={assistant!r}) "
                    f"shows stale intent rules. "
                    + _hint_measure_nlu(
                        assistant,
                        extra=(
                            "Confirm remaining intents still pass on your demo sentences and that "
                            "off-topic lines no longer false-trigger the removed intent."
                        ),
                    )
                ),
            },
            impact,
        )

    @mcp.tool(annotations=WRITES)
    async def assign_copilot_queue(
        assistant: str,
        queue: str,
        media_types: list[str] | None = None,
        assignment_mode: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Switch a copilot on for a routing queue.

        Assigning the queue is only half of it. Which agents get the copilot
        depends on the assignment mode. A new assignment is always "Auto", where
        every member of the queue gets it. Under "Manual" only the users
        explicitly added receive it, so such a queue reads as assigned and still
        reaches nobody until ``set_copilot_queue_users`` has run.

        The mode is not a field on the assignment, even though the object shows
        one and the PUT accepts ``assignmentMode`` in the body. It is changed by
        a job, so passing it here runs that job and waits for it. Setting it
        through the body alone looks like it worked and leaves the queue on
        "Auto", which then refuses every per-user change.

        Args:
            assistant: Assistant name or id.
            queue: Routing queue name or id.
            media_types: Which media the copilot runs on. Defaults to Call,
                Message and Email.
            assignment_mode: "Auto" or "Manual". Unset keeps whatever is there.
        """
        client = get_client()
        assistant_id = await _assistant_id_async(assistant, ctx)
        queue_id = await _routing_queue_id_async(queue, ctx)

        client.request(
            "PUT",
            f"/api/v2/assistants/{assistant_id}/queues/{queue_id}",
            json_body={
                "id": queue_id,
                "mediaTypes": media_types or ["Call", "Message", "Email"],
            },
        )
        if assignment_mode:
            _run_assignment_mode_job(assistant_id, queue_id, assignment_mode)

        live = client.get(f"/api/v2/assistants/{assistant_id}/queues/{queue_id}")
        if assignment_mode and live.get("assignmentMode") != assignment_mode:
            raise ToolError(
                f"Asked for {assignment_mode} assignment but the queue is on "
                f"{live.get('assignmentMode')}."
            )
        mode = live.get("assignmentMode")
        queue_name = _queue_name(queue_id)
        if mode == "Manual":
            hint = (
                f"Queue {queue_name!r} is on Manual assignment — agents will not receive the "
                f"copilot until you call set_copilot_queue_users(assistant={assistant!r}, "
                f"queue={queue_name!r}, add=['<agent email>', ...]). "
                f"Then verify with list_copilot_queues(assistant={assistant!r}) and run an "
                f"end-to-end test conversation on that queue."
            )
        else:
            hint = (
                f"The copilot is live for queue members on {queue_name!r}. Call "
                f"get_copilot(assistant={assistant!r}) and resolve any readiness gaps "
                f"(missing rules, summary setting, unrestricted participant_roles) before demoing. "
                f"Run measure_copilot_nlu(assistant={assistant!r}, strict=True) on your script "
                f"lines if intents or rules changed recently."
            )
        return {
            "assistant_id": assistant_id,
            "queue_id": queue_id,
            "queue_name": queue_name,
            "media_types": live.get("mediaTypes"),
            "assignment_mode": mode,
            "assigned_users_count": live.get("assignedUsersCount"),
            NEXT_STEP_HINT: hint,
        }

    @mcp.tool(annotations=DESTRUCTIVE)
    async def unassign_copilot_queue(assistant: str, queue: str, ctx: Context | None = None) -> dict[str, Any]:
        """Switch a copilot off for a routing queue.

        Takes the user assignments on that queue with it. The queue itself, and
        everything else about the copilot, is untouched.

        Args:
            assistant: Assistant name or id.
            queue: Routing queue name or id.
        """
        client = get_client()
        assistant_id = _assistant_id(assistant)
        queue_id = _routing_queue_id(queue)
        queue_name = _queue_name(queue_id)

        queues = list(client.paginate(f"/api/v2/assistants/{assistant_id}/queues"))
        live_before = next((q for q in queues if q.get("id") == queue_id), None)
        if live_before is None:
            raise ToolError(
                f"Queue {queue_name!r} is not assigned to this assistant — nothing to unassign."
            )
        assigned_count = live_before.get("assignedUsersCount") or 0
        assignment_mode = live_before.get("assignmentMode")
        diff = {
            "queue_id": queue_id,
            "queue_name": queue_name,
            "assignment_mode": assignment_mode,
            "assigned_users_count": assigned_count,
        }
        confirm_message = (
            f"Unassign copilot from queue {queue_name!r}? "
            f"{assigned_count} explicitly assigned user(s) lose access "
            f"(assignment mode {assignment_mode!r})."
        )

        proceed, accepted, unconfirmed = await _confirmation_gate(
            ctx,
            message=confirm_message,
            request_confirmation=True,
            removes_configuration=True,
        )
        if not proceed:
            impact = make_write_impact(
                action="cancelled",
                summary=(
                    f"Queue {queue_name!r} assignment was not removed — "
                    f"{confirmation_block_reason(unconfirmed)}."
                ),
                diff=diff,
                previous_state={"queue_assignment": live_before},
                confirmation_requested=True,
                confirmation_accepted=False,
                unconfirmed_reason=unconfirmed,
            )
            return attach_write_impact(
                {
                    "assistant_id": assistant_id,
                    "queue_id": queue_id,
                    "queue_name": queue_name,
                    NEXT_STEP_HINT: "No change was written.",
                },
                impact,
            )

        client.request("DELETE", f"/api/v2/assistants/{assistant_id}/queues/{queue_id}")
        remaining = [
            q.get("id") for q in client.paginate(f"/api/v2/assistants/{assistant_id}/queues")
        ]
        if queue_id in remaining:
            raise ToolError(f"Queue {queue_id} is still assigned after the delete.")

        impact = make_write_impact(
            action="applied",
            summary=f"Unassigned copilot from queue {queue_name!r} ({assigned_count} user assignment(s) cleared).",
            diff=diff,
            previous_state={"queue_assignment": live_before},
            confirmation_requested=True,
            confirmation_accepted=accepted,
            unconfirmed_reason=unconfirmed,
        )
        return attach_write_impact(
            {
                "assistant_id": assistant_id,
                "queue_id": queue_id,
                "queue_name": queue_name,
                "queues_remaining": len(remaining),
                NEXT_STEP_HINT: (
                    f"No agents on {queue_name!r} receive this copilot anymore. "
                    f"If {len(remaining)} queue(s) remain, list_copilot_queues(assistant={assistant!r}) "
                    f"shows where it is still active. Re-assign with assign_copilot_queue(assistant={assistant!r}, "
                    f"queue='<queue name>') when you want it back on that queue."
                ),
            },
            impact,
        )

    @mcp.tool(annotations=WRITES)
    async def set_copilot_queue_users(
        assistant: str,
        queue: str,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add or remove the users a copilot reaches on a queue.

        Requires the queue to be on "Manual" assignment. Under "Auto" every
        member of the queue gets the copilot and the API refuses per-user
        changes outright, with "Queue assignment mode is Auto". Since a new
        assignment always starts on "Auto", this usually needs
        ``assign_copilot_queue(..., assignment_mode="Manual")`` first.

        Args:
            assistant: Assistant name or id.
            queue: Routing queue name or id.
            add: Users to add, by name, email or id.
            remove: Users to remove, by name, email or id.
        """
        if not add and not remove:
            raise ToolError("Nothing to do: pass add, remove, or both.")

        client = get_client()
        assistant_id = _assistant_id(assistant)
        queue_id = _routing_queue_id(queue)
        queue_name = _queue_name(queue_id)
        base = f"/api/v2/assistants/{assistant_id}/queues/{queue_id}/users"

        before_rows = _queue_user_rows(client, assistant_id, queue_id)
        before_ids = {row["id"] for row in before_rows}
        before_labels = {row["id"]: row["label"] for row in before_rows}

        added = [_user_id(u) for u in add or []]
        removed = [_user_id(u) for u in remove or []]

        add_set = set(added)
        remove_set = set(removed)
        if not add_set and not remove_set:
            raise ToolError("Could not resolve any users from add/remove lists.")

        after_ids = (before_ids | add_set) - remove_set
        users_added = sorted(add_set - before_ids)
        users_removed = sorted(before_ids & remove_set)

        if not users_added and not users_removed:
            impact = make_write_impact(
                action="skipped_no_change",
                summary=(
                    f"Queue {queue_name!r} user list already matches the requested add/remove — "
                    "nothing was written."
                ),
                diff={
                    "queue_name": queue_name,
                    "assigned_users_count": len(before_ids),
                },
            )
            live = client.get(f"/api/v2/assistants/{assistant_id}/queues/{queue_id}")
            return attach_write_impact(
                {
                    "assistant_id": assistant_id,
                    "queue_name": queue_name,
                    "assignment_mode": live.get("assignmentMode"),
                    "added": 0,
                    "removed": 0,
                    "assigned_users_count": live.get("assignedUsersCount"),
                    NEXT_STEP_HINT: "No change was written.",
                },
                impact,
            )

        added_labels: list[str] = []
        for uid in users_added:
            label = before_labels.get(uid)
            if not label:
                try:
                    user = client.get(f"/api/v2/users/{uid}")
                    label = user.get("email") or user.get("name") or uid
                except ApiError:
                    label = uid
            added_labels.append(label)
        removed_labels = [before_labels.get(uid, uid) for uid in users_removed]

        diff = {
            "queue_name": queue_name,
            "before_count": len(before_ids),
            "after_count": len(after_ids),
            "users_added": added_labels,
            "users_removed": removed_labels,
        }
        previous_state = {
            "user_ids": sorted(before_ids),
            "users": [{"id": row["id"], "label": row["label"]} for row in before_rows],
        }

        parts: list[str] = []
        if users_removed:
            parts.append(
                f"remove {len(users_removed)} user(s): {format_sample_list(removed_labels)}"
            )
        if users_added:
            parts.append(f"add {len(users_added)} user(s): {format_sample_list(added_labels)}")
        confirm_message = (
            f"Change copilot users on queue {queue_name!r}? "
            f"{' and '.join(parts)} "
            f"({len(before_ids)} → {len(after_ids)} assigned). Apply or cancel?"
        )

        # The gate only runs when users are leaving, which is also the only case
        # where letting an unanswered prompt through would cost something.
        request_confirmation = bool(users_removed)
        proceed, accepted, unconfirmed = await _confirmation_gate(
            ctx,
            message=confirm_message,
            request_confirmation=request_confirmation,
            removes_configuration=True,
        )
        if not proceed:
            impact = make_write_impact(
                action="cancelled",
                summary=(
                    f"Queue {queue_name!r} user list was not changed — "
                    f"{confirmation_block_reason(unconfirmed)}."
                ),
                diff=diff,
                previous_state=previous_state,
                confirmation_requested=True,
                confirmation_accepted=False,
                unconfirmed_reason=unconfirmed,
            )
            return attach_write_impact(
                {
                    "assistant_id": assistant_id,
                    "queue_name": queue_name,
                    NEXT_STEP_HINT: "No change was written.",
                },
                impact,
            )

        if added:
            client.request(
                "POST", f"{base}/bulk/add", json_body={"entities": [{"id": i} for i in added]}
            )
        if removed:
            client.request(
                "POST", f"{base}/bulk/remove", json_body={"entities": [{"id": i} for i in removed]}
            )

        live = client.get(f"/api/v2/assistants/{assistant_id}/queues/{queue_id}")
        impact = make_write_impact(
            action="applied",
            summary=(
                f"Updated copilot users on {queue_name!r}: "
                f"+{len(users_added)} / −{len(users_removed)} "
                f"({len(before_ids)} → {len(after_ids)} assigned)."
            ),
            diff=diff,
            previous_state=previous_state,
            confirmation_requested=request_confirmation,
            confirmation_accepted=accepted,
            unconfirmed_reason=unconfirmed,
        )
        return attach_write_impact(
            {
                "assistant_id": assistant_id,
                "queue_name": queue_name,
                "assignment_mode": live.get("assignmentMode"),
                "added": len(users_added),
                "removed": len(users_removed),
                "assigned_users_count": live.get("assignedUsersCount"),
                NEXT_STEP_HINT: (
                    f"{live.get('assignedUsersCount', 0)} user(s) now receive the copilot on {queue_name!r}. "
                    f"Have an assigned agent start a test conversation on that queue. "
                    f"If checklist or script rules should fire, confirm with list_copilot_rules(assistant={assistant!r}) "
                    f"and measure_copilot_nlu(assistant={assistant!r}, strict=True) when intents changed recently."
                ),
            },
            impact,
        )

    @mcp.tool(annotations=READ_ONLY)
    def list_copilot_checklists(name_contains: str | None = None) -> list[dict[str, Any]]:
        """List Agent Copilot checklists, optionally filtered by name.

        Checklists are org-wide templates referenced by id from copilot rules.
        Each item's ``description`` is the AI check prompt: Copilot reads the
        live conversation and marks the item when the description is satisfied.
        The ``name`` field is what agents see; it rejects ``&`` and most
        punctuation beyond letters, digits, whitespace, hyphen and apostrophe.

        Args:
            name_contains: Case-insensitive substring of the checklist name.
        """
        client = get_client()
        results = []
        for item in client.paginate("/api/v2/assistants/agentchecklists"):
            name = item.get("name") or ""
            if name_contains and name_contains.lower() not in name.lower():
                continue
            results.append(
                {
                    "id": item.get("id"),
                    "name": name,
                    "language": item.get("language"),
                    "item_count": len(item.get("checklistItems") or []),
                    "date_modified": item.get("dateModified"),
                }
            )
        return results

    @mcp.tool(annotations=READ_ONLY)
    def get_copilot_checklist(checklist: str) -> dict[str, Any]:
        """Fetch one agent checklist with all items and AI prompts.

        The ``description`` on each item is the natural-language criterion
        Copilot evaluates against the transcript. ``automated_check_enabled``
        must be true for AI checking; ``exact_phrase_match`` false is usual
        for demo phrasing flexibility.

        Args:
            checklist: Checklist name or id, for example "Support Opening Checklist".
        """
        client = get_client()
        checklist_id = _checklist_id(checklist)
        record = client.get(f"/api/v2/assistants/agentchecklists/{checklist_id}")
        return {
            "id": checklist_id,
            "name": record.get("name"),
            "language": record.get("language"),
            "items": [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "description": item.get("description"),
                    "automated_check_enabled": item.get("automatedCheckEnabled"),
                    "exact_phrase_match": item.get("exactPhraseMatch"),
                    "important": item.get("important"),
                }
                for item in (record.get("checklistItems") or [])
            ],
            "date_modified": record.get("dateModified"),
        }

    @mcp.tool(annotations=WRITES)
    async def set_copilot_checklist(
        checklist: str,
        items: list[dict[str, Any]],
        name: str | None = None,
        language: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Create or replace an agent checklist's items.

        Updating sends the whole ``checklistItems`` array; omitted items are
        removed. Each item needs at least ``name`` and ``description`` (the AI
        prompt). Creation returns **202**, not 200 — always verify via
        ``get_copilot_checklist`` before retrying a "failed" create.

        Args:
            checklist: Existing checklist name/id to update, or a new name to
                create when no match exists.
            items: Full item list. Keys: ``name``, ``description``,
                ``automated_check_enabled`` (default true),
                ``exact_phrase_match`` (default false), ``important`` (default
                false). Optional ``id`` preserves an existing item on update.
            name: Override display name on update only.
            language: BCP-47 language tag for new checklists, e.g. ``de-de``.
        """
        client = get_client()

        def _normalise(entry: dict[str, Any]) -> dict[str, Any]:
            if not entry.get("name") or not entry.get("description"):
                raise ToolError("Each checklist item needs name and description.")
            normalised = {
                "name": entry["name"],
                "description": entry["description"],
                "automatedCheckEnabled": entry.get("automated_check_enabled", True),
                "exactPhraseMatch": entry.get("exact_phrase_match", False),
                "important": entry.get("important", False),
            }
            if entry.get("id"):
                normalised["id"] = entry["id"]
            return normalised

        payload_items = [_normalise(i) for i in items]

        updating = True
        existing: dict[str, Any] | None = None
        try:
            checklist_id = _checklist_id(checklist)
            existing = client.get(f"/api/v2/assistants/agentchecklists/{checklist_id}")
        except ToolError:
            updating = False

        if updating and existing is not None:
            before_items = _checklist_items_signature(existing.get("checklistItems") or [])
            after_items = _checklist_items_signature(payload_items)
            before_names = [item["name"] for item in before_items if item.get("name")]
            after_names = [item["name"] for item in after_items if item.get("name")]
            name_diff = list_set_diff(before_names, after_names)
            target_name = name or existing.get("name")
            name_changed = name is not None and name != existing.get("name")

            if not name_changed and before_items == after_items:
                checklist_name = existing.get("name")
                impact = make_write_impact(
                    action="skipped_no_change",
                    summary=(
                        f"Checklist {checklist_name!r} already has the same {len(after_items)} item(s) — "
                        "nothing was written."
                    ),
                    diff={"checklist": checklist_name, "item_count": len(after_items)},
                    previous_state={"items": before_items},
                )
                return attach_write_impact(
                    {
                        "id": checklist_id,
                        "name": checklist_name,
                        "created": False,
                        "item_count": len(after_items),
                        NEXT_STEP_HINT: (
                            f"No change was written. Verify with "
                            f"get_copilot_checklist(checklist={checklist_name!r})."
                        ),
                    },
                    impact,
                )

            removed_names = name_diff["removed_all"]
            diff = {
                "checklist": target_name,
                "items_before": len(before_items),
                "items_after": len(after_items),
                "items_removed": removed_names,
                "items_added": name_diff["added_all"],
                "name_changed": name_changed,
            }
            previous_state = {
                "name": existing.get("name"),
                "language": existing.get("language"),
                "items": before_items,
            }
            confirm_message = (
                f"Replace checklist {existing.get('name')!r} items? "
                f"{len(before_items)} → {len(after_items)} item(s)"
            )
            if removed_names:
                confirm_message += f"; {len(removed_names)} removed: {format_sample_list(removed_names)}"
            if name_changed:
                confirm_message += f"; rename to {target_name!r}"
            confirm_message += ". Apply or cancel?"

            proceed, accepted, unconfirmed = await _confirmation_gate(
                ctx, message=confirm_message, request_confirmation=True
            )
            if not proceed:
                impact = make_write_impact(
                    action="cancelled",
                    summary=f"Checklist {existing.get('name')!r} was not updated — confirmation declined.",
                    diff=diff,
                    previous_state=previous_state,
                    confirmation_requested=True,
                    confirmation_accepted=False,
                )
                return attach_write_impact(
                    {
                        "id": checklist_id,
                        "name": existing.get("name"),
                        "created": False,
                        NEXT_STEP_HINT: "No change was written.",
                    },
                    impact,
                )

            body = {
                "name": target_name,
                "language": language or existing.get("language"),
                "checklistItems": payload_items,
            }
            client.request(
                "PUT",
                f"/api/v2/assistants/agentchecklists/{checklist_id}",
                json_body=body,
            )
            created = False
            impact = make_write_impact(
                action="applied",
                summary=(
                    f"Updated checklist {target_name!r}: {len(before_items)} → {len(after_items)} item(s)"
                    + (f", {len(removed_names)} removed" if removed_names else "")
                    + "."
                ),
                diff=diff,
                previous_state=previous_state,
                confirmation_requested=True,
                confirmation_accepted=accepted,
                unconfirmed_reason=unconfirmed,
            )
        else:
            if not language:
                raise ToolError("Creating a checklist requires language, e.g. de-de.")
            client.request(
                "POST",
                "/api/v2/assistants/agentchecklists",
                json_body={
                    "name": name or checklist,
                    "language": language,
                    "checklistItems": payload_items,
                },
            )
            checklist_id = _checklist_id(name or checklist)
            created = True
            impact = make_write_impact(
                action="applied",
                summary=f"Created checklist {name or checklist!r} with {len(payload_items)} item(s).",
                diff={
                    "checklist": name or checklist,
                    "items_after": len(payload_items),
                    "created": True,
                },
                confirmation_requested=False,
            )

        live = client.get(f"/api/v2/assistants/agentchecklists/{checklist_id}")
        checklist_name = live.get("name")
        return attach_write_impact(
            {
                "id": checklist_id,
                "name": checklist_name,
                "created": created,
                "item_count": len(live.get("checklistItems") or []),
                NEXT_STEP_HINT: (
                    f"Wire this checklist into a copilot with set_copilot_rule(assistant='<assistant name>', "
                    f"intent='<intent name>', action='Checklist', checklist={checklist_name!r}, "
                    f"participant_roles=['Agent'] when the checklist tracks agent behaviour). "
                    f"Without a rule, agents never see it. Verify items with "
                    f"get_copilot_checklist(checklist={checklist_name!r})."
                ),
            },
            impact,
        )

    @mcp.tool(annotations=READ_ONLY)
    def list_copilot_summary_settings(name_contains: str | None = None) -> list[dict[str, Any]]:
        """List conversation summary settings usable by Agent Copilot.

        Copilot binds one setting per assistant via ``summary_setting_id`` in
        ``get_copilot``. ``predefined_insights`` cover ReasonForCall, Resolution
        and ActionItems; ``custom_entities`` are free-form extraction fields
        with a label and a natural-language ``description`` prompt.

        Args:
            name_contains: Case-insensitive substring of the setting name.
        """
        client = get_client()
        results = []
        for item in client.paginate("/api/v2/conversations/summaries/settings"):
            name = item.get("name") or ""
            if name_contains and name_contains.lower() not in name.lower():
                continue
            results.append(
                {
                    "id": item.get("id"),
                    "name": name,
                    "language": item.get("language"),
                    "format": item.get("format"),
                    "summary_type": item.get("summaryType"),
                    "custom_entity_count": len(item.get("customEntities") or []),
                    "date_modified": item.get("dateModified"),
                }
            )
        return results

    @mcp.tool(annotations=READ_ONLY)
    def get_copilot_summary_setting(setting: str) -> dict[str, Any]:
        """Fetch a summary setting including custom extraction fields.

        ``custom_entities[].description`` is the extraction prompt: the
        summarisation model reads the transcript and fills each label when
        the description applies, usually citing supporting quotes.

        Args:
            setting: Summary setting name or id.
        """
        client = get_client()
        setting_id = _summary_setting_id(setting)
        record = client.get(f"/api/v2/conversations/summaries/settings/{setting_id}")
        return {
            "id": setting_id,
            "name": record.get("name"),
            "language": record.get("language"),
            "format": record.get("format"),
            "summary_type": record.get("summaryType"),
            "participant_labels": record.get("participantLabels"),
            "predefined_insights": record.get("predefinedInsights"),
            "custom_entities": [
                {"label": e.get("label"), "description": e.get("description")}
                for e in (record.get("customEntities") or [])
            ],
            "date_modified": record.get("dateModified"),
        }

    @mcp.tool(annotations=WRITES)
    async def set_copilot_summary_setting(
        setting: str,
        custom_entities: list[dict[str, str]] | None = None,
        predefined_insights: list[str] | None = None,
        name: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Update a summary setting's extraction fields or insight list.

        Only the fields passed are changed; everything else is carried over
        from the live object. Custom entities need ``label`` and ``description``
        (the extraction prompt). Predefined insight names are fixed enum
        values such as ReasonForCall, Resolution, ActionItems.

        Args:
            setting: Summary setting name or id.
            custom_entities: Replace the full custom entity list when given.
            predefined_insights: Replace predefined insights when given.
            name: Optional rename.
        """
        client = get_client()
        setting_id = _summary_setting_id(setting)
        record = client.get(f"/api/v2/conversations/summaries/settings/{setting_id}")

        before_entities = [
            {"label": e.get("label"), "description": e.get("description")}
            for e in (record.get("customEntities") or [])
        ]
        before_insights = list(record.get("predefinedInsights") or [])
        before_name = record.get("name")

        after_entities = (
            [{"label": e["label"], "description": e["description"]} for e in custom_entities]
            if custom_entities is not None
            else before_entities
        )
        after_insights = list(predefined_insights) if predefined_insights is not None else before_insights
        after_name = name if name is not None else before_name

        entity_labels_before = [e["label"] for e in before_entities if e.get("label")]
        entity_labels_after = [e["label"] for e in after_entities if e.get("label")]
        entity_name_diff = list_set_diff(entity_labels_before, entity_labels_after)
        insights_changed = sorted(before_insights) != sorted(after_insights)
        name_changed = after_name != before_name

        # A field keeps its label but gets a new extraction prompt far more often
        # than it is added or removed, so the label diff alone would report an
        # edited setting as unchanged.
        descriptions_before = {e["label"]: e.get("description") for e in before_entities if e.get("label")}
        relabelled_descriptions = sorted(
            entry["label"]
            for entry in after_entities
            if entry.get("label") in descriptions_before
            and descriptions_before[entry["label"]] != entry.get("description")
        )
        descriptions_changed = bool(relabelled_descriptions)

        if not name_changed and not insights_changed and before_entities == after_entities:
            impact = make_write_impact(
                action="skipped_no_change",
                summary=f"Summary setting {before_name!r} already matches the requested fields — nothing was written.",
                diff={"setting": before_name},
                previous_state={
                    "name": before_name,
                    "custom_entities": before_entities,
                    "predefined_insights": before_insights,
                },
            )
            return attach_write_impact(
                {
                    "id": setting_id,
                    "name": before_name,
                    "custom_entity_count": len(before_entities),
                    "predefined_insights": before_insights,
                    NEXT_STEP_HINT: "No change was written.",
                },
                impact,
            )

        diff: dict[str, Any] = {
            "setting": before_name,
            "name_changed": name_changed,
            "predefined_insights_changed": insights_changed,
        }
        if custom_entities is not None:
            diff["custom_entities"] = {
                **entity_name_diff,
                "descriptions_changed": relabelled_descriptions,
            }
        previous_state = {
            "name": before_name,
            "custom_entities": before_entities,
            "predefined_insights": before_insights,
        }

        replacing = custom_entities is not None or predefined_insights is not None or name_changed
        confirm_message = ""
        if replacing:
            parts: list[str] = []
            if name_changed:
                parts.append(f"rename {before_name!r} → {after_name!r}")
            if entity_name_diff["removed_all"]:
                parts.append(
                    f"remove {len(entity_name_diff['removed_all'])} custom field(s): "
                    f"{format_sample_list(entity_name_diff['removed_all'])}"
                )
            if insights_changed:
                parts.append(f"change predefined insights {before_insights!r} → {after_insights!r}")
            if descriptions_changed:
                parts.append("update custom entity descriptions")
            confirm_message = (
                f"Update summary setting {before_name!r}? "
                f"{'; '.join(parts) or 'replace fields as requested'}. Apply or cancel?"
            )

        proceed, accepted, unconfirmed = await _confirmation_gate(
            ctx,
            message=confirm_message,
            request_confirmation=replacing,
        )
        if not proceed:
            impact = make_write_impact(
                action="cancelled",
                summary=f"Summary setting {before_name!r} was not updated — confirmation declined.",
                diff=diff,
                previous_state=previous_state,
                confirmation_requested=True,
                confirmation_accepted=False,
            )
            return attach_write_impact(
                {
                    "id": setting_id,
                    "name": before_name,
                    NEXT_STEP_HINT: "No change was written.",
                },
                impact,
            )

        body = copy.deepcopy(record)
        for key in ("selfUri", "dateCreated", "dateModified", "createdBy", "modifiedBy"):
            body.pop(key, None)
        if name:
            body["name"] = name
        if custom_entities is not None:
            body["customEntities"] = after_entities
        if predefined_insights is not None:
            body["predefinedInsights"] = after_insights
        client.request(
            "PUT",
            f"/api/v2/conversations/summaries/settings/{setting_id}",
            json_body=body,
        )
        live = client.get(f"/api/v2/conversations/summaries/settings/{setting_id}")
        setting_name = live.get("name")
        impact = make_write_impact(
            action="applied",
            summary=f"Updated summary setting {setting_name!r}.",
            diff=diff,
            previous_state=previous_state,
            confirmation_requested=replacing,
            confirmation_accepted=accepted,
            unconfirmed_reason=unconfirmed,
        )
        return attach_write_impact(
            {
                "id": setting_id,
                "name": setting_name,
                "custom_entity_count": len(live.get("customEntities") or []),
                "predefined_insights": live.get("predefinedInsights"),
                NEXT_STEP_HINT: (
                    f"Bind this setting to each assistant that should summarise conversations: "
                    f"call get_copilot(assistant='<assistant name>') and confirm summary_setting_id "
                    f"is {setting_id!r}. If it differs or summarisation_enabled is false, update the "
                    f"assistant's Copilot summaryGenerationConfig (via genesys_api_call PUT "
                    f"/api/v2/assistants/{{assistantId}}/copilot with summarySetting.id and enabled=true). "
                    f"Then run a test conversation and check the generated summary fields."
                ),
            },
            impact,
        )

    def _run_assignment_mode_job(assistant_id: str, queue_id: str, mode: str) -> None:
        """Switch a queue between Auto and Manual assignment.

        The action names are not the mode names: "Manual" is requested as
        ``ManualAssignment``. The job usually returns Succeeded straight away,
        but it is a job and may not.
        """
        if mode not in ("Auto", "Manual"):
            raise ToolError(f"assignment_mode must be Auto or Manual, not {mode!r}.")

        client = get_client()
        base = f"/api/v2/assistants/{assistant_id}/queues/{queue_id}/users/jobs"
        job = client.request("POST", base, json_body={"action": f"{mode}Assignment"})

        status = job.get("status")
        for _ in range(JOB_POLL_ATTEMPTS):
            if status == "Succeeded":
                return
            if status == "Failed":
                raise ToolError(f"Switching to {mode} assignment failed: {job.get('errorInfo')}")
            time.sleep(JOB_POLL_SECONDS)
            job = client.get(f"{base}/{job['id']}")
            status = job.get("status")
        raise ToolError(f"Switching to {mode} assignment was still running after the time allowed.")

    # --- NLU versions --------------------------------------------------------

    def _live_nlu_version(assistant_id: str) -> tuple[str, dict[str, Any]]:
        """The domain an assistant uses, and the version actually in force."""
        config = _copilot_config(assistant_id)
        domain_id = (((config.get("nluConfig") or {}).get("domain")) or {}).get("id")
        if not domain_id:
            raise ToolError("This assistant has no NLU domain configured.")
        return domain_id, _latest_published_version(domain_id, with_utterances=True)

    def _latest_published_version(domain_id: str, *, with_utterances: bool) -> dict[str, Any]:
        versions = [
            v
            for v in get_client().paginate(
                f"/api/v2/languageunderstanding/domains/{domain_id}/versions",
                params={"includeUtterances": "true" if with_utterances else "false"},
            )
            if v.get("published")
        ]
        if not versions:
            raise ToolError(f"NLU domain {domain_id} has no published version to build on.")
        # Every version reports published: true forever, so the flag says nothing
        # about which one is live; the most recently published one is.
        versions.sort(key=lambda v: v.get("datePublished") or "", reverse=True)
        return versions[0]

    def _carry_forward(intents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Strip the service-assigned ids so intents can be posted back."""
        carried = []
        for intent in intents:
            entry = {k: v for k, v in intent.items() if k not in ("id", "entityNameReferences")}
            entry["utterances"] = [
                {k: v for k, v in utterance.items() if k != "id"}
                for utterance in (intent.get("utterances") or [])
            ]
            carried.append(entry)
        return carried

    def _publish_nlu_version(
        domain_id: str, previous: dict[str, Any], intents: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Create, train and publish a version, then confirm it took."""
        client = get_client()
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
        version_id = draft["id"]

        client.request("POST", f"{base}/{version_id}/train")
        for _ in range(TRAINING_POLL_ATTEMPTS):
            status = client.get(f"{base}/{version_id}").get("trainingStatus")
            if status == "Trained":
                break
            if status == "Error":
                raise ToolError(f"Training version {version_id} failed; it was not published.")
            time.sleep(TRAINING_POLL_SECONDS)
        else:
            raise ToolError(f"Version {version_id} was still training after the time allowed.")

        client.request("POST", f"{base}/{version_id}/publish")

        live = _latest_published_version(domain_id, with_utterances=False)
        if live["id"] != version_id:
            raise ToolError(
                f"Published {version_id} but {live['id']} is the newest published version. "
                "The domain was left on the older one."
            )
        return live

    # --- writing -------------------------------------------------------------

    def _put_rules(
        assistant_id: str, config: dict[str, Any], rules: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Write back the configuration with a new rule list.

        The rules keep every field they came with, ``participantRoles`` among
        them, minus the service-assigned ``id``: sending those back is rejected.
        """
        body = copy.deepcopy(config)
        for field in READ_ONLY_CONFIG_FIELDS:
            body.pop(field, None)

        engine = body.setdefault("ruleEngineConfig", {})
        engine["rules"] = [{k: v for k, v in rule.items() if k != "id"} for rule in rules]

        get_client().request("PUT", f"/api/v2/assistants/{assistant_id}/copilot", json_body=body)
        # Read back rather than trusting the response: this is a full replace,
        # and a field silently dropped is the failure worth catching.
        return _copilot_config(assistant_id)

    def _action_attributes(
        action: str,
        script: str | None,
        page: str | None,
        checklist: str | None,
        response_id: str | None,
    ) -> dict[str, Any]:
        if action == "Script":
            if not script or not page:
                raise ToolError("A Script action needs both script and page.")
            script_id = _script_id(script)
            return {"scriptId": script_id, "pageId": _page_id(script_id, page)}
        if action == "Checklist":
            if not checklist:
                raise ToolError("A Checklist action needs checklist.")
            return {"checklistId": _checklist_id(checklist)}
        if action == "CannedResponse":
            if not response_id:
                raise ToolError("A CannedResponse action needs response_id.")
            return {"responseId": response_id}
        if action == "KnowledgeArticle":
            return {}
        raise ToolError(
            f"Unknown action {action!r}. Use Script, Checklist, CannedResponse or KnowledgeArticle."
        )

    def _index_of_intent_rule(rules: list[dict[str, Any]], intent: str) -> int | None:
        for index, entry in enumerate(rules):
            for condition in ((entry.get("rule") or {}).get("conditions") or []):
                if condition.get("conditionType") != "Intent":
                    continue
                if intent in (condition.get("conditionValues") or []):
                    return index
        return None

    def _intents_of(rules: list[dict[str, Any]]) -> list[str]:
        found = []
        for entry in rules:
            for condition in ((entry.get("rule") or {}).get("conditions") or []):
                found.extend(condition.get("conditionValues") or [])
        return found

    def _intent_is_live(config: dict[str, Any], intent: str) -> bool | None:
        """Whether the published NLU version knows the intent, None if unknowable."""
        domain_id = (((config.get("nluConfig") or {}).get("domain")) or {}).get("id")
        if not domain_id:
            return None
        try:
            versions = [
                v
                for v in get_client().paginate(
                    f"/api/v2/languageunderstanding/domains/{domain_id}/versions",
                    params={"includeUtterances": "false"},
                )
                if v.get("published")
            ]
        except ApiError:
            return None
        if not versions:
            return None
        versions.sort(key=lambda v: v.get("datePublished") or "", reverse=True)
        return any(i.get("name") == intent for i in (versions[0].get("intents") or []))

    # --- shared lookups ------------------------------------------------------

    def _copilot_config(assistant_id: str) -> dict[str, Any]:
        return get_client().get(f"/api/v2/assistants/{assistant_id}/copilot") or {}

    def _describe_action(action: dict[str, Any], cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Turn an action's bare ids into something a reader can check."""
        attributes = action.get("attributes") or {}
        described: dict[str, Any] = {"type": action.get("actionType")}

        script_id = attributes.get("scriptId")
        if script_id:
            described["script"] = _script_name(script_id, cache["scripts"])
            page_id = attributes.get("pageId")
            if page_id:
                described["page"] = _page_name(script_id, page_id, cache["pages"])

        checklist_id = attributes.get("checklistId")
        if checklist_id:
            described["checklist"] = _checklist_name(checklist_id)

        response_id = attributes.get("responseId")
        if response_id:
            described["canned_response"] = _response_name(response_id)

        return described

    # Every id below is resolved best-effort: a rule pointing at something that
    # was since deleted should show up as an unresolved id, not blow up the
    # whole listing - that broken pointer is usually the thing being looked for.

    def _script_name(script_id: str, cache: dict[str, Any]) -> str:
        if script_id not in cache:
            try:
                cache[script_id] = get_client().get(f"/api/v2/scripts/{script_id}").get("name") or script_id
            except ApiError:
                cache[script_id] = f"{script_id} (not found)"
        return cache[script_id]

    def _page_name(script_id: str, page_id: str, cache: dict[str, Any]) -> str:
        if script_id not in cache:
            try:
                pages = get_client().get(f"/api/v2/scripts/{script_id}/pages") or []
            except ApiError:
                pages = []
            cache[script_id] = {p["id"]: p.get("name") or p["id"] for p in pages if p.get("id")}
        return cache[script_id].get(page_id, f"{page_id} (not found)")

    def _checklist_name(checklist_id: str) -> str:
        try:
            record = get_client().get(f"/api/v2/assistants/agentchecklists/{checklist_id}")
            return record.get("name") or checklist_id
        except ApiError:
            return f"{checklist_id} (not found)"

    def _response_name(response_id: str) -> str:
        try:
            record = get_client().get(f"/api/v2/responsemanagement/responses/{response_id}")
            return record.get("name") or response_id
        except ApiError:
            return f"{response_id} (not found)"

    def _queue_name(queue_id: str | None) -> str | None:
        if not queue_id:
            return None
        try:
            return get_client().get(f"/api/v2/routing/queues/{queue_id}").get("name") or queue_id
        except ApiError:
            return f"{queue_id} (not found)"

    # --- name resolution -----------------------------------------------------

    def _assistant_search(client: Any, term: str):
        return [
            a for a in client.paginate("/api/v2/assistants") if term.lower() in (a.get("name") or "").lower()
        ]

    def _user_list_available(client: Any):
        def list_available() -> list[dict[str, Any]]:
            payload = client.request(
                "POST",
                "/api/v2/users/search",
                json_body={"pageSize": LIST_LIMIT},
            )
            return payload.get("results") or []

        return list_available

    def _assistant_id(value: str) -> str:
        client = get_client()
        return resolve(
            value,
            label="assistant",
            search=lambda v: _assistant_search(client, v),
            list_available=paginate_lister(client, "/api/v2/assistants"),
        )

    async def _assistant_id_async(value: str, ctx: Context | None) -> str:
        client = get_client()
        return await resolve_async(
            value,
            ctx=ctx,
            label="assistant",
            search=lambda v: _assistant_search(client, v),
            list_available=paginate_lister(client, "/api/v2/assistants"),
            elicitation_timeout=ELICITATION_TIMEOUT_SECONDS,
        )

    def _script_id(value: str) -> str:
        client = get_client()
        return resolve(
            value,
            label="agent script",
            search=lambda v: [
                s for s in client.paginate("/api/v2/scripts") if v.lower() in (s.get("name") or "").lower()
            ],
            list_available=paginate_lister(client, "/api/v2/scripts"),
        )

    def _page_id(script_id: str, value: str) -> str:
        # Scoped to the one script: page names repeat across scripts, and a rule
        # pointing at a page of a different script than its scriptId is silently
        # accepted and then never fires.
        pages = get_client().get(f"/api/v2/scripts/{script_id}/pages") or []

        def list_available() -> list[dict[str, Any]]:
            return [{"id": p.get("id"), "name": p.get("name")} for p in pages]

        return resolve(
            value,
            label="script page",
            search=lambda v: [p for p in pages if v.lower() in (p.get("name") or "").lower()],
            list_available=list_available,
        )

    def _checklist_id(value: str) -> str:
        client = get_client()
        return resolve(
            value,
            label="agent checklist",
            search=lambda v: [
                c
                for c in client.paginate("/api/v2/assistants/agentchecklists")
                if v.lower() in (c.get("name") or "").lower()
            ],
            list_available=paginate_lister(client, "/api/v2/assistants/agentchecklists"),
        )

    def _summary_setting_id(value: str) -> str:
        client = get_client()
        return resolve(
            value,
            label="summary setting",
            search=lambda v: [
                s
                for s in client.paginate("/api/v2/conversations/summaries/settings")
                if v.lower() in (s.get("name") or "").lower()
            ],
            list_available=paginate_lister(client, "/api/v2/conversations/summaries/settings"),
        )

    def _routing_queue_search(client: Any, term: str):
        return client.paginate("/api/v2/routing/queues", params={"name": f"*{term}*"})

    def _routing_queue_id(value: str) -> str:
        client = get_client()
        return resolve(
            value,
            label="queue",
            search=lambda v: _routing_queue_search(client, v),
            list_available=paginate_lister(client, "/api/v2/routing/queues"),
        )

    async def _routing_queue_id_async(value: str, ctx: Context | None) -> str:
        client = get_client()
        return await resolve_async(
            value,
            ctx=ctx,
            label="queue",
            search=lambda v: _routing_queue_search(client, v),
            list_available=paginate_lister(client, "/api/v2/routing/queues"),
            elicitation_timeout=ELICITATION_TIMEOUT_SECONDS,
        )

    def _user_id(value: str) -> str:
        client = get_client()

        def search(v: str) -> list[dict[str, Any]]:
            payload = client.request(
                "POST",
                "/api/v2/users/search",
                json_body={
                    "pageSize": 25,
                    "query": [{"type": "CONTAINS", "fields": ["name", "email"], "value": v}],
                },
            )
            return payload.get("results") or []

        return resolve(
            value,
            label="user",
            search=search,
            list_available=_user_list_available(client),
        )
