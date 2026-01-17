#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   curl -fsSL https://your-domain/install_hetzner_monitor.sh | bash
# or
#   curl -fsSL https://your-domain/install_hetzner_monitor.sh | bash -s -- /opt/hetzner-web/automation

TARGET_DIR="${1:-/opt/hetzner-web/automation}"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run as root (sudo)." >&2
  exit 1
fi

echo "Installing to ${TARGET_DIR}"
mkdir -p "${TARGET_DIR}"

cp -r /opt/hetzner-web/automation/* "${TARGET_DIR}/"

chmod +x "${TARGET_DIR}/install.sh"
cd "${TARGET_DIR}"
./install.sh

echo "Done."
