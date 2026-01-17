"""Telegram Bot - Hetzner Monitor commands (python-telegram-bot v20+)"""
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime
import json
import logging
import os
import socket
import threading
import time
from typing import Dict, Optional
import yaml

try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes
    TELEGRAM_OK = True
except Exception as e:
    print(f"导入 telegram 失败: {e}")
    TELEGRAM_OK = False
    Application = None
    CommandHandler = None
    ContextTypes = None
    Update = None


class TelegramBot:
    def __init__(self, config, hetzner_manager, traffic_monitor, scheduler):
        self.config = config
        self.hetzner = hetzner_manager
        self.monitor = traffic_monitor
        self.scheduler = scheduler
        tg_config = config.get('telegram', {})
        self.bot_token = tg_config.get('bot_token', '')
        self.chat_id = str(tg_config.get('chat_id', ''))
        self.enabled = tg_config.get('enabled', False) and TELEGRAM_OK and bool(self.bot_token)
        self.app = None

        self.logger = logging.getLogger(__name__)

        if not TELEGRAM_OK:
            self.logger.error("telegram 模块导入失败")
            self.enabled = False
        elif self.enabled:
            self.logger.info(f"Bot Token: {self.bot_token[:20]}...")
            self.logger.info(f"Chat ID: {self.chat_id}")

    def _send(self, msg: str) -> None:
        if self.enabled and self.app:
            try:
                self.app.create_task(self.app.bot.send_message(
                    chat_id=self.chat_id,
                    text=msg,
                    parse_mode='Markdown',
                ))
            except Exception as e:
                self.logger.error(f"发送失败: {e}")

    def _limit_tb(self) -> Decimal:
        return (Decimal(self.config['traffic']['limit_gb']) / Decimal(1024)).quantize(
            Decimal("0.001"), rounding=ROUND_HALF_UP
        )

    @staticmethod
    def _bytes_to_tb(value_bytes: float) -> Decimal:
        return (Decimal(value_bytes) / (Decimal(1024) ** 4)).quantize(
            Decimal("0.001"), rounding=ROUND_HALF_UP
        )

    def _report_state_path(self) -> str:
        return os.environ.get("REPORT_STATE_PATH", "/opt/hetzner-monitor/report_state.json")

    def _config_path(self) -> str:
        return self.config.get('_config_path', 'config.yaml')

    def _save_config(self) -> None:
        try:
            with open(self._config_path(), 'w', encoding='utf-8') as f:
                yaml.safe_dump(self.config, f, sort_keys=False, allow_unicode=True)
        except Exception as e:
            self.logger.error(f"保存配置失败: {e}")

    def _load_report_state(self) -> dict:
        path = self._report_state_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            self.logger.warning(f"读取汇报状态失败: {e}")
            return {}

    def _save_report_state(self, state: dict) -> None:
        path = self._report_state_path()
        try:
            with open(path, "w") as f:
                json.dump(state, f)
        except Exception as e:
            self.logger.warning(f"写入汇报状态失败: {e}")

    def _collect_traffic_snapshot(self) -> dict:
        servers = self.hetzner.get_servers()
        snapshot = {}
        for server in servers:
            sid = str(server["id"])
            detail = self.hetzner.get_server(server["id"]) or {}
            snapshot[sid] = {
                "name": server.get("name", sid),
                "outbound_bytes": detail.get("outgoing_traffic"),
                "inbound_bytes": detail.get("ingoing_traffic"),
            }
        return snapshot

    def _record_hourly_snapshot(self, now: datetime) -> None:
        hour_key = now.strftime("%Y-%m-%d %H:00")
        state = self._load_report_state()
        hourly = state.get("hourly", {})
        if hour_key in hourly:
            return
        hourly[hour_key] = self._collect_traffic_snapshot()
        state["hourly"] = hourly
        self._save_report_state(state)

    def _format_hourly_report(self, hours: int = 24) -> str:
        state = self._load_report_state()
        hourly = state.get("hourly", {})
        if not hourly:
            return "小时分析: 暂无数据"

        keys = sorted(hourly.keys())
        keys = keys[-(hours + 1):]
        if len(keys) < 2:
            return "小时分析: 数据不足"

        servers = {}
        for i in range(1, len(keys)):
            prev_key = keys[i - 1]
            curr_key = keys[i]
            prev = hourly.get(prev_key, {})
            curr = hourly.get(curr_key, {})
            for sid, data in curr.items():
                if sid not in servers:
                    servers[sid] = {"name": data.get("name", sid), "deltas": []}
                prev_out = prev.get(sid, {}).get("outbound_bytes")
                curr_out = data.get("outbound_bytes")
                if prev_out is None or curr_out is None or float(curr_out) < float(prev_out):
                    delta_tb = None
                else:
                    delta_tb = self._bytes_to_tb(float(curr_out) - float(prev_out))
                servers[sid]["deltas"].append((curr_key[-5:], delta_tb))

        parts = ["🕘 *每小时出站(最近24h)*"]
        for sid, data in servers.items():
            lines = [f"🖥 *{data['name']}* (`{sid}`)"]
            for label, delta_tb in data["deltas"]:
                val = f"{delta_tb} TB" if delta_tb is not None else "N/A"
                lines.append(f"{label}: {val}")
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    def _send_scheduled_report(self, label: str) -> None:
        now = datetime.now().astimezone()
        self._record_hourly_snapshot(now)
        state = self._load_report_state()
        last_time = state.get("last_time")
        last_snapshot = state.get("servers", {})

        current_snapshot = self._collect_traffic_snapshot()
        parts = [f"🕒 *定时流量汇报* ({label})"]
        if last_time:
            parts.append(f"统计区间: {last_time} ~ {now.strftime('%Y-%m-%d %H:%M')}")
        else:
            parts.append("统计区间: 首次统计（仅显示累计出站）")

        limit_tb = self._limit_tb()
        for sid, data in current_snapshot.items():
            outbound_bytes = data.get("outbound_bytes")
            total_tb = self._bytes_to_tb(outbound_bytes) if outbound_bytes is not None else Decimal("0.000")
            usage = float((Decimal(outbound_bytes) / (Decimal(1024) ** 4) / limit_tb) * 100) if outbound_bytes is not None else 0.0

            delta_tb = None
            last = last_snapshot.get(sid, {})
            last_out = last.get("outbound_bytes")
            if outbound_bytes is not None and last_out is not None:
                delta = float(outbound_bytes) - float(last_out)
                if delta >= 0:
                    delta_tb = self._bytes_to_tb(delta)

            delta_line = f"区间增量: *{delta_tb} TB*" if delta_tb is not None else "区间增量: N/A"
            parts.append(
                f"🖥 *{data.get('name')}* (`{sid}`)\n"
                f"💾 累计出站: *{total_tb} TB* / {limit_tb} TB\n"
                f"📈 使用率: *{usage:.2f}%*\n"
                f"📊 {delta_line}"
            )

        parts.append(self._format_hourly_report())
        self._send("\n\n".join(parts))
        state = {
            "last_time": now.strftime("%Y-%m-%d %H:%M"),
            "servers": current_snapshot,
            "hourly": state.get("hourly", {}),
        }
        self._save_report_state(state)

    def _start_report_thread(self) -> None:
        def loop():
            last_sent = {"11:55": None, "23:55": None}
            while True:
                now = datetime.now().astimezone()
                if now.minute == 0:
                    self._record_hourly_snapshot(now)
                current_time = now.strftime("%H:%M")
                for target in ("11:55", "23:55"):
                    if current_time == target:
                        if last_sent[target] != now.date().isoformat():
                            self._send_scheduled_report(target)
                            last_sent[target] = now.date().isoformat()
                time.sleep(30)

        t = threading.Thread(target=loop, daemon=True)
        t.start()

    def send_traffic_notification(self, result: Dict) -> None:
        t = result['new_threshold']
        emojis = {10: "💧", 20: "💦", 30: "🌊", 40: "🟢", 50: "🟡", 60: "🟠", 70: "🔶", 80: "🔴", 90: "🚨", 100: "💀"}
        emoji = emojis.get(t, '📊')
        usage = result['usage_percent']
        traffic = result['traffic']
        limit_tb = self._limit_tb()
        outbound_bytes = traffic.get('outbound_bytes')
        if outbound_bytes is not None:
            total_tb = self._bytes_to_tb(outbound_bytes)
        else:
            total_tb = (Decimal(traffic['outbound']) / Decimal(1024)).quantize(
                Decimal("0.001"), rounding=ROUND_HALF_UP
            )

        bars = int(usage / 10)
        progress = "█" * bars + "░" * (10 - bars)

        msg = (
            f"{emoji} *流量通知 - {t}%*\n\n"
            f"🖥 服务器: *{result['server_name']}*\n"
            f"📊 使用进度:\n"
            f"`{progress}` {usage:.1f}%\n\n"
            f"💾 已用(出站): *{total_tb} TB* / {limit_tb} TB\n"
            f"📉 剩余: {(limit_tb - total_tb).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)} TB\n\n"
            f"📥 入站: {traffic['inbound']:.2f} GB\n"
            f"📤 出站: {traffic['outbound']:.2f} GB\n"
            f"📦 出站字节: `{int(outbound_bytes) if outbound_bytes is not None else 'N/A'}`"
        )
        self._send(msg)

    def send_exceed_notification(self, result: Dict) -> None:
        msg = (
            f"🚨 *流量超限警报！*\n\n"
            f"🖥 服务器: *{result['server_name']}*\n"
            f"📊 已达到: *{result['usage_percent']:.2f}%*\n\n"
            f"⚡ 准备自动重建..."
        )
        self._send(msg)

    def send_rebuild_success_notification(self, new: Dict) -> None:
        msg = (
            f"✅ *重建成功！流量已重置*\n\n"
            f"🆔 新ID: `{new.get('new_server_id')}`\n"
            f"🌐 新IP: `{new.get('new_ip')}`\n\n"
            f"💡 流量计数已重置为 0%"
        )
        self._send(msg)

    def send_rebuild_failed_notification(self, new: Dict) -> None:
        self._send(f"❌ *重建失败*\n\n错误: {new.get('error')}")

    def send_dns_update_result(self, record_name: str, ip: str, success: bool, error: Optional[str]) -> None:
        if success:
            self._send(f"✅ DNS已更新: {record_name} -> {ip}")
        else:
            self._send(f"⚠️ DNS更新失败: {record_name} ({error})")

    async def cmd_start(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        text = (
            "🤖 *Hetzner 监控系统*\n\n"
            "✅ Bot 已启动\n"
            "📊 流量限制: 18 TB\n"
            "🔔 通知间隔: 10%\n\n"
            "使用 /help 查看所有命令"
        )
        await u.message.reply_text(text, parse_mode='Markdown')

    async def cmd_help(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        text = (
            "📖 *命令大全*\n\n"
            "*📊 查询类:*\n"
            "/list - 🖥 服务器列表\n"
            "/status - 📈 系统状态\n"
            "/traffic ID - 📊 流量详情(无ID显示全部)\n"
            "/today ID - 📅 今日流量(无ID显示全部)\n"
            "/report - 🕒 手动流量汇报\n"
            "/reportstatus - 📋 上次汇报时间\n\n"
            "/reportreset - ♻️ 重置汇报区间\n\n"
            "/dnstest [ID] - 🔧 测试DNS更新\n\n"
            "/dnscheck ID - ✅ DNS解析检查\n\n"
            "*🔧 控制类:*\n"
            "/startserver <ID> - ▶️ 启动服务器\n"
            "/stopserver <ID> - ⏸️ 停止服务器\n"
            "/reboot <ID> - 🔄 重启服务器\n"
            "/delete <ID> confirm - 🗑 删除服务器\n"
            "/rebuild <ID> - 🔨 重建服务器\n\n"
            "*💾 快照管理:*\n"
            "/snapshots - 📦 查看所有快照\n"
            "/createsnapshot <ID> - 📸 手动创建快照\n\n"
            "*⏰ 定时任务:*\n"
            "/scheduleon - ✅ 开启定时删机\n"
            "/scheduleoff - ⏸️ 关闭定时删机\n"
            "/schedulestatus - 📋 查看定时状态\n"
            "/scheduleset delete=23:50,01:00 create=08:00,09:00 - 设置定时\n\n"
            "💡 服务器ID从 /list 获取"
        )
        await u.message.reply_text(text, parse_mode='Markdown')

    async def cmd_list(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        try:
            servers = self.hetzner.get_servers()
            if not servers:
                await u.message.reply_text("📭 暂无服务器")
                return

            msg = "🖥 *服务器列表*\n\n"
            for s in servers:
                status = "🟢 运行中" if s['status'] == 'running' else "🔴 已停止"
                ip = s['public_net']['ipv4']['ip'] if s['public_net'].get('ipv4') else "N/A"
                msg += f"{status}\n"
                msg += f"📛 *{s['name']}*\n"
                msg += f"🆔 ID: `{s['id']}`\n"
                msg += f"🌐 IP: `{ip}`\n"
                msg += f"⚙️ 类型: {s['server_type']['name']}\n"
                msg += "─────────────\n"

            await u.message.reply_text(msg, parse_mode='Markdown')
        except Exception as e:
            await u.message.reply_text(f"❌ 错误: {e}")

    async def cmd_status(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        try:
            servers = self.hetzner.get_servers()
            total = len(servers)
            running = sum(1 for s in servers if s['status'] == 'running')

            msg = (
                f"📊 *系统状态概览*\n\n"
                f"🖥 服务器总数: {total} 台\n"
                f"🟢 运行中: {running} 台\n"
                f"🔴 已停止: {total - running} 台\n\n"
                f"🔔 通知间隔: 10%\n"
                f"✅ 监控系统正常运行"
            )
            await u.message.reply_text(msg, parse_mode='Markdown')
        except Exception as e:
            await u.message.reply_text(f"❌ {e}")

    async def cmd_traffic(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        try:
            limit_tb = self._limit_tb()
            if not c.args:
                await u.message.reply_text("⏳ 正在获取全部服务器流量数据...")
                servers = self.hetzner.get_servers()
                if not servers:
                    await u.message.reply_text("📭 暂无服务器")
                    return
                parts = ["📊 *流量汇总* (出站计费)\n"]
                for server in servers:
                    sid = server['id']
                    traffic = self.hetzner.calculate_traffic(sid, days=30)
                    outbound_bytes = traffic.get('outbound_bytes')
                    if outbound_bytes is not None:
                        total_tb = self._bytes_to_tb(outbound_bytes)
                        usage = float((Decimal(outbound_bytes) / (Decimal(1024) ** 4) / limit_tb) * 100)
                    else:
                        total_tb = (Decimal(traffic['outbound']) / Decimal(1024)).quantize(
                            Decimal("0.001"), rounding=ROUND_HALF_UP
                        )
                        usage = float((total_tb / limit_tb) * 100)
                    parts.append(
                        f"🖥 *{server['name']}* (`{sid}`)\n"
                        f"💾 已用(出站): *{total_tb} TB* / {limit_tb} TB\n"
                        f"📈 使用率: *{usage:.2f}%*"
                    )
                await u.message.reply_text("\n\n".join(parts), parse_mode='Markdown')
                return

            sid = int(c.args[0])
            server = self.hetzner.get_server(sid)
            if not server:
                await u.message.reply_text("❌ 服务器不存在")
                return

            await u.message.reply_text("⏳ 正在获取流量数据...")

            traffic = self.hetzner.calculate_traffic(sid, days=30)
            outbound_bytes = traffic.get('outbound_bytes')
            if outbound_bytes is not None:
                total_tb = self._bytes_to_tb(outbound_bytes)
                usage = float((Decimal(outbound_bytes) / (Decimal(1024) ** 4) / limit_tb) * 100)
            else:
                total_tb = (Decimal(traffic['outbound']) / Decimal(1024)).quantize(
                    Decimal("0.001"), rounding=ROUND_HALF_UP
                )
                usage = float((total_tb / limit_tb) * 100)

            if usage >= 95:
                emoji = "🚨"
            elif usage >= 80:
                emoji = "🔴"
            elif usage >= 60:
                emoji = "🟡"
            else:
                emoji = "🟢"

            msg = (
                f"📊 *流量详情报告*\n\n"
                f"🖥 服务器: {server['name']}\n"
                f"🆔 ID: `{sid}`\n\n"
                f"{emoji} *本月流量:*\n"
                f"💾 已用(出站): *{total_tb} TB* / {limit_tb} TB\n"
                f"📈 使用率: *{usage:.2f}%*\n"
                f"📉 剩余: *{(limit_tb - total_tb).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)} TB*\n\n"
                f"📥 入站: {traffic['inbound']:.2f} GB\n"
                f"📤 出站: {traffic['outbound']:.2f} GB\n"
                f"📦 出站字节: `{int(outbound_bytes) if outbound_bytes is not None else 'N/A'}`"
            )
            await u.message.reply_text(msg, parse_mode='Markdown')
        except Exception as e:
            await u.message.reply_text(f"❌ 错误: {e}")

    async def cmd_today(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        try:
            if not c.args:
                await u.message.reply_text("⏳ 正在获取全部服务器今日流量...")
                servers = self.hetzner.get_servers()
                if not servers:
                    await u.message.reply_text("📭 暂无服务器")
                    return
                parts = ["📅 *今日流量汇总* (出站计费)\n"]
                for server in servers:
                    sid = server['id']
                    today = self.hetzner.get_today_traffic(sid)
                    outbound_tb = Decimal(today['outbound']) / Decimal(1024)
                    parts.append(
                        f"🖥 *{server['name']}* (`{sid}`)\n"
                        f"📤 出站: *{outbound_tb.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)} TB*\n"
                        f"📥 入站: {today['inbound']:.2f} GB"
                    )
                await u.message.reply_text("\n\n".join(parts), parse_mode='Markdown')
                return

            sid = int(c.args[0])
            server = self.hetzner.get_server(sid)
            if not server:
                await u.message.reply_text("❌ 服务器不存在")
                return

            today = self.hetzner.get_today_traffic(sid)

            msg = (
                f"📅 *今日流量分析*\n\n"
                f"🖥 服务器: {server['name']}\n\n"
                f"📊 今日统计:\n"
                f"💾 总计: *{today['total']:.2f} GB*\n"
                f"📥 入站: {today['inbound']:.2f} GB\n"
                f"📤 出站: {today['outbound']:.2f} GB"
            )
            await u.message.reply_text(msg, parse_mode='Markdown')
        except Exception as e:
            await u.message.reply_text(f"❌ 错误: {e}")

    async def cmd_report(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        try:
            self._send_scheduled_report("manual")
        except Exception as e:
            await u.message.reply_text(f"❌ 错误: {e}")

    async def cmd_reportstatus(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        try:
            state = self._load_report_state()
            last_time = state.get("last_time")
            if last_time:
                await u.message.reply_text(f"📋 上次汇报时间: *{last_time}*", parse_mode='Markdown')
            else:
                await u.message.reply_text("📋 暂无历史汇报记录")
        except Exception as e:
            await u.message.reply_text(f"❌ 错误: {e}")

    async def cmd_reportreset(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        try:
            self._save_report_state({})
            await u.message.reply_text("♻️ 汇报区间已重置")
        except Exception as e:
            await u.message.reply_text(f"❌ 错误: {e}")

    async def cmd_dnstest(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        try:
            cf_cfg = self.config.get('cloudflare', {})
            record_map = cf_cfg.get('record_map', {})
            servers = self.hetzner.get_servers()
            if not servers:
                await u.message.reply_text("📭 暂无服务器")
                return
            target_servers = servers
            if c.args:
                sid = int(c.args[0])
                target_servers = [s for s in servers if s['id'] == sid]
                if not target_servers:
                    await u.message.reply_text("❌ 服务器不存在")
                    return
            for server in target_servers:
                sid = server['id']
                record_name = record_map.get(str(sid))
                if not record_name:
                    await u.message.reply_text(f"⚠️ 未配置DNS映射: {sid}")
                    continue
                ip = server['public_net']['ipv4']['ip'] if server['public_net'].get('ipv4') else None
                if not ip:
                    await u.message.reply_text(f"❌ 获取IP失败: {sid}")
                    continue
                res = self.hetzner.update_cloudflare_a_record(
                    cf_cfg.get('api_token', ''),
                    cf_cfg.get('zone_id', ''),
                    record_name,
                    ip,
                )
                if res.get('success'):
                    await u.message.reply_text(f"✅ DNS已更新: {record_name} -> {ip}")
                else:
                    await u.message.reply_text(f"❌ DNS更新失败: {record_name} ({res.get('error')})")
        except Exception as e:
            await u.message.reply_text(f"❌ 错误: {e}")

    def _resolve_a(self, hostname: str, timeout: int = 5) -> str:
        prev_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout)
        try:
            return socket.gethostbyname(hostname)
        finally:
            socket.setdefaulttimeout(prev_timeout)

    async def cmd_dnscheck(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        try:
            cf_cfg = self.config.get('cloudflare', {})
            record_map = cf_cfg.get('record_map', {})
            servers = self.hetzner.get_servers()
            if not servers:
                await u.message.reply_text("📭 暂无服务器")
                return
            await u.message.reply_text("⏳ 正在检查DNS解析...")
            target_servers = servers
            if c.args:
                sid = int(c.args[0])
                target_servers = [s for s in servers if s['id'] == sid]
                if not target_servers:
                    await u.message.reply_text("❌ 服务器不存在")
                    return
            for server in target_servers:
                sid = server['id']
                record_name = record_map.get(str(sid))
                if not record_name:
                    await u.message.reply_text(f"⚠️ 未配置DNS映射: {sid}")
                    continue
                ip = server['public_net']['ipv4']['ip'] if server['public_net'].get('ipv4') else None
                if not ip:
                    await u.message.reply_text(f"❌ 获取IP失败: {sid}")
                    continue
                try:
                    resolved = self._resolve_a(record_name)
                except Exception as e:
                    await u.message.reply_text(f"❌ DNS解析失败: {record_name} ({e})")
                    continue
                if resolved == ip:
                    await u.message.reply_text(f"✅ DNS解析正常: {record_name} -> {resolved}")
                else:
                    await u.message.reply_text(f"⚠️ DNS解析不一致: {record_name} -> {resolved} (期望 {ip})")
        except Exception as e:
            await u.message.reply_text(f"❌ 错误: {e}")

    async def cmd_startserver(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        if not c.args:
            await u.message.reply_text("📝 用法: /startserver <ID>", parse_mode='Markdown')
            return

        try:
            sid = int(c.args[0])
            server = self.hetzner.get_server(sid)
            if not server:
                await u.message.reply_text("❌ 服务器不存在")
                return

            if self.hetzner.poweron_server(sid):
                await u.message.reply_text(f"✅ *{server['name']}* 已启动", parse_mode='Markdown')
            else:
                await u.message.reply_text("❌ 启动失败")
        except Exception as e:
            await u.message.reply_text(f"❌ {e}")

    async def cmd_stopserver(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        if not c.args:
            await u.message.reply_text("📝 用法: /stopserver <ID>", parse_mode='Markdown')
            return

        try:
            sid = int(c.args[0])
            if self.hetzner.shutdown_server(sid):
                await u.message.reply_text("✅ 服务器已停止", parse_mode='Markdown')
            else:
                await u.message.reply_text("❌ 停止失败")
        except Exception as e:
            await u.message.reply_text(f"❌ {e}")

    async def cmd_reboot(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        if not c.args:
            await u.message.reply_text("📝 用法: /reboot <ID>", parse_mode='Markdown')
            return

        try:
            sid = int(c.args[0])
            if self.hetzner.reboot_server(sid):
                await u.message.reply_text("✅ 服务器已重启", parse_mode='Markdown')
            else:
                await u.message.reply_text("❌ 重启失败")
        except Exception as e:
            await u.message.reply_text(f"❌ {e}")

    async def cmd_delete(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        if len(c.args) < 2 or c.args[1] != 'confirm':
            await u.message.reply_text(
                "⚠️ 用法: /delete <ID> confirm\n\n❗️ 此操作不可撤销！",
                parse_mode='Markdown'
            )
            return

        try:
            sid = int(c.args[0])
            if self.hetzner.delete_server(sid):
                await u.message.reply_text(f"✅ 服务器 {sid} 已删除", parse_mode='Markdown')
            else:
                await u.message.reply_text("❌ 删除失败")
        except Exception as e:
            await u.message.reply_text(f"❌ {e}")

    async def cmd_rebuild(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        if not c.args:
            await u.message.reply_text("📝 用法: /rebuild <服务器ID>", parse_mode='Markdown')
            return

        try:
            sid = int(c.args[0])
            server = self.hetzner.get_server(sid)
            if not server:
                await u.message.reply_text("❌ 服务器不存在")
                return

            await u.message.reply_text(f"🔨 开始重建 *{server['name']}*...", parse_mode='Markdown')

            template = self.config.get('server_template', {})
            server_type = template.get('server_type')
            location = template.get('location')
            ssh_keys = template.get('ssh_keys', [])
            name_prefix = template.get('name_prefix')
            use_original_name = template.get('use_original_name', True)
            snapshot_map = self.config.get('snapshot_map', {})
            override_snapshot_id = snapshot_map.get(sid)

            if override_snapshot_id:
                result = self.hetzner.delete_and_recreate_from_snapshot_id(
                    server_id=sid,
                    snapshot_id=override_snapshot_id,
                    server_type=server_type,
                    location=location,
                    ssh_keys=ssh_keys,
                    name_prefix=name_prefix,
                    use_original_name=use_original_name,
                )
            else:
                result = self.hetzner.delete_and_recreate_from_snapshot(
                    server_id=sid,
                    server_type=server_type,
                    location=location,
                    ssh_keys=ssh_keys,
                    name_prefix=name_prefix,
                    use_original_name=use_original_name,
                )

            if result.get('success'):
                self.monitor.reset_server_thresholds(sid)
                self.monitor.handle_rebuild_success(sid, result)
                msg = (
                    f"✅ *重建成功！*\n\n"
                    f"🆔 新ID: `{result.get('new_server_id')}`\n"
                    f"🌐 新IP: `{result.get('new_ip')}`\n\n"
                    f"💡 流量已重置"
                )
                await u.message.reply_text(msg, parse_mode='Markdown')
            else:
                await u.message.reply_text(f"❌ 重建失败: {result.get('error')}", parse_mode='Markdown')
        except Exception as e:
            await u.message.reply_text(f"❌ 错误: {e}")

    async def cmd_snapshots(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        try:
            snapshots = self.hetzner.get_snapshots()
            if not snapshots:
                await u.message.reply_text("📭 暂无快照")
                return

            msg = "📦 *快照列表*\n\n"
            for idx, snap in enumerate(snapshots[:5], 1):
                msg += f"{idx}. 📸 {snap.get('description', snap.get('name', ''))}\n"
                msg += f"   🆔 ID: `{snap.get('id')}`\n\n"

            await u.message.reply_text(msg, parse_mode='Markdown')
        except Exception as e:
            await u.message.reply_text(f"❌ {e}")

    async def cmd_createsnapshot(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        if not c.args:
            await u.message.reply_text("📝 用法: /createsnapshot <ID>", parse_mode='Markdown')
            return

        try:
            sid = int(c.args[0])
            await u.message.reply_text("📸 正在创建快照...")

            snapshot = self.hetzner.create_snapshot(sid)
            if snapshot:
                await u.message.reply_text(
                    f"✅ 快照创建成功！\n🆔 ID: `{snapshot.get('id')}`",
                    parse_mode='Markdown'
                )
            else:
                await u.message.reply_text("❌ 快照创建失败")
        except Exception as e:
            await u.message.reply_text(f"❌ {e}")

    async def cmd_scheduleon(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        self.scheduler.enable()
        self.scheduler.load_tasks()
        await u.message.reply_text("✅ 定时任务已启用", parse_mode='Markdown')

    async def cmd_scheduleoff(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        self.scheduler.disable()
        self.scheduler.load_tasks()
        await u.message.reply_text("⏸ 定时任务已关闭", parse_mode='Markdown')

    async def cmd_schedulestatus(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        enabled = self.config.get('scheduler', {}).get('enabled')
        emoji = "✅" if enabled else "⏸"
        text = "已启用" if enabled else "已禁用"
        next_run = self.scheduler.get_next_run()
        tasks = self.config.get('scheduler', {}).get('tasks', [])
        lines = [f"📋 *定时任务状态*\n\n{emoji} 状态: *{text}*"]
        if tasks:
            for task in tasks:
                action = task.get('action')
                times = ",".join(task.get('times', []))
                lines.append(f"{action}: {times}")
        lines.append(f"下次执行: {next_run}")
        msg = "\n".join(lines)
        await u.message.reply_text(msg, parse_mode='Markdown')

    async def cmd_scheduleset(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        if not c.args:
            await u.message.reply_text("用法: /scheduleset delete=23:50,01:00 create=08:00,09:00")
            return
        try:
            delete_times = []
            create_times = []
            for part in c.args:
                if part.startswith("delete="):
                    delete_times = [t for t in part.split("=", 1)[1].split(",") if t]
                elif part.startswith("create="):
                    create_times = [t for t in part.split("=", 1)[1].split(",") if t]
            tasks = []
            if delete_times:
                tasks.append({"action": "delete_all", "times": delete_times})
            if create_times:
                tasks.append({"action": "create_from_snapshots", "times": create_times})
            if not tasks:
                await u.message.reply_text("未识别到时间，格式: delete=23:50,01:00 create=08:00,09:00")
                return
            self.config.setdefault('scheduler', {})['tasks'] = tasks
            self._save_config()
            if self.scheduler.is_enabled():
                self.scheduler.load_tasks()
            await u.message.reply_text("✅ 定时任务时间已更新", parse_mode='Markdown')
        except Exception as e:
            await u.message.reply_text(f"❌ 错误: {e}")

    def initialize_commands(self) -> bool:
        if not self.enabled:
            self.logger.warning("Bot 未启用")
            return False

        try:
            self.logger.info("初始化 Application...")
            self.app = Application.builder().token(self.bot_token).build()

            self.logger.info("注册命令...")
            self.app.add_handler(CommandHandler("start", self.cmd_start))
            self.app.add_handler(CommandHandler("help", self.cmd_help))
            self.app.add_handler(CommandHandler("list", self.cmd_list))
            self.app.add_handler(CommandHandler("status", self.cmd_status))
            self.app.add_handler(CommandHandler("traffic", self.cmd_traffic))
            self.app.add_handler(CommandHandler("today", self.cmd_today))
            self.app.add_handler(CommandHandler("report", self.cmd_report))
            self.app.add_handler(CommandHandler("reportstatus", self.cmd_reportstatus))
            self.app.add_handler(CommandHandler("reportreset", self.cmd_reportreset))
            self.app.add_handler(CommandHandler("dnstest", self.cmd_dnstest))
            self.app.add_handler(CommandHandler("dnscheck", self.cmd_dnscheck))
            self.app.add_handler(CommandHandler("startserver", self.cmd_startserver))
            self.app.add_handler(CommandHandler("stopserver", self.cmd_stopserver))
            self.app.add_handler(CommandHandler("reboot", self.cmd_reboot))
            self.app.add_handler(CommandHandler("delete", self.cmd_delete))
            self.app.add_handler(CommandHandler("rebuild", self.cmd_rebuild))
            self.app.add_handler(CommandHandler("snapshots", self.cmd_snapshots))
            self.app.add_handler(CommandHandler("createsnapshot", self.cmd_createsnapshot))
            self.app.add_handler(CommandHandler("scheduleon", self.cmd_scheduleon))
            self.app.add_handler(CommandHandler("scheduleoff", self.cmd_scheduleoff))
            self.app.add_handler(CommandHandler("schedulestatus", self.cmd_schedulestatus))
            self.app.add_handler(CommandHandler("scheduleset", self.cmd_scheduleset))

            self.logger.info("✅ 命令已注册")
            self._start_report_thread()
            return True

        except Exception as e:
            self.logger.error(f"初始化失败: {e}", exc_info=True)
            return False

    def run_polling(self) -> None:
        if not self.app:
            self.logger.error("Application 未初始化")
            return

        try:
            self.logger.info("启动轮询...")
            self.app.run_polling(stop_signals=None)
        except Exception as e:
            self.logger.error(f"运行失败: {e}", exc_info=True)
