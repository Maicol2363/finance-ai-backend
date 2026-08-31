# Finance AI Backend

FastAPI backend for the manual-first Finance AI application. The current identity scope provides registration plus opaque, hash-only Login sessions with rotation, restoration, current-user lookup, replay revocation, and Logout.

## Local commands

From this directory:

- Install/sync: `uv sync --all-groups --frozen`
- Run API: `uv run uvicorn finance_ai.main:app --reload`
- Migrate: `uv run alembic upgrade head`
- Seed: `uv run python -m finance_ai.db.seeds`
- Test: `uv run pytest --cov=finance_ai --cov-report=term-missing`
- Lint: `uv run ruff check .`
- Format check: `uv run ruff format --check .`
- Types: `uv run mypy src`

Use `docker compose --profile test run --rm test pytest -m integration` from the repository root for real PostgreSQL integration tests. Never commit a real `.env`.

The public identity endpoints are `POST /api/v1/auth/register`, `POST /api/v1/auth/login`, `POST /api/v1/auth/refresh`, `GET /api/v1/auth/me`, and `POST /api/v1/auth/logout`. Access credentials expire after 15 minutes; renewable credentials rotate, are bounded by 30-day idle and 90-day absolute lifetimes, and are never persisted in plaintext. The default maximum is ten active sessions per user.
