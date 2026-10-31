$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
python -m venv .venv
if ($LASTEXITCODE -ne 0) { throw "Python 3.12 or 3.13 is required." }
& ".venv\Scripts\python.exe" -m pip install -r requirements.lock
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
& ".venv\Scripts\python.exe" -m pip install --no-deps -e .
if ($LASTEXITCODE -ne 0) { throw "Package installation failed." }
Write-Host "Synthetic wallet + liquidity dashboard: http://127.0.0.1:8341"
& ".venv\Scripts\python.exe" scripts\demo_wallet_desk.py

