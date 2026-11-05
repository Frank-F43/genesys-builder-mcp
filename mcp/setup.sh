#!/usr/bin/env bash
#
# Set up the genesys-builder MCP server and print the editor configuration
# with the absolute paths already filled in.
#
#   ./setup.sh                       # flow repository found above this directory, if any
#   ./setup.sh /path/to/flow-repo    # the repository holding your Architect YAML
#
set -euo pipefail

mcp_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
flow_repo_arg="${1:-}"

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

step "Locating your flow repository"
# The flow tools work on a git repository holding Architect YAML. Flows travel
# to and from the org through Genesys Cloud's Architect job API, so nothing
# else needs installing. The repository is your own and optional: without it
# the flow tools have nothing to work on and the other tools do not care.
flow_repo=""
if [[ -n "$flow_repo_arg" ]]; then
    flow_repo="$(cd "$flow_repo_arg" && pwd)"
else
    # This directory sits inside the toolkit's own checkout, which holds no
    # flows - only a repository that carries the toolkit as toolkit/ is one.
    candidate="$mcp_dir"
    while [[ "$candidate" != "/" ]]; do
        if [[ -e "$candidate/.git" && -d "$candidate/toolkit/mcp" ]]; then
            flow_repo="$candidate"
            break
        fi
        candidate="$(dirname "$candidate")"
    done
fi

if [[ -n "$flow_repo" ]]; then
    if [[ -e "$flow_repo/.git" ]]; then
        ok "flow repository: $flow_repo"
    else
        warn "$flow_repo is not a git repository."
        echo "     The flow tools record what they exported per file, which only"
        echo "     makes sense under version control. git init it first."
    fi
    repo_block=$'\n        "GENESYS_BUILDER_REPO": "'"$flow_repo"$'"'
else
    warn "No flow repository given."
    echo "     This is normal on a fresh install: the toolkit is published on its"
    echo "     own, and the repository with your Architect YAML is separate."
    echo "     GENESYS_BUILDER_REPO is left out of the configuration below, which"
    echo "     makes the flow tools unavailable and changes nothing else."
    echo "     To use them, re-run with the repository as argument:"
    echo "       ./setup.sh /path/to/your-flow-repo"
    repo_block=""
fi

step "Checking credentials"
# Resolved by the server itself, with the env block of the project's
# .cursor/mcp.json applied the way the editor applies it, so this reports
# exactly what the server will see at runtime.
placeholders=$'\n        "GENESYSCLOUD_REGION": "your-region",\n        "GENESYSCLOUD_OAUTHCLIENT_ID": "your-client-id",\n        "GENESYSCLOUD_OAUTHCLIENT_SECRET": "your-client-secret"'
if resolved="$(FLOW_REPO="$flow_repo" uv run --directory "$mcp_dir" python -c "
import json, os
from pathlib import Path
config_file = Path(os.environ['FLOW_REPO'] or '/nonexistent') / '.cursor' / 'mcp.json'
where = 'the environment'
if config_file.is_file():
    env = json.loads(config_file.read_text()).get('mcpServers', {}).get('genesys-builder', {}).get('env', {})
    os.environ.update({k: v for k, v in env.items() if k.startswith('GENESYSCLOUD_')})
    where = str(config_file)
from genesys_builder_mcp.config import load_config
c = load_config()
print(f'region {c.region}, client {c.client_id[:8]}..., from {where}')
" 2>/dev/null)"; then
    ok "credentials resolve: $resolved"
    echo "     Keep the GENESYSCLOUD_* values you already have in place of the"
    echo "     placeholders below."
else
    warn "No credentials found yet."
    echo "     Fill in the three GENESYSCLOUD_* values below. They select the org"
    echo "     for this project, so each project carries its own."
    echo "     See docs/oauth-role.md for the permissions the client needs."
fi
creds_block="$placeholders"

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

To verify without an editor, from the project that holds .cursor/mcp.json:
  uv run --project "$mcp_dir" python "$mcp_dir/smoke_test.py"

EOF
