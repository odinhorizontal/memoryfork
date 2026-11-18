param(
    [Parameter(Mandatory, Position=0, ParameterSetName='Script')][string]$Script,
    [Parameter(Mandatory, ParameterSetName='Tests')][switch]$DiscoverTests
)
$ErrorActionPreference = 'Stop'
$candidates = @('python', 'py', "$env:USERPROFILE\anaconda3\python.exe")
foreach ($candidate in $candidates) {
    $command = Get-Command $candidate -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $command) { continue }
    $options = @()
    if ($candidate -eq 'py') { $options = @('-3') }
    try {
        & $command.Source @options -c 'import sqlite3, sys; sys.exit(sys.version_info < (3, 8))' 2>$null
        if ($LASTEXITCODE -ne 0) { continue }
    } catch { continue }
    if ($DiscoverTests) {
        & $command.Source @options -m unittest discover -v -s $PSScriptRoot
    } else {
        & $command.Source @options (Join-Path $PSScriptRoot $Script)
    }
    exit $LASTEXITCODE
}
throw 'Python 3.8+ with sqlite3 is required.'
