# Starts the Vite dev server (http://localhost:5173, proxies API/WS to :8090)
Set-Location "$PSScriptRoot\frontend"
if (-not (Test-Path "node_modules")) { npm install }
npm run dev
