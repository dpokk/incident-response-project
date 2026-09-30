# Failure injection A: sudden traffic spike -> backend memory exhaustion (OOMKilled).
# Only breaks the system; it does not tell the investigator anything.
param([int]$PeakRps = 800, [int]$HoldSeconds = 90)
kubectl -n loadtest exec deploy/loadgen -- python loadgenctl.py spike --peak $PeakRps --ramp 10 --hold $HoldSeconds --rampdown 5
