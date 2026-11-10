#!/usr/bin/env python3
"""Auction house economy workflow for WoW Forever + Auctionator.

Reads the SavedVariables written on logout/reload, archives a timestamped
snapshot each time, and reports on the market across snapshots.

    python ah.py status      # where the data is, how fresh it is
    python ah.py snapshot    # archive the current scan
    python ah.py report      # analyse latest snapshot + history
    python ah.py refresh     # snapshot then report
    python ah.py export      # CSV of the latest snapshot
    python ah.py discover    # print the shape of the saved data

The price database is stored as a CBOR-packed blob keyed by realm; records
look like {"m": minPrice, "h": {day: price}, "a": {day: quantity}, "l": [...]}
with day numbers counted from 2020-01-01. Decoding is done from the public
CBOR spec (RFC 8949) -- no addon source is read or referenced.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import statistics
import sys

import cbor
import luasv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
SNAPDIR = os.path.join(DATA, "snapshots")
SERIES = os.path.join(DATA, "series.jsonl")
CONFIG = os.path.join(HERE, "config.json")

DAY_EPOCH = dt.date(2020, 1, 1)

DEFAULT_CONFIG = {
    "wow_dir": "D:/World of Warcraft",
    "flavor": "_forever_",
    "account": None,
    "realm": None,
}


# --------------------------------------------------------------------------- #
# paths / config
# --------------------------------------------------------------------------- #

def _win_drives() -> list[str]:
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        return ["C:\\", "D:\\"]
    return [f"{chr(65 + i)}:\\" for i in range(26) if mask >> i & 1]


def detect_wow_dir(flavor: str) -> str | None:
    """Find a WoW install with this flavor's SavedVariables in it.

    A friend's copy of this tool starts with someone else's path in the
    default config, so look for theirs rather than making them edit JSON.
    """
    roots: list[str] = []
    if sys.platform == "win32":
        for d in _win_drives():
            for sub in ("", "Games", "Program Files (x86)", "Program Files",
                        os.path.join("Program Files (x86)", "Battle.net")):
                roots.append(os.path.join(d, sub, "World of Warcraft"))
    else:
        roots += ["/Applications/World of Warcraft",
                  os.path.expanduser("~/Applications/World of Warcraft")]
    for r in roots:
        try:
            if os.path.isdir(os.path.join(r, flavor, "WTF", "Account")):
                return os.path.normpath(r)
        except OSError:
            continue
    return None


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG):
        with open(CONFIG, encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    flavor = cfg.get("flavor") or "_forever_"
    if not os.path.isdir(os.path.join(cfg.get("wow_dir") or "", flavor, "WTF")):
        found = detect_wow_dir(flavor)
        if found:
            cfg["wow_dir"] = found
    return cfg


def sv_dir(cfg: dict) -> str | None:
    root = os.path.join(cfg["wow_dir"], cfg["flavor"], "WTF", "Account")
    if not os.path.isdir(root):
        return None
    accounts = [
        d for d in os.listdir(root)
        if d != "SavedVariables"
        and os.path.isdir(os.path.join(root, d, "SavedVariables"))
    ]
    acct = cfg.get("account")
    pick = acct if acct in accounts else (sorted(accounts)[0] if accounts else None)
    return os.path.join(root, pick, "SavedVariables") if pick else None


def sv_file(cfg: dict, name: str) -> str | None:
    d = sv_dir(cfg)
    if not d:
        return None
    p = os.path.join(d, name)
    return p if os.path.exists(p) else None


def read_sv(path: str) -> dict:
    """Parse a SavedVariables file, preserving raw bytes in strings."""
    with open(path, "rb") as fh:
        return luasv.parse(fh.read().decode("latin-1"))


def mtime(path: str) -> dt.datetime:
    return dt.datetime.fromtimestamp(os.path.getmtime(path))


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #

def gold(copper) -> str:
    if copper is None or copper == "":
        return "-"
    c = int(round(float(copper)))
    sign = "-" if c < 0 else ""
    c = abs(c)
    g, c = divmod(c, 10000)
    s, c = divmod(c, 100)
    if g:
        return f"{sign}{g:,}g{s:02d}s"
    if s:
        return f"{sign}{s}s{c:02d}c"
    return f"{sign}{c}c"


def pct(x) -> str:
    return "-" if x is None else f"{x:+.0f}%"


def day_to_date(day) -> dt.date:
    return DAY_EPOCH + dt.timedelta(days=int(day))


def table(rows, headers, limit=None):
    if not rows:
        return "    (none)"
    rows = [[("" if c is None else str(c)) for c in r] for r in rows[: limit or len(rows)]]
    w = [max(len(headers[i]), *(len(r[i]) for r in rows)) for i in range(len(headers))]
    num = [all(_looks_numeric(r[i]) for r in rows) for i in range(len(headers))]
    def fmt(cells):
        return "  " + "  ".join(
            cells[i].rjust(w[i]) if num[i] else cells[i].ljust(w[i])
            for i in range(len(headers))
        )
    out = [fmt([h for h in headers]), "  " + "  ".join("-" * x for x in w)]
    out += [fmt(r) for r in rows]
    return "\n".join(out)


def _looks_numeric(s: str) -> bool:
    return bool(s) and (s[0].isdigit() or s[0] in "+-") and any(ch.isdigit() for ch in s)


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #

def price_tables(price_db) -> dict:
    """{realm: {item_id: record}} -- decodes CBOR blobs, passes plain tables."""
    out = {}
    if not isinstance(price_db, dict):
        return out
    for realm, val in price_db.items():
        if isinstance(realm, str) and realm.startswith("__"):
            continue
        if isinstance(val, str):
            try:
                decoded = cbor.loads(val.encode("latin-1"))
            except Exception as exc:  # noqa: BLE001
                print(f"  ! could not decode realm {realm!r}: {exc}", file=sys.stderr)
                continue
        elif isinstance(val, dict):
            decoded = val
        else:
            continue
        out[str(realm)] = {
            str(k): v for k, v in decoded.items() if isinstance(v, dict)
        }
    return out


def normalize(item_id: str, rec: dict, names: dict) -> dict:
    hist = sorted(
        (int(d), float(p)) for d, p in (rec.get("h") or {}).items()
        if _is_num(p)
    )
    avail = sorted(
        (int(d), float(q)) for d, q in (rec.get("a") or {}).items()
        if _is_num(q)
    )
    min_seen = rec.get("m") if _is_num(rec.get("m")) else None
    price = hist[-1][1] if hist else min_seen
    return {
        "id": item_id,
        "name": names.get(int(item_id)) if item_id.isdigit() else None,
        "price": price,
        "qty": avail[-1][1] if avail else None,
        "min_seen": min_seen,
        "history": hist,
        "avail": avail,
        "days": len(hist),
        "last_day": hist[-1][0] if hist else (avail[-1][0] if avail else None),
    }


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def label(it: dict, width: int = 30) -> str:
    name = it.get("name") or f"#{it['id']}"
    return name if len(name) <= width else name[: width - 1] + "\u2026"


def _utf8(s):
    """SavedVariables are read byte-preserving; item names are UTF-8."""
    if isinstance(s, str):
        return s.encode("latin-1", "replace").decode("utf-8", "replace")
    return s


def load_addon(cfg: dict) -> dict:
    """Item details and order-book depth captured by the AHEcon addon.

    items: {id: {n name, s sellPrice, st stack, q quality, il itemLevel,
                 c classID, sc subClassID}}
    depth: {id: {t, tq totalQty, ladder [(unitPrice, qty)], agg}}
    """
    empty = {"items": {}, "depth": {}, "meta": {}}
    p = sv_file(cfg, "AHEcon.lua")
    if not p:
        return empty
    try:
        data = read_sv(p)
    except Exception:  # noqa: BLE001
        return empty

    items = {}
    for k, v in (data.get("AHECON_ITEMS") or {}).items():
        try:
            i = int(k)
        except (TypeError, ValueError):
            continue
        if isinstance(v, dict):
            rec = {kk: vv for kk, vv in v.items()}
            rec["n"] = _utf8(rec.get("n"))
            items[i] = rec
    # older files only carry the flat name table
    for k, v in (data.get("AHECON_NAMES") or {}).items():
        try:
            i = int(k)
        except (TypeError, ValueError):
            continue
        if i not in items and isinstance(v, str):
            items[i] = {"n": _utf8(v)}

    depth = {}
    for k, v in (data.get("AHECON_DEPTH") or {}).items():
        try:
            i = int(k)
        except (TypeError, ValueError):
            continue
        if not isinstance(v, dict):
            continue
        rungs = v.get("p")
        ladder = []
        if isinstance(rungs, dict):
            for e in rungs.values():
                if isinstance(e, dict) and _is_num(e.get("u")):
                    ladder.append([float(e["u"]), float(e.get("q") or 0)])
        ladder.sort()
        depth[i] = {
            "t": v.get("t"),
            "tq": v.get("tq"),
            "ladder": ladder,
            "agg": bool(v.get("agg")),
        }
    return {"items": items, "depth": depth, "meta": data.get("AHECON_META") or {}}


def load_names(cfg: dict) -> dict:
    return {i: r.get("n") for i, r in load_addon(cfg)["items"].items() if r.get("n")}


def flatten_postings(hist) -> list[dict]:
    out = []
    if not isinstance(hist, dict):
        return out
    for item_id, entries in hist.items():
        if str(item_id).startswith("__") or not isinstance(entries, dict):
            continue
        for e in entries.values():
            if isinstance(e, dict) and _is_num(e.get("price")):
                out.append({
                    "id": str(item_id),
                    "price": e["price"],
                    "quantity": e.get("quantity"),
                    "time": e.get("time"),
                })
    out.sort(key=lambda r: -(r.get("time") or 0))
    return out


# --------------------------------------------------------------------------- #
# snapshots
# --------------------------------------------------------------------------- #

def read_auctionator(cfg: dict) -> tuple[dict, str]:
    path = sv_file(cfg, "Auctionator.lua")
    if not path:
        raise SystemExit(
            "No Auctionator.lua in SavedVariables yet.\n"
            "Log in, open the AH and run a full scan, then /reload or log out --\n"
            "WoW only writes SavedVariables at those moments."
        )
    return read_sv(path), path


def do_snapshot(cfg: dict, quiet: bool = False):
    data, path = read_auctionator(cfg)
    src = mtime(path)
    os.makedirs(SNAPDIR, exist_ok=True)

    existing = sorted(glob.glob(os.path.join(SNAPDIR, "*.json")))
    if existing:
        with open(existing[-1], encoding="utf-8") as fh:
            if json.load(fh).get("source_mtime") == src.isoformat():
                if not quiet:
                    print(f"Scan from {src:%Y-%m-%d %H:%M} is already archived "
                          f"({os.path.basename(existing[-1])}).")
                return existing[-1]

    realms = price_tables(data.get("AUCTIONATOR_PRICE_DATABASE"))
    addon = load_addon(cfg)
    names = {i: r["n"] for i, r in addon["items"].items() if r.get("n")}
    snap = {
        "captured": dt.datetime.now().isoformat(timespec="seconds"),
        "source_mtime": src.isoformat(),
        "last_browse_scan": (data.get("AUCTIONATOR_SAVEDVARS") or {})
                            .get("TimeOfLastBrowseScan"),
        "realms": realms,
        "vendor": {
            str(k): v for k, v in (data.get("AUCTIONATOR_VENDOR_PRICE_CACHE") or {}).items()
            if _is_num(v) and not str(k).startswith("__")
        },
        "postings": flatten_postings(data.get("AUCTIONATOR_POSTING_HISTORY")),
        "names": {str(k): v for k, v in names.items()},
        "items": {str(k): v for k, v in addon["items"].items()},
        "depth": {str(k): v for k, v in addon["depth"].items()},
    }
    out = os.path.join(SNAPDIR, f"{src:%Y%m%d-%H%M%S}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, separators=(",", ":"))

    with open(SERIES, "a", encoding="utf-8") as fh:
        for realm, items in realms.items():
            for iid, rec in items.items():
                it = normalize(iid, rec, names)
                if it["price"] is None:
                    continue
                fh.write(json.dumps({
                    "ts": src.isoformat(), "realm": realm, "item": iid,
                    "price": it["price"], "qty": it["qty"],
                }, separators=(",", ":")) + "\n")

    if not quiet:
        total = sum(len(v) for v in realms.values())
        print(f"Archived {os.path.basename(out)}: "
              f"{total:,} items across {len(realms)} realm table(s), "
              f"{len(names):,} item names known")
    return out


def load_snapshots() -> list[dict]:
    out = []
    for p in sorted(glob.glob(os.path.join(SNAPDIR, "*.json"))):
        with open(p, encoding="utf-8") as fh:
            s = json.load(fh)
        s["_path"] = p
        out.append(s)
    return out


def pick_realm(snap, cfg, args):
    realms = snap.get("realms") or {}
    if not realms:
        return None, {}
    want = args.realm or cfg.get("realm")
    if want and want in realms:
        return want, realms[want]
    return max(realms.items(), key=lambda kv: len(kv[1]))


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_status(cfg, args):
    print(f"WoW            : {cfg['wow_dir']} [{cfg['flavor']}]")
    print(f"SavedVariables : {sv_dir(cfg) or 'NOT FOUND'}")
    for name in ("Auctionator.lua", "AHEcon.lua"):
        p = sv_file(cfg, name)
        if p:
            hrs = (dt.datetime.now() - mtime(p)).total_seconds() / 3600
            print(f"  {name:17s} {os.path.getsize(p)/1024:7,.0f} KB  "
                  f"{mtime(p):%Y-%m-%d %H:%M} ({hrs:.1f}h ago)")
        else:
            print(f"  {name:17s} not written yet")
    addon = load_addon(cfg)
    sells = sum(1 for r in addon["items"].values() if _is_num(r.get("s")))
    full = sum(1 for d in addon["depth"].values()
               if not d.get("agg") and len(d.get("ladder") or []) > 1)
    print(f"Addon data     : {len(addon['items']):,} items named, "
          f"{sells:,} with sell prices")
    print(f"                 {len(addon['depth']):,} depth records "
          f"({full:,} full ladders)")
    snaps = load_snapshots()
    print(f"Snapshots      : {len(snaps)}")
    for s in snaps[-6:]:
        n = sum(len(v) for v in s["realms"].values())
        print(f"  {os.path.basename(s['_path'])}  {n:,} items")
    if not snaps:
        print("  -> run: python ah.py snapshot")


def _skeleton(node, depth=0, maxdepth=4, prefix=""):
    pad = "  " * depth
    if isinstance(node, dict):
        print(f"{pad}{prefix}table ({len(node)} keys)")
        if depth >= maxdepth:
            return
        for k, v in list(node.items())[:3]:
            _skeleton(v, depth + 1, maxdepth, f"[{k!r}] = ")
        if len(node) > 3:
            print(f"{'  ' * (depth + 1)}... {len(node) - 3} more")
    elif isinstance(node, list):
        print(f"{pad}{prefix}list ({len(node)})")
    elif isinstance(node, str) and len(node) > 60:
        print(f"{pad}{prefix}packed string, {len(node):,} bytes")
    else:
        print(f"{pad}{prefix}{type(node).__name__} = {node!r}")


def cmd_discover(cfg, args):
    data, path = read_auctionator(cfg)
    print(f"{path}\nglobals: {', '.join(sorted(data))}\n")
    for var in sorted(data):
        print(f"=== {var} ===")
        _skeleton(data[var], maxdepth=args.depth)
        print()
    realms = price_tables(data.get("AUCTIONATOR_PRICE_DATABASE"))
    for realm, items in realms.items():
        print(f"=== decoded price table: {realm} ({len(items):,} items) ===")
        for iid, rec in list(items.items())[:4]:
            print(f"  {iid}: {rec}")
        print()


def cmd_report(cfg, args):
    snaps = load_snapshots()
    if not snaps:
        raise SystemExit("No snapshots yet -- run: python ah.py snapshot")
    cur = snaps[-1]
    realm, items = pick_realm(cur, cfg, args)
    if not items:
        raise SystemExit("Latest snapshot contains no realm price table.")

    names = {int(k): v for k, v in (cur.get("names") or {}).items() if k.isdigit()}
    all_items = [normalize(i, r, names) for i, r in items.items()]
    priced = [it for it in all_items if it["price"]]
    by_id = {it["id"]: it for it in priced}
    vendor = cur.get("vendor") or {}
    top = args.top
    W = 72

    scan_ts = cur["source_mtime"][:16].replace("T", " ")
    print("=" * W)
    print(f"  AUCTION HOUSE REPORT  --  realm {realm}")
    print("=" * W)
    named = sum(1 for it in priced if it["name"])
    print(f"  scan written   {scan_ts}")
    if cur.get("last_browse_scan"):
        print(f"  last AH scan   "
              f"{dt.datetime.fromtimestamp(cur['last_browse_scan']):%Y-%m-%d %H:%M}")
    print(f"  items priced   {len(priced):,}   "
          f"({named:,} named, {len(priced)-named:,} unnamed)")
    print(f"  archive        snapshot {len(snaps)} of {len(snaps)}")

    stocked = [it for it in priced if it["qty"]]
    if stocked:
        total_val = sum(it["price"] * it["qty"] for it in stocked)
        total_qty = sum(it["qty"] for it in stocked)
        print(f"  listed value   {gold(total_val)} across "
              f"{int(total_qty):,} units in {len(stocked):,} markets")
    prices = [it["price"] for it in priced]
    print(f"  unit price     median {gold(statistics.median(prices))}, "
          f"mean {gold(statistics.fmean(prices))}, max {gold(max(prices))}")
    if named < len(priced) * 0.5:
        print("\n  NOTE: most items are unnamed. Enable the AHEcon addon, full-scan")
        print("        the AH, /reload, then re-run to get names.")

    # --- where the money is -------------------------------------------- #
    print(f"\n{'-'*W}\n-- Biggest markets by total listed value\n{'-'*W}")
    rows = sorted(stocked, key=lambda it: -it["price"] * it["qty"])[:top]
    print(table(
        [[label(it), gold(it["price"] * it["qty"]), gold(it["price"]), int(it["qty"])]
         for it in rows],
        ["item", "listed value", "unit price", "qty"],
    ))

    print(f"\n{'-'*W}\n-- Highest unit prices\n{'-'*W}")
    rows = sorted(priced, key=lambda it: -it["price"])[:top]
    print(table(
        [[label(it), gold(it["price"]), int(it["qty"]) if it["qty"] else "-"]
         for it in rows],
        ["item", "unit price", "qty"],
    ))

    print(f"\n{'-'*W}\n-- Deepest supply (oversupplied / cheap to buy out)\n{'-'*W}")
    rows = sorted(stocked, key=lambda it: -it["qty"])[:top]
    print(table(
        [[label(it), int(it["qty"]), gold(it["price"]), gold(it["price"] * it["qty"])]
         for it in rows],
        ["item", "qty", "unit price", "listed value"],
    ))

    print(f"\n{'-'*W}\n-- Thin markets (<= {args.thin} listed, priced over "
          f"{gold(args.thin_floor)})\n{'-'*W}")
    thin = [it for it in stocked
            if it["qty"] <= args.thin and it["price"] >= args.thin_floor]
    thin.sort(key=lambda it: -it["price"])
    print(table(
        [[label(it), int(it["qty"]), gold(it["price"])] for it in thin[:top]],
        ["item", "qty", "unit price"],
    ))
    print(f"    {len(thin):,} markets are this thin -- easy to corner, "
          f"but check demand before buying.")

    # --- vendor sell-back ------------------------------------------------ #
    # The game's own sellPrice (what a vendor pays you), captured by the
    # addon. An AH price below it is risk-free: buy, then vendor it.
    meta_items = {int(k): v for k, v in (cur.get("items") or {}).items()
                  if str(k).isdigit()}
    print(f"\n{'-'*W}\n-- Vendor sell-back (buy on the AH, sell to a vendor)\n{'-'*W}")
    sells = {i: r["s"] for i, r in meta_items.items() if _is_num(r.get("s")) and r["s"]}
    if not sells:
        print("    (needs the AHEcon addon -- it records the game's sell prices)")
    else:
        backs = []
        for it in priced:
            if not it["id"].isdigit():
                continue
            s = sells.get(int(it["id"]))
            if s and it["price"] < s and it["price"] >= args.min_price:
                backs.append((it, s, s - it["price"]))
        backs.sort(key=lambda r: -(r[2] * (r[0]["qty"] or 1)))
        print(f"    sell prices known for {len(sells):,} items; "
              f"{len(backs):,} are listed under theirs")
        print(table(
            [[label(it), gold(it["price"]), gold(s), gold(p),
              int(it["qty"]) if it["qty"] else "-", gold(p * (it["qty"] or 1))]
             for it, s, p in backs[:top]],
            ["item", "AH price", "vendor pays", "per unit", "qty", "if you buy all"],
        ))

    # --- vendor flips ---------------------------------------------------- #
    # The vendor cache holds what a MERCHANT CHARGES per unit (captured while
    # you shop, stack price / stack size -- hence the fractional values). So
    # the profitable direction is vendor -> AH: buy at the fixed vendor price,
    # sell above it. The reverse is only a shopping tip.
    print(f"\n{'-'*W}\n-- Vendor -> AH flips (buy at vendor, sell on AH)\n{'-'*W}")
    if not vendor:
        print("    (no vendor price cache yet -- it fills in as you visit vendors)")
    else:
        keep = 1.0 - args.ah_cut / 100.0
        flips = []
        for it in priced:
            v = vendor.get(it["id"])
            if not v or it["price"] < args.min_price:
                continue
            profit = it["price"] * keep - v
            if profit > 0 and it["price"] / v >= 1 + args.min_margin / 100.0:
                flips.append((it, v, profit))
        flips.sort(key=lambda r: -(r[2] * (r[0]["qty"] or 1)))
        print(f"    vendor stock is unlimited, so the cap is demand: "
              f"'listed' is how many are already up for sale.")
        print(f"    net of a {args.ah_cut:.0f}% auction cut.")
        print(table(
            [[label(it), gold(v), gold(it["price"]), gold(p),
              f"{it['price']/v:.1f}x", int(it["qty"]) if it["qty"] else "-"]
             for it, v, p in flips[:top]],
            ["item", "vendor cost", "AH price", "net/unit", "mult", "listed"],
        ))

        print(f"\n  Cheaper on the AH than at a vendor (buy here, don't shop):")
        cheap = [(it, vendor[it["id"]]) for it in priced
                 if vendor.get(it["id"]) and it["price"] < vendor[it["id"]]
                 and it["price"] >= args.min_price]
        cheap.sort(key=lambda r: -(r[1] - r[0]["price"]))
        print(table(
            [[label(it), gold(v), gold(it["price"]), gold(v - it["price"]),
              int(it["qty"]) if it["qty"] else "-"] for it, v in cheap[:top]],
            ["item", "vendor cost", "AH price", "you save", "listed"],
        ))
        print(f"\n    vendor prices known for {len(vendor):,} items "
              f"(grows as you visit merchants)")

    # --- movers between snapshots --------------------------------------- #
    print(f"\n{'-'*W}\n-- Movers since previous snapshot\n{'-'*W}")
    if len(snaps) < 2:
        print("    (need a second snapshot -- re-run after your next AH scan)")
    else:
        prev = snaps[-2]
        prev_items = {
            i: normalize(i, r, names)
            for i, r in (prev["realms"].get(realm) or {}).items()
        }
        moves = []
        for it in priced:
            p = prev_items.get(it["id"], {}).get("price")
            if not p:
                continue
            d = (it["price"] - p) / p * 100
            if abs(d) >= args.min_move:
                moves.append((it, p, d))
        moves.sort(key=lambda r: -abs(r[2]))
        print(f"    {prev['source_mtime'][:16].replace('T',' ')}  ->  {scan_ts}"
              f"   (moves >= {args.min_move:.0f}%)")
        print(table(
            [[label(it), gold(p), gold(it["price"]), pct(d),
              int(it["qty"]) if it["qty"] else "-"]
             for it, p, d in moves[:top]],
            ["item", "was", "now", "change", "qty"],
        ))
        new = [it for it in priced if it["id"] not in prev_items]
        gone = [i for i in prev_items if i not in by_id]
        print(f"\n    appeared: {len(new):,}   vanished: {len(gone):,}")

    # --- vs own daily history ------------------------------------------- #
    multi = [it for it in priced if it["days"] >= args.min_hist]
    print(f"\n{'-'*W}\n-- Versus their own price history\n{'-'*W}")
    if not multi:
        maxdays = max((it["days"] for it in priced), default=0)
        print(f"    Auctionator has only {maxdays} day(s) of history so far "
              f"(needs {args.min_hist}).")
        print(f"    It keeps up to "
              f"{args.hist_days} days; scan daily and this section fills in.")
    else:
        def dev(it):
            med = statistics.median([p for _, p in it["history"]])
            return (it["price"] - med) / med * 100 if med else 0.0
        lows = sorted([it for it in multi if dev(it) <= -args.discount], key=dev)
        highs = sorted([it for it in multi if dev(it) >= args.discount],
                       key=dev, reverse=True)
        print(f"  Buy candidates (>= {args.discount:.0f}% under own median):")
        print(table(
            [[label(it), gold(it["price"]),
              gold(statistics.median([p for _, p in it["history"]])),
              pct(dev(it)), it["days"]] for it in lows[:top]],
            ["item", "now", "median", "vs median", "days"],
        ))
        print(f"\n  Sell candidates (>= {args.discount:.0f}% over own median):")
        print(table(
            [[label(it), gold(it["price"]),
              gold(statistics.median([p for _, p in it["history"]])),
              pct(dev(it)), it["days"]] for it in highs[:top]],
            ["item", "now", "median", "vs median", "days"],
        ))

    # --- your postings --------------------------------------------------- #
    print(f"\n{'-'*W}\n-- Your postings vs the market\n{'-'*W}")
    posts = cur.get("postings") or []
    if not posts:
        print("    (no posting history yet -- fills in once you list items)")
    else:
        rows = []
        for p in posts[:top]:
            it = by_id.get(p["id"])
            mkt = it["price"] if it else None
            d = ((p["price"] - mkt) / mkt * 100) if mkt else None
            when = (dt.datetime.fromtimestamp(p["time"]).strftime("%m-%d %H:%M")
                    if p.get("time") else "-")
            nm = label(it) if it else (names.get(int(p["id"]), f"#{p['id']}")
                                       if p["id"].isdigit() else p["id"])
            rows.append([nm, when, int(p["quantity"] or 0),
                         gold(p["price"]), gold(mkt), pct(d)])
        print(table(rows, ["item", "when", "qty", "your price", "market", "vs market"]))

    # --- order book ------------------------------------------------------ #
    depth = {int(k): v for k, v in (cur.get("depth") or {}).items()
             if str(k).isdigit()}
    ladders = {i: d for i, d in depth.items() if not d.get("agg")
               and len(d.get("ladder") or []) > 1}
    print(f"\n{'-'*W}\n-- Order book\n{'-'*W}")
    if not depth:
        print("    (no depth captured yet -- open the AH and run a full scan)")
    else:
        print(f"    {len(depth):,} items seen, {len(ladders):,} with a full "
              f"price ladder (the rest are scan aggregates)")
        if ladders:
            rows = []
            for i, d in ladders.items():
                lad = d["ladder"]
                gap = (lad[1][0] - lad[0][0]) / lad[0][0] * 100 if lad[0][0] else 0
                nm = (meta_items.get(i) or {}).get("n") or f"#{i}"
                rows.append([nm[:30], gold(lad[0][0]), gold(lad[1][0]),
                             pct(gap), int(lad[0][1]), len(lad)])
            rows.sort(key=lambda r: -float(r[3].rstrip("%")))
            print("\n  Thinnest front of book (cheapest vs next -- snipe targets):")
            print(table(rows[:top],
                        ["item", "cheapest", "next up", "gap", "qty", "rungs"]))

    print(f"\n{'='*W}")
    print("Next: full-scan the AH in game, /reload, then  python ah.py refresh")


def addon_dir(cfg: dict) -> str:
    return cfg.get("addon_dir") or os.path.join(
        cfg["wow_dir"], cfg["flavor"], "Interface", "AddOns", "AHEcon")


def cmd_sync_ids(cfg, args):
    """Write the item IDs found in auction data where the addon can read them."""
    snaps = load_snapshots()
    ids = set()
    if snaps:
        cur = snaps[-1]
        for items in cur["realms"].values():
            ids.update(int(k) for k in items if str(k).isdigit())
        ids.update(int(k) for k in (cur.get("vendor") or {}) if str(k).isdigit())
        ids.update(int(p["id"]) for p in (cur.get("postings") or [])
                   if str(p["id"]).isdigit())
    else:
        data, _ = read_auctionator(cfg)
        for items in price_tables(data.get("AUCTIONATOR_PRICE_DATABASE")).values():
            ids.update(int(k) for k in items if str(k).isdigit())
    if not ids:
        raise SystemExit("No item IDs found -- snapshot a scan first.")

    d = addon_dir(cfg)
    if not os.path.isdir(d):
        raise SystemExit(f"Addon folder not found: {d}")
    out = os.path.join(d, "ItemIDs.lua")
    ordered = sorted(ids)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("-- Generated by ah.py -- item IDs seen in auction data.\n")
        fh.write("-- Regenerate with: python ah.py sync-ids\n")
        fh.write("AHECON_WANTED = {\n")
        for i in range(0, len(ordered), 15):
            fh.write("\t" + ",".join(str(x) for x in ordered[i:i + 15]) + ",\n")
        fh.write("}\n")
    print(f"Wrote {len(ordered):,} item IDs -> {out}")
    print("Log in (or /reload) and the addon will resolve them.")


def cmd_refresh(cfg, args):
    do_snapshot(cfg)
    cmd_sync_ids(cfg, args)
    print()
    cmd_report(cfg, args)


def cmd_export(cfg, args):
    snaps = load_snapshots()
    if not snaps:
        raise SystemExit("No snapshots yet -- run: python ah.py snapshot")
    cur = snaps[-1]
    realm, items = pick_realm(cur, cfg, args)
    names = {int(k): v for k, v in (cur.get("names") or {}).items() if k.isdigit()}
    vendor = cur.get("vendor") or {}
    out = args.out or os.path.join(DATA, f"ah-{realm}-{cur['source_mtime'][:10]}.csv")
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("item_id,name,unit_price_copper,unit_price_gold,quantity,"
                 "listed_value_copper,min_ever_copper,days_of_history,"
                 "median_copper,vendor_sell_copper,last_seen\n")
        for iid, rec in sorted(items.items(), key=lambda kv: int(kv[0])
                               if kv[0].isdigit() else 0):
            it = normalize(iid, rec, names)
            if it["price"] is None:
                continue
            h = [p for _, p in it["history"]]
            med = f"{statistics.median(h):.0f}" if h else ""
            qty = f"{it['qty']:.0f}" if it["qty"] else ""
            lv = f"{it['price'] * it['qty']:.0f}" if it["qty"] else ""
            last = day_to_date(it["last_day"]).isoformat() if it["last_day"] else ""
            name = (it["name"] or "").replace('"', "'")
            fh.write(f'{iid},"{name}",{it["price"]:.0f},{it["price"]/10000:.4f},'
                     f'{qty},{lv},{it["min_seen"] or ""},{it["days"]},{med},'
                     f'{vendor.get(iid, "")},{last}\n')
    print(f"Wrote {out}")


def cmd_publish(cfg, args):
    """Write the latest scan into the shared repo and push it."""
    import subprocess

    import pack

    repo = cfg.get("repo_dir")
    if not repo or not os.path.isdir(os.path.join(repo, ".git")):
        raise SystemExit(
            "No shared repo configured. Set \"repo_dir\" in config.json to a\n"
            "clone of the scan repo, then re-run."
        )
    snaps = load_snapshots()
    if not snaps:
        raise SystemExit("No snapshots yet -- run: python ah.py snapshot")
    cur = snaps[-1]
    realm, _ = pick_realm(cur, cfg, args)
    who = cfg.get("uploader") or os.environ.get("USERNAME") or "me"
    # the same columnar format the web terminal reads, so a published file
    # can be dropped straight onto the page
    digest = pack.pack(cur, realm, who)
    digest["count"] = digest["n"]

    stamp = cur["source_mtime"].replace(":", "").replace("-", "")[:15]
    rel = os.path.join("data", realm.replace(" ", "_"), f"{who}-{stamp}.json")
    dest = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(digest, fh, separators=(",", ":"))

    # a rolling index the web UI can fetch without listing the repo
    idx_path = os.path.join(repo, "data", "index.json")
    index = []
    if os.path.exists(idx_path):
        with open(idx_path, encoding="utf-8") as fh:
            index = json.load(fh)
    index = [e for e in index if e.get("path") != rel.replace(os.sep, "/")]
    index.append({
        "path": rel.replace(os.sep, "/"), "realm": realm,
        "scan": digest["scan"], "uploader": who, "count": digest["count"],
    })
    index.sort(key=lambda e: e["scan"], reverse=True)
    with open(idx_path, "w", encoding="utf-8") as fh:
        json.dump(index[:500], fh, indent=1)

    size = os.path.getsize(dest) / 1024
    print(f"Wrote {rel} ({size:,.0f} KB, {digest['count']:,} items)")
    for cmd in (["add", "-A"],
                ["commit", "-m", f"scan: {realm} {digest['scan'][:16]} ({who})"],
                ["push"]):
        r = subprocess.run(["git", "-C", repo] + cmd,
                           capture_output=True, text=True)
        if r.returncode and "nothing to commit" not in (r.stdout + r.stderr):
            print(f"  git {cmd[0]} failed: {(r.stderr or r.stdout).strip()[:200]}")
            return
    print("Pushed.")


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--realm", help="realm price table to analyse")
    common.add_argument("--top", type=int, default=15, help="rows per section")
    common.add_argument("--min-move", type=float, default=10.0,
                        help="min %% change to count as a mover")
    common.add_argument("--discount", type=float, default=25.0,
                        help="min %% off median for buy/sell candidates")
    common.add_argument("--min-hist", type=int, default=3,
                        help="min days of history before using a median")
    common.add_argument("--hist-days", type=int, default=21,
                        help="days of history Auctionator is configured to keep")
    common.add_argument("--thin", type=int, default=2,
                        help="max quantity for a 'thin market'")
    common.add_argument("--thin-floor", type=float, default=10000,
                        help="min unit price (copper) for a 'thin market'")
    common.add_argument("--ah-cut", type=float, default=5.0,
                        help="auction house cut %% deducted from a sale")
    common.add_argument("--min-margin", type=float, default=15.0,
                        help="min %% markup over vendor cost for a flip")
    common.add_argument("--min-price", type=float, default=100,
                        help="ignore items under this unit price (copper)")
    common.add_argument("--depth", type=int, default=4, help="discover: tree depth")
    common.add_argument("--out", help="export: csv path")
    # pool
    common.add_argument("--project-ref", help="pool-init: Supabase project ref")
    common.add_argument("--org-id", help="pool-init: Supabase organization")
    common.add_argument("--name", help="pool-init: project name")
    common.add_argument("--region", help="pool-init: project region")
    common.add_argument("--size", help="pool-init: instance size (paid plans only)")
    common.add_argument("--create", action="store_true",
                        help="pool-init: create the project if it is missing")
    common.add_argument("--url", help="pool-join: https://<ref>.supabase.co")
    common.add_argument("--key", help="pool-join: the publishable key")
    common.add_argument("--bucket", help="pool-join: storage bucket")
    common.add_argument("--uploader", help="the name your scans are filed under")
    common.add_argument("--days", type=int, default=3,
                        help="push: days of price history to send")
    common.add_argument("--full", action="store_true",
                        help="push: send every day of history, not just --days")
    common.add_argument("--all", action="store_true",
                        help="pull: every scan, not just the newest per person")
    common.add_argument("--limit", type=int, default=25,
                        help="pull/pool: how many scans to consider")
    common.add_argument("--interval", type=int, default=30,
                        help="watch: seconds between checks")
    common.add_argument("--once", action="store_true",
                        help="watch: check once and exit (for Task Scheduler)")
    common.add_argument("--port", type=int, default=8731, help="ui: local port")
    common.add_argument("--no-browser", action="store_true",
                        help="ui: do not open a browser")

    ap = argparse.ArgumentParser(
        description=__doc__, parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    ap.set_defaults(invite=None)
    made = {}
    for name in ("status", "discover", "snapshot", "report", "refresh",
                 "export", "sync-ids", "publish",
                 "pool", "pool-init", "pool-join", "push", "pull", "watch",
                 "ui"):
        made[name] = sub.add_parser(name, parents=[common])
    made["pool-join"].add_argument(
        "invite", nargs="?", help="the invite line you were sent")
    args = ap.parse_args(argv)

    os.makedirs(DATA, exist_ok=True)
    cfg = load_config()
    if not os.path.exists(CONFIG):
        with open(CONFIG, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)

    cmd = args.cmd or "status"
    local = {
        "status": cmd_status,
        "discover": cmd_discover,
        "snapshot": lambda c, a: do_snapshot(c),
        "report": cmd_report,
        "refresh": cmd_refresh,
        "export": cmd_export,
        "sync-ids": cmd_sync_ids,
        "publish": cmd_publish,
    }
    if cmd in local:
        local[cmd](cfg, args)
        return 0

    # pool commands get this module so they can reuse the reader
    import poolcli
    import pool as _pool
    try:
        {
            "pool": poolcli.cmd_pool,
            "pool-init": poolcli.cmd_pool_init,
            "pool-join": poolcli.cmd_pool_join,
            "push": poolcli.cmd_push,
            "pull": poolcli.cmd_pull,
            "watch": poolcli.cmd_watch,
            "ui": poolcli.cmd_ui,
        }[cmd](sys.modules[__name__], cfg, args)
    except _pool.PoolError as e:
        raise SystemExit(f"Pool: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
