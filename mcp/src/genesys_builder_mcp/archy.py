"""Running the Archy CLI.

Archy is the only way to get a flow's full definition in and out as YAML, so it
stays in the picture even though everything else here goes through the API.

Its console output is meant for humans, but every command takes --resultsFile
and writes structured JSON, which is what gets parsed. Exit codes carry meaning
too: validate uses 123 for errors and 1 for warnings, and doesFlowExist inverts
the usual convention with 1 meaning yes.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

# Archy talks to the org and to Architect Scripting; publishing a large flow
# regularly takes minutes.
DEFAULT_TIMEOUT_SECONDS = 900


class ArchyError(ToolError):
    def __init__(self, command: str, exit_code: int, message: str, results: Any = None) -> None:
        super().__init__(f"archy {command} failed (exit {exit_code}): {message}")
        self.command = command
        self.exit_code = exit_code
        self.results = results


def run(
    root: Path,
    command: str,
    args: list[str],
    *,
    ok_exit_codes: tuple[int, ...] = (0,),
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, Any]]:
    """Run one Archy command and return its exit code with the parsed results."""
    wrapper = root / "archy"
    if not wrapper.is_file():
        raise ArchyError(command, -1, f"Archy wrapper not found at {wrapper}")

    with tempfile.TemporaryDirectory() as tmp:
        results_path = Path(tmp) / "results.json"
        try:
            completed = subprocess.run(
                [
                    str(wrapper),
                    command,
                    *args,
                    "--resultsFile",
                    str(results_path),
                    "--overwriteResultsFile",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ArchyError(command, -1, f"timed out after {timeout}s") from exc

        results: dict[str, Any] = {}
        if results_path.is_file():
            try:
                results = json.loads(results_path.read_text())
            except json.JSONDecodeError:
                results = {}

    if completed.returncode not in ok_exit_codes:
        raise ArchyError(
            command,
            completed.returncode,
            _message(results, completed.stdout, completed.stderr),
            results,
        )

    return completed.returncode, results


def errors(results: dict[str, Any]) -> list[str]:
    """The error traces from a results file, cleaned up.

    Archy prefixes them with "- ERROR! " and appends a bracketed component tag,
    neither of which carries information once they are known to be errors.
    """
    traces = (results.get("output") or {}).get("traces") or []
    texts = [t.get("text", "") for t in traces if t.get("type") == "error"]
    cleaned = [_clean(text) for text in texts if text]
    return [text for text in cleaned if not _is_noise(text)]


# A single failure produces one line saying what went wrong and then most of a
# dozen recording the session tearing itself down. Reporting all of them buries
# the cause and invites a wrong diagnosis.
NOISE = re.compile(
    r"^(setting the Archy exit code"
    r"|ending the Session"
    r"|session startup initialization"
    r"|ArchSession\."
    r"|An error occurred"
    r"|Error\(s\) encountered"
    r"|- Architect Scripting errors"
    r"|exit code:)",
    re.IGNORECASE,
)


def _is_noise(text: str) -> bool:
    return bool(NOISE.match(text))


def _clean(text: str) -> str:
    text = re.sub(r"^-\s*ERROR!\s*", "", text.strip())
    return re.sub(r"\s*--\s*\[.*\]$", "", text).strip()


def _message(results: dict[str, Any], stdout: str, stderr: str) -> str:
    """Pull the useful line out of Archy's output.

    The first error is the real cause; the ones after it are the session
    unwinding, which say nothing about what went wrong.
    """
    found = errors(results)
    if found:
        return found[0]

    tail = (stderr or stdout or "").strip().splitlines()
    return tail[-1] if tail else "no output"
