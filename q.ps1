# Queue CLI wrapper: .\q.ps1 add "do the thing" | .\q.ps1 list | .\q.ps1 sessions
$ErrorActionPreference = "Stop"
$env:PYTHONPATH = $PSScriptRoot
& "$PSScriptRoot\.venv\Scripts\python.exe" -m queue_mcp.cli @args
