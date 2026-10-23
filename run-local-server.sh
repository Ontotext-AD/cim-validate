#!/usr/bin/env bash
#From the repo root (after python3 build_dashboard_gdb.py if pages are missing):
#Start server

python3 -m http.server 8765 --bind 127.0.0.1

#Then open http://127.0.0.1:8765/index.html

#Stop In that terminal: Ctrl+C

#Or from another shell:

#fuser -k 8765/tcp