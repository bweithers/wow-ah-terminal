# Joining the auction pool

Five minutes, once. You need Python and nothing else — no pip install, no
Supabase account, no API keys of your own.

## 1. Get Python

If `python --version` prints something, skip this.

Otherwise install it from [python.org](https://www.python.org/downloads/) and
tick **Add python.exe to PATH** in the installer. On a Mac, `brew install
python` or the installer from the same place.

## 2. Get this folder

```bash
git clone <the repo url you were sent>
cd wow-ah-terminal
```

No git? Download the ZIP from the repo page and unzip it anywhere.

## 3. Join the pool

Paste the invite line you were sent. It looks like this:

```bash
python ah.py pool-join eyJ1IjoiaHR0cHM6Ly8uLi4ifQ
```

It will find your WoW install, give your uploads a name, check the pool
answers, and list what is already in there. If it guessed the wrong name,
add `--uploader yourname`.

If it cannot find WoW, open `config.json` and set `wow_dir` to your install
folder — the one containing `_forever_`.

## 4. Get the addon (optional, recommended)

Auctionator records item **ids** and no vendor prices. The small companion
addon in `addon/` fills both gaps, so your scans arrive with names attached.

Copy the `addon` folder to:

```
World of Warcraft\_forever_\Interface\AddOns\AHEcon
```

Then `python ah.py sync-ids`, and log in. It resolves about 4,500 items in
20 seconds and then sits quietly. It never buys, sells, posts or cancels.

## 5. Leave the uploader running

Double-click **`watch.cmd`** (Windows) or run **`./watch.sh`** (Mac, Linux).

Leave the window open while you play. Every time you scan the auction house
and then `/reload`, the scan lands in the pool a few seconds later. WoW only
writes its saved data on `/reload` or logout, so that reload is the part that
matters.

Prefer no open window? `python ah.py watch --once` checks and exits, so Task
Scheduler or cron can run it every few minutes instead.

## 6. Look at the data

```bash
python ah.py ui
```

Opens the terminal in your browser with the whole pool behind it. Number keys
`1`–`6` switch screens: Market, Movers, Arbitrage, Vendor Flips, Depth, Load.
`/` searches, `j`/`k` move, `W` watchlists an item.

`python ah.py report` prints the same analysis in the terminal if you prefer
text.

## What goes where

Your scans go into a shared Supabase project: the auction prices, quantities,
your realm, and the uploader name you chose. Your postings history goes too,
since it rides along in the same scan — that is the one thing in there that is
about you rather than the market.

Nothing else on your machine is read. The tool opens exactly one file,
`Auctionator.lua`, parses it without executing it, and uploads a repacked copy.

Everyone in the pool shares one key. It can read everything and add scans; it
cannot change or delete anything, so nobody can lose history, including by
accident.

## If something breaks

```bash
python ah.py status     # where it is looking, and how fresh the data is
python ah.py pool       # what the pool holds, and whether it answers
```

`status` complaining there is no `Auctionator.lua` almost always means you
have not `/reload`-ed since installing Auctionator.
