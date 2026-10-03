# Build backend for SAM deploy. Dependencies are resolved by `sam build`.
# `--use-container` is required, not optional: tiktoken ships a compiled
# extension, and a native Windows build would package win_amd64 binaries that
# fail on the python3.9 Lambda runtime.
# Example: .\build.ps1 --use-container
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command sam -ErrorAction SilentlyContinue)) {
    Write-Error "SAM CLI not found. Install SAM CLI, then run: sam build && sam deploy"
}

Write-Host "Running sam build ..."
sam build @args
Write-Host "Done. Deploy with: sam deploy"
