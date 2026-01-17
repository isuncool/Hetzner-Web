"""通知模块 - 修复版"""
import logging
from typing import Dict, List


class Notifier:
    def __init__(self, config: Dict):
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # 安全获取配置
        notifications = config.get('notifications', {})
        
        # Telegram 配置（新位置）
        self.telegram_config = config.get('telegram', {})
        self.telegram_enabled = self.telegram_config.get('enabled', False)
        
        # Email 配置
        self.email_config = notifications.get('email', {})
        self.email_enabled = self.email_config.get('enabled', False)
        
        self.logger.info(f"通知模块已初始化 (Telegram: {self.telegram_enabled}, Email: {self.email_enabled})")
    
    def notify_traffic_warning(self, results: List[Dict]):
        """流量警告通知"""
        if not results:
            return
        
        for result in results:
            self.logger.warning(
                f"流量警告: {result['server_name']} - "
                f"{result['usage_percent']:.1f}% "
                f"({result['traffic']['total']:.2f}GB)"
            )
    
    def notify_traffic_exceeded(self, actions: List[Dict]):
        """流量超限通知"""
        if not actions:
            return
        
        for action in actions:
            self.logger.warning(
                f"流量超限: {action['server']} - "
                f"{action['traffic']:.2f}GB - "
                f"操作: {action['action']}"
            )
    
    def notify_summary(self, summary: Dict):
        """监控摘要通知"""
        self.logger.info(
            f"监控摘要: 总计 {summary['total_servers']} 台服务器, "
            f"超限 {len(summary.get('exceeded_servers', []))} 台, "
            f"警告 {len(summary.get('warning_servers', []))} 台"
        )
