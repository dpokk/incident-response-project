# Failure injection C: take PostgreSQL away (scale its Deployment to zero). Backend config stays correct.
kubectl -n shop scale deploy/postgres --replicas=0
