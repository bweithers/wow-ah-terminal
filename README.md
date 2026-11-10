# WoW Forever auction house economy workflow

Reads the auction data Auctionator saves on your machine, archives a snapshot
every time you scan, and reports on the market.

## The loop

1. **In game**: open the auction house, run an Auctionator full scan.
2. **In game**: `/reload` (or log out). WoW only writes SavedVariables then —
   without this step the file on disk is from your *last* session.
3. **Here**: `python ah.py refresh`

Do that once a day and the history-based sections (movers, buy/sell vs own
median) fill in. Auctionator is configured to keep 21 days of daily prices;
the snapshot archive in `data/snapshots/` keeps everything forever, so
long-run trends survive even after Auctionator rolls its window.

## Commands

| command | what it does |
| --- | --- |
| `python ah.py status` | where the data is, how fresh, how many snapshots |
| `python ah.py snapshot` | archive the current scan (skips if already archived) |
| `python ah.py report` | analyse the latest snapshot |
| `python ah.py refresh` | snapshot, then report |
| `python ah.py export` | CSV of the latest snapshot for a spreadsheet |
| `python ah.py discover` | dump the raw shape of the saved data |

Useful flags: `--top 30` (more rows), `--min-move 5` (more sensitive movers),
`--discount 15` (looser buy/sell thresholds), `--thin 5 --thin-floor 50000`
(thin-market definition), `--realm PvE`.

## What the report shows

- **Biggest markets by total listed value** — where the gold actually is.
- **Highest unit prices** — the luxury end.
- **Deepest supply** — oversupplied goods; cheap to corner, slow to sell.
- **Thin markets** — few listings at a high price; easy to corner, but
  thin supply often means thin *demand*, so check before buying.
- **Vendor → AH flips** — items a merchant sells for less than the AH price.
  Vendor stock is unlimited, so the only limit is demand. Net of the auction
  cut (`--ah-cut`, default 5%).
- **Cheaper on the AH than at a vendor** — the other direction, which is a
  shopping tip, not profit.
- **Movers** — price changes between consecutive snapshots.
- **Versus own price history** — items trading well under/over their median.
- **Your postings vs the market** — whether you undercut or overpriced.

## Files

| file | role |
| --- | --- |
| `ah.py` | the CLI |
| `luasv.py` | parser for WoW SavedVariables (a restricted Lua subset) |
| `cbor.py` | CBOR decoder (RFC 8949) for the packed price database |
| `config.json` | WoW path, flavor, account, realm |
| `data/snapshots/*.json` | one archived scan each, lossless |
| `data/series.jsonl` | flat (time, item, price, qty) rows for trend work |

## Item names

Auctionator stores item **IDs**, not names, so reports show `#4234` until
names are available. A small companion addon fills that gap:

    D:\World of Warcraft\_forever_\Interface\AddOns\AHEcon\

It listens for auction house result events, asks the client for the name of
each item ID it sees, and saves the mapping to `AHEcon.lua`. It is additive
and read-only — it does not touch Auctionator or change anything in game.
`/ahecon` prints how many names are cached; `/ahecon clear` resets it.

To activate: log in, make sure **AH Econ Export** is checked in the AddOns
list, full-scan the AH, `/reload`, then `python ah.py refresh`. Delete the
`AHEcon` folder to remove it.

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

`AUCTIONATOR_VENDOR_PRICE_CACHE`, `AUCTIONATOR_POSTING_HISTORY` and the other
globals are plain Lua tables keyed by item ID.

`AUCTIONATOR_VENDOR_PRICE_CACHE` holds **what a merchant charges you per
unit**, not what a vendor pays you. Six entries give it away: ammo at
`0.05`/`0.25` copper and bread at `4.6`/`22.6` copper are stack prices
divided by stack size (200 arrows for 10c, 5 loaves for 23c). So the
profitable comparison is vendor cost → AH price, not the reverse.

The CBOR format is decoded from the public IETF spec (RFC 8949); the schema
above was derived from the saved data itself. No addon source code is read or
used as reference — the Auctionator folder ships an `AGENTS.md` asking that
its repository not be used that way, and this tooling respects it.
