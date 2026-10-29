# Starts the ParcelDocs service (FastAPI on 127.0.0.1:8765) hidden in the background.
# Run at Windows logon by the "ParcelDocs Service" scheduled task; safe to run by hand.
# Does nothing when the service already answers /health.

$ErrorActionPreference = "Stop"
$service = $PSScriptRoot
$logs = Join-Path (Split-Path $service -Parent) "output\.cache"

try {
    Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 2 | Out-Null
    exit 0
} catch {
    # Not running: start it below.
}

New-Item -ItemType Directory -Force $logs | Out-Null
Start-Process -FilePath (Join-Path $service ".venv\Scripts\python.exe") `
    -ArgumentList "-m", "app.main" `
    -WorkingDirectory $service `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logs "service.out.log") `
    -RedirectStandardError (Join-Path $logs "service.err.log")
