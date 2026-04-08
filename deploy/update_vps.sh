#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/antiduplic}"
APP_USER="${APP_USER:-www-data}"
SERVICE_NAME="${SERVICE_NAME:-antiduplic}"
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

run_as_app_user git -C "${APP_DIR}" fetch origin
run_as_app_user git -C "${APP_DIR}" checkout "${GIT_BRANCH}"
run_as_app_user git -C "${APP_DIR}" pull --ff-only origin "${GIT_BRANCH}"
run_as_app_user "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"
${SUDO} systemctl restart "${SERVICE_NAME}"
${SUDO} systemctl status "${SERVICE_NAME}" --no-pager