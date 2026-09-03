#!/usr/bin/env bash
#
# Set up the genesys-builder MCP server and print the editor configuration
# with the absolute paths already filled in.
#
#   ./setup.sh
#
set -euo pipefail

mcp_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

bold=$'\033[1m'; dim=$'\033[2m'; red=$'\033[31m'; green=$'\033[32m'; reset=$'\033[0m'

step() { printf '\n%s==>%s %s\n' "$bold" "$reset" "$1"; }
ok()   { printf '  %s✓%s %s\n' "$green" "$reset" "$1"; }
warn() { printf '  %s!%s %s\n' "$red" "$reset" "$1"; }

step "Checking for uv"
if ! command -v uv >/dev/null 2>&1; then
    warn "uv is not installed."
    echo "     Install it with:  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "     Then run this script again."
    exit 1
fi
uv_path="$(command -v uv)"
ok "$uv_path"

step "Installing dependencies"
uv sync --directory "$mcp_dir" --quiet
ok "virtual environment ready"

step "Checking credentials"
# Resolved by the server itself, so this reports exactly what it will see.
if uv run --directory "$mcp_dir" python -c "
from genesys_builder_mcp.config import load_config
c = load_config()
print(f'  region {c.region}, client {c.client_id[:8]}..., from {c.source}')
" 2>/dev/null; then
    ok "credentials resolve"
    creds_block=""
else
    warn "No credentials found yet."
    echo "     Either configure Archy (~/.archy_config), or fill in the three"
    echo "     GENESYSCLOUD_* values below."
    echo "     See docs/oauth-role.md for the permissions the client needs."
    creds_block=$'\n        "GENESYSCLOUD_REGION": "your-region",\n        "GENESYSCLOUD_OAUTHCLIENT_ID": "your-client-id",\n        "GENESYSCLOUD_OAUTHCLIENT_SECRET": "your-client-secret"'
fi

step "Locating your flow repository and Archy"
# The flow tools need two things: a repository holding Architect YAML, and a
# working Archy to talk to it. Neither lives in this repository - the flow
# repository is your own, and the Archy binary is a 75 MB download kept out of
# git. Both are optional: without them the flow tools are unavailable and the
# other sixty-odd tools do not care.
flow_repo=""
candidate="$mcp_dir"
while [[ "$candidate" != "/" ]]; do
    if [[ -x "$candidate/archy" ]]; then
        flow_repo="$candidate"
        break
    fi
    candidate="$(dirname "$candidate")"
done

if [[ -n "$flow_repo" ]]; then
    ok "flow repository: $flow_repo"

    # Run the wrapper rather than just looking for a file: on macOS a freshly
    # downloaded binary is often quarantined, and that only shows on execution.
    archy_version="$("$flow_repo/archy" --version 2>/dev/null | tail -n 1 || true)"
    if [[ -n "$archy_version" ]]; then
        ok "Archy $archy_version"
    else
        warn "The ./archy wrapper is there, but Archy does not run."
        echo "     The binary is 75 MB and deliberately not in git. Download it from"
        echo "       https://developer.genesys.cloud/devapps/archy/"
        echo "     put it in $flow_repo/.archy/bin/ and make it executable:"
        echo "       chmod +x $flow_repo/.archy/bin/archy-macos-<version>"
        echo "     If it is already there, macOS may have quarantined it:"
        echo "       xattr -d com.apple.quarantine $flow_repo/.archy/bin/archy-macos-*"
        echo "     Until then the flow tools fail; every other tool works."
    fi

    repo_block=$'\n        "GENESYS_BUILDER_REPO": "'"$flow_repo"$'"'
else
    warn "No flow repository above this directory."
    echo "     This is normal on a fresh install: the toolkit is published on its"
    echo "     own, and the flow repository with your Architect YAML is separate."
    echo "     GENESYS_BUILDER_REPO is left out of the configuration below, which"
    echo "     makes the flow tools unavailable and changes nothing else."
    echo
    echo "     To use them later you need both: a repository holding Architect"
    echo "     YAML with the ./archy wrapper at its root, and the Archy binary in"
    echo "     its .archy/bin/ (https://developer.genesys.cloud/devapps/archy/)."
    echo "     Then add GENESYS_BUILDER_REPO by hand, or re-run this script from"
    echo "     inside that repository. Pointing it at a directory without the"
    echo "     wrapper fails every flow tool, so leaving it unset is the safer"
    echo "     of the two."
    repo_block=""
fi

# Both blocks are optional, so the entries are joined rather than concatenated -
# a fixed layout would leave a trailing comma whenever one of them is absent.
env_entries=""
for block in "$creds_block" "$repo_block"; do
    [[ -z "$block" ]] && continue
    [[ -n "$env_entries" ]] && env_entries+=","
    env_entries+="$block"
done

step "Editor configuration"
cat <<EOF

Add this to your MCP configuration. In Cursor that is ${bold}.cursor/mcp.json${reset}
in your project root; other editors use the same shape.

{
  "mcpServers": {
    "genesys-builder": {
      "command": "$uv_path",
      "args": [
        "run",
        "--directory",
        "$mcp_dir",
        "genesys-builder-mcp"
      ],
      "env": {$env_entries
      }
    }
  }
}

Then restart the editor and ask it to list your Genesys Cloud queues.

To verify without an editor:
  uv run --directory "$mcp_dir" python smoke_test.py

EOF
