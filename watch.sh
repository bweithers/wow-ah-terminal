#!/bin/sh
# Leave this running: every auction scan you /reload after goes to the pool.
cd "$(dirname "$0")" || exit 1
exec python3 ah.py watch "$@"
