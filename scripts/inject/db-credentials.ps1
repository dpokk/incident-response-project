# Failure injection E (demo): a credential rotation gone wrong. A new Secret with a stale database password is
# created and the backend is switched to read PGPASSWORD from it, then rolled out. PostgreSQL itself is unchanged and
# healthy; every backend query fails with "password authentication failed". Only breaks the system; it tells the
# investigator nothing. Undo with scripts\restore-credentials.ps1 (the demo's Reset does this).
$ErrorActionPreference = "Stop"
$stale = -join ((48..57) + (97..122) | Get-Random -Count 20 | ForEach-Object { [char]$_ })
kubectl -n shop create secret generic postgres-credentials-rotated --from-literal=POSTGRES_PASSWORD=$stale --dry-run=client -o yaml | kubectl apply -f -
kubectl -n shop patch deploy/backend --type strategic --patch-file "$PSScriptRoot\patches\pgpassword-rotated.json"
kubectl -n shop rollout status deploy/backend --timeout=120s
