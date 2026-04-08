#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/antiduplic}"

cd "${APP_DIR}"

if docker compose version >/dev/null 2>&1; then
  docker compose up -d
  exit 0
fi

if command -v docker-compose >/dev/null 2>&1; then
  docker-compose up -d
  exit 0
fi

if command -v podman-compose >/dev/null 2>&1; then
  podman-compose up -d
  exit 0
fi

if podman compose version >/dev/null 2>&1; then
  podman compose up -d
  exit 0
fi

echo "No se encontró un comando compose compatible (docker compose, docker-compose, podman-compose, podman compose)."
exit 1