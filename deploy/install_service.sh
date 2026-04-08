#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/antiduplic}"
SERVICE_NAME="${SERVICE_NAME:-antiduplic}"

if [ ! -f "${APP_DIR}/.env" ]; then
  echo "Falta ${APP_DIR}/.env"
  exit 1
fi

sudo cp "${APP_DIR}/deploy/antiduplic.service" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo sed -i "s|/opt/antiduplic|${APP_DIR}|g" "/etc/systemd/system/${SERVICE_NAME}.service"

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"
sudo systemctl status "${SERVICE_NAME}" --no-pager