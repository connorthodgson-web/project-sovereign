"""HTTP health check helper for local and VPS deployment validation."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


DEFAULT_URL = "http://127.0.0.1:8000/health"


def check_health(url: str, timeout: float) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            status_code = getattr(response, "status", response.getcode())
    except urllib.error.URLError as exc:
        return False, f"health check failed: {exc}"

    if status_code != 200:
        return False, f"health check returned HTTP {status_code}: {body[:200]}"

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return False, f"health check returned non-JSON body: {body[:200]}"

    if payload.get("status") != "ok":
        return False, f"health check returned unexpected payload: {payload}"

    return True, f"healthy: {url}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the Sovereign backend health endpoint.")
    parser.add_argument(
        "--url",
        default=os.getenv("SOVEREIGN_HEALTH_URL", DEFAULT_URL),
        help="Health endpoint URL. Defaults to SOVEREIGN_HEALTH_URL or local backend.",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    ok, detail = check_health(args.url, args.timeout)
    print(detail)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
