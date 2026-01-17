#!/usr/bin/env bash
set -euo pipefail

curl -fsSL https://raw.githubusercontent.com/liuweiqiang0523/Hetzner-Web/main/automation/install_hetzner_monitor.sh | sudo bash -s -- "$@"
