"""End-to-end test of the pool client against a stand-in Supabase.

Run from the project root:  python tests/test_pool.py

Exercises the real snapshot on disk through the real CLI entry point, so what
is tested is what `python ah.py push` actually does.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [ROOT, HERE]

import fake_supabase  # noqa: E402

import ah  # noqa: E402
import pack  # noqa: E402
import pool  # noqa: E402
import poolcli  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f" -- {detail}" if detail and not cond else ""))


class quiet:
    """Swallow a command's own output; we assert on state, not on prose."""

    def __enter__(self):
        self._old = sys.stdout
        sys.stdout = self.buf = io.StringIO()
        return self

    def __exit__(self, *a):
        sys.stdout = self._old
        return False

    @property
    def text(self):
        return self.buf.getvalue()


def main() -> int:
    url, state, srv = fake_supabase.start()
    tmp = tempfile.mkdtemp(prefix="ahpool-")

    # point the CLI at the stand-in and a scratch data dir, but keep the real
    # snapshot archive so the payload under test is the genuine scan
    ah.CONFIG = os.path.join(tmp, "config.json")
    ah.DATA = os.path.join(tmp, "data")
    real = ah.load_config.__wrapped__ if hasattr(ah.load_config, "__wrapped__") else None
    base = {}
    live = os.path.join(ROOT, "config.json")
    if os.path.exists(live):
        with open(live, encoding="utf-8") as fh:
            base = json.load(fh)
    cfg = dict(base)
    cfg.update({"supabase_url": url, "supabase_key": state.key,
                "supabase_bucket": "scans", "uploader": "tester"})
    with open(ah.CONFIG, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)

    snaps = ah.load_snapshots()
    if not snaps:
        print("No snapshots on disk -- run `python ah.py snapshot` first.")
        return 2

    print("\ninvites")
    tok = poolcli.make_invite(url, state.key, "scans")
    back = poolcli.read_invite(tok)
    check("invite round-trips", back["supabase_url"] == url
          and back["supabase_key"] == state.key)
    try:
        poolcli.read_invite("not-an-invite")
        check("garbage invite refused", False)
    except SystemExit:
        check("garbage invite refused", True)

    print("\nkeys never leak into errors")
    bad = pool.Pool(url, "wrong-key-value")
    try:
        bad.ping()
        check("bad key rejected", False)
    except pool.PoolError as e:
        check("bad key rejected", "401" in str(e), str(e))
    check("key scrubbed from messages",
          "<key>" in pool._scrub("a/wrong-key-value/b", "wrong-key-value"))

    print("\npush")
    with quiet() as q:
        rc = ah.main(["push"])
    check("push returns 0", rc == 0, q.text[-300:])
    check("one scan indexed", len(state.scans) == 1, str(len(state.scans)))
    check("one object stored", len(state.objects) == 1)

    row = next(iter(state.scans.values()))
    key = next(iter(state.objects))
    blob = state.objects[key]
    packed_back = json.loads(blob.decode())

    cur = snaps[-1]
    realm = row["realm"]
    expect = pack.pack(cur, realm, "tester")
    expect_bytes = json.dumps(expect, separators=(",", ":")).encode()
    check("stored object is byte-identical to the packed scan",
          blob == expect_bytes,
          f"{len(blob)} vs {len(expect_bytes)} bytes")
    check("index row matches the payload",
          row["items"] == packed_back["n"] and row["bytes"] == len(blob)
          and row["object_path"] == key and row["uploader"] == "tester",
          json.dumps(row)[:200])
    check("object path is namespaced by realm and uploader",
          key.startswith(f"{pool.slug(realm)}/tester/"), key)

    print("\nhistory ingest")
    check("price rows landed", len(state.prices) > 0, str(len(state.prices)))
    days = {k[2] for k in state.prices}
    check("default push sends at most 3 days", len(days) <= 3, str(sorted(days)))
    full = pool.price_rows(expect, None)
    check("--full would send more than the default",
          len({r["day"] for r in full}) >= len(days))
    check("every row is realm/item/day keyed",
          all(isinstance(r["item"], int) and len(r["day"]) == 10 for r in full))

    some = next(iter(state.prices.values()))
    k = (some["realm"], some["item"], some["day"])
    before = state.prices[k]["price"]
    p = pool.from_config(cfg)
    p.ingest_prices([{**some, "price": (before or 100) + 50}])
    check("a higher price does not raise the stored one",
          state.prices[k]["price"] == before, str(state.prices[k]["price"]))
    p.ingest_prices([{**some, "price": max(1, (before or 100) - 50)}])
    check("a lower price wins",
          state.prices[k]["price"] == max(1, (before or 100) - 50),
          str(state.prices[k]["price"]))

    print("\nchunking")
    n_before = len([c for c in state.calls if "ingest_prices" in c[1]])
    many = [{"realm": "Chunky", "item": i, "day": "2026-11-01",
             "price": 10 + i, "qty": 1, "min_seen": 10} for i in range(9001)]
    sent = p.ingest_prices(many)
    n_after = len([c for c in state.calls if "ingest_prices" in c[1]])
    check("9001 rows go in one batch per 4000", n_after - n_before == 3,
          str(n_after - n_before))
    check("every row acknowledged", sent == 9001, str(sent))
    check("chunked rows all stored",
          len([1 for kk in state.prices if kk[0] == "Chunky"]) == 9001)

    print("\nidempotence")
    with quiet() as q2:
        ah.main(["push"])
    check("re-push adds no scan row", len(state.scans) == 1)
    check("re-push adds no object", len(state.objects) == 1)
    check("re-push says so", "already in the pool" in q2.text, q2.text[-200:])

    print("\npull")
    with quiet() as q3:
        rc = ah.main(["pull"])
    check("pull returns 0", rc == 0, q3.text[-300:])
    dest = os.path.join(ah.DATA, "pool", row["id"] + ".json")
    check("pulled file exists", os.path.exists(dest), dest)
    if os.path.exists(dest):
        with open(dest, "rb") as fh:
            check("pulled bytes match what was pushed", fh.read() == blob)

    print("\nsummary view")
    realms = p.realms()
    check("one realm summarised", len(realms) == 1, str(realms))
    if realms:
        check("realm counts right",
              realms[0]["scans"] == 1 and realms[0]["uploaders"] == 1,
              json.dumps(realms[0]))
    with quiet() as q4:
        rc = ah.main(["pool"])
    check("pool status returns 0", rc == 0, q4.text[-300:])
    check("pool status prints an invite line",
          "pool-join" in q4.text, q4.text[-200:])

    print("\nwatch --once")
    state.scans.clear()
    state.objects.clear()
    with quiet() as q5:
        rc = ah.main(["watch", "--once"])
    check("watch --once returns 0", rc == 0, q5.text[-400:])
    check("watch pushed the current scan", len(state.scans) == 1,
          q5.text[-400:])

    print("\nunreachable pool")
    dead = pool.Pool("http://127.0.0.1:9", state.key)
    try:
        dead.ping()
        check("unreachable pool raises", False)
    except pool.PoolError as e:
        check("unreachable pool explains itself", "cannot reach" in str(e), str(e))

    srv.shutdown()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED: " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
