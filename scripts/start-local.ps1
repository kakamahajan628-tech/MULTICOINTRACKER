param([string]$PythonPath = ".venv\Scripts\python.exe", [int]$Port = 8002)
$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
Push-Location -LiteralPath $projectRoot
try {
    if (-not (Test-Path -LiteralPath ".env.whale")) { throw "Create .env.whale first; see LOCAL-LIVE.md" }
    $pythonExe = if (Test-Path -LiteralPath $PythonPath) { (Resolve-Path -LiteralPath $PythonPath).Path } else { (Get-Command $PythonPath -ErrorAction Stop).Source }
    Write-Output "Starting private dashboard at http://127.0.0.1:$Port. Login user: whale; password is WHALE_ADMIN_TOKEN in .env.whale. Keep this terminal open; Ctrl+C stops the server."
    & $pythonExe -m uvicorn whale_watcher.app:app --host 127.0.0.1 --port $Port --env-file .env.whale --workers 1
    if ($LASTEXITCODE -ne 0) { throw "Server exited with code $LASTEXITCODE" }
} finally { Pop-Location }
