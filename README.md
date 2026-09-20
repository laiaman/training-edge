<p align="center">
  <h1 align="center">TrainingEdge</h1>
  <p align="center">
    Self-hosted sports analytics engine — data-driven training decisions
    <br />
    <a href="#quick-start">Quick Start</a> · <a href="#features">Features</a> · <a href="#api-reference">API Reference</a>
    <br /><br />
    <a href="README.zh-CN.md">中文文档</a>
  </p>
</p>

> 🤖 Built with [Claude Code](https://claude.ai/claude-code) — engine, web dashboard, deployment scripts, and docs.

---

## What is this?

TrainingEdge is a **fully self-hosted** sports training analytics platform. It syncs data from your Garmin watch, computes professional training metrics, and uses AI to generate training plans and ride reviews.

**Your data stays on your machine.** No cloud services, no subscriptions, no third party touching your training data.

### Core Capabilities

- 🔄 **Garmin Auto-Sync** — activities, sleep, HRV, resting HR, Body Battery
- 📊 **Pro Metrics** — NP / TSS / IF / CTL / ATL / TSB / PDC / eFTP / W'
- 🤖 **AI Training Plans** — auto-generated weekly plans based on fitness state and constraints
- 📋 **Plan Compliance Tracking** — auto-matches actual workouts to plan (flexible scheduling)
- 🏥 **Daily Readiness** — combines HRV, sleep, TSB to decide if you should train today
- 📈 **Web Dashboard** — dark theme, conclusion-first design, mobile-friendly

### Design Philosophy

**"Conclusion → Evidence → Action"** — every page tells you what to do first, shows why, then gives you the controls.

---

## Features

### Dashboard

| Page | Content |
|------|---------|
| Home | Today's readiness, weekly training summary, fitness trend chart, anomaly alerts |
| Activity Detail | AI ride review, power/HR time series, zone distribution, lap analysis |
| Training Plan | AI weekly plan, constraint checklist, planned vs actual comparison |
| Body Data | Health trends (HRV/sleep/HR), body composition records (InBody) |

### Metrics

| Metric | Description |
|--------|-------------|
| NP / TSS / IF | Normalized Power, Training Stress Score, Intensity Factor |
| CTL / ATL / TSB | Fitness / Fatigue / Form |
| PDC / eFTP / W' | Power Duration Curve, Estimated FTP, Anaerobic Work Capacity |
| xPower / TRIMP | Exponentially Weighted Power, Training Impulse |
| HR Drift / VDOT | Heart Rate Drift, Running Ability Index |

---

## Quick Start

### Requirements

- Python 3.10+ or Docker
- Garmin watch + Garmin Connect account

### Docker

```bash
git clone https://github.com/sisjune/training-edge.git
cd training-edge
cp .env.example .env   # edit with your parameters
docker compose up -d
```

Open `http://localhost:8420`

### Local Development (macOS)

Run the service from a local project checkout. OneDrive is backup-only:

- Code source: `<local-checkout>/training-edge`
- Runtime dir: `~/Library/Application Support/TrainingEdge/` (DB, venv, tokens, logs)
- OneDrive: only the three allowlisted plan files described below
- Configure paths in `.env` (auto-loaded on import)

```bash
bash scripts/install_service.sh --preflight
bash scripts/install_service.sh    # launchd: auto-start + crash recovery
bash scripts/verify_local_runtime.sh
bash scripts/backup_plan_to_onedrive.sh --dry-run
bash scripts/backup_plan_to_onedrive.sh
bash scripts/start_server.sh --status
python scripts/smoke_test.py
```

`install_service.sh` rejects a checkout under `CloudStorage/OneDrive`, even if
that older copy still exists. On a bootstrap failure it starts the same local
checkout as a temporary non-reload service and reports the launchd failure.

See [README.zh-CN.md](README.zh-CN.md) for full macOS setup guide.

### Local Development (generic)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
python scripts/cli.py init
python scripts/cli.py sync --days 7
python scripts/cli.py serve --reload --port 8420
```

### Reuse Hermes `garmin.db` (full import)

If you already have a Hermes-maintained `garmin.db` (e.g. `../garmin.db` in the parent project), you can import your full history into TrainingEdge without calling Garmin API:

```bash
python scripts/cli.py sync-hermes --db ../garmin.db --all
```

> Note: this import writes into TrainingEdge `activities` / `wellness` / `fitness_history` and stores Hermes raw JSON into `hermes_*` tables. It **does not download FIT files**, so power-derived metrics (NP/TSS/IF, PDC, etc.) will be empty until you run `python scripts/cli.py sync --days N`.

### How do I access it after startup?

- **Web dashboard**: `http://localhost:8420/`
- **Health check**: `http://localhost:8420/api/health` (no auth)
- **API calls**: run `python scripts/cli.py init` to get an **API Key**, then send `X-API-Key` (example):

```bash
curl -H 'X-API-Key: <API_KEY>' http://localhost:8420/api/summary
```

> If `TRAININGEDGE_PASSWORD` is set, browser access will redirect to `/login`. API calls still require `X-API-Key`.

### Configuration

Copy `.env.example` to `.env`:

| Variable | Description |
|----------|-------------|
| `TRAININGEDGE_FTP` | Your FTP (watts) |
| `TRAININGEDGE_MAX_HR` | Max heart rate (bpm) |
| `TRAININGEDGE_RESTING_HR` | Resting heart rate (bpm) |
| `TRAININGEDGE_PASSWORD` | Web access password (optional) |
| `GARMIN_EMAIL` | Garmin login email (for auto-fetching token) |
| `GARMIN_PASSWORD` | Garmin login password (for auto-fetching token) |
| `GARMINTOKENS` | Garmin OAuth token directory |
| `GARMIN_IS_CN` | Set to `true` if your account is in Garmin China (`garmin.cn`) |
| `OPENROUTER_API_KEY` | Required for AI features (or configure in web settings) |

See [.env.example](.env.example) for all variables.

---

## Architecture

```
Garmin Watch → Garmin Connect → garminconnect API
                                       │
                                       ▼
                              FIT parsing (fitparse)
                                       │
                                       ▼
                            Metrics engine (engine/metrics.py)
                                       │
                                       ▼
                             SQLite (/data/training_edge.db)
                                       │
                          ┌────────────┼────────────┐
                          ▼            ▼            ▼
                     REST API    AI plan gen    Web dashboard
                     (FastAPI)   (OpenRouter)   (Jinja2)
```

### Tech Stack

Python 3.13 · FastAPI · SQLite (WAL) · Jinja2 · Chart.js · fitparse · garminconnect · Docker

---

## API Reference

### Activities

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/activities` | GET | List activities |
| `/api/activity/{id}` | GET | Activity detail with computed metrics |
| `/api/activities/{id}/ai-review` | GET | AI activity review |

### Fitness & Health

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/fitness` | GET | CTL/ATL/TSB history |
| `/api/pdc` | GET | Power Duration Curve |
| `/api/wellness` | GET | HRV / sleep / resting HR |
| `/api/decision-summary` | GET | Today's readiness assessment |

### Training Plan

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/plan/generate` | POST | Generate AI weekly plan |
| `/api/plan/workouts` | GET | Current plan workouts |
| `/api/constraint-status` | GET | Constraint compliance |

### Sync & Settings

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/sync` | POST | Trigger Garmin data sync |
| `/api/settings` | GET/POST | Read/update settings |
| `/api/health` | GET | Health check |

---

## Project Structure

```
training-edge/
├── engine/              # Core analytics engine
│   ├── metrics.py       # NP/TSS/IF/CTL/ATL/TSB/PDC computation
│   ├── database.py      # SQLite data layer
│   ├── sync.py          # Garmin data sync
│   ├── readiness.py     # Daily readiness assessment
│   ├── plan_generator.py # AI training plan generation
│   └── fit_parser.py    # FIT file parsing
├── api/app.py           # FastAPI application
├── web/templates/       # Jinja2 page templates
├── scripts/cli.py       # CLI tool
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

---

## Migration & Backup

Full AICoachPortable migration (vault, garmin.db, Hermes): see [../README.md#项目迁移指南](../README.md#项目迁移指南).

**Rule: local code runs, local state stays local, OneDrive receives only plan backups.**
Never run this service from OneDrive and never put SQLite DB / tokens / FIT / logs / venv there.

| Asset | Location | Migrate how |
|-------|----------|-------------|
| Code | local `AICoachPortableOnMac/training-edge/` | Git checkout/update |
| Config | `.env` | Manual copy (secrets) |
| Runtime DB | `~/Library/Application Support/TrainingEdge/` | Manual tar backup |
| venv | same dir `venv/` | **Rebuild** on new machine |
| Plan backup | OneDrive `AICoachPortable/vault/` | `scripts/backup_plan_to_onedrive.sh` |

**New Mac (quick):**

```bash
# Old machine: stop service, backup runtime (exclude venv) + .env
RUNTIME="$HOME/Library/Application Support/TrainingEdge"
tar czf ~/Desktop/te-runtime.tar.gz -C "$RUNTIME" --exclude='venv' .
cp .env ~/Desktop/trainingedge.env.backup

# New machine: restore, rebuild venv, reinstall service
mkdir -p "$RUNTIME" && tar xzf ~/Desktop/te-runtime.tar.gz -C "$RUNTIME"
cp ~/Desktop/trainingedge.env.backup training-edge/.env
python3 -m venv "$RUNTIME/venv" && "$RUNTIME/venv/bin/pip" install -e training-edge/
cd training-edge && bash scripts/install_service.sh && python scripts/smoke_test.py
```

See [README.zh-CN.md — 迁移与备份](README.zh-CN.md#迁移与备份) for Docker/NAS and dual-machine scenarios.

---

## Troubleshooting

### 503 Service Unavailable / Can't Connect

The server process is not running. Common causes: macOS sleep/lid close killing the process, or OneDrive sync triggering excessive reloads.

```bash
bash scripts/start_server.sh --status   # Check
bash scripts/start_server.sh            # Start/restart
bash scripts/start_server.sh --stop     # Stop first if port conflict
```

### 500 Internal Server Error

Usually caused by a template referencing a variable not passed from the route. Run the smoke test to diagnose:

```bash
python scripts/smoke_test.py --offline  # Syntax + engine + render check
```

### OneDrive checkout is rejected

The production installer intentionally rejects any project path under
`CloudStorage/OneDrive`. Move/open the local checkout, then install from there:

```bash
cd <local-checkout>/training-edge
bash scripts/install_service.sh
```

### Smoke Test

Run after every code change to catch regressions:

```bash
python scripts/smoke_test.py --offline  # No server needed
python scripts/smoke_test.py            # Full test (server must be running)
```

---

## License

[MIT](LICENSE)
