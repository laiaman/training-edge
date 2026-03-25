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

### Local Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
python scripts/cli.py init
python scripts/cli.py sync --days 7
python scripts/cli.py serve --reload --port 8420
```

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

## License

[MIT](LICENSE)
