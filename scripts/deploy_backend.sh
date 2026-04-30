#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${1:-${VPS_PROJECT_PATH:-${SOVEREIGN_PROJECT_PATH:-/opt/project-sovereign}}}"
BACKEND_SERVICE="${SOVEREIGN_BACKEND_SERVICE:-sovereign-backend.service}"
WORKER_SERVICE="${SOVEREIGN_WORKER_SERVICE:-sovereign-worker.service}"
HEALTH_URL="${SOVEREIGN_HEALTH_URL:-http://127.0.0.1:8000/health}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
TEST_COMMAND="${SOVEREIGN_DEPLOY_TEST_COMMAND:-python -m pytest tests/test_shared_transport.py tests/test_slack_interface.py tests/test_operator_console_api.py tests/test_operator_loop.py}"

echo "Deploying Project Sovereign backend"
echo "Project directory: ${PROJECT_DIR}"
cd "${PROJECT_DIR}"

echo "Updating repository"
git pull --ff-only

if [ ! -d "venv" ]; then
  echo "Creating virtual environment"
  "${PYTHON_BIN}" -m venv venv
fi

if [ -f "venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "venv/bin/activate"
elif [ -f "venv/Scripts/activate" ]; then
  # shellcheck disable=SC1091
  source "venv/Scripts/activate"
else
  echo "Could not find venv activation script" >&2
  exit 1
fi

echo "Installing Python dependencies"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "Running deploy test command"
eval "${TEST_COMMAND}"

echo "Restarting backend service: ${BACKEND_SERVICE}"
sudo systemctl restart "${BACKEND_SERVICE}"

if [ -n "${WORKER_SERVICE}" ]; then
  echo "Restarting worker service: ${WORKER_SERVICE}"
  sudo systemctl restart "${WORKER_SERVICE}"
fi

echo "Running health check"
python scripts/health_check.py --url "${HEALTH_URL}"

echo "Backend deploy completed"
