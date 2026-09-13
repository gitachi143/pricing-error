#!/usr/bin/env bash
# Local dev server. Reads .env automatically.
set -e
cd "$(dirname "$0")"
exec python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
