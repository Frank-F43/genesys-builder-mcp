#!/usr/bin/env python3
"""
Reset an External Contact: read it fully (incl. custom schema fields and
identifiers), delete it, recreate it from scratch with the same field
values but no merge/identity-stitching history.

Why this exists: Genesys Cloud External Contacts accumulate a "mergeSet"
entry every time a new visitor identity (a browser cookie, typically) gets
stitched onto the same contact via matching email/phone. Repeated demo/test
runs against the same contact (same email/phone every time) inflate this
list fast, and there's a platform limit on how many identities can be
stitched to one contact - once hit, identity stitching starts failing.
Deleting and recreating the contact with the same core data resets the
stitching count to zero while keeping the contact itself (and any
downstream config that references its email/phone) working exactly as
before.

Usage:
    python3 reset_external_contact.py <workEmail>

Requires ~/.archy_config (same client-credentials config used by archy).
The OAuth client's assigned role needs externalContacts:contact
[view, delete, add] - "Architect for Archy" already has this.
"""
import sys
import json
import base64
import urllib.request
import urllib.parse
import urllib.error

from genesys_config import ConfigError, load_config

# Fields to preserve verbatim from the old contact into the new one.
# Deliberately excludes: id, division, modifyDate, createDate, type,
# canonicalContact, mergeSet, mergedFrom, mergedTo, mergeOperation,
# selfUri, identifiers - all either system-generated, merge-history, or
# (for identifiers) exactly what we're trying to reset.
CORE_FIELDS = [
    "firstName", "middleName", "lastName", "salutation", "title",
    "workPhone", "cellPhone", "homePhone", "otherPhone",
    "workEmail", "personalEmail", "otherEmail",
    "address", "twitterId", "lineId", "whatsAppId", "facebookId",
    "instagramId", "appleOpaqueIds", "externalIds",
    "externalOrganization", "surveyOptOut", "externalSystemUrl",
    "customFields",
]

# Phone-type sub-fields to keep when copying a phone object - the rest
# (e164, countryCode, normalizationCountryCode, acceptsSMS) are re-derived
# by the platform from "display" on create.
PHONE_FIELDS = ["display", "extension", "acceptsSMS"]


def get_token(cfg):
    auth = base64.b64encode(f"{cfg['clientId']}:{cfg['clientSecret']}".encode()).decode()
    req = urllib.request.Request(
        f"https://login.{cfg['location']}/oauth/token",
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(req).read())["access_token"]


def api(cfg, token, method, path, body=None):
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"https://api.{cfg['location']}{path}", data=data, headers=headers, method=method
    )
    try:
        resp = urllib.request.urlopen(req)
        if resp.status == 204 or resp.length == 0:
            return None
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"ERROR {method} {path}: {e.read().decode()}", file=sys.stderr)
        raise


def clean_phone(phone):
    if not phone:
        return phone
    return {k: v for k, v in phone.items() if k in PHONE_FIELDS and v is not None}


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 reset_external_contact.py <workEmail>", file=sys.stderr)
        sys.exit(1)
    email = sys.argv[1]

    try:
        cfg = load_config()
    except ConfigError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    token = get_token(cfg)

    q = urllib.parse.quote(email)
    found = api(cfg, token, "GET", f"/api/v2/externalcontacts/contacts?q={q}")
    entities = found.get("entities", [])
    if not entities:
        print(f"No external contact found matching '{email}'.")
        sys.exit(1)
    if len(entities) > 1:
        print(f"WARNING: {len(entities)} contacts matched '{email}', using the first one:")
        for e in entities:
            print(f"  - {e['id']}: {e.get('firstName')} {e.get('lastName')}")

    old = entities[0]
    old_id = old["id"]
    merge_count = len(old.get("mergeSet", []))
    print(f"Found contact {old_id}: {old.get('firstName')} {old.get('lastName')} "
          f"({email}) - mergeSet has {merge_count} entries.")

    identifiers = api(cfg, token, "GET", f"/api/v2/externalcontacts/contacts/{old_id}/identifiers")
    cookie_count = sum(1 for i in identifiers.get("entities", []) if i.get("type") == "Cookie")
    print(f"Identifiers: {len(identifiers.get('entities', []))} total, {cookie_count} of type Cookie.")

    new_body = {}
    for field in CORE_FIELDS:
        val = old.get(field)
        if val is None:
            continue
        if field in ("workPhone", "cellPhone", "homePhone", "otherPhone"):
            val = clean_phone(val)
        if field == "externalOrganization" and val:
            val = {"id": val["id"]}
        new_body[field] = val
    if old.get("schema"):
        new_body["schema"] = {"id": old["schema"]["id"], "version": old["schema"]["version"]}

    print(f"Deleting {old_id}...")
    api(cfg, token, "DELETE", f"/api/v2/externalcontacts/contacts/{old_id}")

    print("Recreating with the same field values, no merge history...")
    new = api(cfg, token, "POST", "/api/v2/externalcontacts/contacts", new_body)

    print(f"Done. New contact id: {new['id']} (mergeSet reset to empty).")
    print(json.dumps(new, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
