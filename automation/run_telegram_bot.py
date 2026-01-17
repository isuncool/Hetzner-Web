#!/usr/bin/env python3
"""独立 Telegram Bot - v13 同步版本"""
import sys
import yaml
import logging
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

from hetzner_manager import HetznerManager
from traffic_monitor import TrafficMonitor
from scheduler import TaskScheduler
from telegram_bot import TelegramBot


def main():
    # 加载配置
    with open('config.yaml') as f:
        config = yaml.safe_load(f)
    config['_config_path'] = 'config.yaml'
    
    logger.info("=" * 60)
    logger.info("Telegram Bot 启动 (v13)")
    logger.info("=" * 60)
    
    # 初始化
    hetzner = HetznerManager(config['hetzner']['api_token'])
    monitor = TrafficMonitor(hetzner, config)
    scheduler = TaskScheduler(hetzner, config)
    bot = TelegramBot(config, hetzner, monitor, scheduler)
    monitor.set_telegram_bot(bot)
    
    if not bot.enabled:
        logger.error("❌ Bot 未启用")
        return
    
    # 初始化并运行
    if bot.initialize_commands():
        bot.run_polling()
    else:
        logger.error("❌ 初始化失败")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\n👋 Bot 已停止")
