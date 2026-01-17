from __future__ import annotations

import base64
import json
import os
import socket
import threading
import time
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

import requests
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_ROOT, "static")

CONFIG_PATH = os.environ.get("HETZNER_CONFIG_PATH", "/app/config.yaml")
WEB_CONFIG_PATH = os.environ.get("WEB_CONFIG_PATH", "/app/web_config.json")
REPORT_STATE_PATH = os.environ.get("REPORT_STATE_PATH", "/app/report_state.json")

ALERT_STATE: Dict[str, Dict[str, Optional[float]]] = {}
REBUILD_LOCKS: Dict[str, threading.Lock] = {}
SCHEDULE_STATE: Dict[str, Optional[str]] = {"last_daily_report": None}
BOT_STATE: Dict[str, int] = {"update_offset": 0}


def _load_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def _load_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)


def _now_local() -> datetime:
    return datetime.now().astimezone()


def _bytes_to_tb(value_bytes: float) -> Decimal:
    return (Decimal(value_bytes) / (Decimal(1024) ** 4)).quantize(
        Decimal("0.001"), rounding=ROUND_HALF_UP
    )


def _quantize_tb(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def _date_from_hour_key(key: str) -> Optional[str]:
    if not key:
        return None
    return key.split(" ", 1)[0] if " " in key else None


def _merge_hourly_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}

    def _sum_optional(a: Optional[float], b: Optional[float]) -> Optional[float]:
        if a is None and b is None:
            return None
        if a is None:
            return float(b)
        if b is None:
            return float(a)
        return float(a) + float(b)

    for sid, data in snapshot.items():
        name = data.get("name") or str(sid)
        entry = merged.setdefault(
            name, {"name": name, "outbound_bytes": None, "inbound_bytes": None}
        )
        entry["outbound_bytes"] = _sum_optional(entry.get("outbound_bytes"), data.get("outbound_bytes"))
        entry["inbound_bytes"] = _sum_optional(entry.get("inbound_bytes"), data.get("inbound_bytes"))
    return merged


def _merge_hourly_series(hourly: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {key: _merge_hourly_snapshot(snapshot) for key, snapshot in hourly.items()}


def _parse_hour(key: str) -> Optional[int]:
    try:
        return datetime.strptime(key, "%Y-%m-%d %H:%M").hour
    except Exception:
        return None


def _compute_cycle_data(
    hourly: Dict[str, Any],
    include_ids: Optional[set] = None,
    name_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    keys = sorted(hourly.keys())
    if len(keys) < 2:
        return {"servers": {}}

    server_ids = set()
    for snapshot in hourly.values():
        server_ids.update(snapshot.keys())
    if include_ids:
        server_ids = {sid for sid in server_ids if str(sid) in include_ids}

    servers: Dict[str, Any] = {}
    for sid in server_ids:
        cycle_out = Decimal("0.000")
        cycle_age = 0
        points: List[Dict[str, Any]] = []
        rebuilds: List[str] = []
        name = name_map.get(str(sid)) if name_map else None

        for i in range(1, len(keys)):
            prev_key = keys[i - 1]
            curr_key = keys[i]
            prev = hourly.get(prev_key, {})
            curr = hourly.get(curr_key, {})
            prev_data = prev.get(sid)
            curr_data = curr.get(sid)
            if curr_data and not name:
                name = curr_data.get("name") or str(sid)

            rebuild = False
            if prev_data and curr_data:
                prev_out = prev_data.get("outbound_bytes")
                curr_out = curr_data.get("outbound_bytes")
                if prev_out is not None and curr_out is not None and float(curr_out) < float(prev_out):
                    rebuild = True
            if rebuild:
                cycle_out = Decimal("0.000")
                cycle_age = 0
                rebuilds.append(curr_key)

            deltas = _delta_by_name(prev, curr)
            data = deltas.get(sid, {})
            total_out = data["out"] if data.get("has_out") else Decimal("0.000")
            cycle_out += total_out
            cycle_out = _quantize_tb(cycle_out)
            points.append(
                {
                    "time": curr_key,
                    "out_tb_h": str(_quantize_tb(total_out)),
                    "cycle_out_cum_tb": str(cycle_out),
                    "cycle_age_h": cycle_age,
                    "hour_of_day": _parse_hour(curr_key),
                }
            )
            cycle_age += 1

        if points:
            servers[str(sid)] = {"name": name or str(sid), "points": points, "rebuilds": rebuilds}

    return {"servers": servers}

def _delta_by_name(prev: Dict[str, Any], curr: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    aggregates: Dict[str, Dict[str, Any]] = {}
    for sid, data in curr.items():
        prev_data = prev.get(sid, {})
        name = data.get("name") or prev_data.get("name") or str(sid)
        prev_out = prev_data.get("outbound_bytes")
        curr_out = data.get("outbound_bytes")
        prev_in = prev_data.get("inbound_bytes")
        curr_in = data.get("inbound_bytes")
        out_delta = None
        in_delta = None
        if prev_out is not None and curr_out is not None and float(curr_out) >= float(prev_out):
            out_delta = _bytes_to_tb(float(curr_out) - float(prev_out))
        if prev_in is not None and curr_in is not None and float(curr_in) >= float(prev_in):
            in_delta = _bytes_to_tb(float(curr_in) - float(prev_in))
        entry = aggregates.setdefault(
            name, {"out": Decimal("0.000"), "in": Decimal("0.000"), "has_out": False, "has_in": False}
        )
        if out_delta is not None:
            entry["out"] += out_delta
            entry["has_out"] = True
        if in_delta is not None:
            entry["in"] += in_delta
            entry["has_in"] = True
    return aggregates


def _compute_tracking_totals(
    hourly: Dict[str, Any], start_override: Optional[str] = None
) -> Dict[str, Optional[str]]:
    keys = sorted(hourly.keys())
    if not keys:
        return {"start": None, "outbound_tb": "0.000", "inbound_tb": "0.000"}
    start_idx = 0
    start_label = keys[0]
    if start_override:
        for idx, key in enumerate(keys):
            if key >= start_override:
                start_idx = idx
                start_label = start_override
                break
        else:
            return {"start": start_override, "outbound_tb": "0.000", "inbound_tb": "0.000"}
    total_out = Decimal("0.000")
    total_in = Decimal("0.000")
    for i in range(start_idx + 1, len(keys)):
        prev = hourly.get(keys[i - 1], {})
        curr = hourly.get(keys[i], {})
        deltas = _delta_by_name(prev, curr)
        for data in deltas.values():
            if data.get("has_out"):
                total_out += data["out"]
            if data.get("has_in"):
                total_in += data["in"]
    return {
        "start": start_label,
        "outbound_tb": str(_quantize_tb(total_out)),
        "inbound_tb": str(_quantize_tb(total_in)),
    }


def _detect_last_rebuilds(hourly: Dict[str, Any]) -> Dict[str, str]:
    keys = sorted(hourly.keys())
    last: Dict[str, str] = {}
    prev_out: Dict[str, float] = {}
    for key in keys:
        snapshot = hourly.get(key, {})
        for sid, data in snapshot.items():
            out = data.get("outbound_bytes")
            if out is None:
                continue
            try:
                current = float(out)
            except Exception:
                continue
            prev = prev_out.get(str(sid))
            if prev is not None and current < prev:
                last[str(sid)] = key
            prev_out[str(sid)] = current
    return last


class HetznerClient:
    BASE_URL = "https://api.hetzner.cloud/v1"
    CF_API_BASE = "https://api.cloudflare.com/client/v4"

    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        url = f"{self.BASE_URL}/{endpoint}"
        resp = requests.request(method, url, headers=self.headers, timeout=20, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def get_servers(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "servers")
        return data.get("servers", [])

    def get_server(self, server_id: int) -> Optional[Dict[str, Any]]:
        try:
            data = self._request("GET", f"servers/{server_id}")
            return data.get("server")
        except Exception:
            return None

    def get_server_metrics(self, server_id: int, start: str, end: str) -> Dict[str, Any]:
        try:
            params = {"type": "traffic", "start": start, "end": end}
            data = self._request("GET", f"servers/{server_id}/metrics", params=params)
            return data.get("metrics", {})
        except Exception:
            return {}

    def delete_server(self, server_id: int) -> bool:
        try:
            self._request("DELETE", f"servers/{server_id}")
            return True
        except Exception:
            return False

    def get_snapshots(self) -> List[Dict[str, Any]]:
        try:
            data = self._request("GET", "images", params={"type": "snapshot"})
            snapshots = data.get("images", [])
            snapshots.sort(key=lambda x: x.get("created", ""), reverse=True)
            return snapshots
        except Exception:
            return []

    def rebuild_server(self, server_id: int, config: Dict[str, Any]) -> Dict[str, Any]:
        old_server = self.get_server(server_id)
        if not old_server:
            return {"success": False, "error": "服务器不存在"}

        snapshot_id_map = config.get("rebuild", {}).get("snapshot_id_map", {})
        mapped_id = snapshot_id_map.get(str(server_id)) or snapshot_id_map.get(old_server.get("name"))
        if mapped_id:
            image = mapped_id
        else:
            snapshots = self.get_snapshots()
            if not snapshots:
                return {"success": False, "error": "没有可用快照，已取消重建"}
            image = snapshots[0]["id"]

        if not self.delete_server(server_id):
            return {"success": False, "error": "删除服务器失败"}

        time.sleep(5)
        create_data = {
            "name": old_server["name"],
            "server_type": old_server["server_type"]["name"],
            "image": image,
            "location": old_server["datacenter"]["location"]["name"],
            "start_after_create": True,
        }
        last_error: Optional[Exception] = None
        new_server: Optional[Dict[str, Any]] = None
        for _ in range(3):
            try:
                resp = self._request("POST", "servers", json=create_data)
                new_server = resp.get("server")
                if new_server:
                    break
            except Exception as e:
                last_error = e
                time.sleep(5)
        if not new_server:
            return {"success": False, "error": str(last_error) if last_error else "创建服务器失败"}

        return {
            "success": True,
            "new_server_id": new_server["id"],
            "new_ip": new_server["public_net"]["ipv4"]["ip"],
            "snapshot_id": image,
        }

    def update_cloudflare_a_record(
        self, api_token: str, zone_id: str, record_name: str, ip: str, attempts: int = 3
    ) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        for _ in range(attempts):
            try:
                headers = {
                    "Authorization": f"Bearer {api_token}",
                    "Content-Type": "application/json",
                }
                list_url = f"{self.CF_API_BASE}/zones/{zone_id}/dns_records"
                params = {"type": "A", "name": record_name}
                resp = requests.get(list_url, headers=headers, params=params, timeout=15)
                resp.raise_for_status()
                data = resp.json()
                records = data.get("result", [])
                if not records:
                    return {"success": False, "error": "DNS记录不存在"}
                record = records[0]
                record_id = record.get("id")
                update_url = f"{self.CF_API_BASE}/zones/{zone_id}/dns_records/{record_id}"
                payload = {
                    "type": "A",
                    "name": record_name,
                    "content": ip,
                    "ttl": record.get("ttl", 1),
                    "proxied": record.get("proxied", False),
                }
                upd = requests.put(update_url, headers=headers, json=payload, timeout=15)
                upd.raise_for_status()
                return {"success": True}
            except Exception as e:
                last_error = e
                time.sleep(3)
        return {"success": False, "error": str(last_error)}


def _get_basic_auth(request: Request) -> Optional[tuple]:
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Basic "):
        return None
    raw = auth.split(" ", 1)[1].strip()
    try:
        decoded = base64.b64decode(raw).decode("utf-8")
        if ":" not in decoded:
            return None
        user, pwd = decoded.split(":", 1)
        return user, pwd
    except Exception:
        return None


def _require_auth(request: Request) -> None:
    cfg = _load_json(WEB_CONFIG_PATH)
    auth = _get_basic_auth(request)
    if not auth:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user, pwd = auth
    if user != cfg.get("username") or pwd != cfg.get("password"):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _parse_alert_levels(raw_levels: Any) -> List[int]:
    if isinstance(raw_levels, list):
        levels = []
        for item in raw_levels:
            try:
                levels.append(int(item))
            except Exception:
                continue
        levels = [level for level in levels if level > 0]
        if levels:
            return sorted(set(levels))
    return [80, 90, 95, 100]


def _format_iso(dt: datetime) -> str:
    return dt.isoformat()


def _integrate_time_series(series: List[List[Any]]) -> float:
    total = 0.0
    if not series or len(series) < 2:
        return 0.0
    for i in range(len(series) - 1):
        try:
            value = float(series[i][1])
            t_curr = datetime.fromisoformat(series[i][0].replace("Z", "+00:00"))
            t_next = datetime.fromisoformat(series[i + 1][0].replace("Z", "+00:00"))
            duration = (t_next - t_curr).total_seconds()
            total += value * duration
        except Exception:
            continue
    return total


def _get_today_traffic_bytes(client: "HetznerClient", server_id: int) -> Dict[str, float]:
    now = _now_local()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    metrics = client.get_server_metrics(server_id, start=_format_iso(start), end=_format_iso(now))
    time_series = metrics.get("time_series", {}) if isinstance(metrics, dict) else {}
    out_series = time_series.get("traffic.0.out", [])
    in_series = time_series.get("traffic.0.in", [])
    return {
        "out_bytes": _integrate_time_series(out_series),
        "in_bytes": _integrate_time_series(in_series),
    }


def _send_telegram_message(bot_token: str, chat_id: str, text: str) -> bool:
    if not bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=15)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[alert] telegram send failed: {e}")
        return False


def _send_telegram_markdown(bot_token: str, chat_id: str, text: str) -> bool:
    if not bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[alert] telegram send failed: {e}")
        return False


def _resolve_cf_record(record_cfg: Any, fallback_zone: str, fallback_token: str) -> Optional[Dict[str, str]]:
    if isinstance(record_cfg, str):
        return {"record": record_cfg, "zone_id": fallback_zone, "api_token": fallback_token}
    if isinstance(record_cfg, dict):
        record = record_cfg.get("record") or record_cfg.get("name")
        zone_id = record_cfg.get("zone_id") or fallback_zone
        api_token = record_cfg.get("api_token") or fallback_token
        if record and zone_id and api_token:
            return {"record": record, "zone_id": zone_id, "api_token": api_token}
    return None


def _build_daily_report(config: Dict[str, Any], client: "HetznerClient") -> str:
    traffic_cfg = config.get("traffic", {})
    limit_gb = traffic_cfg.get("limit_gb")
    limit_bytes = None
    if limit_gb:
        try:
            limit_bytes = float(Decimal(limit_gb) * (Decimal(1024) ** 3))
        except Exception:
            limit_bytes = None

    servers = client.get_servers()
    lines = [f"📅 **每日定时战报 ({_now_local().strftime('%Y-%m-%d')})**"]
    for s in servers:
        detail = client.get_server(s["id"]) or {}
        outgoing = detail.get("outgoing_traffic")
        ingoing = detail.get("ingoing_traffic")
        if outgoing is None or ingoing is None:
            lines.append(f"━━━━━━━━━━\n🖥️ `{s.get('name') or s['id']}`\n❌ 获取失败")
            continue
        usage = _get_today_traffic_bytes(client, s["id"])
        percent = None
        if limit_bytes:
            percent = (float(outgoing) / limit_bytes) * 100
        outbound_tb = _bytes_to_tb(float(outgoing))
        inbound_tb = _bytes_to_tb(float(ingoing))
        today_up_gb = Decimal(usage["out_bytes"]) / (Decimal(1024) ** 3)
        today_down_gb = Decimal(usage["in_bytes"]) / (Decimal(1024) ** 3)
        percent_text = f" ({percent:.2f}%)" if percent is not None else ""
        lines.append(
            "━━━━━━━━━━\n"
            f"🖥️ `{detail.get('name') or s.get('name') or s['id']}`\n"
            f"📤 总上传: `{outbound_tb} TB`{percent_text}\n"
            f"📥 总下载: `{inbound_tb} TB`\n"
            f"📈 **今日新增**: ⬆️ `{today_up_gb:.2f} GB` | ⬇️ `{today_down_gb:.2f} GB`"
        )
    return "\n".join(lines)


def _perform_rebuild(
    server_id: int, server_name: str, config: Dict[str, Any], source: str, client: "HetznerClient"
) -> Dict[str, Any]:
    lock = REBUILD_LOCKS.setdefault(str(server_id), threading.Lock())
    if not lock.acquire(blocking=False):
        return {"success": False, "error": "重建正在进行中"}
    try:
        telegram_cfg = config.get("telegram", {})
        bot_token = telegram_cfg.get("bot_token", "")
        chat_id = telegram_cfg.get("chat_id", "")
        if telegram_cfg.get("enabled") and bot_token and chat_id:
            _send_telegram_markdown(
                bot_token,
                chat_id,
                f"🚨 **[{server_name}]** 触发重建 ({source})",
            )

        result = client.rebuild_server(server_id, config)
        if not result.get("success"):
            return result

        cf_cfg = config.get("cloudflare", {})
        record_cfg = (cf_cfg.get("record_map", {}) or {}).get(str(server_id))
        resolved = _resolve_cf_record(record_cfg, cf_cfg.get("zone_id", ""), cf_cfg.get("api_token", ""))
        dns_result = None
        if resolved:
            dns_result = client.update_cloudflare_a_record(
                resolved["api_token"],
                resolved["zone_id"],
                resolved["record"],
                result.get("new_ip", ""),
            )
        if telegram_cfg.get("enabled") and bot_token and chat_id:
            dns_text = ""
            if dns_result:
                dns_text = "✅ DNS 已更新" if dns_result.get("success") else f"❌ DNS 失败: {dns_result.get('error')}"
            _send_telegram_markdown(
                bot_token,
                chat_id,
                f"✅ **[{server_name}]** 重建完成\nIP: `{result.get('new_ip')}`\n{dns_text}",
            )
        return {"success": True, "dns": dns_result}
    finally:
        lock.release()


def _sync_cloudflare_records(config: Dict[str, Any], client: "HetznerClient") -> None:
    cf_cfg = config.get("cloudflare", {})
    if not cf_cfg.get("sync_on_start"):
        return
    record_map = cf_cfg.get("record_map", {}) or {}
    if not record_map:
        return
    servers = client.get_servers()
    for s in servers:
        sid = str(s["id"])
        record_cfg = record_map.get(sid) or record_map.get(s.get("name", ""))
        resolved = _resolve_cf_record(record_cfg, cf_cfg.get("zone_id", ""), cf_cfg.get("api_token", ""))
        if not resolved:
            continue
        ip = None
        public_net = s.get("public_net", {})
        if public_net.get("ipv4"):
            ip = public_net["ipv4"].get("ip")
        if not ip:
            detail = client.get_server(s["id"]) or {}
            if detail.get("public_net", {}).get("ipv4"):
                ip = detail["public_net"]["ipv4"].get("ip")
        if not ip:
            continue
        client.update_cloudflare_a_record(resolved["api_token"], resolved["zone_id"], resolved["record"], ip)


def _monitor_traffic_loop() -> None:
    while True:
        try:
            config = _load_yaml(CONFIG_PATH)
            traffic_cfg = config.get("traffic", {})
            telegram_cfg = config.get("telegram", {})
            enabled = bool(telegram_cfg.get("enabled"))
            limit_gb = traffic_cfg.get("limit_gb")
            bot_token = telegram_cfg.get("bot_token", "")
            chat_id = telegram_cfg.get("chat_id", "")
            exceed_action = traffic_cfg.get("exceed_action", "")
            check_interval = traffic_cfg.get("check_interval", 5)
            interval_seconds = max(30, int(check_interval) * 60)

            if not limit_gb:
                time.sleep(interval_seconds)
                continue

            try:
                limit_bytes = float(Decimal(limit_gb) * (Decimal(1024) ** 3))
            except Exception:
                time.sleep(interval_seconds)
                continue

            levels = _parse_alert_levels(telegram_cfg.get("notify_levels"))
            client = HetznerClient(config["hetzner"]["api_token"])
            servers = client.get_servers()

            for s in servers:
                sid = str(s["id"])
                detail = client.get_server(s["id"]) or {}
                outgoing = detail.get("outgoing_traffic")
                if outgoing is None:
                    continue
                percent = (float(outgoing) / limit_bytes) * 100
                state = ALERT_STATE.setdefault(
                    sid, {"last_level": 0, "last_outgoing": None, "auto_rebuild": False}
                )
                last_outgoing = state.get("last_outgoing")
                if last_outgoing is not None and float(outgoing) < float(last_outgoing):
                    state["last_level"] = 0
                    state["auto_rebuild"] = False
                state["last_outgoing"] = float(outgoing)

                reached = [level for level in levels if percent >= level]
                if not reached:
                    continue
                new_level = max(reached)
                if int(new_level) <= int(state.get("last_level") or 0):
                    continue

                outbound_tb = _bytes_to_tb(float(outgoing))
                server_name = detail.get("name") or s.get("name") or sid
                message = (
                    f"[Hetzner-Web] {server_name} 流量提醒: {new_level}%\n"
                    f"出站: {outbound_tb} TB\n"
                    f"阈值: {limit_gb} GB"
                )
                if enabled and bot_token and chat_id:
                    if _send_telegram_message(bot_token, chat_id, message):
                        state["last_level"] = int(new_level)

                if exceed_action == "rebuild" and float(outgoing) >= limit_bytes:
                    if not state.get("auto_rebuild"):
                        server_name = detail.get("name") or s.get("name") or sid
                        result = _perform_rebuild(
                            s["id"],
                            server_name,
                            config,
                            "流量超标自动重建",
                            client,
                        )
                        if result.get("success"):
                            state["auto_rebuild"] = True
        except Exception as e:
            print(f"[alert] monitor error: {e}")
        time.sleep(interval_seconds)


def _daily_report_loop() -> None:
    while True:
        try:
            config = _load_yaml(CONFIG_PATH)
            telegram_cfg = config.get("telegram", {})
            if not telegram_cfg.get("enabled"):
                time.sleep(30)
                continue
            daily_time = telegram_cfg.get("daily_report_time")
            bot_token = telegram_cfg.get("bot_token", "")
            chat_id = telegram_cfg.get("chat_id", "")
            if not daily_time or not bot_token or not chat_id:
                time.sleep(30)
                continue
            now = _now_local()
            current_time = now.strftime("%H:%M")
            current_date = now.strftime("%Y-%m-%d")
            if current_time == daily_time and SCHEDULE_STATE.get("last_daily_report") != current_date:
                client = HetznerClient(config["hetzner"]["api_token"])
                report = _build_daily_report(config, client)
                _send_telegram_markdown(bot_token, chat_id, report)
                SCHEDULE_STATE["last_daily_report"] = current_date
        except Exception as e:
            print(f"[alert] daily report error: {e}")
        time.sleep(30)


def _handle_bot_command(text: str, config: Dict[str, Any], client: "HetznerClient") -> str:
    cmd = (text or "").strip()
    if cmd.startswith("/start") or cmd.startswith("/help"):
        return "🤖 监控已启动\n/status 或 /ll 查看战报\n/rebuild 服务器名 执行重建"
    if cmd.startswith("/ll") or cmd.startswith("/status"):
        return _build_daily_report(config, client)
    if cmd.startswith("/rebuild"):
        parts = cmd.split(maxsplit=1)
        if len(parts) < 2:
            return "⚠️ 用法: /rebuild 服务器名"
        name = parts[1].strip()
        servers = client.get_servers()
        target = next((s for s in servers if s.get("name") == name), None)
        if not target:
            return "❌ 找不到该服务器"
        result = _perform_rebuild(target["id"], name, config, "Telegram 指令", client)
        if result.get("success"):
            return f"✅ 已触发 {name} 重建"
        return f"❌ 重建失败: {result.get('error', '未知错误')}"
    return "⚠️ 未知指令"


def _telegram_bot_loop() -> None:
    while True:
        try:
            config = _load_yaml(CONFIG_PATH)
            telegram_cfg = config.get("telegram", {})
            if not telegram_cfg.get("enabled"):
                time.sleep(10)
                continue
            bot_token = telegram_cfg.get("bot_token", "")
            chat_id = str(telegram_cfg.get("chat_id", "")).strip()
            if not bot_token or not chat_id:
                time.sleep(10)
                continue

            offset = BOT_STATE.get("update_offset", 0)
            url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
            resp = requests.get(url, params={"timeout": 25, "offset": offset}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                time.sleep(10)
                continue
            for update in data.get("result", []):
                update_id = update.get("update_id")
                if update_id is not None:
                    BOT_STATE["update_offset"] = update_id + 1
                message = update.get("message") or {}
                if not message:
                    continue
                if str(message.get("chat", {}).get("id")) != chat_id:
                    continue
                text = message.get("text", "")
                if not text:
                    continue
                client = HetznerClient(config["hetzner"]["api_token"])
                reply = _handle_bot_command(text, config, client)
                _send_telegram_markdown(bot_token, chat_id, reply)
        except Exception as e:
            print(f"[alert] telegram bot error: {e}")
        time.sleep(3)


app = FastAPI()
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def _start_traffic_monitor() -> None:
    threading.Thread(target=_monitor_traffic_loop, daemon=True).start()
    threading.Thread(target=_daily_report_loop, daemon=True).start()
    threading.Thread(target=_telegram_bot_loop, daemon=True).start()
    def _sync_wrapper() -> None:
        try:
            config = _load_yaml(CONFIG_PATH)
            client = HetznerClient(config["hetzner"]["api_token"])
            _sync_cloudflare_records(config, client)
        except Exception as e:
            print(f"[alert] cloudflare sync error: {e}")
    threading.Thread(target=_sync_wrapper, daemon=True).start()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/servers")
def api_servers(request: Request) -> JSONResponse:
    _require_auth(request)
    config = _load_yaml(CONFIG_PATH)
    client = HetznerClient(config["hetzner"]["api_token"])
    servers = client.get_servers()
    traffic_cfg = config.get("traffic", {})
    limit_gb = traffic_cfg.get("limit_gb")
    limit_tb = None
    if limit_gb:
        try:
            limit_tb = _quantize_tb(Decimal(limit_gb) / Decimal(1024))
        except Exception:
            limit_tb = None
    rows = []
    for s in servers:
        detail = client.get_server(s["id"]) or {}
        outgoing = detail.get("outgoing_traffic")
        ingoing = detail.get("ingoing_traffic")
        outbound_tb = _bytes_to_tb(float(outgoing)) if outgoing is not None else Decimal("0.000")
        inbound_tb = _bytes_to_tb(float(ingoing)) if ingoing is not None else Decimal("0.000")
        rows.append(
            {
                "id": s["id"],
                "name": s["name"],
                "status": s["status"],
                "ip": s["public_net"]["ipv4"]["ip"] if s["public_net"].get("ipv4") else None,
                "server_type": s["server_type"]["name"],
                "location": s["datacenter"]["location"]["name"],
                "outbound_tb": str(outbound_tb),
                "inbound_tb": str(inbound_tb),
                "outbound_bytes": outgoing,
                "inbound_bytes": ingoing,
            }
        )
    state = _load_json(REPORT_STATE_PATH)
    web_cfg = _load_json(WEB_CONFIG_PATH)
    hourly = _merge_hourly_series(state.get("hourly", {}))
    tracking = _compute_tracking_totals(hourly, web_cfg.get("tracking_start"))
    rebuilds = _detect_last_rebuilds(state.get("hourly", {}))
    return JSONResponse(
        {
            "servers": rows,
            "updated_at": _now_local().strftime("%Y-%m-%d %H:%M:%S"),
            "tracking": tracking,
            "traffic": {
                "limit_gb": limit_gb,
                "limit_tb": str(limit_tb) if limit_tb is not None else None,
                "cost_per_tb_eur": 1,
            },
            "rebuilds": rebuilds,
        }
    )


@app.post("/api/rebuild")
async def api_rebuild(request: Request) -> JSONResponse:
    _require_auth(request)
    payload = await request.json()
    server_id = int(payload.get("server_id"))
    config = _load_yaml(CONFIG_PATH)
    client = HetznerClient(config["hetzner"]["api_token"])
    result = client.rebuild_server(server_id, config)
    if not result.get("success"):
        return JSONResponse(result, status_code=500)
    cf_cfg = config.get("cloudflare", {})
    record_map = cf_cfg.get("record_map", {})
    record_name = record_map.get(str(server_id))
    dns = None
    if record_name:
        dns = client.update_cloudflare_a_record(
            cf_cfg.get("api_token", ""),
            cf_cfg.get("zone_id", ""),
            record_name,
            result.get("new_ip", ""),
        )
    return JSONResponse({"rebuild": result, "dns": dns})


@app.post("/api/dns_check")
async def api_dns_check(request: Request) -> JSONResponse:
    _require_auth(request)
    payload = await request.json()
    server_id = payload.get("server_id")
    config = _load_yaml(CONFIG_PATH)
    client = HetznerClient(config["hetzner"]["api_token"])
    servers = client.get_servers()
    if server_id:
        servers = [s for s in servers if s["id"] == int(server_id)]
    cf_cfg = config.get("cloudflare", {})
    record_map = cf_cfg.get("record_map", {})
    results = []
    for s in servers:
        record = record_map.get(str(s["id"]))
        ip = s["public_net"]["ipv4"]["ip"] if s["public_net"].get("ipv4") else None
        if not record or not ip:
            results.append({"id": s["id"], "status": "missing"})
            continue
        try:
            socket.setdefaulttimeout(5)
            resolved = socket.gethostbyname(record)
            ok = resolved == ip
            results.append({"id": s["id"], "record": record, "resolved": resolved, "expected": ip, "ok": ok})
        except Exception as e:
            results.append({"id": s["id"], "record": record, "error": str(e)})
    return JSONResponse({"results": results})


@app.get("/api/hourly")
def api_hourly(request: Request, date: Optional[str] = None) -> JSONResponse:
    _require_auth(request)
    state = _load_json(REPORT_STATE_PATH)
    hourly = state.get("hourly", {})
    keys = sorted(hourly.keys())
    if date:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format")
        selected_keys = [key for key in keys if key.startswith(date)]
        if not selected_keys:
            return JSONResponse({"servers": {}, "hours": []})
        prev_map = {keys[i]: keys[i - 1] for i in range(1, len(keys))}
        rows: Dict[str, Any] = {}
        for curr_key in selected_keys:
            prev_key = prev_map.get(curr_key)
            prev = hourly.get(prev_key, {}) if prev_key else {}
            curr = hourly.get(curr_key, {})
            deltas = _delta_by_name(prev, curr)
            for name in deltas:
                if name not in rows:
                    rows[name] = {"name": name, "deltas": []}
            for name, data in rows.items():
                delta = deltas.get(name, {})
                delta_tb = str(_quantize_tb(delta["out"])) if delta.get("has_out") else None
                delta_in_tb = str(_quantize_tb(delta["in"])) if delta.get("has_in") else None
                data["deltas"].append({"hour": curr_key, "tb": delta_tb, "in_tb": delta_in_tb})
        return JSONResponse({"servers": rows, "hours": selected_keys})

    keys = keys[-25:]
    rows: Dict[str, Any] = {}
    for i in range(1, len(keys)):
        prev_key = keys[i - 1]
        curr_key = keys[i]
        prev = hourly.get(prev_key, {})
        curr = hourly.get(curr_key, {})
        deltas = _delta_by_name(prev, curr)
        for name in deltas:
            if name not in rows:
                rows[name] = {"name": name, "deltas": []}
        for name, data in rows.items():
            delta = deltas.get(name, {})
            delta_tb = str(_quantize_tb(delta["out"])) if delta.get("has_out") else None
            delta_in_tb = str(_quantize_tb(delta["in"])) if delta.get("has_in") else None
            data["deltas"].append({"hour": curr_key, "tb": delta_tb, "in_tb": delta_in_tb})
    return JSONResponse({"servers": rows, "hours": keys[1:]})


@app.get("/api/daily")
def api_daily(request: Request) -> JSONResponse:
    _require_auth(request)
    state = _load_json(REPORT_STATE_PATH)
    hourly = state.get("hourly", {})
    keys = sorted(hourly.keys())
    if len(keys) < 2:
        return JSONResponse({"days": [], "peak": "0.000", "total": "0.000", "servers": []})

    daily_totals: Dict[str, Decimal] = {}
    daily_in_totals: Dict[str, Decimal] = {}
    per_server: Dict[str, Dict[str, Decimal]] = {}
    per_server_in: Dict[str, Dict[str, Decimal]] = {}
    for i in range(1, len(keys)):
        prev_key = keys[i - 1]
        curr_key = keys[i]
        date_key = _date_from_hour_key(curr_key)
        if not date_key:
            continue
        prev = hourly.get(prev_key, {})
        curr = hourly.get(curr_key, {})
        deltas = _delta_by_name(prev, curr)
        for name, data in deltas.items():
            if data.get("has_out"):
                delta_tb = data["out"]
                daily_totals[date_key] = daily_totals.get(date_key, Decimal("0.000")) + delta_tb
                if name not in per_server:
                    per_server[name] = {}
                per_server[name][date_key] = per_server[name].get(date_key, Decimal("0.000")) + delta_tb
            if data.get("has_in"):
                delta_in_tb = data["in"]
                daily_in_totals[date_key] = daily_in_totals.get(date_key, Decimal("0.000")) + delta_in_tb
                if name not in per_server_in:
                    per_server_in[name] = {}
                per_server_in[name][date_key] = per_server_in[name].get(date_key, Decimal("0.000")) + delta_in_tb

    day_keys = sorted(daily_totals.keys())
    day_keys = day_keys[-35:]
    days = []
    for date_key in day_keys:
        total = _quantize_tb(daily_totals[date_key])
        inbound_total = _quantize_tb(daily_in_totals.get(date_key, Decimal("0.000")))
        days.append({"date": date_key, "outbound_tb": str(total), "inbound_tb": str(inbound_total)})

    peak = _quantize_tb(max((Decimal(d["outbound_tb"]) for d in days), default=Decimal("0.000")))
    total = _quantize_tb(sum((Decimal(d["outbound_tb"]) for d in days), Decimal("0.000")))
    in_peak = _quantize_tb(max((Decimal(d["inbound_tb"]) for d in days), default=Decimal("0.000")))
    in_total = _quantize_tb(sum((Decimal(d["inbound_tb"]) for d in days), Decimal("0.000")))
    servers = []
    for name in sorted(per_server.keys()):
        rows = []
        for date_key in day_keys:
            value = _quantize_tb(per_server[name].get(date_key, Decimal("0.000")))
            in_value = _quantize_tb(per_server_in.get(name, {}).get(date_key, Decimal("0.000")))
            rows.append({"date": date_key, "outbound_tb": str(value), "inbound_tb": str(in_value)})
        servers.append({"id": name, "name": name, "days": rows})
    return JSONResponse(
        {
            "days": days,
            "peak": str(peak),
            "total": str(total),
            "in_peak": str(in_peak),
            "in_total": str(in_total),
            "servers": servers,
        }
    )


@app.get("/api/cycle")
def api_cycle(request: Request) -> JSONResponse:
    _require_auth(request)
    state = _load_json(REPORT_STATE_PATH)
    hourly = state.get("hourly", {})
    config = _load_yaml(CONFIG_PATH)
    client = HetznerClient(config["hetzner"]["api_token"])
    servers = client.get_servers()
    include_ids = {str(s["id"]) for s in servers}
    name_map = {str(s["id"]): s.get("name") or str(s["id"]) for s in servers}
    return JSONResponse(_compute_cycle_data(hourly, include_ids=include_ids, name_map=name_map))
