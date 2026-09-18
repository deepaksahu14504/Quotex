# Starts the FastAPI backend (serves API + built frontend on http://127.0.0.1:8090)
Set-Location "$PSScriptRoot\backend"
if (-not (Test-Path ".venv")) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -q -r requirements.txt
if (-not (Test-Path ".env")) { Copy-Item .env.example .env }
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8090
