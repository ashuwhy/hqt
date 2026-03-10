# hqt – hybrid quantum trading database system

CS39006 DBMS Lab project: high-QPS LOB, TimescaleDB, Apache AGE, Qiskit arbitrage, security & observability.

## Repo setup

- **Branches:** `dev` (default), `main` (weekly updates).
- **Linting:** Pre-commit runs Ruff (Python) and common checks before each commit.

### One-time setup

When cloning the repository, you **must** include the submodules for the LOB engine components:

```bash
git clone --recursive https://github.com/ashuwhy/lob.git
```

If you have already cloned the repository without submodules, run:

```bash
git submodule update --init --recursive
```

Setting up the development environment requires installing linting and formatting tools. We use `ruff` for code formatting and standard checks, and `pre-commit` to automatically run these checks on every commit.

```bash
# 1. Create and activate a virtual environment
python3.12 -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows

# 2. Install development tools (ruff and pre-commit)
pip install -r requirements-dev.txt

# 3. Initialize git hooks to automatically check your code
pre-commit install
```

Then each commit will run the hooks. To run manually across all files:

```bash
pre-commit run --all-files
```

## Docs

- [PRD](docs/PRD.md) · [Architecture](docs/ARCHITECTURE.md) · [Database schema](docs/DATABASE_SCHEMA.md) · [API spec](docs/API_SPEC.md) · [Module specs](docs/MODULE_SPECS.md) · [Task list](docs/TASK_LIST.md)

## Quick start

```bash
docker compose up -d
# First run builds the Postgres image (TimescaleDB + AGE on Debian); allow a few minutes.
# When healthy: Postgres 5432, Kafka 9092, Redis 6379, Prometheus 9090, Grafana 3000.
# Proxy (when implemented): http://localhost:8000  ·  Grafana: http://localhost:3000
```

If you see **Docker I/O errors** during build (`input/output error` when committing or pulling), try: restart Docker Desktop, free disk space, then `docker builder prune -f` and `docker compose build --no-cache` again.
