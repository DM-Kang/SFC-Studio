$ErrorActionPreference = "Stop"

$venvPath = Join-Path $PSScriptRoot ".venv"
$pythonCommand = Get-Command py -ErrorAction SilentlyContinue
if ($pythonCommand) {
    py -3 -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw "Python 3.11 or newer is required; install it and rerun setup." }
} else {
    python -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw "Python 3.11 or newer is required; install it and rerun setup." }
}

$venvPython = Join-Path $venvPath "Scripts\python.exe"
$pythonVersion = & $venvPython --version
if ($pythonVersion -notmatch '^Python 3\.(\d+)') { throw "Could not determine the virtual environment Python version." }
if ([int]$Matches[1] -lt 11) { throw "Python 3.11 or newer is required; found $pythonVersion." }
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed with exit code $LASTEXITCODE" }
& $venvPython -m pip install -r (Join-Path $PSScriptRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed with exit code $LASTEXITCODE" }

if ($env:SHORTFORM_STUDIO_HOME) {
    $appHome = [System.IO.Path]::GetFullPath($env:SHORTFORM_STUDIO_HOME)
} elseif ($env:LOCALAPPDATA) {
    $appHome = Join-Path $env:LOCALAPPDATA "ShortformContentStudio"
} else {
    $appHome = Join-Path $env:USERPROFILE ".local\share\ShortformContentStudio"
}
New-Item -ItemType Directory -Force -Path $appHome | Out-Null
if (-not (Test-Path (Join-Path $appHome ".env"))) {
    Copy-Item (Join-Path $PSScriptRoot ".env.example") (Join-Path $appHome ".env")
}

Write-Host "Setup complete. Edit $appHome\.env, then run .\run.ps1"
