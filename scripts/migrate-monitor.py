#!/usr/bin/env python3
import ast
import os
import sys
import time

import yaml


WANT = {
    "HETZNER_TOKEN",
    "TG_BOT_TOKEN",
    "TG_CHAT_ID",
    "CF_ENABLE",
    "CF_API_TOKEN",
    "NOTIFY_LEVELS",
    "CHECK_INTERVAL",
    "DAILY_REPORT_TIME",
    "SERVERS",
}


def _load_assignments(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    values = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in WANT:
                try:
                    values[target.id] = ast.literal_eval(node.value)
                except Exception:
                    pass
    return values


def _load_yaml(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _dump_yaml(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=False)


def _ensure_dict(root: dict, key: str) -> dict:
    val = root.get(key)
    if not isinstance(val, dict):
        val = {}
        root[key] = val
    return val


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else "/root/hetzner_monitor/main.py"
    dest = sys.argv[2] if len(sys.argv) > 2 else "/opt/hetzner-web/config.yaml"
    example = "/opt/hetzner-web/config.example.yaml"

    values = _load_assignments(src)
    config = _load_yaml(dest) or _load_yaml(example)

    hetzner = _ensure_dict(config, "hetzner")
    if values.get("HETZNER_TOKEN"):
        hetzner["api_token"] = values["HETZNER_TOKEN"]

    traffic = _ensure_dict(config, "traffic")
    servers = values.get("SERVERS") or []
    limit_tb = None
    for server in servers:
        try:
            limit_tb = max(limit_tb or 0, float(server.get("limit_tb") or 0))
        except Exception:
            continue
    if limit_tb:
        traffic["limit_gb"] = int(limit_tb * 1024)
    if values.get("CHECK_INTERVAL"):
        try:
            minutes = max(1, int(round(float(values["CHECK_INTERVAL"]) / 60)))
            traffic["check_interval"] = minutes
        except Exception:
            pass
    traffic["exceed_action"] = "rebuild"

    telegram = _ensure_dict(config, "telegram")
    telegram["enabled"] = True
    if values.get("TG_BOT_TOKEN"):
        telegram["bot_token"] = values["TG_BOT_TOKEN"]
    if values.get("TG_CHAT_ID"):
        telegram["chat_id"] = str(values["TG_CHAT_ID"])
    if values.get("NOTIFY_LEVELS"):
        telegram["notify_levels"] = values["NOTIFY_LEVELS"]
    if values.get("DAILY_REPORT_TIME"):
        telegram["daily_report_time"] = values["DAILY_REPORT_TIME"]

    cloudflare = _ensure_dict(config, "cloudflare")
    if values.get("CF_API_TOKEN"):
        cloudflare["api_token"] = values["CF_API_TOKEN"]
    cloudflare["sync_on_start"] = bool(values.get("CF_ENABLE"))

    record_map = _ensure_dict(cloudflare, "record_map")
    first_zone = cloudflare.get("zone_id")
    for server in servers:
        name = server.get("name")
        record = server.get("cf_domain")
        zone_id = server.get("cf_zone_id")
        if name and record and zone_id:
            record_map[name] = {"record": record, "zone_id": zone_id, "api_token": cloudflare.get("api_token")}
            if not first_zone:
                first_zone = zone_id
    if first_zone:
        cloudflare["zone_id"] = first_zone

    rebuild = _ensure_dict(config, "rebuild")
    snapshot_map = _ensure_dict(rebuild, "snapshot_id_map")
    for server in servers:
        name = server.get("name")
        snapshot_id = server.get("snapshot_id")
        if name and snapshot_id:
            snapshot_map[name] = snapshot_id

    if os.path.exists(dest):
        backup = f"{dest}.bak.{time.strftime('%Y%m%d%H%M%S')}"
        os.replace(dest, backup)

    _dump_yaml(dest, config)
    print(f"Updated {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
