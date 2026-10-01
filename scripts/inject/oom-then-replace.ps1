# Iteration 4 validation scenario: OOM, then the failing instances disappear.
#
#   1. traffic spike -> backend memory grows -> backend is OOM-killed (restarts)
#   2. once at least $MinKills OOM kills have happened, a simulated operator stops the spike and
#      replaces the backend pods (rollout restart): the failing instances and their logs are gone
#   3. the system recovers; investigate afterwards (the investigator is told nothing)
#
# Only breaks the system and plays the operator; it never talks to the investigator. Recording must already
# be running (python -m investigator record, or watch) for retained evidence to exist.
param([int]$PeakRps = 800, [int]$HoldSeconds = 150, [int]$MinKills = 2, [int]$TimeoutSeconds = 300)

function Get-OomKills {
    $json = kubectl -n shop get pods -l app=backend -o json | ConvertFrom-Json
    $n = 0
    foreach ($p in $json.items) {
        foreach ($c in $p.status.containerStatuses) {
            if ($c.lastState.terminated -and $c.lastState.terminated.reason -eq "OOMKilled") { $n += $c.restartCount }
        }
    }
    return $n
}

$t0 = Get-Date
Write-Host "[$($t0.ToString('HH:mm:ss'))] spike to $PeakRps req/s" -ForegroundColor Cyan
kubectl -n loadtest exec deploy/loadgen -- python loadgenctl.py spike --peak $PeakRps --ramp 10 --hold $HoldSeconds --rampdown 5 | Out-Null

do {
    Start-Sleep 5
    $kills = Get-OomKills
    $elapsed = ((Get-Date) - $t0).TotalSeconds
} while ($kills -lt $MinKills -and $elapsed -lt $TimeoutSeconds)

if ($kills -lt $MinKills) {
    Write-Host "Only $kills OOM kill(s) after $([int]$elapsed)s; replacing the pods anyway" -ForegroundColor Yellow
} else {
    Write-Host "[$((Get-Date).ToString('HH:mm:ss'))] $kills OOM kill(s) seen" -ForegroundColor Cyan
}

Write-Host "[$((Get-Date).ToString('HH:mm:ss'))] operator: stop the spike and replace the backend pods" -ForegroundColor Cyan
kubectl -n loadtest exec deploy/loadgen -- python loadgenctl.py stop | Out-Null
kubectl -n shop rollout restart deploy/backend | Out-Null
kubectl -n shop rollout status deploy/backend --timeout=180s | Out-Null
Write-Host "[$((Get-Date).ToString('HH:mm:ss'))] backend replaced; the OOM-killed pods no longer exist" -ForegroundColor Green
kubectl -n shop get pods -l app=backend
