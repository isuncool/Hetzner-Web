import sys
import asyncio

# 在 main() 函数中添加
from telegram_bot import TelegramBot

# ... 现有代码 ...

def main():
    # ... 现有代码 ...
    
    hetzner = HetznerManager(config['hetzner']['api_token'])
    monitor = TrafficMonitor(hetzner, config)
    scheduler = TaskScheduler(hetzner, config)
    
    # 添加 Telegram Bot
    telegram_bot = TelegramBot(config, hetzner, monitor, scheduler)
    
    # 启动 Telegram Bot
    if telegram_bot.enabled:
        async def start_bot():
            await telegram_bot.initialize_commands()
            asyncio.create_task(telegram_bot.run_polling())
        
        asyncio.run(start_bot())
    
    # ... 继续现有代码 ...
