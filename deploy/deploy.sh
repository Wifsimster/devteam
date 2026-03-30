#!/bin/sh
# deploy.sh — Restricted deploy script for Devteam
# This script is called by GitHub Actions via the self-hosted runner.
# It pulls the latest Docker images and restarts the service.

set -eu

COMPOSE_DIR="${DEVTEAM_COMPOSE_DIR:-/opt/docker/dev-agents}"

echo "[deploy] Pulling latest images..."
docker compose -f "$COMPOSE_DIR/compose.yml" pull dev-agents

echo "[deploy] Restarting services..."
docker compose -f "$COMPOSE_DIR/compose.yml" up -d dev-agents

echo "[deploy] Cleaning up old images..."
docker image prune -f

echo "[deploy] Done."
