#!/usr/bin/env python3
"""Finnhub earnings webhook receiver (Hatch port of the Pi's manual-webhook.py).

Behavior (from config/webhook_config.json + docs/deployment.md):
- GET  /health                      -> {"status":"ok","platform":"webhook"}
- POST /webhooks/finnhub-earnings   -> validates X-Finnhub-Secret, logs event to
                                       logs/webhook_events.jsonl, clears the earnings
                                       cache so the next strategy run refetches.
- Secret comes from FINNHUB_WEBHOOK_SECRET env, falling back to config/webhook_config.json.
"""
import json
import os
import sys
import time
import http.server
import socketserver
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
EVENTS_LOG = LOGS / "webhook_events.jsonl"
EARNINGS_CACHE = LOGS / "earnings_cache.json"
PORT = int(os.environ.get("WEBHOOK_PORT", "8644"))


def load_secret() -> str:
    secret = os.environ.get("FINNHUB_WEBHOOK_SECRET")
    if secret:
        return secret
    cfg = ROOT / "config" / "webhook_config.json"
    try:
        return json.loads(cfg.read_text())["secret"]
    except Exception as e:
        print(f"FATAL: no FINNHUB_WEBHOOK_SECRET env and cannot read {cfg}: {e}", file=sys.stderr)
        sys.exit(1)


SECRET = load_secret()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "wheel-webhook/1.0"

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok", "platform": "webhook"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/webhooks/finnhub-earnings":
            self._json(404, {"error": "not found"})
            return
        # Match the Pi's original behavior: posts without a secret header are
        # accepted (the documented usage in config/webhook_config.json sends
        # none), but a wrong secret is still rejected.
        sent = self.headers.get("X-Finnhub-Secret")
        if sent is not None and sent != SECRET:
            self._json(401, {"error": "invalid secret"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json(400, {"error": "invalid JSON"})
            return

        # Finnhub's guide: acknowledge with a 2xx BEFORE doing any work so
        # their sender never times out and the endpoint stays enabled.
        self._json(200, {"status": "received"})

        try:
            LOGS.mkdir(exist_ok=True)
            event = {"received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "payload": payload}
            with EVENTS_LOG.open("a") as f:
                f.write(json.dumps(event) + "\n")

            # Clear the earnings cache so the next strategy run refetches fresh dates.
            if EARNINGS_CACHE.exists():
                EARNINGS_CACHE.unlink()
        except Exception as e:
            print(f"post-ack processing error: {e}", flush=True)

    def log_message(self, fmt, *args):
        line = "%s - %s" % (self.address_string(), fmt % args)
        print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {line}", flush=True)


socketserver.ThreadingTCPServer.allow_reuse_address = True
with socketserver.ThreadingTCPServer(("", PORT), Handler) as srv:
    print(f"webhook receiver listening on :{PORT}", flush=True)
    srv.serve_forever()
