"""Getting flow YAML in and out of the org through Architect jobs.

Genesys Cloud runs Archy itself behind two endpoints, always in its current
version, so no local binary is involved:

- ``POST /api/v2/flows/jobs`` hands out a pre-signed URL. A YAML file PUT there
  is validated and, when valid, published - creating the flow if its name and
  type are new. An invalid file is rejected before anything is created or
  changed. There is no validate-only mode for a file.
- ``POST /api/v2/flows/export/jobs`` exports existing flows as YAML.

Both run asynchronously and are polled. Their messages are Archy's own output,
so a failure is a page of session bookkeeping around one or two lines that say
what is actually wrong; ``errors`` extracts those.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .client import get_client

# Publishing a large flow regularly takes minutes.
DEFAULT_TIMEOUT_SECONDS = 900
POLL_INTERVAL_SECONDS = 3

DONE = {"Success", "Failure"}


class ArchitectJobError(ToolError):
    def __init__(self, what: str, message: str, result: JobResult | None = None) -> None:
        super().__init__(f"{what} failed: {message}")
        self.result = result


@dataclass
class JobResult:
    job_id: str
    status: str
    flow_id: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    download_url: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "Success"

    @property
    def errors(self) -> list[str]:
        return errors(self.messages)

    @property
    def warnings(self) -> list[str]:
        return [m.get("text", "") for m in self.messages if m.get("type") == "Warning"]


def publish_yaml(content: bytes, *, create_stubs: bool = False, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> JobResult:
    """Upload flow YAML, which publishes it when it is valid."""
    client = get_client()
    job = client.request("POST", "/api/v2/flows/jobs", json_body={"createStubs": create_stubs} if create_stubs else {})
    client.put_raw(job["presignedUrl"], content=content, headers=job.get("headers") or {})
    return _wait(f"/api/v2/flows/jobs/{job['id']}", job["id"], timeout)


def export_yaml(flow_id: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> str:
    """The published flow as YAML text."""
    client = get_client()
    job = client.request(
        "POST",
        "/api/v2/flows/export/jobs",
        json_body={"flows": [{"flow": {"id": flow_id}, "exportType": "Yaml"}]},
    )
    result = _wait(f"/api/v2/flows/export/jobs/{job['id']}", job["id"], timeout)
    if not result.succeeded or not result.download_url:
        raise ArchitectJobError("Exporting the flow", _first(result), result)
    return client.get_raw(result.download_url).decode("utf-8")


_LANGUAGE_KEY = re.compile(r"^    ([a-z]{2,3}-[a-z0-9]{2,4}):\s*$")
_ENGINE_NAME = re.compile(r"^(          name: )(.*)$")


def correct_speech_engines(text: str, flow_id: str, version: str) -> tuple[str, list[str]]:
    """Put the published speech-to-text engines back into an exported flow.

    The export job names the engine of a bot flow's languages wrongly: a flow
    published with "Genesys Enhanced v2" comes back as "Microsoft Azure".
    Publishing that file would quietly switch the bot's speech recognition, so
    each language's engine is taken from the published configuration instead.
    """
    config = get_client().get(f"/api/v2/flows/{flow_id}/versions/{version}/configuration") or {}
    published = {
        (option.get("language") or "").lower(): ((option.get("speechToText") or {}).get("engine") or {}).get("name")
        for option in config.get("supportedLanguageOptions") or []
    }
    published = {language: name for language, name in published.items() if language and name}
    if not published:
        return text, []

    lines = text.split("\n")
    corrections = []
    in_languages = False
    language = None
    previous = ""
    for index, line in enumerate(lines):
        if re.match(r"^  \S", line):
            in_languages = line.rstrip() == "  supportedLanguages:"
            language = None
        elif in_languages and (key := _LANGUAGE_KEY.match(line)):
            language = key.group(1)
        elif in_languages and language and previous.rstrip() == "        engine:" and (name := _ENGINE_NAME.match(line)):
            exported = name.group(2).strip().strip("\"'")
            wanted = published.get(language)
            if wanted and exported != wanted:
                lines[index] = name.group(1) + (json.dumps(wanted) if re.search(r"[:#'\"]", wanted) else wanted)
                corrections.append(f"{language}: speech-to-text engine {exported!r} -> {wanted!r}, as published")
        previous = line
    return "\n".join(lines), corrections


def find_flow(name: str, api_type: str) -> dict[str, Any] | None:
    """The org's flow with exactly this name and type, if there is one."""
    client = get_client()
    for flow in client.paginate("/api/v2/flows", params={"name": name, "type": api_type}):
        if (flow.get("name") or "").lower() == name.lower():
            return flow
    return None


def delete_flow(flow_id: str) -> None:
    get_client().request("DELETE", f"/api/v2/flows/{flow_id}")


def unlock_flow(flow_id: str) -> None:
    """Release a check-out held in Architect, which would otherwise block a publish."""
    get_client().request("POST", "/api/v2/flows/actions/unlock", params={"flow": flow_id})


def _wait(path: str, job_id: str, timeout: int) -> JobResult:
    client = get_client()
    deadline = time.monotonic() + timeout
    while True:
        state = client.get(path, params={"expand": "messages"}) or {}
        status = state.get("status", "")
        if status in DONE:
            return JobResult(
                job_id=job_id,
                status=status,
                flow_id=(state.get("flow") or {}).get("id"),
                messages=state.get("messages") or [],
                download_url=state.get("downloadUrl"),
            )
        if time.monotonic() > deadline:
            raise ArchitectJobError(f"Architect job {job_id}", f"still {status or 'pending'} after {timeout}s")
        time.sleep(POLL_INTERVAL_SECONDS)


def errors(messages: list[dict[str, Any]]) -> list[str]:
    """The error messages that carry information, cleaned up.

    Archy prefixes them with "- ERROR! " and appends a bracketed session tag,
    neither of which carries information once they are known to be errors.
    """
    texts = [m.get("text", "") for m in messages if m.get("type") == "Error"]
    cleaned = [_clean(text) for text in texts if text]
    return [text for text in cleaned if text and not _is_noise(text)]


# A single failure produces one line saying what went wrong and then most of a
# dozen recording the session tearing itself down. Reporting all of them buries
# the cause and invites a wrong diagnosis. The validation summary repeats the
# numbered issues that follow it.
NOISE = re.compile(
    r"^(setting the Archy exit code"
    r"|Validation Summary"
    r"|ERROR the flow has \d+ error"
    r"|ending the Session"
    r"|session startup initialization"
    r"|ArchSession\."
    r"|An error occurred"
    r"|Error\(s\) encountered"
    r"|- Architect Scripting errors"
    r"|Architect Scripting errors will be listed"
    r"|___ Yaml Info ___"
    r"|exit code:)",
    re.IGNORECASE,
)


def _is_noise(text: str) -> bool:
    return bool(NOISE.match(text))


def _clean(text: str) -> str:
    text = re.sub(r"^-\s*ERROR!\s*", "", text.strip())
    return re.sub(r"\s*--\s*\[[^\]]*\]$", "", text).strip()


def _first(result: JobResult) -> str:
    found = result.errors
    return found[0] if found else f"job {result.job_id} ended with status {result.status}"
