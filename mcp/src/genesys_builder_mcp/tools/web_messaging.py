"""Web Messaging tools: Messenger configurations and web deployments."""

from __future__ import annotations

import copy
import time
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError

from ..annotations import READ_ONLY, WRITES
from ..client import ApiError, get_client
from ..resolve import resolve
from ..write_guard import attach_write_impact, elicit_write_confirmation, make_write_impact
from .knowledge import _resolve_knowledge_base_id

CONFIGURATIONS = "/api/v2/webdeployments/configurations"
DEPLOYMENTS = "/api/v2/webdeployments/deployments"

# Every successful write flips the draft to "Pending" for several seconds, and a
# write landing in that window fails with the same bare illegal.operation as an
# invalid field. Waiting for "Active" is what keeps the two apart.
PENDING_POLL_SECONDS = 3
PENDING_TIMEOUT_SECONDS = 90

SYSTEM_FIELDS = {
    "id", "version", "status", "selfUri", "snippet",
    "dateCreated", "dateModified", "datePublished",
    "createdUser", "lastModifiedUser", "publishedUser",
}

# What a configuration is created with before the rest is written onto it.
BASE_FIELDS = ("name", "description", "languages", "defaultLanguage")

HUMANIZE_PATH = ("messenger", "apps", "conversations", "humanize")

# The Messenger UI says "less than 15 characters"; the API enforces the same
# limit but only answers illegal.operation.
MAX_BOT_NAME_LENGTH = 14


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def list_messenger_configurations(name_contains: str | None = None) -> list[dict[str, Any]]:
        """List Messenger configurations with their draft status and published version.

        A configuration only reaches visitors through a web deployment, and only
        in the version the deployment pins. list_web_deployments shows which.
        """
        client = get_client()
        results = []
        for item in _entities(client, CONFIGURATIONS):
            name = item.get("name") or ""
            if name_contains and name_contains.lower() not in name.lower():
                continue
            messenger = item.get("messenger") or {}
            # showOnlyPublished only filters which configurations are listed;
            # each still comes back as its draft, so the version needs its own call.
            versions = _entities(client, f"{CONFIGURATIONS}/{item.get('id')}/versions")
            numbered = [int(v["version"]) for v in versions if str(v.get("version", "")).isdigit()]
            results.append(
                {
                    "id": item.get("id"),
                    "name": name,
                    "description": item.get("description"),
                    "languages": item.get("languages"),
                    "draft_status": item.get("status"),
                    "published_version": str(max(numbered)) if numbered else None,
                    "primary_color": (messenger.get("styles") or {}).get("primaryColor"),
                }
            )
        return results

    @mcp.tool(annotations=READ_ONLY)
    def list_web_deployments(name_contains: str | None = None) -> list[dict[str, Any]]:
        """List web deployments with the flow and configuration version each one serves."""
        client = get_client()
        results = []
        for item in _entities(client, DEPLOYMENTS):
            name = item.get("name") or ""
            if name_contains and name_contains.lower() not in name.lower():
                continue
            configuration = item.get("configuration") or {}
            results.append(
                {
                    "id": item.get("id"),
                    "name": name,
                    "description": item.get("description"),
                    "status": item.get("status"),
                    "flow": (item.get("flow") or {}).get("name"),
                    "configuration": configuration.get("name"),
                    "configuration_version": configuration.get("version"),
                    "allow_all_domains": item.get("allowAllDomains"),
                }
            )
        return results

    @mcp.tool(annotations=WRITES)
    def copy_messenger_configuration(
        source: str,
        name: str,
        description: str | None = None,
        primary_color: str | None = None,
        logo_url: str | None = None,
        knowledge_base: str | None = None,
        bot_name: str | None = None,
        bot_avatar_url: str | None = None,
        publish: bool = True,
    ) -> dict[str, Any]:
        """Copy a Messenger configuration under a new name, with brand overrides.

        Copies the source's latest published version (its draft if it was never
        published), so work in progress on the source is not carried over.

        The API reports every rejected write as 400 illegal.operation with no
        field named. The copy therefore creates a bare configuration, writes the
        full body onto it, and if that is refused, adds the sections one at a
        time and leaves out the ones the platform refuses. Those are listed in
        skipped_fields and have to be set in the Messenger UI. That fallback
        waits out the Pending state after every accepted section, so it can
        take a few minutes.

        The bot name must be under 15 characters. The API refuses a longer one
        with that same bare illegal.operation, so it is checked up front.

        Logos and avatars are plain URLs. An external https URL is accepted.
        Uploading an image into Genesys public assets needs
        uploads:publicasset:upload, which a demo client usually lacks.

        Args:
            source: Name or id of the configuration to copy.
            name: Name of the new configuration. Refused if one already exists.
            description: Defaults to the source's description.
            primary_color: Hex colour for the Messenger, e.g. "#2167AE".
            logo_url: Home screen logo.
            knowledge_base: Name or id of the knowledge base the Messenger
                searches.
            bot_name: Name shown for bot messages, at most 14 characters.
            bot_avatar_url: Avatar shown for bot messages.
            publish: Publish version 1 straight away. Without it the copy stays
                a draft that no deployment can pin.
        """
        if bot_name and len(bot_name) > MAX_BOT_NAME_LENGTH:
            raise ToolError(
                f"Bot name {bot_name!r} has {len(bot_name)} characters; the Messenger allows "
                f"at most {MAX_BOT_NAME_LENGTH}."
            )
        client = get_client()
        source_id = _configuration_id(source)
        source_config, source_version = _latest_published(client, source_id)

        if any((c.get("name") or "").lower() == name.lower() for c in _entities(client, CONFIGURATIONS)):
            raise ToolError(f"A Messenger configuration named {name!r} already exists.")

        body = _strip(source_config)
        body["name"] = name
        if description is not None:
            body["description"] = description

        overrides: dict[str, Any] = {}
        if primary_color:
            _set(body, ("messenger", "styles", "primaryColor"), primary_color)
            overrides["primary_color"] = primary_color
        if logo_url:
            _set(body, ("messenger", "homeScreen", "enabled"), True)
            _set(body, ("messenger", "homeScreen", "logoUrl"), logo_url)
            overrides["logo_url"] = logo_url
        if knowledge_base:
            kb_id = _resolve_knowledge_base_id(knowledge_base)
            _set(body, ("messenger", "apps", "knowledge", "enabled"), True)
            _set(body, ("messenger", "apps", "knowledge", "knowledgeBase"), {"id": kb_id})
            overrides["knowledge_base_id"] = kb_id

        if bot_name or bot_avatar_url:
            _set(body, HUMANIZE_PATH + ("enabled",), True)
            if bot_name:
                _set(body, HUMANIZE_PATH + ("bot", "name"), bot_name)
                overrides["bot_name"] = bot_name
            if bot_avatar_url:
                _set(body, HUMANIZE_PATH + ("bot", "avatarUrl"), bot_avatar_url)
                overrides["bot_avatar_url"] = bot_avatar_url

        created = client.request(
            "POST", CONFIGURATIONS, json_body={k: body[k] for k in BASE_FIELDS if k in body}
        )
        config_id = created["id"]
        draft = f"{CONFIGURATIONS}/{config_id}/versions/draft"

        skipped: list[dict[str, str]] = []
        _wait_active(client, draft)
        try:
            client.request("PUT", draft, json_body=body)
            accepted = body
        except ApiError as exc:
            if exc.status != 400:
                raise
            _, skipped = _write_section_by_section(client, draft, body)

        published_version = None
        if publish:
            _wait_active(client, draft)
            published_version = client.request("POST", f"{draft}/publish").get("version")

        return attach_write_impact(
            {
                "id": config_id,
                "name": name,
                "source": source_config.get("name"),
                "source_version": source_version,
                "published_version": published_version,
                "overrides": overrides,
                "skipped_fields": skipped,
            },
            make_write_impact(
                action="applied",
                summary=(
                    f"Created Messenger configuration {name!r} from {source_config.get('name')!r} "
                    f"v{source_version}"
                    + (f", published as v{published_version}" if published_version else ", left as draft")
                    + (f"; {len(skipped)} field(s) skipped." if skipped else ".")
                ),
            ),
        )

    @mcp.tool(annotations=WRITES)
    async def set_deployment_configuration(
        deployment: str,
        configuration: str,
        version: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Point a web deployment at a Messenger configuration version.

        Visitors on every site embedding the deployment get the new Messenger
        on their next page load, so this asks before replacing a different
        configuration.

        Args:
            deployment: Name or id of the web deployment.
            configuration: Name or id of the Messenger configuration.
            version: Published version to pin. Defaults to the latest; a draft
                cannot be pinned.
        """
        client = get_client()
        deployment_id = _deployment_id(deployment)
        configuration_id = _configuration_id(configuration)
        current = client.get(f"{DEPLOYMENTS}/{deployment_id}")

        if version is None:
            target, version = _latest_published(client, configuration_id, require_published=True)
        else:
            target = client.get(f"{CONFIGURATIONS}/{configuration_id}/versions/{version}")

        before = current.get("configuration") or {}
        previous_state = {"configuration": before.get("name"), "configuration_id": before.get("id"), "version": before.get("version")}
        diff = {
            "before": previous_state,
            "after": {"configuration": target.get("name"), "configuration_id": configuration_id, "version": version},
        }
        result = {"deployment_id": deployment_id, "deployment": current.get("name")}

        if before.get("id") == configuration_id and str(before.get("version")) == str(version):
            return attach_write_impact(
                result,
                make_write_impact(
                    action="skipped_no_change",
                    summary=f"{current.get('name')!r} already serves {target.get('name')!r} v{version}.",
                    diff=diff,
                    previous_state=previous_state,
                ),
            )

        outcome, unconfirmed = await elicit_write_confirmation(
            ctx,
            f"Switch web deployment {current.get('name')!r} from {before.get('name')!r} "
            f"v{before.get('version')} to {target.get('name')!r} v{version}? Every site "
            "embedding it shows the new Messenger on the next page load.",
        )
        if outcome == "declined":
            return attach_write_impact(
                result,
                make_write_impact(
                    action="cancelled",
                    summary=f"{current.get('name')!r} was not changed - confirmation declined.",
                    diff=diff,
                    previous_state=previous_state,
                    confirmation_requested=True,
                    confirmation_accepted=False,
                ),
            )

        body = _strip(current)
        body["configuration"] = {"id": configuration_id, "version": str(version)}
        client.request("PUT", f"{DEPLOYMENTS}/{deployment_id}", json_body=body)

        return attach_write_impact(
            result,
            make_write_impact(
                action="applied",
                summary=(
                    f"{current.get('name')!r} now serves {target.get('name')!r} v{version} "
                    f"(was {before.get('name')!r} v{before.get('version')})."
                ),
                diff=diff,
                previous_state=previous_state,
                confirmation_requested=True,
                confirmation_accepted=(outcome == "confirmed"),
                unconfirmed_reason=unconfirmed,
            ),
        )


def _entities(client: Any, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    # Cursor-paged with a default page of 300, which no demo org comes near.
    return (client.get(path, params=params) or {}).get("entities") or []


def _configuration_id(value: str) -> str:
    client = get_client()
    return resolve(
        value,
        label="Messenger configuration",
        search=lambda v: [c for c in _entities(client, CONFIGURATIONS) if v.lower() in (c.get("name") or "").lower()],
        list_available=lambda: _entities(client, CONFIGURATIONS),
    )


def _deployment_id(value: str) -> str:
    client = get_client()
    return resolve(
        value,
        label="web deployment",
        search=lambda v: [d for d in _entities(client, DEPLOYMENTS) if v.lower() in (d.get("name") or "").lower()],
        list_available=lambda: _entities(client, DEPLOYMENTS),
    )


def _latest_published(
    client: Any, configuration_id: str, *, require_published: bool = False
) -> tuple[dict[str, Any], str]:
    versions = _entities(client, f"{CONFIGURATIONS}/{configuration_id}/versions")
    numbered = [v for v in versions if str(v.get("version", "")).isdigit()]
    if numbered:
        latest = max(numbered, key=lambda v: int(v["version"]))
        return latest, latest["version"]
    if require_published:
        raise ToolError(f"Messenger configuration {configuration_id} has never been published.")
    draft = next((v for v in versions if str(v.get("version")).upper() == "DRAFT"), None)
    if draft is None:
        raise ToolError(f"Messenger configuration {configuration_id} has no versions.")
    return draft, "DRAFT"


def _wait_active(client: Any, draft_path: str) -> None:
    deadline = time.monotonic() + PENDING_TIMEOUT_SECONDS
    while True:
        status = (client.get(draft_path) or {}).get("status")
        if status != "Pending":
            return
        if time.monotonic() > deadline:
            raise ToolError(f"{draft_path} stayed Pending for {PENDING_TIMEOUT_SECONDS}s.")
        time.sleep(PENDING_POLL_SECONDS)


def _write_section_by_section(
    client: Any, draft_path: str, body: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Add sections one at a time, keeping those accepted and naming those refused."""
    accepted = {k: body[k] for k in BASE_FIELDS if k in body}
    messenger = body.get("messenger") or {}
    if messenger:
        accepted["messenger"] = {k: v for k, v in messenger.items() if not isinstance(v, dict)}

    skipped: list[dict[str, str]] = []
    for path in _sections(body):
        attempt = copy.deepcopy(accepted)
        _set(attempt, path, copy.deepcopy(_get(body, path)))
        _wait_active(client, draft_path)
        try:
            client.request("PUT", draft_path, json_body=attempt)
            accepted = attempt
        except ApiError as exc:
            if exc.status != 400:
                raise
            skipped.append({"field": ".".join(path), "error": str(exc)})
    return accepted, skipped


def _sections(body: dict[str, Any]) -> list[tuple[str, ...]]:
    """Top-level fields, with messenger and messenger.apps split into their parts."""
    paths: list[tuple[str, ...]] = []
    for key, value in body.items():
        if key in BASE_FIELDS:
            continue
        if key != "messenger":
            paths.append((key,))
            continue
        for sub, sub_value in value.items():
            if not isinstance(sub_value, dict):
                continue
            if sub == "apps":
                paths.extend(("messenger", "apps", app) for app in sub_value)
            else:
                paths.append(("messenger", sub))
    return paths


def _strip(record: dict[str, Any]) -> dict[str, Any]:
    # Nested ids point at other objects (flow, knowledge base) and must survive,
    # so system fields are only dropped at the top level.
    return {k: _drop_self_uris(v) for k, v in record.items() if k not in SYSTEM_FIELDS}


def _drop_self_uris(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_self_uris(v) for k, v in value.items() if k != "selfUri"}
    if isinstance(value, list):
        return [_drop_self_uris(v) for v in value]
    return value


def _get(body: dict[str, Any], path: tuple[str, ...]) -> Any:
    for key in path:
        body = body[key]
    return body


def _set(body: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    for key in path[:-1]:
        body = body.setdefault(key, {})
    body[path[-1]] = value

