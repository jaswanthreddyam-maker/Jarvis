from __future__ import annotations

import argparse
import os
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen


def _health_url() -> str:
    base_url = os.getenv("JARVIS_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
    return f"{base_url}/health"


def main() -> int:
    parser = argparse.ArgumentParser(description="Wait until the Jarvis backend accepts health checks.")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--interval", type=float, default=0.75)
    args = parser.parse_args()

    deadline = time.monotonic() + max(1.0, args.timeout)
    url = _health_url()
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=2.0) as response:
                if 200 <= int(response.status) < 300:
                    print(f"[BOOT] Backend ready: {url}")
                    return 0
                last_error = f"HTTP {response.status}"
        except (OSError, URLError) as exc:
            last_error = str(exc)
        time.sleep(max(0.1, args.interval))

    print(f"[ERROR] Backend did not become ready at {url}. Last error: {last_error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
