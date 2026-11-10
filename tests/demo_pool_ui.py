"""Run the terminal against a stand-in pool, for looking at it with your eyes.

    python tests/demo_pool_ui.py

Seeds a fake Supabase with the real snapshot on disk, points the page at it,
serves web/ and stays up until Ctrl-C. Writes web/pool.js, so run
`python ah.py pool-join <invite>` afterwards to point the page back at the
real pool (or just delete that file).
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [ROOT, HERE]

import fake_supabase  # noqa: E402

import ah  # noqa: E402
import pack  # noqa: E402
import pool  # noqa: E402
import poolcli  # noqa: E402

WEB_PORT = 8732


def main() -> int:
    url, state, _srv = fake_supabase.start()
    cfg = ah.load_config()
    cfg.update({"supabase_url": url, "supabase_key": state.key,
                "supabase_bucket": "scans"})

    snaps = ah.load_snapshots()
    if not snaps:
        print("No snapshots -- run `python ah.py snapshot` first.")
        return 2
    cur = snaps[-1]
    realm = cfg.get("realm") or sorted(cur["realms"])[0]

    p = pool.Pool(url, state.key)
    for who in ("brian", "friend-a", "friend-b"):
        packed = pack.pack(cur, realm, who)
        # nudge the timestamps apart so the list has something to sort
        if who != "brian":
            packed["scan"] = packed["scan"][:11] + f"0{len(who)}:00:00"
        body = json.dumps(packed, separators=(",", ":")).encode()
        poolcli.push_packed(p, packed, body, days=None, quiet=True)
    print(f"stand-in pool: {len(state.scans)} scans, "
          f"{len(state.prices):,} price rows, {len(state.objects)} objects")

    js = os.path.join(ROOT, "web", "pool.js")
    with open(js, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("window.POOL = " + json.dumps(
            {"url": url, "key": state.key, "bucket": "scans"}) + ";\n")

    root = os.path.join(ROOT, "web")

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=root, **kw)

        def guess_type(self, path):
            ext = os.path.splitext(str(path))[1].lower()
            if ext in (".html", ".js", ".json"):
                return {".html": "text/html", ".js": "text/javascript",
                        ".json": "application/json"}[ext] + "; charset=utf-8"
            return super().guess_type(path)

        def log_message(self, *a):
            pass

    class Srv(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    httpd = Srv(("127.0.0.1", WEB_PORT), H)
    print(f"terminal:  http://127.0.0.1:{WEB_PORT}/terminal.html")
    print(f"pool REST: {url}")
    print("Ctrl-C to stop.")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
