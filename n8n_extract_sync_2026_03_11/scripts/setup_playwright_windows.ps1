$ErrorActionPreference = "Stop"

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
  throw "Node.js is required. Install Node 20+ first."
}

if (-not (Test-Path "package.json")) {
  npm init -y | Out-Null
}

npm install --save-dev playwright
npx playwright install chromium

Write-Host "Windows setup complete. Run smoke test with: npm run pw:test:diff"
