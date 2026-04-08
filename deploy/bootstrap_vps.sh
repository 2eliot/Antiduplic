#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/antiduplic}"
APP_USER="${APP_USER:-www-data}"
APP_GROUP="${APP_GROUP:-www-data}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
GIT_REPO_URL="${GIT_REPO_URL:?Debes definir GIT_REPO_URL}"
GIT_BRANCH="${GIT_BRANCH:-main}"
REQUIRED_PYTHON_MAJOR=3
REQUIRED_PYTHON_MINOR=9

if command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
else
  SUDO=""
fi

run_as_app_user() {
  if [ "$(id -un)" = "${APP_USER}" ]; then
    "$@"
  elif [ -n "${SUDO}" ]; then
    ${SUDO} -u "${APP_USER}" "$@"
  else
    su -s /bin/bash - "${APP_USER}" -c "$(printf '%q ' "$@")"
  fi
}

python_is_compatible() {
  local candidate="$1"
  "${candidate}" -c "import sys; raise SystemExit(0 if sys.version_info >= (${REQUIRED_PYTHON_MAJOR}, ${REQUIRED_PYTHON_MINOR}) else 1)"
}

resolve_python_bin() {
  local candidates=()

  if command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    candidates+=("${PYTHON_BIN}")
  fi

  for candidate in python3.12 python3.11 python3.10 python3.9 python39 python3; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      candidates+=("${candidate}")
    fi
  done

  for candidate in "${candidates[@]}"; do
    if python_is_compatible "${candidate}"; then
      PYTHON_BIN="${candidate}"
      return
    fi
  done

  echo "No se encontró Python ${REQUIRED_PYTHON_MAJOR}.${REQUIRED_PYTHON_MINOR}+ en el servidor."
  echo "Instala python3.9, python3.10 o python3.11 y luego reintenta exportando PYTHON_BIN=python3.9, PYTHON_BIN=python39, PYTHON_BIN=python3.10 o PYTHON_BIN=python3.11."
  exit 1
}

install_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    ${SUDO} apt-get update
    ${SUDO} apt-get install -y git python3 python3-venv python3-pip docker.io docker-compose-plugin curl
    return
  fi

  if command -v dnf >/dev/null 2>&1; then
    ${SUDO} dnf install -y git podman podman-compose curl python3.11 python3.11-pip || \
      ${SUDO} dnf install -y git podman podman-compose curl python3.10 python3.10-pip || \
      ${SUDO} dnf install -y git podman podman-compose curl python3.9 python3.9-pip || \
      ${SUDO} dnf install -y git podman podman-compose curl python39 python39-pip || \
      ${SUDO} dnf install -y git python3 python3-pip python3-virtualenv podman podman-compose curl
    return
  fi

  if command -v yum >/dev/null 2>&1; then
    ${SUDO} yum install -y git python3 python3-pip python3-virtualenv podman curl
    if ! command -v podman-compose >/dev/null 2>&1; then
      ${SUDO} python3 -m pip install podman-compose
    fi
    return
  fi

  echo "No se detectó un gestor de paquetes soportado (apt, dnf, yum)."
  exit 1
}

install_packages
resolve_python_bin

echo "Usando ${PYTHON_BIN}: $(${PYTHON_BIN} --version 2>&1)"

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