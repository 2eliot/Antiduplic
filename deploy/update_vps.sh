#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/antiduplic}"
APP_USER="${APP_USER:-www-data}"
SERVICE_NAME="${SERVICE_NAME:-antiduplic}"
GIT_BRANCH="${GIT_BRANCH:-main}"

sudo -u "${APP_USER}" git -C "${APP_DIR}" fetch origin
sudo -u "${APP_USER}" git -C "${APP_DIR}" checkout "${GIT_BRANCH}"
sudo -u "${APP_USER}" git -C "${APP_DIR}" pull --ff-only origin "${GIT_BRANCH}"
sudo -u "${APP_USER}" "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"
sudo systemctl restart "${SERVICE_NAME}"
sudo systemctl status "${SERVICE_NAME}" --no-pager