param([string]$OutputDir = (Join-Path $PSScriptRoot 'benchmark-output'))
$ErrorActionPreference = 'Stop'
& "$PSScriptRoot\run-python.ps1" -Script 'benchmark.py' -ScriptArguments @('--output', $OutputDir)
exit $LASTEXITCODE
