#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/antiduplic}"
SERVICE_NAME="${SERVICE_NAME:-antiduplic}"
APP_USER="${APP_USER:-www-data}"
APP_GROUP="${APP_GROUP:-www-data}"

if command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
else
  SUDO=""
fi

if [ ! -f "${APP_DIR}/.env" ]; then
  echo "Falta ${APP_DIR}/.env"
  exit 1
fi

${SUDO} cp "${APP_DIR}/deploy/antiduplic.service" "/etc/systemd/system/${SERVICE_NAME}.service"
${SUDO} sed -i "s|/opt/antiduplic|${APP_DIR}|g" "/etc/systemd/system/${SERVICE_NAME}.service"
${SUDO} sed -i "s/^User=.*/User=${APP_USER}/" "/etc/systemd/system/${SERVICE_NAME}.service"
${SUDO} sed -i "s/^Group=.*/Group=${APP_GROUP}/" "/etc/systemd/system/${SERVICE_NAME}.service"

${SUDO} systemctl daemon-reload
${SUDO} systemctl enable "${SERVICE_NAME}"
${SUDO} systemctl restart "${SERVICE_NAME}"
${SUDO} systemctl status "${SERVICE_NAME}" --no-pager