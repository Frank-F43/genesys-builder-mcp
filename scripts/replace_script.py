#!/usr/bin/env python3
"""
Replace and publish a Genesys Cloud Scripter script from a local .script
file - the automated equivalent of the Scripter UI's Import -> Replace.

This uses an endpoint that is NOT in the public /api/v2 Swagger spec (and
therefore not in the Genesys Cloud CLI either, since that's generated from
the spec): a multipart upload to the org's *app* domain, not the api
domain. Confirmed working by empirical testing against a disposable script,
2026-08-21 - full round trip verified (create, replace-in-place via
scriptIdToReplace with a content diff confirming the replace actually
happened, publish, delete), not just copied from an unverified source.

The three-step flow:
  1. POST https://apps.<region>/uploads/v2/scripter
     multipart/form-data: file, scriptName, scriptIdToReplace
     -> {"correlationId": "..."}  (this IS the uploadId for step 2)
  2. GET  /api/v2/scripts/uploads/{uploadId}/status?longPoll=false
     -> {"succeeded": true/false, "message": "..."}  (poll until settled)
  3. POST /api/v2/scripts/published
     body: {"scriptId": "..."}   -> publishes the just-replaced version

Usage:
    python3 replace_script.py <path-to.script> <scriptId> [scriptName]

    scriptName defaults to the script's own "name" field inside the file.
    Pass it explicitly to rename during replace (rare - usually you want
    the existing name kept).

Requires ~/.archy_config (same client-credentials config used by archy).
"""
import sys
import json
import time
import uuid
import base64
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


def multipart_body(fields, file_field_name, file_path):
    boundary = "----WebKitFormBoundary" + uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    file_content = open(file_path, "rb").read()
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field_name}"; filename="upload.script"\r\n'
        f'Content-Type: application/octet-stream\r\n\r\n'.encode() + file_content + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def main():
    if len(sys.argv) not in (3, 4):
        print("Usage: python3 replace_script.py <path-to.script> <scriptId> [scriptName]", file=sys.stderr)
        sys.exit(1)
    script_path, script_id = sys.argv[1], sys.argv[2]

    try:
        cfg = load_config()
    except ConfigError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    script_name = sys.argv[3] if len(sys.argv) == 4 else json.load(open(script_path)).get("name")
    if not script_name:
        print("Could not determine scriptName - pass it explicitly.", file=sys.stderr)
        sys.exit(1)

    token = get_token(cfg)
    app_domain = "apps." + cfg["location"]

    print(f"Uploading '{script_path}' to replace script {script_id} (\"{script_name}\")...")
    body, boundary = multipart_body(
        {"scriptName": script_name, "scriptIdToReplace": script_id}, "file", script_path
    )
    req = urllib.request.Request(
        f"https://{app_domain}/uploads/v2/scripter",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        upload_resp = json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        print(f"Upload failed: {e.code} {e.read().decode()}", file=sys.stderr)
        sys.exit(1)
    upload_id = upload_resp["correlationId"]
    print(f"Upload accepted, uploadId={upload_id}. Polling status...")

    headers = {"Authorization": f"Bearer {token}"}
    status = None
    for attempt in range(15):
        req = urllib.request.Request(
            f"https://api.{cfg['location']}/api/v2/scripts/uploads/{upload_id}/status?longPoll=false",
            headers=headers,
        )
        try:
            status = json.loads(urllib.request.urlopen(req).read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # the upload record isn't queryable for a moment right after
                # the initial POST - not a real failure, just not ready yet
                time.sleep(2)
                continue
            raise
        if "succeeded" in status:
            break
        time.sleep(2)
    else:
        print("Timed out waiting for upload status.", file=sys.stderr)
        sys.exit(1)

    if not status.get("succeeded"):
        print(f"Upload did not succeed: {status}", file=sys.stderr)
        sys.exit(1)
    print(f"Upload succeeded: {status.get('message')}")

    print("Publishing...")
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"https://api.{cfg['location']}/api/v2/scripts/published",
        data=json.dumps({"scriptId": script_id}).encode(),
        headers=headers,
        method="POST",
    )
    try:
        publish_resp = json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        print(f"Publish failed: {e.code} {e.read().decode()}", file=sys.stderr)
        sys.exit(1)

    print(f"Done. Published version: {publish_resp.get('versionId')}, "
          f"publishedDate: {publish_resp.get('publishedDate')}")


if __name__ == "__main__":
    main()
