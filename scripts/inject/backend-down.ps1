# Failure injection (demo): the backend deployment is scaled to 0 replicas (a mistaken scale-down).
# The frontend has nothing to call; users get errors. Only breaks the system; it does not tell the investigator
# anything. "Reset to healthy" scales it back to 2.
kubectl -n shop scale deploy/backend --replicas=0
