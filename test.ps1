& "$PSScriptRoot\run-python.ps1" 'test_experiment.py'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& "$PSScriptRoot\run-python.ps1" 'test_memory.py'
exit $LASTEXITCODE
