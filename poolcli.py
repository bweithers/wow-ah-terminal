"""Pool commands: provision it, join it, push scans, watch for new ones.

`pool-init` is the owner's one-off and drives the Supabase CLI with the
owner's own login. Everything else runs on the publishable key alone, so a
friend needs no Supabase account at all.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import secrets
import shutil
import subprocess
import time

import pack
import pool

REGIONS = ("us-east-1", "us-east-2", "us-west-1", "us-west-2", "eu-west-1",
           "eu-west-2", "eu-central-1", "ap-southeast-1", "ap-southeast-2",
           "ap-northeast-1", "sa-east-1", "ca-central-1")


# --------------------------------------------------------------------------- #
# driving the Supabase CLI
# --------------------------------------------------------------------------- #

def supabase_cmd() -> list[str]:
    """How to invoke the Supabase CLI here, preferring an installed binary."""
    exe = shutil.which("supabase")
    if exe:
        return [exe]
    npx = shutil.which("npx")
    if npx:
        return [npx, "--yes", "supabase@latest"]
    raise SystemExit(
        "The Supabase CLI is not available.\n"
        "Install Node (which brings npx) or the CLI itself:\n"
        "  https://supabase.com/docs/guides/local-development/cli/getting-started"
    )


def sb(args: list[str], *, json_out: bool = False, check: bool = True,
       quiet: bool = False, timeout: int = 300):
    """Run a Supabase CLI subcommand. Returns parsed JSON or raw stdout."""
    cmd = supabase_cmd() + args
    if json_out:
        cmd += ["--output-format", "json"]
    if not quiet:
        print(f"  $ supabase {' '.join(args)}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if "AccessTokenRequiredError" in out + err:
        raise SystemExit(
            "Supabase needs you to log in first. Run this, approve it in the\n"
            "browser it opens, then re-run the same ah.py command:\n\n"
            "    npx supabase login\n"
        )
    if check and r.returncode != 0:
        detail = (err or out)[:800]
        raise SystemExit(f"supabase {' '.join(args)} failed:\n{detail}")
    if not json_out:
        return out
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        # the CLI sometimes prefixes notices; take the last JSON-looking line
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith(("{", "[")):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        raise SystemExit(f"could not read JSON from supabase {args[0]}:\n{out[:400]}")


def _rows(payload):
    """The CLI wraps some payloads; get at the list either way."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("data", "result", "rows", "projects", "organizations"):
            v = payload.get(k)
            if isinstance(v, list):
                return v
    return []


def _get(d: dict, *names, default=None):
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return default


# --------------------------------------------------------------------------- #
# invites
# --------------------------------------------------------------------------- #

def make_invite(url: str, key: str, bucket: str) -> str:
    raw = json.dumps({"u": url, "k": key, "b": bucket},
                     separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def read_invite(token: str) -> dict:
    t = token.strip()
    pad = "=" * (-len(t) % 4)
    try:
        d = json.loads(base64.urlsafe_b64decode(t + pad))
        if not d.get("u") or not d.get("k"):
            raise ValueError
        return {"supabase_url": d["u"], "supabase_key": d["k"],
                "supabase_bucket": d.get("b") or "scans"}
    except Exception:
        raise SystemExit("That does not look like a pool invite. Ask for the "
                         "line that `ah.py pool-init` printed.")


# --------------------------------------------------------------------------- #
# writing config
# --------------------------------------------------------------------------- #

def save_config(ah, updates: dict) -> dict:
    cfg = ah.load_config()
    cfg.update(updates)
    with open(ah.CONFIG, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    return cfg


def write_pool_js(ah, cfg: dict) -> str:
    """Hand the web terminal the pool's address, outside the HTML itself."""
    path = os.path.join(ah.HERE, "web", "pool.js")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = {
        "url": cfg["supabase_url"],
        "key": cfg["supabase_key"],
        "bucket": cfg.get("supabase_bucket") or "scans",
    }
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("/* Written by ah.py -- the shared pool this page reads.\n"
                 "   The key is Supabase's publishable key: safe in client\n"
                 "   code, and the pool's row-level security is what holds. */\n")
        fh.write("window.POOL = " + json.dumps(body, indent=2) + ";\n")
    return path


# --------------------------------------------------------------------------- #
# pool-init  (owner, once)
# --------------------------------------------------------------------------- #

def cmd_pool_init(ah, cfg, args):
    print("Supabase pool setup")
    print("=" * 60)
    ver = sb(["--version"], quiet=True)
    print(f"  CLI {ver or '?'}")

    projects = _rows(sb(["projects", "list"], json_out=True, quiet=True))
    ref = args.project_ref
    want = args.name or "wow-ah-pool"

    if not ref:
        named = [p for p in projects
                 if str(_get(p, "name", default="")).lower() == want.lower()]
        if named:
            ref = _get(named[0], "id", "ref", "project_ref")
            print(f"  using existing project {want} ({ref})")

    if not ref and not args.create:
        if projects:
            print("\nYour Supabase projects:")
            for p in projects:
                print(f"  {_get(p, 'id', 'ref', 'project_ref'):22} "
                      f"{_get(p, 'name', default='?'):28} "
                      f"{_get(p, 'region', default=''):14} "
                      f"{_get(p, 'status', default='')}")
            print("\nPick one with  --project-ref <ref>")
        print(f"\nOr make a new one:  python ah.py pool-init --create "
              f"--name {want}")
        return

    if not ref:
        orgs = _rows(sb(["orgs", "list"], json_out=True, quiet=True))
        org = args.org_id or (_get(orgs[0], "id", "slug") if orgs else None)
        if not org:
            raise SystemExit("No Supabase organization found for your login.")
        region = args.region if args.region in REGIONS else "us-east-1"
        # Postgres needs a password at creation. We generate one, never store
        # it and never print it: nothing here uses it, because the CLI talks
        # to the project through your login instead. Reset it in the Supabase
        # dashboard if you ever want direct psql access.
        pw = secrets.token_urlsafe(24)
        print(f"\n  creating project {want} in {org} ({region}) ...")
        sb(["projects", "create", want, "--org-id", str(org),
            "--db-password", pw, "--region", region, "--size", "micro",
            "--yes"], quiet=True)
        ref = None
        for _ in range(40):
            for p in _rows(sb(["projects", "list"], json_out=True, quiet=True)):
                if str(_get(p, "name", default="")).lower() == want.lower():
                    ref = _get(p, "id", "ref", "project_ref")
                    status = str(_get(p, "status", default=""))
                    break
            else:
                status = ""
            if ref and status.upper().startswith("ACTIVE"):
                break
            print(f"    waiting for the project to come up ({status or '...'})")
            time.sleep(15)
        if not ref:
            raise SystemExit("The project did not appear; check the dashboard.")

    url = f"https://{ref}.supabase.co"
    print(f"\n  project {ref}")

    # --- schema -----------------------------------------------------------
    sql = os.path.join(ah.HERE, "schema.sql")
    print("\n  applying schema.sql ...")
    sb(["db", "query", f"--remote={ref}", "-f", sql, "--yes"], quiet=True)
    print("    tables, policies and buckets are in place")

    # --- keys -------------------------------------------------------------
    keys = _rows(sb(["projects", "api-keys", f"--remote={ref}"],
                    json_out=True, quiet=True))
    key = None
    for k in keys:
        tag = f"{_get(k, 'name', 'type', default='')}".lower()
        val = _get(k, "api_key", "apiKey", "key", default="")
        if not val or "secret" in tag or "service" in tag:
            continue
        if "publishable" in tag or "anon" in tag or val.startswith("sb_publishable_"):
            key = val
            break
    if not key:
        raise SystemExit(
            "Could not find the project's publishable key. Copy it from\n"
            f"  https://supabase.com/dashboard/project/{ref}/settings/api\n"
            "and run:  python ah.py pool-join --url {url} --key <key>"
        )

    cfg = save_config(ah, {"supabase_url": url, "supabase_key": key,
                           "supabase_bucket": "scans",
                           "supabase_ref": ref})
    js = write_pool_js(ah, cfg)
    print(f"    publishable key stored in config.json and {os.path.basename(js)}")

    # --- prove it works ---------------------------------------------------
    p = pool.from_config(cfg)
    p.ping()
    print("    pool answers on the publishable key")

    # --- host the page (optional, nice to have) ---------------------------
    hosted = None
    try:
        for f, ctype in (("terminal.html", "text/html; charset=utf-8"),
                         ("pool.js", "application/javascript; charset=utf-8")):
            src = os.path.join(ah.HERE, "web", f)
            sb(["storage", "cp", src, f"ss:///web/{f}", f"--remote={ref}",
                "--content-type", ctype, "--cache-control", "max-age=60",
                "--yes"], quiet=True)
        hosted = f"{url}/storage/v1/object/public/web/terminal.html"
        ctype = _head_content_type(hosted)
        if ctype and "html" not in ctype:
            print(f"    (hosted copy serves as {ctype}, so browsers would "
                  "download it rather than show it -- skipping that link)")
            hosted = None
    except SystemExit as e:
        print(f"    (could not upload the page: {str(e)[:120]})")
        hosted = None

    invite = make_invite(url, key, "scans")
    print("\n" + "=" * 60)
    print("Pool is live.")
    print(f"  REST     {url}")
    if hosted:
        print(f"  terminal {hosted}")
    print("\nSend your friends this one line:\n")
    print(f"  python ah.py pool-join {invite}\n")
    print("Then they run  python ah.py watch  and every scan they make lands")
    print("in the pool. Push your own current scan now with:  python ah.py push")


def _head_content_type(url: str) -> str | None:
    import urllib.request
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.headers.get("Content-Type")
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# pool-join  (everyone else)
# --------------------------------------------------------------------------- #

def cmd_pool_join(ah, cfg, args):
    if args.invite:
        updates = read_invite(args.invite)
    elif args.url and args.key:
        updates = {"supabase_url": args.url.rstrip("/"),
                   "supabase_key": args.key,
                   "supabase_bucket": args.bucket or "scans"}
    else:
        raise SystemExit(
            "Usage:  python ah.py pool-join <invite>\n"
            "    or  python ah.py pool-join --url https://xxx.supabase.co "
            "--key <publishable key>"
        )
    if "secret" in updates["supabase_key"] or "service_role" in updates["supabase_key"]:
        raise SystemExit(
            "That is a secret key. The pool wants the *publishable* key "
            "(sb_publishable_... or the anon key) -- ask for that one instead."
        )
    who = args.uploader or cfg.get("uploader")
    if not who:
        who = os.environ.get("USERNAME") or os.environ.get("USER") or "friend"
    updates["uploader"] = pool.slug(who)

    cfg = save_config(ah, updates)
    write_pool_js(ah, cfg)
    p = pool.from_config(cfg)
    p.ping()
    scans = p.list_scans(limit=5)
    print(f"Joined the pool at {cfg['supabase_url']} as \"{cfg['uploader']}\".")
    if scans:
        print(f"{len(scans)} recent scan(s):")
        for s in scans:
            print(f"  {s['scanned_at'][:16].replace('T', ' ')}  "
                  f"{s['realm']:12} {s['uploader']:12} {s['items']:>6,} items")
    else:
        print("The pool is empty so far -- yours can be the first.")
    print("\nNext:  python ah.py push        (send your latest scan)")
    print("       python ah.py watch       (send every scan from now on)")


# --------------------------------------------------------------------------- #
# push / pull
# --------------------------------------------------------------------------- #

def pack_latest(ah, cfg, args) -> tuple[dict, bytes]:
    snaps = ah.load_snapshots()
    if not snaps:
        raise SystemExit("No snapshots yet -- run: python ah.py snapshot")
    cur = snaps[-1]
    realm, _ = ah.pick_realm(cur, cfg, args)
    who = pool.slug(cfg.get("uploader") or os.environ.get("USERNAME") or "me")
    packed = pack.pack(cur, realm, who)
    body = json.dumps(packed, separators=(",", ":")).encode()
    return packed, body


def push_packed(p: pool.Pool, packed: dict, body: bytes, *,
                days: int | None, quiet: bool = False) -> dict:
    path = pool.object_path(packed["realm"], packed["uploader"], packed["scan"])
    fresh_obj = p.put_object(path, body)
    row = pool.scan_row(packed, path, len(body))
    fresh_row = p.add_scan(row)
    rows = pool.price_rows(packed, days)
    sent = p.ingest_prices(rows) if rows else 0
    if not quiet:
        state = "uploaded" if (fresh_obj or fresh_row) else "already in the pool"
        print(f"  {packed['realm']} / {packed['uploader']} / "
              f"{packed['scan'][:16].replace('T', ' ')}: {state}, "
              f"{len(body)/1024:,.0f} KB, {packed['n']:,} items, "
              f"{sent:,} price rows")
    return {"path": path, "new": fresh_obj or fresh_row, "rows": sent}


def cmd_push(ah, cfg, args):
    p = pool.from_config(cfg)
    packed, body = pack_latest(ah, cfg, args)
    days = None if args.full else max(1, args.days)
    push_packed(p, packed, body, days=days)
    print("Pushed.")


def cmd_pull(ah, cfg, args):
    p = pool.from_config(cfg)
    realm = args.realm or cfg.get("realm")
    scans = p.list_scans(realm=realm, limit=args.limit)
    if not scans:
        print("The pool has no scans yet.")
        return
    outdir = os.path.join(ah.DATA, "pool")
    os.makedirs(outdir, exist_ok=True)

    if not args.all:
        newest: dict[tuple, dict] = {}
        for s in scans:
            k = (s["realm"], s["uploader"])
            if k not in newest:
                newest[k] = s
        scans = list(newest.values())

    print(f"{len(scans)} scan(s):")
    got = 0
    for s in scans:
        dest = os.path.join(outdir, s["id"] + ".json")
        mark = "cached"
        if not os.path.exists(dest):
            with open(dest, "wb") as fh:
                fh.write(p.get_object(s["object_path"]))
            mark = "downloaded"
            got += 1
        print(f"  {s['scanned_at'][:16].replace('T', ' ')}  {s['realm']:12} "
              f"{s['uploader']:12} {s['items']:>6,} items  {mark}")
    print(f"\n{got} new file(s) in {os.path.relpath(outdir, ah.HERE)}")


# --------------------------------------------------------------------------- #
# watch
# --------------------------------------------------------------------------- #

def _stamp() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


def _state_path(ah) -> str:
    return os.path.join(ah.DATA, "watch-state.json")


def load_watch_state(ah) -> dict:
    """What the watcher last saw, so `--once` is incremental across runs.

    Without this, a scheduled `watch --once` would repack and re-upload the
    same scan every few minutes. The pool would absorb it -- a repeat is a
    no-op there -- but it is pure waste.
    """
    try:
        with open(_state_path(ah), encoding="utf-8") as fh:
            d = json.load(fh)
        return {"sig": tuple(d["sig"]) if d.get("sig") else None,
                "first": bool(d.get("first", True))}
    except (OSError, ValueError, KeyError):
        return {"sig": None, "first": True}


def save_watch_state(ah, state: dict) -> None:
    try:
        os.makedirs(ah.DATA, exist_ok=True)
        with open(_state_path(ah), "w", encoding="utf-8") as fh:
            json.dump({"sig": list(state["sig"]) if state.get("sig") else None,
                       "first": state.get("first", False)}, fh)
    except OSError:
        pass            # a watcher that cannot save state still works


def _one_pass(ah, cfg, args, p, *, state: dict) -> bool:
    """Snapshot and push if the SavedVariables file has moved on. True if sent."""
    path = ah.sv_file(cfg, "Auctionator.lua")
    if not path:
        if not state.get("warned_missing"):
            print(f"[{_stamp()}] waiting for Auctionator.lua to appear "
                  f"(log in, scan, then /reload)")
            state["warned_missing"] = True
        return False
    state["warned_missing"] = False

    sig = (os.path.getmtime(path), os.path.getsize(path))
    if sig == state.get("sig"):
        return False
    # WoW writes the whole file at once, but wait for the size to settle
    # before reading, so a push never sees half a file.
    time.sleep(1.5)
    if (os.path.getmtime(path), os.path.getsize(path)) != sig:
        return False

    print(f"[{_stamp()}] new scan data ({ah.mtime(path):%Y-%m-%d %H:%M})")
    try:
        ah.do_snapshot(cfg, quiet=True)
        if os.path.isdir(ah.addon_dir(cfg)):
            try:
                ah.cmd_sync_ids(cfg, args)
            except SystemExit as e:
                print(f"  (sync-ids skipped: {e})")
        packed, body = pack_latest(ah, cfg, args)
        days = None if state.get("first") else max(1, args.days)
        push_packed(p, packed, body, days=days)
        state["first"] = False
    except pool.PoolError as e:
        print(f"  pool error, will retry next time: {e}")
        return False
    except SystemExit as e:
        print(f"  skipped: {e}")
    state["sig"] = sig
    save_watch_state(ah, state)
    return True


def cmd_watch(ah, cfg, args):
    p = pool.from_config(cfg)
    p.ping()
    path = ah.sv_file(cfg, "Auctionator.lua")
    state = load_watch_state(ah)

    if args.once:
        if not _one_pass(ah, cfg, args, p, state=state):
            print(f"[{_stamp()}] nothing new since the last check.")
        return

    print(f"Watching for new scans every {args.interval}s. Ctrl-C to stop.")
    print(f"  file  {path or '(not there yet)'}")
    print(f"  pool  {cfg['supabase_url']} as \"{cfg.get('uploader')}\"")
    print("Scan the AH in game, then /reload -- that is when WoW writes the "
          "file, and\nthis will pick it up within a few seconds.\n")
    if path:
        # one pass now, so a scan made before the watcher started still goes
        # in; the saved signature keeps this from repeating needlessly.
        _one_pass(ah, cfg, args, p, state=state)
    try:
        while True:
            time.sleep(max(5, args.interval))
            try:
                _one_pass(ah, cfg, args, p, state=state)
            except Exception as e:  # keep the watcher alive overnight
                print(f"[{_stamp()}] unexpected: {type(e).__name__}: {e}")
    except KeyboardInterrupt:
        print("\nStopped.")


# --------------------------------------------------------------------------- #
# ui  -- serve the terminal locally, where it can reach the pool
# --------------------------------------------------------------------------- #

def cmd_ui(ah, cfg, args):
    import http.server
    import socketserver
    import threading
    import webbrowser

    root = os.path.join(ah.HERE, "web")
    if not os.path.isdir(root):
        raise SystemExit(f"No web directory at {root}")

    class Handler(http.server.SimpleHTTPRequestHandler):
        # SimpleHTTPRequestHandler sends bare "text/html", and a browser then
        # falls back to windows-1252 and mangles every non-ASCII item name.
        TEXT = {".html": "text/html", ".js": "text/javascript",
                ".json": "application/json", ".css": "text/css",
                ".lua": "text/plain"}

        def __init__(self, *a, **kw):
            super().__init__(*a, directory=root, **kw)

        def guess_type(self, path):
            ext = os.path.splitext(str(path))[1].lower()
            if ext in self.TEXT:
                return self.TEXT[ext] + "; charset=utf-8"
            return super().guess_type(path)

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def log_message(self, *a):
            pass

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    port = args.port
    for attempt in range(10):
        try:
            httpd = Server(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    else:
        raise SystemExit(f"No free port near {args.port}")

    url = f"http://127.0.0.1:{port}/terminal.html"
    print(f"Terminal at {url}")
    if pool.configured(cfg):
        print(f"Pool      {cfg['supabase_url']}")
    else:
        print("No pool configured -- drag a SavedVariables file onto the page.")
    print("Ctrl-C to stop.")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


# --------------------------------------------------------------------------- #
# pool status
# --------------------------------------------------------------------------- #

def cmd_pool(ah, cfg, args):
    if not pool.configured(cfg):
        print("No pool configured.")
        print("  owner:   python ah.py pool-init --create")
        print("  invited: python ah.py pool-join <invite>")
        return
    p = pool.from_config(cfg)
    print(f"Pool     {cfg['supabase_url']}")
    print(f"You are  {cfg.get('uploader') or '(unnamed -- set uploader in config.json)'}")
    realms = p.realms()
    if realms:
        print()
        print(ah.table(
            [[r["realm"], f"{r['scans']:,}", f"{r['uploaders']:,}",
              r["latest"][:16].replace("T", " "),
              f"{(r['bytes'] or 0)/1048576:.1f} MB"] for r in realms],
            ["realm", "scans", "people", "latest", "stored"]))
    scans = p.list_scans(limit=args.limit)
    if scans:
        print()
        print(ah.table(
            [[s["scanned_at"][:16].replace("T", " "), s["realm"], s["uploader"],
              f"{s['items']:,}", f"{s['bytes']/1024:,.0f} KB"] for s in scans],
            ["scanned", "realm", "who", "items", "size"]))
    if not pool.configured(cfg):
        return
    print(f"\nInvite line for a friend:\n  python ah.py pool-join "
          f"{make_invite(cfg['supabase_url'], cfg['supabase_key'], cfg.get('supabase_bucket') or 'scans')}")
