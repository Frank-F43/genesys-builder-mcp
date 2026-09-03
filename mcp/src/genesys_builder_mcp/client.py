"""Thin Genesys Cloud Platform API client.

Deliberately small: token caching, pagination and readable errors. Everything
domain-specific lives in the tool modules.
"""

from __future__ import annotations

import time
from typing import Any, Iterator

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from .config import Config, load_config

# Refresh a little before the token actually expires, so a long-running call
# can't start with a token that dies mid-flight.
TOKEN_EXPIRY_MARGIN_SECONDS = 60


class ApiError(ToolError):
    """A Genesys Cloud API call failed.

    Genesys error bodies carry a machine-readable ``code`` and a message that is
    usually specific enough to act on, so both are surfaced rather than reduced
    to a status code.

    Validation failures are the exception: the top-level message only counts the
    problems ("contains 1 validation errors") and names none of them. What is
    actually wrong sits in a nested ``errors`` list, so that is unpacked too --
    without it, a rejected field looks indistinguishable from a rejected request.
    """

    def __init__(self, method: str, path: str, status: int, body: Any):
        self.method = method
        self.path = path
        self.status = status
        self.body = body

        detail = ""
        if isinstance(body, dict):
            parts = [str(body[k]) for k in ("code", "message") if body.get(k)]
            nested = _nested_errors(body)
            if nested:
                parts.append("; ".join(nested))
            detail = " - ".join(parts)
        elif body:
            detail = str(body)[:500]

        super().__init__(f"{method} {path} failed with {status}" + (f": {detail}" if detail else ""))


class TransportError(ToolError):
    """The API could not be reached at all.

    Separate from ApiError because nothing was rejected - the request never got
    an answer, so the cause is the environment rather than the payload: an
    unreachable region, a proxy, an expired DNS entry, no network. Raising a
    ToolError matters more than the wording: an exception the framework does not
    recognise reaches the caller as a bare "Error executing tool" with the reason
    stripped, which is indistinguishable from a broken tool.
    """

    def __init__(self, method: str, url: str, exc: Exception):
        super().__init__(
            f"{method} {url} could not be reached - {type(exc).__name__}: {exc}. "
            "Check the configured region and that the host is reachable from here."
        )


class GenesysClient:
    def __init__(self, config: Config | None = None):
        self._config = config or load_config()
        self._client = httpx.Client(timeout=60.0)
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._org_id: str | None = None

    @property
    def config(self) -> Config:
        return self._config

    @property
    def org_id(self) -> str:
        """Id of the connected org, cached for the process lifetime.

        Local state written by the flow tools is only meaningful for the org it
        came from, so it is tagged with this.
        """
        if self._org_id is None:
            self._org_id = self.get("/api/v2/organizations/me")["id"]
        return self._org_id

    def token(self) -> str:
        if self._token and time.time() < self._token_expires_at:
            return self._token

        response = self._send(
            "POST",
            f"{self._config.login_base}/oauth/token",
            data={"grant_type": "client_credentials"},
            auth=(self._config.client_id, self._config.client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code != 200:
            raise ApiError("POST", "/oauth/token", response.status_code, _body_of(response))

        payload = response.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.time() + payload.get("expires_in", 86400) - TOKEN_EXPIRY_MARGIN_SECONDS
        return self._token

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise TransportError(method, url, exc) from exc

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        base: str | None = None,
    ) -> Any:
        url = f"{base or self._config.api_base}{path}"
        headers = {"Authorization": f"Bearer {self.token()}"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        response = self._send(method, url, headers=headers, json=json_body, params=params)
        if response.status_code >= 400:
            raise ApiError(method, path, response.status_code, _body_of(response))
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post_multipart(self, path: str, *, fields: dict[str, str], file: tuple[str, bytes], base: str | None = None) -> Any:
        """Multipart form POST, needed by the Scripter upload endpoint."""
        url = f"{base or self._config.api_base}{path}"
        filename, content = file
        response = self._send(
            "POST",
            url,
            headers={"Authorization": f"Bearer {self.token()}"},
            data=fields,
            files={"file": (filename, content, "application/octet-stream")},
        )
        if response.status_code >= 400:
            raise ApiError("POST", path, response.status_code, _body_of(response))
        return response.json() if response.content else None

    def put_raw(self, url: str, *, content: bytes, headers: dict[str, str] | None = None) -> None:
        """PUT to a pre-signed URL. Used by the knowledge import upload step."""
        response = self._send("PUT", url, content=content, headers=headers or {})
        if response.status_code >= 400:
            raise ApiError("PUT", url, response.status_code, _body_of(response))

    def paginate(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        entity_key: str = "entities",
        page_size: int = 100,
        max_items: int | None = None,
        max_pages: int = 500,
    ) -> Iterator[dict[str, Any]]:
        """Walk a paged collection endpoint.

        Genesys Cloud uses two pagination schemes depending on the endpoint:
        most return ``pageCount`` and take a ``pageNumber``, but some (knowledge
        bases among them) are cursor-based and only return a ``nextUri``. Those
        ignore ``pageNumber`` entirely, so treating every endpoint as
        page-numbered re-requests the first page forever.

        ``max_items`` exists because some collections (users, flows) are large
        enough that returning all of them would swamp the model's context.
        ``max_pages`` is a backstop against a malformed response looping.
        """
        base_query = dict(params or {})
        base_query["pageSize"] = page_size

        next_path: str | None = path
        next_params: dict[str, Any] | None = base_query
        pages = 0
        yielded = 0

        while next_path is not None and pages < max_pages:
            pages += 1
            payload = self.get(next_path, params=next_params) or {}

            entities = payload.get(entity_key) or []
            for entity in entities:
                yield entity
                yielded += 1
                if max_items is not None and yielded >= max_items:
                    return

            if not entities:
                return

            page_count = payload.get("pageCount")
            next_uri = payload.get("nextUri")

            if page_count is not None:
                current = payload.get("pageNumber") or pages
                if current >= page_count:
                    return
                next_path, next_params = path, {**base_query, "pageNumber": current + 1}
            elif next_uri:
                # nextUri already carries its own query string, cursor included,
                # so it must not be given separate params.
                next_path, next_params = "/" + next_uri.lstrip("/"), None
            else:
                return

    def close(self) -> None:
        self._client.close()


def _body_of(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _nested_errors(body: dict[str, Any], limit: int = 5) -> list[str]:
    """Pull the individual complaints out of a Genesys validation body.

    Each entry carries its own message and, in ``details``, the field it is
    about. The field name is the useful half -- "size must be between 1 and 50"
    says nothing until you know it is talking about ``changes``.
    """
    messages = []
    for error in body.get("errors") or []:
        if not isinstance(error, dict):
            continue
        text = str(error.get("message") or "").strip()
        fields = [
            str(detail["fieldName"])
            for detail in error.get("details") or []
            if isinstance(detail, dict) and detail.get("fieldName")
        ]
        if fields:
            text = f"{text} [{', '.join(fields)}]" if text else f"[{', '.join(fields)}]"
        if text:
            messages.append(text)
        if len(messages) >= limit:
            break
    return messages


_client: GenesysClient | None = None


def get_client() -> GenesysClient:
    """Shared client, created on first use.

    Constructed lazily so that missing credentials surface as a readable error
    from whichever tool was called, instead of killing the server at startup
    where the IDE would only show "server failed to start".
    """
    global _client
    if _client is None:
        _client = GenesysClient()
    return _client
