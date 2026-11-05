#!/usr/bin/env bash
#
# Bring this clone of genesys-builder-mcp up to date, then check what the
# update needs from you.
#
#   ./update.sh                       # update, check ~/.cursor/mcp.json
#   ./update.sh /path/to/your-project # update, check that project's .cursor/mcp.json
#
# Safe to re-run. It changes this clone, refreshes skills you already
# installed, and touches a project's .cursor/mcp.json only after asking.
#
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_arg="${1:-}"

bold=$'\033[1m'; red=$'\033[31m'; green=$'\033[32m'; reset=$'\033[0m'
step() { printf '\n%s==>%s %s\n' "$bold" "$reset" "$1"; }
ok()   { printf '  %s✓%s %s\n' "$green" "$reset" "$1"; }
warn() { printf '  %s!%s %s\n' "$red" "$reset" "$1"; }

cd "$here"
if [[ ! -d .git || ! -d mcp ]]; then
    warn "Run this from the root of a genesys-builder-mcp clone."
    exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
    warn "uv is not installed: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

tool_names() {
    uv run --quiet --directory "$here/mcp" python show_tools.py 2>/dev/null \
        | sed -n 's/^  \([a-z_]*\) .*/\1/p' | sort
}

step "Checking this clone"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    warn "You have local changes to tracked files:"
    git status --short --untracked-files=no | sed 's/^/       /'
    echo "     Commit or stash them first (git stash), then run this again."
    exit 1
fi
ok "no local changes"

before="$(mktemp)"; after="$(mktemp)"
trap 'rm -f "$before" "$after"' EXIT
[[ -d mcp/.venv ]] && tool_names > "$before" || true

# The release last reported here, kept inside .git so a manual reset - the
# way into the first release that has this script - does not hide its notes.
release_seen="$(git rev-parse --absolute-git-dir)/genesys-builder-release"
known_release="$(cat "$release_seen" 2>/dev/null || true)"

step "Fetching"
git fetch --quiet origin
branch="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || echo origin/main)"
old="$(git rev-parse HEAD)"
new="$(git rev-parse "$branch")"

if [[ "$old" == "$new" ]]; then
    ok "already current ($(git log -1 --format='%h, %cd' --date=short))"
elif git merge-base --is-ancestor "$old" "$new"; then
    git merge --quiet --ff-only "$new"
    ok "updated to $(git log -1 --format='%h, %cd' --date=short)"
else
    # Each release is published as a single commit without history, so a
    # plain git pull refuses with "unrelated histories". Jumping to it is
    # what an update means here; the old state stays reachable as a branch.
    backup="backup/before-update-$(date +%Y%m%d-%H%M%S)"
    git branch "$backup" "$old"
    git reset --quiet --hard "$new"
    ok "updated to $(git log -1 --format='%h, %cd' --date=short)"
    echo "     The previous state is kept as branch $backup."
fi

if [[ -f CHANGELOG.md && "$(grep -m 1 '^## ' CHANGELOG.md)" != "$known_release" ]]; then
    step "What is new"
    # Every release above the one this clone knew; the newest alone when it
    # knew none, since older entries describe states it never had.
    awk -v known="$known_release" '
        /^## / { if ($0 == known || (known == "" && seen)) exit; seen = 1 }
        seen { print "  " $0 }
    ' CHANGELOG.md
    grep -m 1 '^## ' CHANGELOG.md > "$release_seen"
fi

step "Installing dependencies"
uv sync --quiet --directory "$here/mcp"
ok "virtual environment ready"

step "Refreshing installed skills"
skills_home="$HOME/.cursor/skills"
refreshed=0; available=()
for skill in skills/*/; do
    name="$(basename "$skill")"
    if [[ -d "$skills_home/$name" ]]; then
        cp -R "$skill." "$skills_home/$name/"
        refreshed=$((refreshed + 1))
    else
        available+=("$name")
    fi
done
ok "$refreshed skill(s) refreshed in $skills_home"
if (( ${#available[@]} )); then
    echo "     Not installed: ${available[*]}"
    echo "     Install one with: cp -R skills/<name> $skills_home/"
fi

step "Checking your configuration"
if [[ -n "$project_arg" ]]; then
    config_file="$(cd "$project_arg" && pwd)/.cursor/mcp.json"
else
    config_file="$HOME/.cursor/mcp.json"
fi

CONFIG_FILE="$config_file" MCP_DIR="$here/mcp" uv run --quiet --directory "$here/mcp" python - <<'PY'
import json
import os
from pathlib import Path

GREEN, RED, RESET = "\033[32m", "\033[31m", "\033[0m"
ok = lambda text: print(f"  {GREEN}✓{RESET} {text}")
warn = lambda text: print(f"  {RED}!{RESET} {text}")
note = lambda text: print(f"     {text}")

config_file = Path(os.environ["CONFIG_FILE"])
mcp_dir = os.environ["MCP_DIR"]
keys = {
    "GENESYSCLOUD_REGION": "location",
    "GENESYSCLOUD_OAUTHCLIENT_ID": "clientId",
    "GENESYSCLOUD_OAUTHCLIENT_SECRET": "clientSecret",
}
archy_config = Path.home() / ".archy_config"


def ask(question: str) -> bool:
    try:
        with open("/dev/tty") as tty:
            print(f"     {question} [y/N] ", end="", flush=True)
            return tty.readline().strip().lower() in {"y", "yes", "j", "ja"}
    except OSError:
        return False


def from_archy_config() -> dict[str, str] | None:
    try:
        data = json.loads(archy_config.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    values = {env: data.get(field) for env, field in keys.items()}
    return values if all(values.values()) else None


if not config_file.is_file():
    warn(f"{config_file} does not exist.")
    note("Pass your project: ./update.sh /path/to/your-project")
    raise SystemExit(0)

document = json.loads(config_file.read_text())
server = (document.get("mcpServers") or {}).get("genesys-builder")
if server is None:
    warn(f"No genesys-builder server in {config_file}.")
    note("Pass the project that has it: ./update.sh /path/to/your-project")
    raise SystemExit(0)
ok(f"configuration: {config_file}")

args = server.get("args") or []
directory = args[args.index("--directory") + 1] if "--directory" in args[:-1] else None
if directory and Path(directory).resolve() != Path(mcp_dir).resolve():
    warn(f"--directory points at {directory}")
    note(f"but this clone's server is {mcp_dir}. Fix it there, or re-run mcp/setup.sh.")
else:
    ok("server path matches this clone")

env = server.setdefault("env", {})
placeholder = lambda value: not value or value.startswith("your-")
missing = [key for key in keys if placeholder(env.get(key, ""))]
if not missing:
    ok(f"credentials in the env block (region {env['GENESYSCLOUD_REGION']})")
else:
    warn("The env block lacks " + ", ".join(missing) + ".")
    note("The server no longer reads ~/.archy_config, so it will not start")
    note("talking to your org until these are in the env block.")
    legacy = from_archy_config()
    if legacy and ask(f"Copy them from {archy_config} into {config_file.name}?"):
        env.update(legacy)
        config_file.write_text(json.dumps(document, indent=2) + "\n")
        config_file.chmod(0o600)
        ok("credentials copied (values not shown)")
    elif legacy:
        note(f"Copy location, clientId and clientSecret from {archy_config} by hand.")
    else:
        note("Fill them in; mcp/docs/oauth-role.md says what the client needs.")

repo = env.get("GENESYS_BUILDER_REPO")
if repo:
    repo_path = Path(repo).expanduser()
    if not (repo_path / ".git").exists():
        warn(f"GENESYS_BUILDER_REPO={repo} is not a git repository.")
    else:
        ok(f"flow repository: {repo}")
        ignore = (repo_path / ".gitignore").read_text() if (repo_path / ".gitignore").is_file() else ""
        for entry in (".cursor/mcp.json", ".genesys-builder/"):
            if entry not in ignore:
                warn(f"{entry} is not in {repo}/.gitignore - add it.")
        leftovers = [name for name in ("archy", ".archy/bin") if (repo_path / name).exists()]
        if leftovers:
            note("Archy is no longer used; these can go from the flow repository:")
            note("  " + ", ".join(leftovers) + "  (.archy/flow-state.json moves by itself)")

if archy_config.is_file():
    note(f"{archy_config} is no longer read by anything here. Delete it once")
    note("your credentials are in the env block, unless other tools still use it.")
PY

if [[ -s "$before" ]]; then
    tool_names > "$after"
    added="$(comm -13 "$before" "$after" | tr '\n' ' ')"
    removed="$(comm -23 "$before" "$after" | tr '\n' ' ')"
    if [[ -n "$added$removed" ]]; then
        step "Tools"
        [[ -n "$added" ]] && ok "new: $added"
        [[ -n "$removed" ]] && warn "gone: $removed"
    fi
fi

step "Last step: restart Cursor completely"
echo "  Cmd+Q and reopen. Reload Window and toggling the server off and on keep"
echo "  the old server process running, so the update would not take effect."
echo
echo "  If a tool answers 403 afterwards, its permission is missing from the"
echo "  OAuth role: mcp/docs/oauth-role.md. This release needs architect:job:*,"
echo "  architect:jobExport:*, architect:flow:unlock and architect:flow:delete"
echo "  for the flow tools."
