from __future__ import annotations

import argparse
import urllib.request
from pathlib import Path

# Cloudflare in front of the SePay API rejects the default Python-urllib agent (error 1010).
USER_AGENT = "payment-module-sepay-probe/1"


def load(path):
    out = {}
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("\"'")
    return out


def main(path, base_url="https://userapi-sandbox.sepay.vn"):
    env = load(path)
    for key in ("SEPAY_ENVIRONMENT", "SEPAY_TEST_API_TOKEN", "SEPAY_TEST_WEBHOOK_SECRET"):
        print(f"{key}: {'present' if env.get(key) else 'missing'}")
    if env.get("SEPAY_ENVIRONMENT") != "test" or not env.get("SEPAY_TEST_API_TOKEN"):
        return 2
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v2/transactions?per_page=1",
        headers={
            "Authorization": "Bearer " + env["SEPAY_TEST_API_TOKEN"],
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"api_http_status: {r.status}")
    except Exception as exc:
        print(f"api_http_status: {getattr(exc, 'code', 'unavailable')}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--env", required=True)
    p.add_argument("--base-url", default="https://userapi-sandbox.sepay.vn")
    raise SystemExit(main(p.parse_args().env, p.parse_args().base_url))
