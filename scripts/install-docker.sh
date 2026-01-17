#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/liuweiqiang0523/Hetzner-Web.git}"
BRANCH="${BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/hetzner-web}"

info() {
  printf '[install] %s\n' "$1"
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Missing command: %s\n' "$1" >&2
    exit 1
  fi
}

need_cmd git
need_cmd docker

if docker compose version >/dev/null 2>&1; then
  COMPOSE='docker compose'
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE='docker-compose'
else
  printf 'Missing docker compose plugin (docker compose) or docker-compose\n' >&2
  exit 1
fi

if [ ! -d "$INSTALL_DIR" ]; then
  info "Cloning to $INSTALL_DIR"
  git clone --depth 1 -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
elif [ -d "$INSTALL_DIR/.git" ]; then
  info "Updating existing repo in $INSTALL_DIR"
  git -C "$INSTALL_DIR" pull --ff-only
else
  printf 'Install dir exists but is not a git repo: %s\n' "$INSTALL_DIR" >&2
  exit 1
fi

cd "$INSTALL_DIR"

if [ ! -f config.yaml ]; then
  info 'Creating config.yaml from example'
  cp config.example.yaml config.yaml
fi

if [ ! -f web_config.json ]; then
  info 'Creating web_config.json from example'
  cp web_config.example.json web_config.json
fi

if [ ! -f report_state.json ]; then
  info 'Creating report_state.json from example'
  cp report_state.example.json report_state.json
fi

info 'Build and start containers'
$COMPOSE up -d --build

info 'Done. Please edit config.yaml and web_config.json if needed.'
