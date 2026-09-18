#!/usr/bin/env bash
# Starts the Vite dev server (http://localhost:5173, proxies API/WS to :8090)
# Works on Linux/macOS. Usage:  bash run-dev.sh
set -euo pipefail
cd "$(dirname "$0")/frontend"

if [ ! -d node_modules ]; then
  npm install
fi
exec npm run dev
