"""Guardrails for MCP tools that overwrite or delete existing configuration.

Every guarded write returns a ``write_impact`` block: what was about to change,
whether confirmation ran, and enough prior state to undo a mistake.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal, TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from mcp.server.mcpserver.context import Context

# Returned alongside existing tool fields (same spirit as next_step_hint).
WRITE_IMPACT = "write_impact"

ELICITATION_TIMEOUT_SECONDS = 30.0

WriteAction = Literal["applied", "skipped_no_change", "cancelled"]


class WriteConfirmationChoice(BaseModel):
    """Elicitation schema for overwriting or destructive writes."""

    decision: Literal["apply", "cancel"] = Field(
        description="Apply the change described in the message, or cancel without writing",
    )


def list_set_diff(before: list[str], after: list[str], *, cap: int = 15) -> dict[str, Any]:
    """Compare two string lists; cap long removed/added samples in diff, keep full lists."""
    before_set = set(before)
    after_set = set(after)
    removed = sorted(before_set - after_set)
    added = sorted(after_set - before_set)
    return {
        "before_count": len(before),
        "after_count": len(after),
        "unchanged_count": len(before_set & after_set),
        "removed": removed[:cap],
        "removed_truncated": max(0, len(removed) - cap),
        "added": added[:cap],
        "added_truncated": max(0, len(added) - cap),
        "removed_all": removed,
        "added_all": added,
    }


def format_sample_list(items: list[str], *, cap: int = 3) -> str:
    """Format a few quoted samples plus an overflow count."""
    if not items:
        return ""
    shown = items[:cap]
    text = ", ".join(repr(item) for item in shown)
    extra = len(items) - cap
    if extra > 0:
        text += f" (and {extra} more)"
    return text


def confirmation_block_reason(unconfirmed_reason: str | None) -> str:
    """Say why a guarded write did not happen, in words that fit a summary.

    ``None`` means the user actually said no. Anything else means nobody was
    asked, or nobody answered — a different thing, and worth not conflating.
    """
    if unconfirmed_reason is None:
        return "confirmation declined"
    if unconfirmed_reason == "timeout":
        return "the confirmation prompt went unanswered"
    if unconfirmed_reason == "no_context":
        return "there was no client to ask"
    return "the client could not be asked to confirm"


def make_write_impact(
    *,
    action: WriteAction,
    summary: str,
    diff: dict[str, Any] | None = None,
    previous_state: dict[str, Any] | None = None,
    confirmation_requested: bool = False,
    confirmation_accepted: bool | None = None,
    unconfirmed_reason: str | None = None,
) -> dict[str, Any]:
    """Build the standard write_impact object attached to tool results."""
    confirmation: dict[str, Any] = {"requested": confirmation_requested}
    if confirmation_requested:
        confirmation["accepted"] = confirmation_accepted
        if unconfirmed_reason:
            confirmation["unconfirmed_reason"] = unconfirmed_reason

    impact: dict[str, Any] = {
        "action": action,
        "summary": summary,
        "confirmation": confirmation,
    }
    if diff is not None:
        impact["diff"] = diff
    if previous_state is not None:
        impact["previous_state"] = previous_state
    return impact


def attach_write_impact(result: dict[str, Any], impact: dict[str, Any]) -> dict[str, Any]:
    """Add write_impact to a tool result without altering existing keys."""
    result[WRITE_IMPACT] = impact
    return result


async def elicit_write_confirmation(
    ctx: Context | None,
    message: str,
    *,
    timeout: float = ELICITATION_TIMEOUT_SECONDS,
) -> tuple[Literal["confirmed", "declined", "unavailable"], str | None]:
    """Ask the user to confirm a concrete change; always times out."""
    if ctx is None:
        return "unavailable", "no_context"

    try:
        result = await asyncio.wait_for(
            ctx.elicit(message=message, schema=WriteConfirmationChoice),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return "unavailable", "timeout"
    except (ValueError, OSError):
        return "unavailable", "elicitation_error"
    except Exception:
        return "unavailable", "elicitation_error"

    if result.action != "accept" or result.data is None:
        return "declined", None
    if result.data.decision == "cancel":
        return "declined", None
    return "confirmed", None
