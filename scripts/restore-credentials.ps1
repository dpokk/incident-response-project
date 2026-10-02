# Undo injection E: point the backend back at the original postgres-credentials Secret and remove the stale one.
# A no-op (no rollout) if the backend already uses the original Secret.
$ErrorActionPreference = "Continue"
$ref = kubectl -n shop get deploy/backend -o jsonpath="{.spec.template.spec.containers[0].env[?(@.name=='PGPASSWORD')].valueFrom.secretKeyRef.name}"
if ($ref -ne "postgres-credentials") {
    Write-Host "Restoring the backend's PGPASSWORD to Secret postgres-credentials (was: $ref)" -ForegroundColor Cyan
    kubectl -n shop patch deploy/backend --type strategic --patch-file "$PSScriptRoot\inject\patches\pgpassword-original.json"
    kubectl -n shop rollout status deploy/backend --timeout=180s
} else {
    Write-Host "Backend already uses Secret postgres-credentials"
}
kubectl -n shop delete secret postgres-credentials-rotated --ignore-not-found
