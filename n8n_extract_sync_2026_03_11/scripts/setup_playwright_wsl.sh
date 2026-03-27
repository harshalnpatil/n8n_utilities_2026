#!/usr/bin/env bash
set -euo pipefail

if ! command -v node >/dev/null 2>&1; then
  echo "Node.js is required. Install Node 20+ first."
  exit 1
fi

if [ ! -f package.json ]; then
  npm init -y >/dev/null
fi

npm install --save-dev playwright
npx playwright install chromium

if command -v sudo >/dev/null 2>&1; then
  if ! sudo -n true 2>/dev/null; then
    echo "sudo access required for Linux browser deps. Run manually:" >&2
    echo "  sudo npx playwright install-deps chromium" >&2
  else
    sudo npx playwright install-deps chromium
  fi
else
  echo "sudo not found. Install system deps manually if browser launch fails:" >&2
  echo "  npx playwright install-deps chromium" >&2
fi

echo "WSL setup complete. Run smoke test with: npm run pw:test:diff"
