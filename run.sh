#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python -m uvicorn app.server:app --host 127.0.0.1 --port 8000
