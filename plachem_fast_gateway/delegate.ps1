param(
    [Parameter(Mandatory=$true)][string]$Task,
    [string]$Workspace = "delegation-demo",
    [string]$Agent = "achilles",
    [string]$TaskId = ""
)

$script = Join-Path $PSScriptRoot "fast_gateway.py"
$argsList = @($script, "--task", $Task, "--workspace", $Workspace, "--agent", $Agent)
if ($TaskId -ne "") { $argsList += @("--task-id", $TaskId) }
python @argsList
exit $LASTEXITCODE
