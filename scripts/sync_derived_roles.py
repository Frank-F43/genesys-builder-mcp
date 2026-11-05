#!/usr/bin/env python3
"""Maintain the two roles that are derived from the org's permission catalog.

    scripts/sync_derived_roles.py delta             # dry run
    scripts/sync_derived_roles.py delta --apply
    scripts/sync_derived_roles.py agent --apply

``delta``
    Everything the default roles do not cover. Genesys ships default roles that
    together span only about three quarters of the catalog - newer areas
    (workitems, caseManagement, socialmedia, aiStudio) often arrive with no
    default role at all, so a permission exists in the org but nobody can hold
    it. Historically each gap bred its own custom role. Default roles plus Delta
    now cover the catalog exactly.

``agent``
    Everything except the permissions that would let the holder grant itself
    more. Meant for an automation's OAuth client: full reach over the org's
    configuration, no path to privilege escalation. See ESCALATION below.

Both are recomputed from the live catalog, so re-run after a Genesys release -
the catalog grows and the roles should follow.

Credentials come from GENESYSCLOUD_* or the project's .cursor/mcp.json. The client running this needs authorization:role:view
and :edit - which is deliberately *not* true for the ``agent`` role it creates.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from genesys_config import ConfigError, load_config  # noqa: E402

# --------------------------------------------------------------------------
# ESCALATION - permissions withheld from the agent role.
#
# The threat is not an agent going rogue, it is whoever reaches the client
# credentials. Everything here is a path from "can configure the org" to "can
# grant myself more", so each entry closes a loop rather than protecting data.
# Read access stays granted throughout: analysis must keep working.
# --------------------------------------------------------------------------

# Entities where every write action is withheld. `view` survives.
DENIED_WRITE_ENTITIES = {
    # Editing a role means writing any permission into one's own role.
    ("authorization", "role"),
    # Granting roles to subjects - the other half of the same loop.
    ("authorization", "grant"),
    # A second route to permissions that never touches a role.
    ("authorization", "policy"),
    # Divisions decide the scope every grant is evaluated in.
    ("authorization", "division"),
    # Holds the Role Backfill switch; turning it off starves the default roles.
    ("authorization", "settings"),
    # New clients, and `edit` is what rotates a client secret.
    ("oauth", "client"),
    ("directory", "org"),
    ("directory", "organization"),
}

# Org trust lets administrators of a *foreign* organization in - an escalation
# that needs no local user and no local role, and is easy to overlook.
DENIED_TRUST_PREFIXES = ("orgTrustee", "orgTrustor")

# Creating an identity, or taking over an existing one. `edit` is deliberately
# not here: maintaining demo users has to stay possible. It does leave a
# residual path (change an admin's address, then trigger a password reset).
DENIED_USER_ACTIONS = {"add", "delete", "invite", "setPassword", "reconcileEmail"}

AGENT_ROLE_NAME = "oAuth-Cursor Agent (no permission granting)"
AGENT_ROLE_DESCRIPTION = (
    "Derived role for the Cursor automation's OAuth client: the full permission "
    "catalog except the permissions that would let the client grant itself more "
    "(roles, grants, policies, divisions, OAuth clients, user creation, org "
    "trust). Assign this role on its own - adding any default role puts the "
    "withheld permissions back. Do not edit by hand: "
    "toolkit/scripts/sync_derived_roles.py agent --apply."
)

DELTA_ROLE_NAME = "Delta"
DELTA_ROLE_DESCRIPTION = (
    "Derived role: every permission in the org catalog that no default role "
    "covers. Do not edit by hand: "
    "toolkit/scripts/sync_derived_roles.py delta --apply."
)


def get_token(cfg: dict[str, str]) -> str:
    auth = base64.b64encode(f"{cfg['clientId']}:{cfg['clientSecret']}".encode()).decode()
    req = urllib.request.Request(
        f"https://login.{cfg['location']}/oauth/token",
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req) as response:
        return json.load(response)["access_token"]


def api(cfg, token, path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"https://api.{cfg['location']}{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 4:
                time.sleep(5)
                continue
            raise RuntimeError(f"{exc.code} {method} {path}: {exc.read().decode()[:400]}") from exc


def paged(cfg, token, path):
    page = 1
    while True:
        sep = "&" if "?" in path else "?"
        data = api(cfg, token, f"{path}{sep}pageSize=500&pageNumber={page}")
        yield from data.get("entities", [])
        if page >= data.get("pageCount", 1):
            return
        page += 1


def catalog(cfg, token) -> set[tuple[str, str, str]]:
    """Every permission the org knows, as ``(domain, entity, action)``."""
    out = set()
    for entry in paged(cfg, token, "/api/v2/authorization/permissions"):
        domain = entry.get("domain")
        for policies in (entry.get("permissionMap") or {}).values():
            for policy in policies:
                out.add((domain, policy.get("entityType"), policy.get("action")))
    return out


def triples(policies) -> set[tuple[str, str, str]]:
    out = set()
    for policy in policies or []:
        for action in policy.get("actionSet") or []:
            out.add((policy.get("domain"), policy.get("entityName"), action))
    return out


def to_policies(items) -> list[dict]:
    """Collapse triples back into the actionSet-per-entity shape the API wants."""
    grouped = defaultdict(set)
    for domain, entity, action in items:
        grouped[(domain, entity)].add(action)
    return [
        {
            "domain": domain,
            "entityName": entity,
            "actionSet": sorted(actions),
            "allowConditions": False,
        }
        for (domain, entity), actions in sorted(grouped.items())
    ]


def is_escalation(triple) -> bool:
    domain, entity, action = triple
    if action == "view":
        return False
    if (domain, entity) in DENIED_WRITE_ENTITIES:
        return True
    if domain == "authorization" and entity.startswith(DENIED_TRUST_PREFIXES):
        return True
    if domain == "directory" and entity == "user" and action in DENIED_USER_ACTIONS:
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", choices=["delta", "agent"], help="which derived role to sync")
    parser.add_argument("--apply", action="store_true", help="write it; default is a dry run")
    args = parser.parse_args()

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    token = get_token(cfg)

    everything = catalog(cfg, token)
    roles = list(paged(cfg, token, "/api/v2/authorization/roles"))
    full = [api(cfg, token, f"/api/v2/authorization/roles/{r['id']}") for r in roles]

    if args.spec == "delta":
        name, description = DELTA_ROLE_NAME, DELTA_ROLE_DESCRIPTION
        covered = set()
        defaults = 0
        for role in full:
            if role.get("defaultRoleId"):
                defaults += 1
                covered |= triples(role.get("permissionPolicies"))
        wanted = everything - covered
        print(f"Default-Rollen ....... {defaults} mit {len(covered)} Permissions")
    else:
        name, description = AGENT_ROLE_NAME, AGENT_ROLE_DESCRIPTION
        withheld = {t for t in everything if is_escalation(t)}
        wanted = everything - withheld
        print(f"Gesperrt ............. {len(withheld)} Permissions")
        for t in sorted(withheld):
            print(f"    {':'.join(t)}")

    target = next((r for r in full if r["name"] == name), None)
    current = triples(target.get("permissionPolicies")) if target else set()

    print(f"\nOrg .................. {cfg['location']}")
    print(f"Katalog .............. {len(everything)}")
    print(f"Rolle ................ {name}")
    print(f"  soll ............... {len(wanted)} Permissions ({len(to_policies(wanted))} Policies)")
    print(f"  hat ................ {len(current)}" if target else "  existiert noch nicht")
    print(f"  hinzuzufuegen ...... {len(wanted - current)}")
    print(f"  zu entfernen ....... {len(current - wanted)}")

    stale_text = bool(target) and (target.get("description") or "") != description
    if stale_text:
        print("  Beschreibung ....... weicht ab, wird mitgeschrieben")

    if target and wanted == current and not stale_text:
        print("\nNichts zu tun.")
        return 0
    if not args.apply:
        print("\nDry run - mit --apply schreiben.")
        return 0

    body = {"name": name, "description": description, "permissionPolicies": to_policies(wanted)}
    if target:
        api(cfg, token, f"/api/v2/authorization/roles/{target['id']}", "PUT", body)
        print(f"\n{name} aktualisiert ({target['id']}).")
    else:
        created = api(cfg, token, "/api/v2/authorization/roles", "POST", body)
        print(f"\n{name} angelegt ({created['id']}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
