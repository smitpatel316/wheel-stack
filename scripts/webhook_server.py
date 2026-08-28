#!/usr/bin/env python3
"""Finnhub earnings webhook receiver (Hatch port of the Pi's manual-webhook.py).

Behavior (from config/webhook_config.json + docs/deployment.md):
- GET  /health                      -> {"status":"ok","platform":"webhook"}
- GET  /earnings/state              -> invalidation state for the engine's
                                       fail-open earnings pull (core/earnings_source.py):
                                       {"last_invalidation": <epoch|null>,
                                        "last_event_at": <iso|null>,
                                        "events_received": <n>}
- POST /webhooks/finnhub-earnings   -> validates X-Finnhub-Secret, logs event to
                                       logs/webhook_events.jsonl, records the
                                       invalidation in logs/webhook_state.json
                                       (atomic), clears the earnings cache so
                                       the next strategy run refetches.
- Secret comes from FINNHUB_WEBHOOK_SECRET env, falling back to config/webhook_config.json.
- Listen port comes from WEBHOOK_PORT (default 8644; the Pi copy runs with 8744).
- The local earnings-cache clear only matters when the engine runs on this
  same host; on the Pi it is harmless and the /earnings/state endpoint is
  what the Hatch engine actually consumes.
"""
import hmac
import json
import logging
import os
import sys
import time
import http.server
import socketserver
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent

try:  # load .env so FINNHUB_WEBHOOK_SECRET never has to live in tracked config
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError as e:
    logging.getLogger(__name__).debug("[SWALLOWED] python-dotenv unavailable, relying on process env only: %r", e)

LOGS = ROOT / "logs"
EVENTS_LOG = LOGS / "webhook_events.jsonl"
EARNINGS_CACHE = LOGS / "earnings_cache.json"
STATE_FILE = LOGS / "webhook_state.json"
PORT = int(os.environ.get("WEBHOOK_PORT", "8644"))


def load_state() -> dict:
    """Invalidation state served to the engine's earnings pull. Never raises."""
    try:
        state = json.loads(STATE_FILE.read_text())
        if isinstance(state, dict):
            return {
                "last_invalidation": state.get("last_invalidation"),
                "last_event_at": state.get("last_event_at"),
                "events_received": int(state.get("events_received") or 0),
            }
    except Exception as e:
        log.debug("[SWALLOWED] webhook state read failed (serving empty state): %r", e)
    return {"last_invalidation": None, "last_event_at": None, "events_received": 0}


def record_event() -> dict:
    """Record a Finnhub push event as a cache invalidation (atomic write)."""
    state = load_state()
    now = time.time()
    state["last_invalidation"] = now
    state["last_event_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    state["events_received"] = int(state.get("events_received") or 0) + 1
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_FILE)
    return state

# Robinhood Agentic Trading MCP OAuth callback (read-only connector project).
# The /oauth/robinhood/callback path is routed here via a
# Cloudflare tunnel. State is validated against pending_auth.json written by
# rh_mcp_client.py `auth`; the code is stashed for rh_mcp_client.py `finish`.
RH_DIR = Path(os.environ.get(
    "RH_MCP_DIR", str(Path.home() / "workspace" / "robinhood-mcp")))
# Pi migration 2026-08-27: Finnhub receiver lives on the Pi; the Robinhood
# OAuth callback still needs to land on Hatch. When set, this handler relays
# the callback to RH_RELAY_URL (Hatch sink via rh-callback hostname).
# cloudflared cannot do this itself: CF bot rules block its Go TLS fingerprint.
RH_RELAY_URL = os.environ.get("RH_RELAY_URL", "")
RH_PENDING = RH_DIR / "pending_auth.json"
RH_CALLBACK = RH_DIR / "callback_result.json"


def load_secret() -> str:
    secret = os.environ.get("FINNHUB_WEBHOOK_SECRET")
    if secret:
        return secret
    cfg = ROOT / "config" / "webhook_config.json"
    try:
        return json.loads(cfg.read_text())["secret"]
    except Exception as e:
        log.warning("[SWALLOWED] webhook secret config read failed for %s: %r", cfg, e)
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
        elif urlsplit(self.path).path == "/earnings/state":
            self._json(200, load_state())
        elif urlsplit(self.path).path == "/oauth/robinhood/callback":
            self._rh_oauth_callback(parse_qs(urlsplit(self.path).query))
        else:
            self._json(404, {"error": "not found"})

    def _html(self, code: int, title: str, msg: str):
        body = (f"<!doctype html><html><head><title>{title}</title></head>"
                f"<body style=\"font-family:sans-serif;text-align:center;"
                f"padding-top:4em\"><h2>{title}</h2><p>{msg}</p></body></html>"
                ).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _rh_oauth_callback(self, qs: dict):
        if RH_RELAY_URL:
            import urllib.request, urllib.parse
            target = RH_RELAY_URL + "?" + urllib.parse.urlencode(
                {k: v[0] for k, v in qs.items()})
            try:
                req = urllib.request.Request(target, headers={"User-Agent": "wheel-webhook-relay/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = resp.read()
                    code = resp.status
            except Exception as e:
                log.error("[RH-RELAY] relay to %s failed: %r", target, e)
                self._html(502, "Relay failed",
                           "Could not reach the Hatch callback sink. Try again.")
                return
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        state = (qs.get("state") or [""])[0]
        code = (qs.get("code") or [""])[0]
        error = (qs.get("error") or [""])[0]
        try:
            expected = json.loads(RH_PENDING.read_text()).get("state", "")
        except Exception as e:
            log.warning("[SWALLOWED] Robinhood pending-auth state read failed, rejecting callback: %r", e)
            expected = ""
        if not expected or not state or not hmac.compare_digest(expected, state):
            self._html(400, "Robinhood callback rejected",
                       "State mismatch or no pending authorization. "
                       "Run <code>rh_mcp_client.py auth</code> again.")
            return
        RH_DIR.mkdir(parents=True, exist_ok=True)
        tmp = RH_CALLBACK.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "code": code or None,
            "state": state,
            "error": error or None,
            "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }))
        os.replace(tmp, RH_CALLBACK)
        os.chmod(RH_CALLBACK, 0o600)
        if error:
            self._html(200, "Authorization failed",
                       f"Robinhood returned an error ({error}). "
                       "You can close this tab.")
        else:
            self._html(200, "Robinhood connected",
                       "Authorization received. You can close this tab.")

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
        except Exception as e:
            log.warning("[SWALLOWED] webhook POST body parse failed from %s: %r", self.client_address, e)
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

            # Record the invalidation for the engine's pull (works whether the
            # engine is local or, post-migration, pulling over the network).
            record_event()

            # Clear the earnings cache so the next strategy run refetches fresh dates.
            if EARNINGS_CACHE.exists():
                EARNINGS_CACHE.unlink()
        except Exception as e:
            log.warning("[SWALLOWED] webhook post-ack processing (event log/state/cache clear) failed: %r", e)
            print(f"post-ack processing error: {e}", flush=True)

    def log_message(self, fmt, *args):
        line = "%s - %s" % (self.address_string(), fmt % args)
        print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {line}", flush=True)


if __name__ == "__main__":
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("", PORT), Handler) as srv:
        print(f"webhook receiver listening on :{PORT}", flush=True)
        srv.serve_forever()
