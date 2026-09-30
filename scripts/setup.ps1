# Builds the demo image inside minikube and deploys the app, load generator and Prometheus.
param(
    [string]$MinikubeProfile = "incident-demo",
    [switch]$SkipBuild
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

$status = minikube status -p $MinikubeProfile --format '{{.Host}}' 2>$null
if ($status -ne "Running") {
    Write-Host "Starting minikube profile '$MinikubeProfile'..." -ForegroundColor Cyan
    minikube start -p $MinikubeProfile --driver=docker --cpus=4 --memory=3584
}
kubectl config use-context $MinikubeProfile | Out-Null

if (-not $SkipBuild) {
    Write-Host "Building image incident-demo/app:0.2 inside minikube..." -ForegroundColor Cyan
    minikube image build -p $MinikubeProfile -t incident-demo/app:0.2 "$root\app"
    if ($LASTEXITCODE -ne 0) { throw "image build failed" }
}

Write-Host "Applying Kubernetes manifests..." -ForegroundColor Cyan
kubectl apply -f "$root\k8s\00-namespaces.yaml"
kubectl apply -f "$root\k8s"

Write-Host "Waiting for rollouts..." -ForegroundColor Cyan
kubectl -n monitoring rollout status deploy/prometheus --timeout=300s
kubectl -n shop rollout status deploy/postgres --timeout=300s
kubectl -n shop rollout status deploy/backend --timeout=180s
kubectl -n shop rollout status deploy/frontend --timeout=180s
kubectl -n loadtest rollout status deploy/loadgen --timeout=180s

kubectl get pods -n shop -o wide
Write-Host "`nReady. Next: python -m investigator check" -ForegroundColor Green
