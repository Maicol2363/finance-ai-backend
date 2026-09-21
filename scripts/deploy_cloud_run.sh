#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────
# Finance AI Backend — Manual Deployment to Google Cloud Run
# ─────────────────────────────────────────────────────────────

PROJECT="finance-ai-api"
REGION="southamerica-east1"
SERVICE_NAME="finance-api"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$BACKEND_DIR"

echo "=== 1. Checking git state ==="
git diff --check

echo "=== 2. Running local quality checks ==="
uv sync --all-groups --frozen
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest

echo "=== 3. Running Neon database migrations ==="
if [ -z "${FINANCE_AI_DATABASE_URL:-}" ]; then
  echo "Fetching FINANCE_AI_DATABASE_URL from Cloud Run service config..."
  DB_URL=$(gcloud run services describe "$SERVICE_NAME" \
    --project="$PROJECT" \
    --region="$REGION" \
    --format='value(spec.template.spec.containers[0].env[1].value)')
  export FINANCE_AI_DATABASE_URL="$DB_URL"
fi

uv run alembic upgrade head
uv run python -m finance_ai.db.seeds
echo "Alembic migrations and database seeds up to date on Neon."

echo "=== 4. Building container image (--target runtime) ==="
COMMIT_HASH=$(git rev-parse HEAD)
IMAGE="$REGION-docker.pkg.dev/$PROJECT/finance-ai/backend:$COMMIT_HASH"

gcloud auth configure-docker "$REGION-docker.pkg.dev" --quiet

docker build \
  --target runtime \
  --tag "$IMAGE" \
  .

echo "=== 5. Pushing container image ==="
docker push "$IMAGE"

echo "=== 6. Deploying to Cloud Run ==="
gcloud run deploy "$SERVICE_NAME" \
  --project="$PROJECT" \
  --region="$REGION" \
  --image="$IMAGE" \
  --port=8000 \
  --memory=512Mi \
  --cpu=1 \
  --min-instances=0 \
  --max-instances=3 \
  --allow-unauthenticated \
  --quiet

echo "=== 7. Verifying Cloud Run deployment ==="
URL=$(gcloud run services describe "$SERVICE_NAME" \
  --project="$PROJECT" \
  --region="$REGION" \
  --format='value(status.url)')

echo "Service URL: $URL"
echo -n "Health check: "
curl -fsS "$URL/health/live"
echo ""

STATUS=$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/v1/income-sources")
echo "Protected endpoint status: HTTP $STATUS (expected 401)"

if [ "$STATUS" != "401" ]; then
  echo "Verification failed: expected HTTP 401, got $STATUS"
  exit 1
fi

echo "=== Deployment completed successfully! ==="
