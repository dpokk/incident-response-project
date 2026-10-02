# Undo the demo-only injections (bad image, low memory limit, backend scaled to 0). Run before restore.ps1, whose
# backend rollout would otherwise wait on a pod that cannot start.
$ErrorActionPreference = "Continue"
Write-Host "Restoring the backend image, memory limit and replica count..." -ForegroundColor Cyan
kubectl -n shop patch deploy/backend --type strategic --patch-file "$PSScriptRoot\inject\patches\backend-image-good.json"
kubectl -n shop patch deploy/backend --type strategic --patch-file "$PSScriptRoot\inject\patches\backend-memory-baseline.json"
kubectl -n shop scale deploy/backend --replicas=2
