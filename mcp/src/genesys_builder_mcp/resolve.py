"""Turn names into ids.

Prompts talk about "the Weekend Support queue", not about
b3f1c0a2-1234-4c56-89ab-cdef01234567. Every tool that takes a reference accepts
either, so the model never has to look an id up first.
"""

from __future__ import annotations

import asyncio
import difflib
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, TYPE_CHECKING

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field, create_model

if TYPE_CHECKING:
    from mcp.server.mcpserver import Context

UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# How many names to show when nothing matched, and how many ambiguous candidates
# to list before asking the caller to narrow down.
LIST_LIMIT = 25
AMBIGUOUS_LIST_LIMIT = 10
SIMILAR_SUGGESTION_COUNT = 3
SIMILAR_CUTOFF = 0.5

# Elicitation backstop when the client does not answer (seconds).
ELICITATION_TIMEOUT_SECONDS = 30.0


class AmbiguousReference(ToolError):
    """More than one entity matched, so the caller has to be more specific."""


def looks_like_id(value: str) -> bool:
    return bool(UUID_PATTERN.match(value.strip()))


def paginate_lister(
    client: Any,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    max_items: int = LIST_LIMIT,
    name_key: str = "name",
) -> Callable[[], list[dict[str, Any]]]:
    """Build a bounded ``list_available`` callback for paginated endpoints."""

    def list_available() -> list[dict[str, Any]]:
        return [
            {"id": item.get("id"), name_key: item.get(name_key)}
            for item in client.paginate(path, params=params, max_items=max_items + 1)
        ]

    return list_available


def filter_paginate_lister(
    client: Any,
    path: str,
    *,
    max_items: int = LIST_LIMIT,
    name_key: str = "name",
) -> Callable[[], list[dict[str, Any]]]:
    """Alias for :func:`paginate_lister` — kept for call-site readability."""
    return paginate_lister(client, path, max_items=max_items, name_key=name_key)


@dataclass(frozen=True)
class _MatchOutcome:
    entity_id: str | None = None
    ambiguous: tuple[dict[str, Any], ...] = ()


def _display_name(item: dict[str, Any], name_key: str) -> str:
    return str(item.get(name_key) or item.get("id") or "")


def _match(value: str, search: Callable[[str], Iterable[dict[str, Any]]], name_key: str) -> _MatchOutcome:
    candidates = list(search(value))
    if not candidates:
        return _MatchOutcome()

    exact = [c for c in candidates if _display_name(c, name_key).lower() == value.lower()]
    if len(exact) == 1:
        return _MatchOutcome(entity_id=exact[0]["id"])
    if len(exact) > 1:
        candidates = exact

    if len(candidates) > 1:
        return _MatchOutcome(ambiguous=tuple(candidates))

    return _MatchOutcome(entity_id=candidates[0]["id"])


def _collect_available_once(
    list_available: Callable[[], Iterable[dict[str, Any]]] | None,
    *,
    name_key: str,
) -> tuple[list[str], bool]:
    if list_available is None:
        return [], False

    names: list[str] = []
    for item in list_available():
        name = _display_name(item, name_key)
        if name:
            names.append(name)

    truncated = len(names) > LIST_LIMIT
    return names[:LIST_LIMIT], truncated


def _similar_names(query: str, available: list[str]) -> list[str]:
    if not available:
        return []
    return difflib.get_close_matches(query, available, n=SIMILAR_SUGGESTION_COUNT, cutoff=SIMILAR_CUTOFF)


def _format_name_list(names: list[str], *, truncated: bool) -> str:
    if not names:
        return ""
    listed = ", ".join(repr(name) for name in names)
    suffix = f" (showing {LIST_LIMIT}, more exist)" if truncated else ""
    return f" Your org has: {listed}{suffix}."


def _no_match_message(
    value: str,
    label: str,
    *,
    name_key: str,
    list_available: Callable[[], Iterable[dict[str, Any]]] | None,
) -> str:
    available, truncated = _collect_available_once(list_available, name_key=name_key)
    message = f"No {label} found matching {value!r}."
    message += _format_name_list(available, truncated=truncated)

    similar = _similar_names(value, available)
    if similar:
        message += f" Did you mean: {', '.join(repr(name) for name in similar)}?"

    return message


def _ambiguous_message(value: str, candidates: Iterable[dict[str, Any]], label: str, *, name_key: str) -> str:
    items = list(candidates)
    listed = ", ".join(f"{_display_name(c, name_key)} ({c.get('id')})" for c in items[:AMBIGUOUS_LIST_LIMIT])
    extra = ""
    if len(items) > AMBIGUOUS_LIST_LIMIT:
        extra = f" (+{len(items) - AMBIGUOUS_LIST_LIMIT} more)"
    return (
        f"{len(items)} {label}s match {value!r}: {listed}{extra}. "
        f"Call again with the exact full name or id."
    )


def resolve(
    value: str,
    *,
    search: Callable[[str], Iterable[dict[str, Any]]],
    label: str,
    name_key: str = "name",
    list_available: Callable[[], Iterable[dict[str, Any]]] | None = None,
) -> str:
    """Return the id for ``value``, which may already be one.

    An exact name match wins over partial ones: with queues named "Support" and
    "Support Escalation", asking for "Support" should not be ambiguous.

    When nothing matches, the error lists what exists in the org (bounded) and
    may suggest close names via ``difflib``.
    """
    value = value.strip()
    if looks_like_id(value):
        return value

    outcome = _match(value, search, name_key)
    if outcome.entity_id:
        return outcome.entity_id
    if outcome.ambiguous:
        raise AmbiguousReference(_ambiguous_message(value, outcome.ambiguous, label, name_key=name_key))

    raise ToolError(_no_match_message(value, label, name_key=name_key, list_available=list_available))


async def _elicit_disambiguation(
    ctx: Context | None,
    *,
    query: str,
    label: str,
    candidates: tuple[dict[str, Any], ...],
    name_key: str,
    timeout: float,
) -> str | None:
    """Ask the user to pick one candidate; None when elicitation fails or is declined."""
    if ctx is None or not candidates:
        return None

    options = [_display_name(c, name_key) for c in candidates]
    if not options or len(set(options)) != len(options):
        # Duplicate display names — fall back to the error message instead of a broken enum.
        return None

    schema = create_model(
        "DisambiguationChoice",
        choice=(
            str,
            Field(
                description=f"The exact {label} name you meant",
                json_schema_extra={"enum": options},
            ),
        ),
    )

    try:
        result = await asyncio.wait_for(
            ctx.elicit(
                message=f"Multiple {label}s match {query!r}. Which one did you mean?",
                schema=schema,
            ),
            timeout=timeout,
        )
    except (asyncio.TimeoutError, ValueError, ToolError, OSError):
        return None
    except Exception:
        return None

    if result.action != "accept" or result.data is None:
        return None

    selected = result.data.choice
    for candidate in candidates:
        if _display_name(candidate, name_key) == selected:
            return candidate["id"]
    return None


async def resolve_async(
    value: str,
    *,
    ctx: Context | None,
    search: Callable[[str], Iterable[dict[str, Any]]],
    label: str,
    name_key: str = "name",
    list_available: Callable[[], Iterable[dict[str, Any]]] | None = None,
    elicitation_timeout: float = ELICITATION_TIMEOUT_SECONDS,
) -> str:
    """Like :func:`resolve`, but may ask the user to disambiguate via MCP elicitation.

    Elicitation is attempted only on ambiguity, never on zero matches, and always
    falls back to the same errors as :func:`resolve` when it fails or is declined.
    """
    value = value.strip()
    if looks_like_id(value):
        return value

    outcome = _match(value, search, name_key)
    if outcome.entity_id:
        return outcome.entity_id

    if outcome.ambiguous:
        chosen = await _elicit_disambiguation(
            ctx,
            query=value,
            label=label,
            candidates=outcome.ambiguous,
            name_key=name_key,
            timeout=elicitation_timeout,
        )
        if chosen:
            return chosen
        raise AmbiguousReference(_ambiguous_message(value, outcome.ambiguous, label, name_key=name_key))

    raise ToolError(_no_match_message(value, label, name_key=name_key, list_available=list_available))
