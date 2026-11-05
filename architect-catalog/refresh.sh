#!/usr/bin/env bash
#
# Install the newest Architect Scripting SDK and rebuild the action catalog.
# Genesys releases the SDK almost weekly, so run this whenever a flow needs an
# action you have not used before, or simply once a week.
#
#   toolkit/architect-catalog/refresh.sh
#
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

npm install --prefix "$here" --no-save --no-audit --no-fund --silent \
    purecloud-flow-scripting-api-sdk-javascript@latest

node "$here/build_catalog.mjs"
