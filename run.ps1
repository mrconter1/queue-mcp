# Start the queue MCP server in the foreground. Ctrl+C to stop.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
& "$PSScriptRoot\.venv\Scripts\python.exe" -m queue_mcp.server
