$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) { throw "Virtual environment missing; run .\setup.ps1 first." }
& $venvPython -m uvicorn shortform_studio.main:app --host 127.0.0.1 --port 8765
if ($LASTEXITCODE -ne 0) { throw "Shortform Studio exited with code $LASTEXITCODE" }
