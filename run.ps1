# One-shot setup + run for Windows PowerShell.
# Usage:  ./run.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..." -ForegroundColor Cyan
    py -m venv .venv
}
& .\.venv\Scripts\Activate.ps1

Write-Host "Installing dependencies..." -ForegroundColor Cyan
python -m pip install --upgrade pip | Out-Null
python -m pip install -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from template. Add your API keys to enable AI features." -ForegroundColor Yellow
}

Write-Host "Seeding sample data..." -ForegroundColor Cyan
python seed.py

Write-Host "Starting server at http://127.0.0.1:8000" -ForegroundColor Green
python -m uvicorn app.main:app --reload --port 8000
