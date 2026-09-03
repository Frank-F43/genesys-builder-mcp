#!/usr/bin/env python3
"""Download the Genesys Cloud public API v2 spec into the local cache.

    scripts/fetch_api_spec.py                 # region from your configuration
    scripts/fetch_api_spec.py --region mypurecloud.com

The spec is a reproducible ~22 MB download, so it is gitignored rather than
committed: a copy in git would be stale within weeks and bloat every clone.

No credentials needed — ``/api/v2/docs/swagger`` is public. Only the region
matters, because it decides the ``host`` recorded inside the spec, and that is
what example URLs are built from later.

Downloads through ``curl`` rather than urllib on purpose: corporate networks
that terminate TLS break Python's bundled certificate store, while curl uses
the system keychain and keeps working.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from genesys_config import ARCHY_CONFIG, VALID_REGIONS  # noqa: E402

# scripts/ sits next to reference/ in both layouts: in the working repository
# under toolkit/, and at the root of the published copy.
TARGET = Path(__file__).resolve().parent.parent / "reference" / "swagger" / "publicapi-v2-latest.json"


def resolve_region(explicit: str | None) -> str:
    if explicit:
        return explicit
    import os

    region = os.environ.get("GENESYSCLOUD_REGION")
    if region:
        return region
    if ARCHY_CONFIG.is_file():
        try:
            region = json.loads(ARCHY_CONFIG.read_text()).get("location")
        except (OSError, json.JSONDecodeError):
            region = None
        if region:
            return region
    sys.exit(
        "No region found. Pass --region, set GENESYSCLOUD_REGION, or configure "
        f"Archy ({ARCHY_CONFIG}).\nRegions: {', '.join(sorted(VALID_REGIONS))}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", help="e.g. mypurecloud.de, mypurecloud.com")
    args = parser.parse_args()

    region = resolve_region(args.region)
    if region not in VALID_REGIONS:
        sys.exit(f"Unknown region {region!r}.\nExpected one of: {', '.join(sorted(VALID_REGIONS))}")

    url = f"https://api.{region}/api/v2/docs/swagger"
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    # Straight to a temporary name so a failed download cannot leave a
    # truncated file behind that later reads as valid-but-incomplete.
    tmp = TARGET.with_suffix(".json.part")

    print(f"Fetching {url}")
    result = subprocess.run(
        ["curl", "-L", "--retry", "2", "-w", "%{http_code}", "-o", str(tmp), url],
        capture_output=True,
        text=True,
    )
    status = result.stdout.strip()
    if result.returncode != 0 or status != "200":
        tmp.unlink(missing_ok=True)
        if status == "403":
            # Measured across the region list: every region serves this
            # endpoint except euc2.pure.cloud, which refuses it outright.
            sys.exit(
                f"{region} refuses this endpoint (403). It is not a credential problem — "
                "the endpoint is simply not served there.\n"
                "The contract shapes are the same everywhere; only the 'host' recorded "
                "inside the spec differs. So fetch from another region instead:\n"
                "  scripts/fetch_api_spec.py --region mypurecloud.ie"
            )
        sys.exit(f"Download failed (HTTP {status}): {result.stderr.strip() or result.returncode}")

    try:
        spec = json.loads(tmp.read_text())
    except json.JSONDecodeError as exc:
        tmp.unlink(missing_ok=True)
        sys.exit(f"Downloaded file is not valid JSON: {exc}")

    tmp.replace(TARGET)
    schemas = spec.get("definitions") or spec.get("components", {}).get("schemas", {})
    print(
        f"{TARGET}\n"
        f"  {TARGET.stat().st_size / 1024 / 1024:.1f} MB, host {spec.get('host')}, "
        f"{len(spec.get('paths', {}))} paths, {len(schemas)} definitions"
    )
    print("\nDo not read this file whole — it does not fit in a context window. Search it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
