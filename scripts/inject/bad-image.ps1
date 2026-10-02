# Failure injection (demo): a release points the backend at an image tag that does not exist
# (incident-demo/app:0.3, pull policy IfNotPresent). The new pod cannot pull it (ErrImagePull / ImagePullBackOff);
# the rollout stalls while the old pods keep serving. No typed action fits: an engineer must roll back.
# Only breaks the system; it does not tell the investigator anything. "Reset to healthy" restores app:0.2.
kubectl -n shop patch deploy/backend --type strategic --patch-file "$PSScriptRoot\patches\backend-image-bad.json"
