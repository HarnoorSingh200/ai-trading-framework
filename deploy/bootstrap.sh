#!/usr/bin/env bash
#
# Install or update the trading framework on an instance that is already
# running. Idempotent: the first run installs everything, later runs pull the
# current revision and reinstall.
#
#   sudo FRAMEWORK_REPO_URL=https://github.com/you/trading-framework-v3.git \
#       bash deploy/bootstrap.sh
#
# Without FRAMEWORK_REPO_URL the script expects the tree to be at
# $FRAMEWORK_ROOT already (rsync'd from a development machine) and only does
# the virtual environment, unit and service parts.

set -euo pipefail

REPO_URL="${FRAMEWORK_REPO_URL:-}"
REPO_REF="${FRAMEWORK_REPO_REF:-main}"
TARGET="${FRAMEWORK_ROOT:-/opt/trading-framework-v3}"
SERVICE_USER="${FRAMEWORK_USER:-trading}"
SERVICE="trading-framework.service"

log() { printf '%s\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
  log "run me with sudo: sudo bash $0"
  exit 1
fi

id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$SERVICE_USER"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$TARGET" /etc/trading-framework

if [ -n "$REPO_URL" ]; then
  if [ ! -d "$TARGET/.git" ]; then
    runuser -u "$SERVICE_USER" -- git clone --branch "$REPO_REF" "$REPO_URL" "$TARGET"
  else
    runuser -u "$SERVICE_USER" -- git -C "$TARGET" fetch --all
    runuser -u "$SERVICE_USER" -- git -C "$TARGET" checkout "$REPO_REF"
    runuser -u "$SERVICE_USER" -- git -C "$TARGET" pull --ff-only
  fi
fi

if [ ! -f "$TARGET/pyproject.toml" ]; then
  log "no pyproject.toml under $TARGET: set FRAMEWORK_REPO_URL, or rsync the tree there first"
  exit 1
fi

runuser -u "$SERVICE_USER" -- python3 -m venv "$TARGET/.venv"
runuser -u "$SERVICE_USER" -- "$TARGET/.venv/bin/pip" install --upgrade pip
runuser -u "$SERVICE_USER" -- "$TARGET/.venv/bin/pip" install "$TARGET"

install -m 0644 "$TARGET/deploy/trading-framework.service" "/etc/systemd/system/$SERVICE"
systemctl daemon-reload
systemctl enable "$SERVICE"

if [ -f "$TARGET/deploy/trading-site.service" ] && command -v npm >/dev/null 2>&1; then
  log "installing the operator site's dependencies"
  runuser -u "$SERVICE_USER" -- bash -lc "cd '$TARGET/ui' && npm ci --no-audit --no-fund"
  install -m 0644 "$TARGET/deploy/trading-site.service" /etc/systemd/system/trading-site.service
  systemctl daemon-reload
  systemctl enable trading-site.service
  systemctl restart trading-site.service
fi

if [ -f /etc/trading-framework/secrets.env ]; then
  log "secrets found: (re)starting the framework"
  systemctl restart "$SERVICE"
else
  log "no /etc/trading-framework/secrets.env yet: place it, then 'systemctl start trading-framework'"
fi

log "done. status: systemctl status $SERVICE --no-pager"
