# Build backend for SAM deploy. Dependencies are resolved by `sam build` (use
# `sam build --use-container` on Windows so Linux-compatible wheels are used).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command sam -ErrorAction SilentlyContinue)) {
    Write-Error "SAM CLI not found. Install SAM CLI, then run: sam build && sam deploy"
}

Write-Host "Running sam build ..."
sam build @args
Write-Host "Done. Deploy with: sam deploy"
