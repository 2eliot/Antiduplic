#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/antiduplic}"
APP_USER="${APP_USER:-www-data}"
APP_GROUP="${APP_GROUP:-www-data}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
GIT_REPO_URL="${GIT_REPO_URL:?Debes definir GIT_REPO_URL}"
GIT_BRANCH="${GIT_BRANCH:-main}"

sudo apt-get update
sudo apt-get install -y git ${PYTHON_BIN} ${PYTHON_BIN}-venv python3-pip docker.io docker-compose-plugin

if [ ! -d "${APP_DIR}" ]; then
  sudo mkdir -p "${APP_DIR}"
fi

sudo chown -R "${APP_USER}:${APP_GROUP}" "${APP_DIR}"

if [ ! -d "${APP_DIR}/.git" ]; then
  sudo -u "${APP_USER}" git clone -b "${GIT_BRANCH}" "${GIT_REPO_URL}" "${APP_DIR}"
else
  sudo -u "${APP_USER}" git -C "${APP_DIR}" fetch origin
  sudo -u "${APP_USER}" git -C "${APP_DIR}" checkout "${GIT_BRANCH}"
  sudo -u "${APP_USER}" git -C "${APP_DIR}" pull --ff-only origin "${GIT_BRANCH}"
fi

sudo -u "${APP_USER}" ${PYTHON_BIN} -m venv "${APP_DIR}/.venv"
sudo -u "${APP_USER}" "${APP_DIR}/.venv/bin/pip" install --upgrade pip
sudo -u "${APP_USER}" "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

if [ ! -f "${APP_DIR}/.env" ]; then
  sudo -u "${APP_USER}" cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
  echo "Se creó ${APP_DIR}/.env. Debes editarlo antes de levantar el servicio."
fi

sudo systemctl enable docker
sudo systemctl start docker

echo "Bootstrap completado. Edita ${APP_DIR}/.env y luego ejecuta deploy/install_service.sh"