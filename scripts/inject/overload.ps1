# Failure injection (demo): a SUSTAINED traffic overload (800 req/s for 15 minutes) -> the backend's request backlog
# grows faster than it is served and it is OOM-killed again and again. A higher memory limit would only delay the
# kill. Used to show that the Remediation Agent's policy gate refuses to automate when the evidence is not conclusive.
# Only breaks the system; it does not tell the investigator anything. "Reset to healthy" stops the load.
param([int]$PeakRps = 800, [int]$HoldSeconds = 900)
kubectl -n loadtest exec deploy/loadgen -- python loadgenctl.py spike --peak $PeakRps --ramp 10 --hold $HoldSeconds --rampdown 5
