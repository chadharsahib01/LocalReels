#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

export LOCALREELS_OLD_ROOT="${LOCALREELS_OLD_ROOT:-/mnt/c/LocalReels}"
export LOCALREELS_MEDIA_DIR="${LOCALREELS_MEDIA_DIR:-/mnt/c/LocalReels/media}"
export LOCALREELS_AUTH="${LOCALREELS_AUTH:-nayi_app_secret_123}"
export LOCALREELS_PORT="${LOCALREELS_PORT:-8000}"

mkdir -p "$LOCALREELS_MEDIA_DIR"
PY=python3; [ -x "venv/bin/python" ] && PY="venv/bin/python"
"$PY" -m pip install -q -r requirements.txt

IP=$("$PY" -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('8.8.8.8',80));print(s.getsockname()[0]);s.close()" 2>/dev/null || echo 127.0.0.1)
echo "  PC    : http://127.0.0.1:$LOCALREELS_PORT"
echo "  Phone : http://$IP:$LOCALREELS_PORT"

exec "$PY" -m uvicorn backend.server:app --host 0.0.0.0 --port "$LOCALREELS_PORT" --reload --reload-dir backend