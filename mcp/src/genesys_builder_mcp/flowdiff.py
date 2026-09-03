"""Comparing a flow YAML against a fresh export.

Archy's exporter reorders variable blocks and decides per value whether to quote
it, so two exports of the same flow can differ on hundreds of lines while being
identical. Sorting cancels the reordering, unquoting cancels the rest.
"""

from __future__ import annotations

import difflib
import re
import tempfile
from pathlib import Path

from . import archy
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


def compare_with_org(root: Path, local: LocalFlow) -> dict[str, object]:
    """Export the flow to a scratch directory and diff it against the local file.

    Answers "does the org hold anything this file does not", which is the
    question to ask before publishing when there is no recorded export to
    compare against.
    """
    with tempfile.TemporaryDirectory(prefix="flow-compare-") as tmp:
        scratch = Path(tmp)
        archy.run(
            root,
            "export",
            [
                "--flowName", local.name,
                "--flowType", local.flow_type,
                "--exportType", "yaml",
                "--outputDir", str(scratch),
                "--exportFileName", f"{local.name}.yaml",
                "--force",
            ],
        )
        org_text = (scratch / f"{local.name}.yaml").read_text(encoding="utf-8")

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
