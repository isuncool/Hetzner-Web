#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   curl -fsSL https://raw.githubusercontent.com/liuweiqiang0523/Hetzner-Web/main/automation/install_hetzner_monitor.sh | sudo bash
#   curl -fsSL https://raw.githubusercontent.com/liuweiqiang0523/Hetzner-Web/main/automation/install_hetzner_monitor.sh | sudo bash -s -- /opt/hetzner-web

TARGET_DIR="${1:-/opt/hetzner-web}"
REPO_URL="https://github.com/liuweiqiang0523/Hetzner-Web.git"
BRANCH="main"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run as root (sudo)." >&2
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  echo "git is required. Please install git first." >&2
  exit 1
fi

if [[ -d "${TARGET_DIR}/.git" ]]; then
  echo "Updating existing repo in ${TARGET_DIR}"
  git -C "${TARGET_DIR}" fetch --all
  git -C "${TARGET_DIR}" reset --hard "origin/${BRANCH}"
else
  echo "Cloning into ${TARGET_DIR}"
  git clone --branch "${BRANCH}" "${REPO_URL}" "${TARGET_DIR}"
fi

AUTOMATION_DIR="${TARGET_DIR}/automation"
if [[ ! -d "${AUTOMATION_DIR}" ]]; then
  echo "Missing automation directory in ${TARGET_DIR}. Please check the repo contents." >&2
  exit 1
fi

write_map_yaml() {
  local input="$1"
  local indent="$2"
  IFS=',' read -ra PAIRS <<< "${input}"
  for pair in "${PAIRS[@]}"; do
    if [[ -z "${pair}" ]]; then
      continue
    fi
    local key="${pair%%=*}"
    local val="${pair#*=}"
    printf "%s\"%s\": \"%s\"\n" "${indent}" "${key}" "${val}"
  done
}

if [[ -n "${HETZNER_API_TOKEN:-}" ]]; then
  echo "Writing config.yaml from environment variables"
  cat > "${AUTOMATION_DIR}/config.yaml" <<EOF
hetzner:
  api_token: "${HETZNER_API_TOKEN}"

traffic:
  limit_gb: ${LIMIT_GB:-18000}
  check_interval: ${CHECK_INTERVAL:-5}
  exceed_action: "${EXCEED_ACTION:-delete_rebuild}"
  confirm_before_delete: false
  warning_thresholds:
    - 10
    - 20
    - 30
    - 40
    - 50
    - 60
    - 70
    - 80
    - 90
    - 95
    - 100

scheduler:
  enabled: false
  tasks: []

telegram:
  enabled: true
  bot_token: "${TELEGRAM_BOT_TOKEN:-}"
  chat_id: "${TELEGRAM_CHAT_ID:-}"
  notify_on:
    - traffic_warning
    - traffic_exceeded
    - server_deleted

cloudflare:
  api_token: "${CF_API_TOKEN:-}"
  zone_id: "${CF_ZONE_ID:-}"
  record_map:
EOF
  if [[ -n "${CF_RECORD_MAP:-}" ]]; then
    write_map_yaml "${CF_RECORD_MAP}" "    " >> "${AUTOMATION_DIR}/config.yaml"
  else
    echo "    \"SERVER_ID\": \"host.example.com\"" >> "${AUTOMATION_DIR}/config.yaml"
  fi

  cat >> "${AUTOMATION_DIR}/config.yaml" <<EOF

notifications:
  email:
    enabled: false
    smtp_server: "smtp.gmail.com"
    smtp_port: 587
    username: ""
    password: ""
    to_addresses: []

logging:
  level: "INFO"
  file: "hetzner_monitor.log"
  max_size_mb: 10
  backup_count: 5

whitelist:
  server_ids: []
  server_names: []

server_template:
  server_type: "${SERVER_TYPE:-cx43}"
  location: "${LOCATION:-nbg1}"
  ssh_keys: []
  name_prefix: "auto-"
  use_original_name: true

snapshot_map:
EOF
  if [[ -n "${SNAPSHOT_MAP:-}" ]]; then
    write_map_yaml "${SNAPSHOT_MAP}" "  " >> "${AUTOMATION_DIR}/config.yaml"
  else
    echo "  SERVER_ID: SNAPSHOT_ID" >> "${AUTOMATION_DIR}/config.yaml"
  fi
else
  echo "No HETZNER_API_TOKEN provided; using config.example.yaml"
fi

chmod +x "${AUTOMATION_DIR}/install.sh"
cd "${AUTOMATION_DIR}"
./install.sh

echo "Done."
