Project Status
prototype/
├── docker-compose.yml           ✅ 5 services, shared volume, bridge network
├── .env.example                 ✅ Dual webhook URLs + Gemini key
├── README.md                    ✅ Architecture + quick start
├── victim-app/                  ✅ Flask + Prometheus counter + PII logging
│   ├── app.py, Dockerfile, requirements.txt
├── prometheus/                  ✅ 5s scrape + CriticalServiceFailure rule
│   ├── prometheus.yml, alert_rules.yml
├── alertmanager/                ✅ Dual-path: raw-slack + sentry-webhook
│   └── alertmanager.yml
├── sentry-agent/                ✅ PII sanitizer + log fetcher + MCP forwarder
│   ├── main.py, Dockerfile, requirements.txt
└── cloud-architect/             ✅ Gemini RCA + Slack Block Kit + remediation
    ├── main.py, Dockerfile, requirements.txt
Prerequisites (What You Need Before Running)
1. Docker Desktop
Install: docker.com/products/docker-desktop
Must be running before you start the stack
Ensure Docker Compose v2 is available (docker compose version)
2. Google Gemini API Key
Go to aistudio.google.com/apikey
Create a new API key (free tier works fine for prototyping)
You'll paste this into the .env file
3. Slack Workspace (already configured)
Your two webhook URLs are already in 
.env.example
:
#raw-monitoring — receives raw Alertmanager noise
#sentinel-ops — receives AI-generated RCA reports
Both channels must exist in your Slack workspace
4. System Resources
~2 GB RAM available for all containers
Ports 5001, 8001, 8002, 9090, 9093 must be free
Step-by-Step Deployment
bash
# 1. Navigate to project
cd d:/Major-Project/prototype
# 2. Create .env from template
cp .env.example .env
# 3. Edit .env — replace the Gemini API key placeholder
#    GEMINI_API_KEY=your-actual-key-here
#    (Slack webhooks are already filled in)
# 4. Build and start all services
docker compose up -d --build
# 5. Verify all containers are running
docker compose ps
#    Should show: sentinel-victim-app, sentinel-prometheus,
#    sentinel-alertmanager, sentinel-sentry-agent, sentinel-cloud-architect
#    All with status "Up"
# 6. Wait ~15 seconds for all services to initialize
# 7. Quick health checks
curl http://localhost:5001/          # Victim app
curl http://localhost:8001/health    # Sentry Agent
curl http://localhost:8002/health    # Cloud Architect
Triggering & Verifying the Pipeline
bash
# Trigger the error
curl http://localhost:5001/trigger-error
# Expected timeline:
#   0s  — Error counter incremented, log written with fake IPs/UUIDs
#   5s  — Prometheus scrapes the metric
#  15s  — Alert fires (10s threshold met)
#  20s  — Alertmanager sends to BOTH:
#          → #raw-monitoring (raw alert)
#          → Sentry Agent webhook
#  25s  — Sentry fetches logs, redacts PII, forwards MCP JSON
#  35s  — Cloud Architect queries Gemini, posts RCA to #sentinel-ops
#
# Total: ~30-40 seconds (within 45s target)
Verification Commands
bash
# Watch the full pipeline in real-time
docker logs -f sentinel-sentry-agent      # PII redaction happening
docker logs -f sentinel-cloud-architect   # Gemini RCA + Slack post
# Privacy check: ensure no raw IPs leak to Cloud Architect
docker logs sentinel-cloud-architect 2>&1 | grep -cP '\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}'
# Expected output: 0
# Check Prometheus UI
# Open: http://localhost:9090/alerts → CriticalServiceFailure should show as "firing"
# Check Slack
# #raw-monitoring → raw Prometheus alert
# #sentinel-ops   → Gemini RCA with "Execute Remediation" button
Teardown
bash
docker compose down          # Stop and remove containers
docker compose down -v       # Also remove volumes (logs, etc.)