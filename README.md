# WoW Forever auction house economy

Reads the auction data Auctionator saves on disk, archives a snapshot every
time you scan, reports on the market, and feeds a trading-terminal web UI you
and your friends can pool scans into.

- **Terminal UI**: https://claude.ai/artifact/8UTonQ7uWrxwEEvoTf9g4x
- **Companion addon**: `addon/` (copy to `Interface\AddOns\AHEcon\`)

## The loop

1. **In game**: open the auction house, run an Auctionator full scan.
2. **In game**: `/reload` (or log out). WoW only writes SavedVariables then —
   without this the file on disk is still from your *last* session.
3. **On the desktop**: `python ah.py refresh`

Do that once a day and the history sections fill in. Auctionator keeps 21 days
of daily prices; the snapshot archive in `data/snapshots/` keeps everything
forever, so long-run trends survive after Auctionator rolls its window.

## Commands

| command | what it does |
| --- | --- |
| `python ah.py status` | where the data is, how fresh, how many snapshots |
| `python ah.py snapshot` | archive the current scan (skips one already archived) |
| `python ah.py report` | analyse the latest snapshot |
| `python ah.py refresh` | snapshot, sync item ids, then report |
| `python ah.py export` | CSV of the latest snapshot for a spreadsheet |
| `python ah.py sync-ids` | write `ItemIDs.lua` for the addon to resolve |
| `python ah.py publish` | push a scan into a shared git repo (needs `repo_dir`) |
| `python ah.py discover` | dump the raw shape of the saved data |
| `python ah.py ui` | serve the terminal locally, where it can reach the pool |
| `python ah.py pool` | what is in the shared pool, and your invite line |
| `python ah.py pool-init` | create the Supabase pool (owner, once) |
| `python ah.py pool-join` | join someone else's pool from their invite |
| `python ah.py push` | send your latest scan to the pool |
| `python ah.py pull` | download the pool's scans into `data/pool/` |
| `python ah.py watch` | upload every scan from now on, automatically |

Useful flags: `--top 30`, `--min-move 5`, `--discount 15`, `--ah-cut 5`,
`--min-margin 15`, `--thin 5 --thin-floor 50000`, `--realm PvE`.

## What the report shows

- **Biggest markets by total listed value** — where the gold actually is.
- **Highest unit prices** — the luxury end.
- **Deepest supply** — oversupplied goods, cheap to corner and slow to sell.
- **Thin markets** — few listings at a high price. Thin supply often means
  thin demand, so check before buying.
- **Vendor sell-back** — listed below what a vendor pays. Risk-free.
- **Vendor → AH flips** — a merchant sells it for less than the AH price.
  Vendor stock is unlimited, so the limit is demand. Net of the auction cut.
- **Cheaper on the AH than at a vendor** — the other direction: a shopping
  tip, not profit.
- **Movers** — price changes between consecutive snapshots.
- **Versus own price history** — trading well under or over their median.
- **Your postings vs the market** — whether you undercut or overpriced.

## The terminal UI

Screens are the number keys `1`–`6` (browsers swallow the F-keys): Market, Movers, Arbitrage, Vendor Flips,
Depth, Load. `/` searches, `j`/`k` or arrows move, `W` watchlists an item.

The page carries a baseline scan, so it opens with real data. Key `6` loads more:
drop `Auctionator.lua` on it and the Lua tables and the CBOR-packed price
database are decoded **in the browser** — no install, nothing uploaded. Drop
`AHEcon.lua` alongside and items get names and vendor sell prices instead of
bare ids.

### Pooling scans with friends

Scans pool through a Supabase project on the free tier. One person creates it;
everyone else joins with a single line.

**The owner, once:**

```
npx supabase login            # opens a browser, approve it there
python ah.py pool-init --create
```

That creates the project, applies `schema.sql`, reads back the project's
*publishable* key, stores it in `config.json`, proves the pool answers, and
prints an invite line to send round.

**Everyone else, once:**

```
python ah.py pool-join <the invite line you were sent>
```

**Everyone, from then on:** leave `watch.cmd` running (or `./watch.sh`, or
`python ah.py watch`). It notices the moment WoW rewrites its SavedVariables
-- which is the moment you `/reload` after a scan -- archives the scan, and
uploads it. Nothing to remember after a session.

For a machine you would rather not leave a window open on, `python ah.py
watch --once` checks and exits, so Task Scheduler or cron can run it every
few minutes.

Then `python ah.py ui` opens the terminal with the pool behind it: everyone's
scans in the list, and price history that reaches past the 21 days
Auctionator itself keeps.

### Who can do what

Everyone shares the project's publishable key, the kind Supabase means to be
embedded in client code. What actually holds is the row-level security in
`schema.sql`:

- anyone with the key can **read** every scan and every price,
- anyone with the key can **append** a scan,
- **nobody** can update or delete anything, through that key.

So the worst a leaked invite costs you is a junk scan in the list, never
somebody's history. Writes to the price table go through one function that
keeps the lowest price seen for each item and day, which is the same rule
Auctionator applies to its own records, so two people scanning the same realm
refine each other's data instead of fighting over it.

Treat the project as holding auction prices and nothing else: the invite is a
read-and-append credential for whoever has it.

### Three ways to open the terminal

| where | pool | key |
| --- | --- | --- |
| the artifact link above | the page's own store | none needed |
| `python ah.py ui` | Supabase | read from `web/pool.js` |
| a public copy, e.g. GitHub Pages | Supabase | pasted once, per browser |

The artifact is the no-install view: it opens on a baseline scan, reads files
you drop on it, and pools through its own store for people who can open the
page. It cannot reach Supabase -- a hosted artifact may only talk to its own
origin -- so it does not try.

`python ah.py ui` serves the same file from your own machine, where it reads
the Supabase pool directly.

A copy served from a public URL **ships no key**. It asks for the invite once
and keeps it in that browser's local storage, so the pool stays invite-gated
even though the page is public. Pasting the whole `python ah.py pool-join …`
line works, and so does a link ending `#invite=<token>` -- a fragment is
never sent to the web server, so it reaches only the person who opens it. The
Load screen has a **Forget pool** button to undo it.

Supabase holds the data and nothing else. Its Storage serves anything
HTML-ish as `text/plain` whatever content type is set, so the page cannot be
hosted there; it would download instead of opening.

### What it costs

Nothing, on the free tier, for a group this size. A packed scan is about
300 KB in storage, and the price table grows by day rather than by scan: one
row per item per day, however many of you upload. At 4,500 items that is a
couple of MB a day against a 500 MB database and 1 GB of storage.

## Files

| file | role |
| --- | --- |
| `ah.py` | the CLI |
| `luasv.py` | parser for WoW SavedVariables (a restricted Lua subset) |
| `cbor.py` | CBOR decoder (RFC 8949) for the packed price database |
| `pack.py` | columnar packing for the web UI and the shared store |
| `pool.py` | Supabase REST and Storage client, standard library only |
| `poolcli.py` | the pool commands, and the Supabase CLI provisioning |
| `schema.sql` | the pool's tables, policies and buckets |
| `watch.cmd`, `watch.sh` | double-click uploader for everyone else |
| `tests/` | the pool tested against a stand-in Supabase |
| `web/terminal.html` | the terminal UI source |
| `addon/` | the AHEcon companion addon |
| `config.json` | WoW path, flavor, realm, uploader, pool address (not in git) |
| `web/pool.js` | the pool address the local page reads (not in git) |
| `data/snapshots/*.json` | one archived scan each, lossless |
| `data/series.jsonl` | flat (time, item, price, qty) rows for trend work |

## The companion addon

Auctionator stores item **ids**, not names, and no vendor sell prices. AHEcon
fills both gaps. Copy `addon/` to
`World of Warcraft\_forever_\Interface\AddOns\AHEcon\`, then:

1. `python ah.py sync-ids` writes `ItemIDs.lua` with every item id seen in
   auction data.
2. Log in (or `/reload`). On login the addon resolves each id to a name,
   vendor sell price, stack size, quality and item level, asking the server for
   anything not cached. ~4,500 items take about 20 seconds.
3. While the auction house is open it also records order-book depth: the full
   price ladder for any item whose search results come past, and an aggregate
   row per item from each full scan.

`/ahecon` reports progress, `/ahecon scan` forces another pass, `/ahecon clear`
resets. Delete the folder to remove it. It is additive and read-only: it does
not touch Auctionator and never buys, sells, posts or cancels.

## How the data is read

`AUCTIONATOR_PRICE_DATABASE` (dbversion 8) holds one CBOR-packed blob per
realm. Decoded, each record looks like:

```
"15115": {"m": 600, "h": {"2504": 600}, "a": {"2504": 11}, "l": []}
```

- `m` — minimum price seen, in copper
- `h` — day number → price that day
- `a` — day number → quantity available that day
- `l` — a list, empty in the data seen so far
- day numbers count from 2020-01-01 (day 2504 = 2026-11-09)

`AUCTIONATOR_VENDOR_PRICE_CACHE` holds **what a merchant charges you per
unit**, not what a vendor pays you. Six entries give it away: ammo at `0.05`
and `0.25` copper, bread at `4.6` and `22.6`, are stack prices divided by stack
size (200 arrows for 10c, 5 loaves for 23c). Against the game's own sell prices
the cache runs about 4× higher — the standard vendor markup. So the profitable
comparison is vendor cost → AH price, and separately AH price → the sell price
AHEcon records.

The CBOR format is decoded from the public IETF spec (RFC 8949); the schema
above was derived from the saved data itself. No addon source code is read or
used as reference — the Auctionator folder ships an `AGENTS.md` asking that its
repository not be used that way, and this tooling respects it.

The browser parser in `web/terminal.html` is checked against the Python reader:
every column (price, quantity, min seen, sell price, stack, quality, item
level, vendor cost, name) and the whole price history match across all 4,567
items in the current scan.
