"""Columnar packing for scan data.

One scan as parallel arrays instead of a list of objects: no repeated keys,
integers instead of floats, and sparse side tables for history and depth.
Small enough to fit a shared-store document, and quick to load in a browser.
"""

from __future__ import annotations

import json

SCHEMA = 1


def _int(v):
    if v is None:
        return None
    f = float(v)
    return int(f) if f.is_integer() else round(f, 2)


def pack(snap: dict, realm: str, uploader: str) -> dict:
    """Pack one snapshot's realm table into columnar form."""
    items = snap["realms"][realm]
    meta = {int(k): v for k, v in (snap.get("items") or {}).items()
            if str(k).isdigit()}
    depth = {int(k): v for k, v in (snap.get("depth") or {}).items()
             if str(k).isdigit()}
    vendor = snap.get("vendor") or {}
    names = {i: r.get("n") for i, r in meta.items()}

    import ah  # normalize() lives with the reader

    rows = []
    for iid, rec in items.items():
        it = ah.normalize(iid, rec, names)
        if it["price"] is None:
            continue
        rows.append((int(iid) if str(iid).isdigit() else iid, it))
    rows.sort(key=lambda r: r[0] if isinstance(r[0], int) else 0)

    ids, nm, price, qty, minseen = [], [], [], [], []
    sell, stack, qual, ilvl, vbuy = [], [], [], [], []
    hist: dict[str, list] = {}
    ladders: dict[str, list] = {}

    for idx, (iid, it) in enumerate(rows):
        m = meta.get(iid) if isinstance(iid, int) else None
        ids.append(iid)
        nm.append(it["name"] or "")
        price.append(_int(it["price"]))
        qty.append(_int(it["qty"]))
        minseen.append(_int(it["min_seen"]))
        sell.append(_int((m or {}).get("s")))
        stack.append(_int((m or {}).get("st")))
        qual.append(_int((m or {}).get("q")))
        ilvl.append(_int((m or {}).get("il")))
        vbuy.append(_int(vendor.get(str(iid))))
        for day, p in it["history"]:
            hist.setdefault(str(int(day)), []).append([idx, _int(p)])
        lad = (depth.get(iid) or {}).get("ladder") if isinstance(iid, int) else None
        if lad:
            ladders[str(idx)] = [[_int(u), _int(q)] for u, q in lad]

    def drop_if_empty(col):
        return None if all(v is None for v in col) else col

    return {
        "schema": SCHEMA,
        "realm": realm,
        "scan": snap["source_mtime"],
        "captured": snap.get("captured"),
        "uploader": uploader,
        "n": len(ids),
        "id": ids,
        "nm": nm,
        "p": price,
        "q": qty,
        "ms": minseen,
        "s": drop_if_empty(sell),
        "st": drop_if_empty(stack),
        "ql": drop_if_empty(qual),
        "il": drop_if_empty(ilvl),
        "vb": drop_if_empty(vbuy),
        "hist": hist,
        "ld": ladders or None,
        "posts": [
            [int(p["id"]) if str(p["id"]).isdigit() else p["id"],
             _int(p["price"]), _int(p.get("quantity")), p.get("time")]
            for p in (snap.get("postings") or [])
        ],
    }


def chunks(packed: dict, limit: int = 200_000) -> list[dict]:
    """Split a packed scan into documents that each fit the store's limit."""
    body = json.dumps(packed, separators=(",", ":"))
    if len(body.encode()) <= limit:
        return [packed]
    cols = ["id", "nm", "p", "q", "ms", "s", "st", "ql", "il", "vb"]
    total = packed["n"]
    parts = max(2, (len(body.encode()) // limit) + 1)
    step = (total + parts - 1) // parts
    out = []
    for pi in range(parts):
        lo, hi = pi * step, min(total, (pi + 1) * step)
        if lo >= hi:
            break
        part = {k: v for k, v in packed.items()
                if k not in cols + ["hist", "ld", "posts"]}
        part.update({"part": pi, "parts": parts, "lo": lo, "n": hi - lo})
        for c in cols:
            col = packed.get(c)
            part[c] = col[lo:hi] if col else None
        part["hist"] = {
            d: [[i - lo, p] for i, p in v if lo <= i < hi]
            for d, v in (packed.get("hist") or {}).items()
        }
        part["hist"] = {d: v for d, v in part["hist"].items() if v}
        part["ld"] = {
            str(int(i) - lo): v for i, v in (packed.get("ld") or {}).items()
            if lo <= int(i) < hi
        } or None
        part["posts"] = packed["posts"] if pi == 0 else []
        out.append(part)
    return out
