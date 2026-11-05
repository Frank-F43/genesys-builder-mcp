"""Comparing a flow YAML against a fresh export.

The exporter reorders variable blocks and decides per value whether to quote
it, so two exports of the same flow can differ on hundreds of lines while being
identical. Sorting cancels the reordering, unquoting cancels the rest.
"""

from __future__ import annotations

import difflib
import re

from . import architect_jobs
from .workspace import LocalFlow

QUOTED_SCALAR = re.compile(r'^(\s*[\w.$-]+:\s*)"(.*)"\s*$')


def normalise(text: str) -> list[str]:
    lines = []
    for line in text.splitlines():
        match = QUOTED_SCALAR.match(line)
        if match:
            value = match.group(2).replace('\\"', '"').replace("\\\\", "\\")
            line = match.group(1) + value
        lines.append(line.rstrip())
    return sorted(lines)


def same_content(a: str, b: str) -> bool:
    """Whether two flow YAML texts differ only in order and quoting."""
    return normalise(a) == normalise(b)


def compare_with_org(local: LocalFlow) -> dict[str, object]:
    """Export the published flow and diff it against the local file.

    Answers "does the org hold anything this file does not", which is the
    question to ask before publishing when there is no recorded export to
    compare against.
    """
    org = architect_jobs.find_flow(local.name, local.api_type)
    if org is None:
        return {"identical": False, "only_in_org": [], "only_local": [], "only_in_org_count": 0, "only_local_count": 0}
    org_text = architect_jobs.export_yaml(org["id"])

    local_lines = normalise(local.path.read_text(encoding="utf-8"))
    org_lines = normalise(org_text)

    diff = list(difflib.unified_diff(org_lines, local_lines, lineterm="", n=0))
    only_local = [line[1:].strip() for line in diff if line.startswith("+") and not line.startswith("+++")]
    only_in_org = [line[1:].strip() for line in diff if line.startswith("-") and not line.startswith("---")]

    return {
        "identical": local_lines == org_lines,
        "only_in_org": _sample(only_in_org),
        "only_local": _sample(only_local),
        "only_in_org_count": len(only_in_org),
        "only_local_count": len(only_local),
    }


def _sample(lines: list[str], limit: int = 10) -> list[str]:
    """Enough lines to recognise what changed, without flooding the context."""
    return [line[:200] for line in lines[:limit]]
