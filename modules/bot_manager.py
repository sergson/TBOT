# modules/bot_manager.py (updated version)
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import asyncio
import os
import time
import gc
import threading
from .database import get_all_bots, get_bot_config, update_bot_status, delete_bot
from .logger import perf_logger
from .bot_registry import bot_registry

logger = perf_logger.get_logger('bot_manager', 'app')

class BotManager:
    def __init__(self):
        self.bots = {}          # bot_id -> bot instance
        self.loop = asyncio.get_event_loop()
        self.graph_hashes = {}  # for optimizing graph updates (optional)

    def load_bots(self):
        """Loads all bots with status 'running' from the database and starts them."""
        bots_data = get_all_bots()
        for bot in bots_data:
            if bot['status'] == 'running':
                self.add_bot(bot['id'])

    def add_bot(self, bot_id: int):
        """Creates and starts a bot by its ID."""
        config = get_bot_config(bot_id)
        if not config:
            logger.error(f"Bot {bot_id} config not found")
            return

        bot_type = config['bot_type']
        meta = bot_registry.get_type(bot_type)
        if not meta:
            logger.error(f"Unknown bot type: {bot_type}")
            return

        bot_class = meta['bot_class']
        bot_instance = bot_class(bot_id)
        self.bots[bot_id] = bot_instance
        asyncio.run_coroutine_threadsafe(bot_instance.start(), self.loop)

    def remove_bot(self, bot_id: int):
        """Stops the bot and removes it from memory."""
        if bot_id in self.bots:
            bot = self.bots[bot_id]
            future = asyncio.run_coroutine_threadsafe(bot.stop(), self.loop)
            try:
                future.result(timeout=5)
            except Exception as e:
                logger.error(f"Error stopping bot {bot_id}: {e}")
            del self.bots[bot_id]
        self.graph_hashes.pop(bot_id, None)

        # Schedule deletion of the database file if path is specified in config
        config = get_bot_config(bot_id)
        if config and 'data_db_path' in config:
            db_path = config['data_db_path']
            if os.path.exists(db_path):
                threading.Timer(3.0, self._delete_file, args=[db_path]).start()
                logger.info(f"Scheduled deletion of {db_path} in 3 seconds")

    def _delete_file(self, path: str):
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                gc.collect()
                if os.path.exists(path):
                    os.remove(path)
                    logger.info(f"Deleted database file: {path}")
                return
            except Exception as e:
                if attempt == max_attempts:
                    logger.error(f"Failed to delete {path} after {max_attempts} attempts: {e}")
                else:
                    time.sleep(1)

    def start_bot(self, bot_id: int):
        if bot_id in self.bots:
            asyncio.run_coroutine_threadsafe(self.bots[bot_id].start(), self.loop)

    def stop_bot(self, bot_id: int):
        if bot_id in self.bots:
            asyncio.run_coroutine_threadsafe(self.bots[bot_id].stop(), self.loop)

    def shutdown(self):
        for bot in self.bots.values():
            asyncio.run_coroutine_threadsafe(bot.stop(), self.loop)