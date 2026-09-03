"""List the intents and tasks each bot flow implements.

Use cases live inside bot flows rather than in folders of their own, so the only
honest inventory is the one read out of the flows themselves.

    python3 toolkit/scripts/list_use_cases.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Both the NLU domain's intent list and the per-intent settings use "- intent:",
# so intents get counted twice unless deduplicated.
INTENT = re.compile(r"^\s*- intent:\s*$")
TASK = re.compile(r"^\s*- task:\s*$")
NAME = re.compile(r"^\s*name:\s*(.+?)\s*$")


def collect(path: Path) -> tuple[list[str], list[str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    intents: list[str] = []
    tasks: list[str] = []

    for index, line in enumerate(lines):
        target = intents if INTENT.match(line) else tasks if TASK.match(line) else None
        if target is None:
            continue
        for following in lines[index + 1 : index + 4]:
            match = NAME.match(following)
            if match:
                name = match.group(1).strip('"').strip("'")
                if name not in target:
                    target.append(name)
                break

    return intents, tasks


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    for path in sorted(root.glob("industries/*/*/bots/*.yaml")):
        industry, channel = path.relative_to(root).parts[1], path.relative_to(root).parts[2]
        intents, tasks = collect(path)
        print(f"\n{industry}/{channel}: {path.stem}")
        print(f"  intents ({len(intents)}): {', '.join(intents) or '-'}")
        print(f"  tasks   ({len(tasks)}): {', '.join(tasks) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
