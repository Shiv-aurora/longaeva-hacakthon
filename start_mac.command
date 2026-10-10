#!/bin/bash
# One-click local launcher for macOS. Requires installed python3 and network on first install.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo 'Creating the isolated Python environment...'
  python3 -m venv .venv
fi
source .venv/bin/activate
python -m pip install -r requirements.txt
(sleep 2 && open 'http://127.0.0.1:8000') &
exec python -m uvicorn app.server:app --host 127.0.0.1 --port 8000
