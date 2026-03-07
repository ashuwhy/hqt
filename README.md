# hqt – Hybrid Trading Database System

CS39006 DBMS Lab project: high-QPS LOB, TimescaleDB, Apache AGE, Qiskit arbitrage, security & observability.

## Repo setup

- **Branches:** `main` (default), `dev` (feature work).
- **Linting:** Pre-commit runs Ruff (Python) and common checks before each commit.

### One-time setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements-dev.txt
pre-commit install
```

Then each commit will run the hooks. To run manually:

```bash
pre-commit run --all-files
```

## Docs

- [PRD](docs/PRD.md) · [Architecture](docs/ARCHITECTURE.md) · [Database schema](docs/DATABASE_SCHEMA.md) · [API spec](docs/API_SPEC.md) · [Module specs](docs/MODULE_SPECS.md) · [Task list](docs/TASK_LIST.md)

## Quick start (when implemented)

```bash
docker compose up -d
# Proxy: http://localhost:8000  ·  Grafana: http://localhost:3000
```
