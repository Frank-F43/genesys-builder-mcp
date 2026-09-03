"""Print what this server actually tells a client about its tools.

Editors cache a server's tool list for the length of a session, so a
description can still read as it did before an edit even though the server is
already serving the new one. That looks like a failed restart and sends you
looking in the wrong place. This speaks the protocol to a fresh server, so what
it prints is what is really being served.

    uv run python show_tools.py                 every tool, one line each
    uv run python show_tools.py knowledge       full text of matching tools

Note that the server must be given time to answer before its stdin closes,
which is why this is a script and not a shell pipeline.
"""

from __future__ import annotations

import json
import subprocess
import sys

COMMAND = ["uv", "run", "genesys-builder-mcp"]
TIMEOUT_SECONDS = 120


def list_tools() -> dict[str, str]:
    server = subprocess.Popen(
        COMMAND, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1,
    )

    def send(message: dict) -> None:
        server.stdin.write(json.dumps(message) + "\n")
        server.stdin.flush()

    def reply(expect_id: int) -> dict | None:
        while True:
            line = server.stdout.readline()
            if not line:
                return None
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == expect_id:
                return message

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "show_tools", "version": "1"},
        }})
        if reply(1) is None:
            sys.exit(f"The server did not start:\n{server.stderr.read()[-1500:]}")

        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        answer = reply(2)
        if answer is None:
            sys.exit("The server did not return a tool list.")
        return {t["name"]: (t.get("description") or "").strip() for t in answer["result"]["tools"]}
    finally:
        if server.stdin and not server.stdin.closed:
            server.stdin.close()
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


def main() -> None:
    pattern = sys.argv[1].lower() if len(sys.argv) > 1 else None
    tools = list_tools()

    if pattern is None:
        print(f"{len(tools)} tools\n")
        for name, description in sorted(tools.items()):
            first_line = description.splitlines()[0] if description else "(no description)"
            print(f"  {name:30} {first_line}")
        return

    matches = {n: d for n, d in tools.items() if pattern in n.lower() or pattern in d.lower()}
    if not matches:
        sys.exit(f"Nothing matching {pattern!r} among {len(tools)} tools.")
    for name, description in sorted(matches.items()):
        print(f"=== {name}")
        print(description or "(no description)")
        print()


main()
