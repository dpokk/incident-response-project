# Failure injection B: point the backend at a database host that does not exist, then roll it out
# (like a bad config change being deployed). PostgreSQL itself keeps running.
$ErrorActionPreference = "Stop"
kubectl -n shop patch configmap backend-config --type merge --patch-file "$PSScriptRoot\patches\db-url-wrong.json"
kubectl -n shop rollout restart deploy/backend
kubectl -n shop rollout status deploy/backend --timeout=120s
