#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/antiduplic}"
APP_USER="${APP_USER:-www-data}"
APP_GROUP="${APP_GROUP:-www-data}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
GIT_REPO_URL="${GIT_REPO_URL:?Debes definir GIT_REPO_URL}"
GIT_BRANCH="${GIT_BRANCH:-main}"

if command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
else
  SUDO=""
fi

run_as_app_user() {
  if [ "$(id -un)" = "${APP_USER}" ]; then
    "$@"
  else
    ${SUDO} -u "${APP_USER}" "$@"
  fi
}

install_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    ${SUDO} apt-get update
    ${SUDO} apt-get install -y git ${PYTHON_BIN} ${PYTHON_BIN}-venv python3-pip docker.io docker-compose-plugin curl
    return
  fi

  if command -v dnf >/dev/null 2>&1; then
    ${SUDO} dnf install -y git python3 python3-pip python3-virtualenv podman podman-compose curl
    return
  fi

  if command -v yum >/dev/null 2>&1; then
    ${SUDO} yum install -y git python3 python3-pip python3-virtualenv podman curl
    ${SUDO} python3 -m pip install podman-compose
    return
  fi

  echo "No se detectó un gestor de paquetes soportado (apt, dnf, yum)."
  exit 1
}

install_packages

if [ ! -d "${APP_DIR}" ]; then
  ${SUDO} mkdir -p "${APP_DIR}"
fi

${SUDO} chown -R "${APP_USER}:${APP_GROUP}" "${APP_DIR}"

if [ ! -d "${APP_DIR}/.git" ]; then
  run_as_app_user git clone -b "${GIT_BRANCH}" "${GIT_REPO_URL}" "${APP_DIR}"
else
  run_as_app_user git -C "${APP_DIR}" fetch origin
  run_as_app_user git -C "${APP_DIR}" checkout "${GIT_BRANCH}"
  run_as_app_user git -C "${APP_DIR}" pull --ff-only origin "${GIT_BRANCH}"
fi

run_as_app_user ${PYTHON_BIN} -m venv "${APP_DIR}/.venv"
run_as_app_user "${APP_DIR}/.venv/bin/pip" install --upgrade pip
run_as_app_user "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

if [ ! -f "${APP_DIR}/.env" ]; then
  run_as_app_user cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
  echo "Se creó ${APP_DIR}/.env. Debes editarlo antes de levantar el servicio."
fi

if systemctl list-unit-files | grep -q '^docker.service'; then
  ${SUDO} systemctl enable docker
  ${SUDO} systemctl start docker
elif systemctl list-unit-files | grep -q '^podman.service'; then
  ${SUDO} systemctl enable podman
  ${SUDO} systemctl start podman || true
fi

echo "Bootstrap completado. Edita ${APP_DIR}/.env y luego ejecuta deploy/install_service.sh"