# Failure injection (demo): a bad configuration deploy lowers the backend's memory limit to 32Mi (request 24Mi).
# Normal traffic needs about 36-38 MB per pod, so the new pods are OOM-killed and crash-loop. A higher limit is the
# real fix. Only breaks the system; it does not tell the investigator anything. "Reset to healthy" restores 192Mi.
kubectl -n shop patch deploy/backend --type strategic --patch-file "$PSScriptRoot\patches\backend-memory-low.json"
