"""A stand-in Supabase, enough of one to test the pool client against.

Implements the handful of REST and Storage endpoints `pool.py` uses, with the
same status codes and error wording the real service returns for the cases the
client branches on: 401 without a key, 409 "duplicate key value violates
unique constraint" from PostgREST, 409 "The resource already exists" from
Storage. `ingest_prices` applies the same LEAST/GREATEST merge the SQL
function does, so the merge rule is exercised too.

This is a test double, not a Supabase implementation: it is here to prove the
client's behaviour, not the server's.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class State:
    def __init__(self, key: str):
        self.key = key
        self.scans: dict[str, dict] = {}
        self.prices: dict[tuple, dict] = {}
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []


def _least(a, b):
    vals = [v for v in (a, b) if v is not None]
    return min(vals) if vals else None


def _greatest(a, b):
    vals = [v for v in (a, b) if v is not None]
    return max(vals) if vals else None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: State = None  # type: ignore[assignment]

    def log_message(self, *a):
        pass

    # ------------------------------------------------------------- utilities

    def _send(self, code: int, payload=None, ctype="application/json"):
        body = b"" if payload is None else (
            payload if isinstance(payload, bytes)
            else json.dumps(payload).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _cors(self):
        # the real service answers any origin; the page is served from
        # somewhere else entirely, so without this the browser never looks
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "apikey, authorization, content-type, prefer, "
                         "x-client-info")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "600")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def _authed(self) -> bool:
        if self.headers.get("apikey") == self.state.key:
            return True
        self._send(401, {"message": "Invalid API key"})
        return False

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        return raw

    def _query(self):
        q = urllib.parse.urlsplit(self.path).query
        return urllib.parse.parse_qs(q, keep_blank_values=True)

    # -------------------------------------------------------------- requests

    def do_GET(self):
        self.state.calls.append(("GET", self.path))
        if not self._authed():
            return
        path = urllib.parse.urlsplit(self.path).path
        q = self._query()

        if path == "/rest/v1/scans":
            rows = list(self.state.scans.values())
            realm = (q.get("realm") or [""])[0]
            if realm.startswith("eq."):
                rows = [r for r in rows if r["realm"] == realm[3:]]
            order = (q.get("order") or [""])[0]
            if order.startswith("scanned_at"):
                rows.sort(key=lambda r: r["scanned_at"],
                          reverse="desc" in order)
            limit = int((q.get("limit") or ["100"])[0])
            return self._send(200, rows[:limit])

        if path == "/rest/v1/prices":
            rows = list(self.state.prices.values())
            realm = (q.get("realm") or [""])[0]
            if realm.startswith("eq."):
                rows = [r for r in rows if r["realm"] == realm[3:]]
            item = (q.get("item") or [""])[0]
            m = re.match(r"in\.\(([\d,]*)\)", item)
            if m and m.group(1):
                keep = {int(x) for x in m.group(1).split(",")}
                rows = [r for r in rows if r["item"] in keep]
            since = (q.get("day") or [""])[0]
            if since.startswith("gte."):
                rows = [r for r in rows if r["day"] >= since[4:]]
            rows.sort(key=lambda r: r["day"])
            limit = int((q.get("limit") or ["10000"])[0])
            return self._send(200, rows[:limit])

        if path == "/rest/v1/pool_realms":
            by: dict[str, dict] = {}
            for s in self.state.scans.values():
                r = by.setdefault(s["realm"], {
                    "realm": s["realm"], "scans": 0, "uploaders": set(),
                    "latest": "", "bytes": 0})
                r["scans"] += 1
                r["uploaders"].add(s["uploader"])
                r["latest"] = max(r["latest"], s["scanned_at"])
                r["bytes"] += s["bytes"]
            out = [{**r, "uploaders": len(r["uploaders"])} for r in by.values()]
            out.sort(key=lambda r: r["latest"], reverse=True)
            return self._send(200, out)

        m = re.match(r"^/storage/v1/object/([^/]+)/(.+)$", path)
        if m:
            bucket, key = m.group(1), m.group(2)
            if bucket != "scans":
                return self._send(400, {"message": "unknown bucket"})
            blob = self.state.objects.get(key)
            if blob is None:
                return self._send(404, {"message": "Object not found"})
            return self._send(200, blob)

        self._send(404, {"message": "no such endpoint: " + path})

    def do_POST(self):
        self.state.calls.append(("POST", self.path))
        if not self._authed():
            return
        path = urllib.parse.urlsplit(self.path).path
        raw = self._body()

        if path == "/rest/v1/scans":
            rows = json.loads(raw)
            for r in rows:
                if r["id"] in self.state.scans:
                    return self._send(409, {
                        "code": "23505",
                        "message": 'duplicate key value violates unique '
                                   'constraint "scans_pkey"'})
                if len(r["uploader"]) > 48 or len(r["realm"]) > 48:
                    return self._send(400, {"message": "check constraint"})
                self.state.scans[r["id"]] = r
            return self._send(201, None)

        if path == "/rest/v1/rpc/ingest_prices":
            payload = json.loads(raw).get("payload")
            if not isinstance(payload, list):
                return self._send(400, {"message": "payload must be a JSON array"})
            if len(payload) > 20000:
                return self._send(400, {"message": "too many rows in one call"})
            for r in payload:
                k = (r["realm"], int(r["item"]), r["day"])
                cur = self.state.prices.get(k)
                new = {"realm": k[0], "item": k[1], "day": k[2],
                       "price": r.get("price"), "qty": r.get("qty"),
                       "min_seen": r.get("min_seen")}
                if cur:
                    new["price"] = _least(cur["price"], new["price"])
                    new["qty"] = _greatest(cur["qty"], new["qty"])
                    new["min_seen"] = _least(cur["min_seen"], new["min_seen"])
                self.state.prices[k] = new
            return self._send(200, len(payload))

        m = re.match(r"^/storage/v1/object/([^/]+)/(.+)$", path)
        if m:
            bucket, key = m.group(1), m.group(2)
            if bucket != "scans":
                return self._send(400, {"message": "unknown bucket"})
            if self.headers.get("Content-Type") != "application/json":
                return self._send(415, {"message": "mime type not supported"})
            if key in self.state.objects:
                return self._send(409, {"statusCode": "409",
                                        "error": "Duplicate",
                                        "message": "The resource already exists"})
            self.state.objects[key] = raw
            return self._send(200, {"Key": f"{bucket}/{key}"})

        self._send(404, {"message": "no such endpoint: " + path})


def start(key: str = "sb_publishable_test") -> tuple[str, State, ThreadingHTTPServer]:
    state = State(key)
    handler = type("H", (Handler,), {"state": state})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}", state, srv
