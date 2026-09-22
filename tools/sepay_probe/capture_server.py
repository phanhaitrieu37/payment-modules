from __future__ import annotations

import argparse
import hashlib
import hmac
import json
from contextlib import suppress
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_BODY = 64 * 1024


def load_env(path):
    out = {}
    for line in Path(path).read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("\"'")
    return out


def make_handler(output: Path, secret: str, mode_file: Path):
    output.mkdir(parents=True, exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/sepay":
                self.send_error(404)
                return
            body = self.rfile.read(min(int(self.headers.get("Content-Length", "0")), MAX_BODY + 1))
            ts = self.headers.get("X-SePay-Timestamp", "")
            sig = self.headers.get("X-SePay-Signature", "")
            expected = hmac.new(
                secret.encode(), (ts + ".").encode() + body, hashlib.sha256
            ).hexdigest()
            valid = bool(
                len(body) <= MAX_BODY
                and ts
                and sig.startswith("sha256=")
                and hmac.compare_digest(expected, sig.removeprefix("sha256="))
            )
            payload = None
            with suppress(ValueError, UnicodeDecodeError):
                payload = json.loads(body)
            record = {
                "received_at": datetime.now(UTC).isoformat(),
                "path": self.path,
                "id": payload.get("id") if isinstance(payload, dict) else None,
                "timestamp": ts,
                "hmac_valid": valid,
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "content_length": len(body),
            }
            if valid:
                record["payload"] = payload
            with (output / "capture.jsonl").open("a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
            reject = mode_file.exists() and mode_file.read_text().strip() == "reject503"
            self.send_response(503 if reject else (200 if valid else 401))
            self.end_headers()
            self.wfile.write(b'{"success":true}' if valid and not reject else b'{"success":false}')

        def do_GET(self):
            self.send_error(404)

        def log_message(self, *args):
            pass

    return Handler


def serve(output, secret, mode_file=None):
    output = Path(output)
    mode_file = Path(mode_file) if mode_file else output / "mode"
    ThreadingHTTPServer(
        ("127.0.0.1", 8787), make_handler(output, secret, mode_file)
    ).serve_forever()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True)
    p.add_argument("--env", required=True)
    p.add_argument("--mode-file")
    a = p.parse_args()
    e = load_env(a.env)
    if e.get("SEPAY_ENVIRONMENT") != "test":
        raise SystemExit("SEPAY_ENVIRONMENT must be test")
    serve(Path(a.output), e.get("SEPAY_TEST_WEBHOOK_SECRET", ""), a.mode_file)
