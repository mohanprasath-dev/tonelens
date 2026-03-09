#!/usr/bin/env bash
set -euo pipefail

PROJECT="notional-cirrus-458606-e0"
REGION="us-central1"
SERVICE="tonelens"
REPO="tonelens-repo"
IMAGE="us-central1-docker.pkg.dev/${PROJECT}/${REPO}/app:latest"

echo "==> Setting project to ${PROJECT}"
gcloud config set project "${PROJECT}"

echo "==> Creating Artifact Registry repo (if not exists)"
gcloud artifacts repositories create "${REPO}" \
  --repository-format=docker \
  --location="${REGION}" \
  --description="ToneLens Docker repo" \
  2>/dev/null || true

echo "==> Configuring Docker authentication"
gcloud auth configure-docker us-central1-docker.pkg.dev --quiet

echo "==> Building Docker image"
docker build -t "${IMAGE}" .

echo "==> Pushing Docker image"
docker push "${IMAGE}"

echo "==> Deploying to Cloud Run"
gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}" \
  --platform managed \
  --region "${REGION}" \
  --allow-unauthenticated \
  --memory 2Gi \
  --cpu 2 \
  --min-instances 0 \
  --max-instances 5 \
  --port 8080 \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_REGION=${REGION}"

URL=$(gcloud run services describe "${SERVICE}" \
  --region="${REGION}" \
  --format='value(status.url)')

echo ""
echo "✅ ToneLens live at: ${URL}"
