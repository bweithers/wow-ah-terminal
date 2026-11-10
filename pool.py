"""The shared scan pool, on Supabase.

Talks to Supabase over its REST and Storage HTTP APIs using nothing but the
standard library, so a friend needs only Python to contribute scans.

Two stores, on purpose:

* **Storage** keeps the whole packed scan as one JSON object. Lossless, and
  what the terminal UI loads.
* **Postgres** keeps a thin index of scans plus one row per item per day, so
  history outlives the 21 days Auctionator itself remembers.

Writes go in with the project's *publishable* key. That key is meant to sit in
client code; what protects the pool is the row-level security in `schema.sql`,
which lets anyone holding it read everything and append scans, and nobody
update or delete.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 90
RPC_CHUNK = 4000


class PoolError(RuntimeError):
    """A pool request failed. Never carries the key."""


def _scrub(s: str, key: str) -> str:
    return s.replace(key, "<key>") if key else s


def slug(s: str) -> str:
    """A path- and id-safe version of a realm or uploader name."""
    out = re.sub(r"[^A-Za-z0-9._-]+", "-", str(s or "")).strip("-")
    return out[:48] or "unknown"


def configured(cfg: dict) -> bool:
    return bool(cfg.get("supabase_url") and cfg.get("supabase_key"))


def from_config(cfg: dict) -> "Pool":
    if not configured(cfg):
        raise PoolError(
            "No pool configured. Run `python ah.py pool-init` (project owner) "
            "or put supabase_url and supabase_key in config.json."
        )
    return Pool(cfg["supabase_url"], cfg["supabase_key"],
                cfg.get("supabase_bucket") or "scans")


class Pool:
    def __init__(self, url: str, key: str, bucket: str = "scans"):
        self.url = url.rstrip("/")
        self.key = key
        self.bucket = bucket

    # ---------------------------------------------------------------- plumbing

    def _request(self, method: str, path: str, *, body: bytes | None = None,
                 headers: dict | None = None, want_bytes: bool = False):
        url = self.url + path
        h = {
            "apikey": self.key,
            "Authorization": "Bearer " + self.key,
            "Accept": "application/json",
            "User-Agent": "wow-ah-terminal/1.0",
        }
        h.update(headers or {})
        req = urllib.request.Request(url, data=body, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:600]
            except Exception:
                pass
            try:
                j = json.loads(detail)
                detail = j.get("message") or j.get("error") or detail
            except Exception:
                pass
            raise PoolError(
                f"{method} {_scrub(path, self.key)} -> {e.code} {e.reason}"
                + (f": {_scrub(detail, self.key)}" if detail else "")
            ) from None
        except urllib.error.URLError as e:
            raise PoolError(
                f"cannot reach {urllib.parse.urlsplit(self.url).netloc}: "
                f"{e.reason}"
            ) from None
        if want_bytes:
            return raw
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    def _rest(self, method: str, table: str, *, query: str = "",
              rows=None, prefer: str | None = None):
        body = None
        headers = {}
        if rows is not None:
            body = json.dumps(rows, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        if prefer:
            headers["Prefer"] = prefer
        path = f"/rest/v1/{table}" + (f"?{query}" if query else "")
        return self._request(method, path, body=body, headers=headers)

    # ------------------------------------------------------------------ checks

    def ping(self) -> None:
        """Raise unless the pool answers and the schema is in place."""
        self._rest("GET", "scans", query="select=id&limit=1")

    # ------------------------------------------------------------------ writes

    def put_object(self, path: str, payload: bytes,
                   content_type: str = "application/json") -> bool:
        """Store a payload. False if that exact path already exists.

        No upsert: the pool grants insert only, and a scan's path is derived
        from its contents, so a collision means the same scan twice.
        """
        try:
            self._request(
                "POST", f"/storage/v1/object/{self.bucket}/{path}",
                body=payload, headers={"Content-Type": content_type},
            )
        except PoolError as e:
            msg = str(e).lower()
            if "already exists" in msg or "duplicate" in msg or "-> 409" in msg:
                return False
            raise
        return True

    def add_scan(self, row: dict) -> bool:
        """Append an index row. False if this scan was already in the pool.

        The pool grants no UPDATE, so a repeat is a plain duplicate-key error
        rather than something to overwrite — which is the point.
        """
        try:
            self._rest("POST", "scans", rows=[row], prefer="return=minimal")
        except PoolError as e:
            if "duplicate key" in str(e).lower() or "-> 409" in str(e):
                return False
            raise
        return True

    def ingest_prices(self, rows: list[dict]) -> int:
        """Upsert item/day observations, keeping the lowest price per day."""
        done = 0
        for i in range(0, len(rows), RPC_CHUNK):
            chunk = rows[i:i + RPC_CHUNK]
            self._rest("POST", "rpc/ingest_prices", rows={"payload": chunk},
                       prefer="return=minimal")
            done += len(chunk)
        return done

    # ------------------------------------------------------------------- reads

    def realms(self) -> list:
        """One row per realm: how many scans, from how many people, how new."""
        return self._rest("GET", "pool_realms",
                          query="select=*&order=latest.desc") or []

    def list_scans(self, realm: str | None = None, limit: int = 100) -> list:
        q = ["select=*", "order=scanned_at.desc", f"limit={int(limit)}"]
        if realm:
            q.append("realm=eq." + urllib.parse.quote(realm, safe=""))
        return self._rest("GET", "scans", query="&".join(q)) or []

    def get_object(self, path: str) -> bytes:
        return self._request(
            "GET", f"/storage/v1/object/{self.bucket}/{path}", want_bytes=True
        )

    def history(self, realm: str, item_ids: list[int] | None = None,
                since: str | None = None, limit: int = 10000) -> list:
        q = ["select=item,day,price,qty,min_seen",
             "realm=eq." + urllib.parse.quote(realm, safe=""),
             "order=day.asc", f"limit={int(limit)}"]
        if item_ids:
            ids = ",".join(str(int(i)) for i in item_ids)
            q.append(f"item=in.({ids})")
        if since:
            q.append(f"day=gte.{since}")
        return self._rest("GET", "prices", query="&".join(q)) or []


# --------------------------------------------------------------------- payloads


def scan_id(realm: str, uploader: str, scanned_at: str) -> str:
    stamp = re.sub(r"[^0-9]", "", str(scanned_at))[:14]
    return f"{slug(realm)}_{slug(uploader)}_{stamp}"


def object_path(realm: str, uploader: str, scanned_at: str) -> str:
    return f"{slug(realm)}/{slug(uploader)}/{scan_id(realm, uploader, scanned_at)}.json"


def price_rows(packed: dict, days: int | None = 3) -> list[dict]:
    """Flatten a packed scan into (realm, item, day, price, qty) rows.

    `days` keeps only the most recent N days of each item's history; None
    sends the lot, which is how a first upload backfills Auctionator's whole
    21-day window.
    """
    import datetime as dt

    realm = packed["realm"]
    ids = packed["id"]
    qty = packed.get("q") or []
    ms = packed.get("ms") or []
    hist = packed.get("hist") or {}

    day_nums = sorted(int(d) for d in hist if str(d).lstrip("-").isdigit())
    if days is not None:
        day_nums = day_nums[-days:]
    epoch = dt.date(2020, 1, 1)

    rows = []
    for dn in day_nums:
        date = (epoch + dt.timedelta(days=dn)).isoformat()
        for idx, price in hist.get(str(dn), []):
            if not isinstance(idx, int) or idx >= len(ids):
                continue
            iid = ids[idx]
            if not isinstance(iid, int):
                continue
            rows.append({
                "realm": realm,
                "item": iid,
                "day": date,
                "price": price,
                "qty": qty[idx] if idx < len(qty) else None,
                "min_seen": ms[idx] if idx < len(ms) else None,
            })
    return rows


def scan_row(packed: dict, path: str, nbytes: int) -> dict:
    return {
        "id": scan_id(packed["realm"], packed["uploader"], packed["scan"]),
        "realm": packed["realm"],
        "uploader": str(packed["uploader"])[:48],
        "scanned_at": packed["scan"],
        "items": packed["n"],
        "schema": packed.get("schema", 1),
        "bytes": nbytes,
        "object_path": path,
    }
