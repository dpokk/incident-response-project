# Manual recovery after a demo (the investigator never remediates). Undoes every injection:
# stops any traffic spike, restores the database URL, brings PostgreSQL back, neutralises bad orders
# and restarts the backend.
$ErrorActionPreference = "Continue"
Write-Host "Stopping any traffic spike..." -ForegroundColor Cyan
kubectl -n loadtest exec deploy/loadgen -- python loadgenctl.py stop | Out-Null

Write-Host "Restoring DATABASE_URL and PostgreSQL..." -ForegroundColor Cyan
kubectl -n shop patch configmap backend-config --type merge --patch-file "$PSScriptRoot\inject\patches\db-url-correct.json"
kubectl -n shop scale deploy/postgres --replicas=1
kubectl -n shop rollout status deploy/postgres --timeout=180s

Write-Host "Neutralising invalid orders (quantity = 0)..." -ForegroundColor Cyan
kubectl -n shop exec deploy/postgres -- psql -U shop -d shop -c "UPDATE orders SET quantity = 1 WHERE quantity = 0" 2>$null

Write-Host "Restarting backend..." -ForegroundColor Cyan
kubectl -n shop rollout restart deploy/backend
kubectl -n shop rollout status deploy/backend --timeout=180s
kubectl -n shop get pods
Write-Host "Restored. Wait ~2 minutes of healthy traffic before the next injection." -ForegroundColor Green
