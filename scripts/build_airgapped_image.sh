#!/usr/bin/env bash
# ==============================================================================
# Build & Package CCTV Monitoring for Airgapped Deployment (Python Base)
# ==============================================================================
set -euo pipefail

IMAGE_TAG="cctv-monitor:airgapped-latest"
TAR_NAME="cctv-monitor-airgapped.tar"

echo "🔨 Building airgapped image using official Python base..."
docker build -f Dockerfile -t "${IMAGE_TAG}" .

echo "📦 Exporting self-contained image archive: ${TAR_NAME}..."
docker save "${IMAGE_TAG}" -o "${TAR_NAME}"

echo "✅ Done! You can now transfer '${TAR_NAME}' and 'docker-compose.yml' to your airgapped server."
echo "🚀 On the airgapped machine, run: docker load -i ${TAR_NAME} && docker compose up -d"
