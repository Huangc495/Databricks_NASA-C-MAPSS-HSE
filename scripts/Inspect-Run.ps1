param([Parameter(Mandatory=$true)][long]$RunId)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/Use-SentinelOps.ps1"
$cli = Join-Path $PSScriptRoot '../.tools/databricks/databricks.exe'
$raw = & $cli jobs get-run $RunId -o json
if ($LASTEXITCODE -ne 0) { throw 'Could not read Databricks run' }
$run = $raw | ConvertFrom-Json
[pscustomobject]@{
    RunId = $run.run_id
    State = $run.state.life_cycle_state
    Result = $run.state.result_state
    Message = $run.state.state_message
    Url = $run.run_page_url
} | ConvertTo-Json
foreach ($task in $run.tasks) {
    if ($task.state.result_state -eq 'FAILED') {
        $output = & $cli jobs get-run-output $task.run_id -o json | ConvertFrom-Json
        $output | Select-Object error,error_trace | ConvertTo-Json -Depth 4
    }
}
