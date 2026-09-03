#!/usr/bin/env python3
"""
Create (or update) a Genesys Cloud "purecloud-data-actions" Data Action from
a local folder holding one file per part of the action:

  inputschema.json      - JSON Schema (draft-04) for the action's input
  successschema.json    - JSON Schema (draft-04) for the action's success output
  requesttemplate.vm    - Velocity template for the outbound request body
  successtemplate.vm    - Velocity template for the success response body

Creates a draft (POST /api/v2/integrations/actions/drafts), validates it,
publishes it, and writes the resulting action id + full published definition
back into the folder as action.json.

Usage:
    python3 create_data_action.py <folder> <name> <category> <integrationId> <requestUrlTemplate> <requestType>

Requires ~/.archy_config (same client-credentials config used by archy).
"""
import sys
import json
import base64
import time
import urllib.request
import urllib.parse
import urllib.error

from genesys_config import ConfigError, load_config


def get_token(cfg):
    auth = base64.b64encode(f"{cfg['clientId']}:{cfg['clientSecret']}".encode()).decode()
    req = urllib.request.Request(
        f"https://login.{cfg['location']}/oauth/token",
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(req).read())["access_token"]


def api(method, path, token, location, body=None):
    url = f"https://api.{location}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        resp = urllib.request.urlopen(req)
        raw = resp.read()
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        print(f"{method} {path} failed: {e.code} {e.read().decode()}", file=sys.stderr)
        raise


def main():
    if len(sys.argv) != 7:
        print("Usage: create_data_action.py <folder> <name> <category> <integrationId> <requestUrlTemplate> <requestType>", file=sys.stderr)
        sys.exit(1)
    folder, name, category, integration_id, request_url_template, request_type = sys.argv[1:7]

    try:
        cfg = load_config()
    except ConfigError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    token = get_token(cfg)
    location = cfg["location"]

    input_schema = json.load(open(f"{folder}/inputschema.json"))
    success_schema = json.load(open(f"{folder}/successschema.json"))
    request_template = open(f"{folder}/requesttemplate.vm").read()
    success_template = open(f"{folder}/successtemplate.vm").read()
    try:
        translation_map = json.load(open(f"{folder}/translationmap.json"))
    except FileNotFoundError:
        translation_map = {"RECORD_ID": "$.id"}
    try:
        translation_map_defaults = json.load(open(f"{folder}/translationmapdefaults.json"))
    except FileNotFoundError:
        translation_map_defaults = {}

    body = {
        "name": name,
        "category": category,
        "integrationId": integration_id,
        "secure": False,
        "contract": {
            "input": {"inputSchema": input_schema},
            "output": {"successSchema": success_schema},
        },
        "config": {
            "request": {
                "requestUrlTemplate": request_url_template,
                "requestType": request_type,
                "requestTemplate": request_template,
                "headers": {
                    "Content-Type": "application/json",
                    "UserAgent": "PureCloudIntegrations/1.0",
                },
            },
            "response": {
                "translationMap": translation_map,
                "translationMapDefaults": translation_map_defaults,
                "successTemplate": success_template,
            },
        },
    }

    print(f"Creating draft '{name}'...")
    draft = api("POST", "/api/v2/integrations/actions/drafts", token, location, body)
    action_id = draft["id"]
    print(f"Draft created: {action_id}")

    print("Validating draft...")
    validation = api("GET", f"/api/v2/integrations/actions/{action_id}/draft/validation", token, location)
    print(f"Validation valid: {validation.get('valid')}")
    if not validation.get("valid"):
        print(json.dumps(validation, indent=2), file=sys.stderr)
        sys.exit(1)

    print("Publishing...")
    api("POST", f"/api/v2/integrations/actions/{action_id}/draft/publish", token, location, {"version": draft["version"]})

    published = api("GET", f"/api/v2/integrations/actions/{action_id}?includeConfig=true", token, location)
    with open(f"{folder}/action.json", "w") as f:
        json.dump(published, f, indent=2)
        f.write("\n")

    print(f"Done. Published action id: {action_id}")
    print(f"Wrote {folder}/action.json")


if __name__ == "__main__":
    main()
