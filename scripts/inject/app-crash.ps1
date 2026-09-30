# Failure injection D: a single user order with quantity=0. The backend accepts it (no validation);
# its invoice job later divides by zero, the unhandled exception kills the process, and because the
# bad order is still pending after every restart, the pods crash-loop.
kubectl -n loadtest exec deploy/loadgen -- python loadgenctl.py order --quantity 0 --total 10
