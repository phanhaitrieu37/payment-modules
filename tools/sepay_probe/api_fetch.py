from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path


def load_env(path):
    out = {}
    for line in Path(path).read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("\"'")
    return out


def fetch(out, env_path, params=None, base_url="https://userapi-sandbox.sepay.vn"):
    e = load_env(env_path)
    if e.get("SEPAY_ENVIRONMENT") != "test":
        raise RuntimeError("refusing non-test environment")
    if not e.get("SEPAY_TEST_API_TOKEN"):
        raise RuntimeError("SEPAY_TEST_API_TOKEN missing")
    q = urllib.parse.urlencode({"per_page": "100", **(params or {})})
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v2/transactions?" + q,
        headers={
            "Authorization": "Bearer " + e["SEPAY_TEST_API_TOKEN"],
            "Accept": "application/json",
        },
    )
    time.sleep(0.5)
    with urllib.request.urlopen(req, timeout=30) as r:
        status = r.status
        data = json.load(r)
    Path(out).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return status


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True)
    p.add_argument("--env", required=True)
    p.add_argument("--base-url", default="https://userapi-sandbox.sepay.vn")
    a = p.parse_args()
    print(fetch(a.output, a.env, base_url=a.base_url))
