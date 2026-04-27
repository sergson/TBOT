# core/bot_manager.py
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
from .registry import bot_registry
from .exchange import ExchangeHandle

logger = perf_logger.get_logger('bot_manager', 'app')

class BotManager:
    def __init__(self):
        self.bots = {}          # bot_id -> bot instance
        self.loop = None
        self.graph_hashes = {}

    def set_loop(self, loop):
        self.loop = loop

    def load_bots(self):
        bots_data = get_all_bots()
        for bot in bots_data:
            if bot['status'] == 'running':
                self.add_bot(bot['id'])

    def add_bot(self, bot_id: int):
        config = get_bot_config(bot_id)
        if not config:
            logger.error(f"Bot {bot_id} config not found")
            return

        bot_type = config['bot_type']
        bot_class = bot_registry.get_model(f"{bot_type}.bot")
        if not bot_class:
            logger.error(f"Unknown bot model: {bot_type}.bot")
            return

        # We pass the manager to the constructor
        bot_instance = bot_class(bot_id, manager=self)
        self.bots[bot_id] = bot_instance
        if self.loop:
            asyncio.run_coroutine_threadsafe(bot_instance.start(), self.loop)

    def remove_bot(self, bot_id: int):
        if bot_id in self.bots:
            bot = self.bots[bot_id]
            if self.loop:
                future = asyncio.run_coroutine_threadsafe(bot.stop(), self.loop)
                try:
                    future.result(timeout=5)
                except Exception as e:
                    logger.error(f"Error stopping bot {bot_id}: {e}")
            del self.bots[bot_id]

        # We delete all handles associated with this bot from other bots
        for req_bot in self.bots.values():
            req_bot.dynamics.pop(bot_id, None)

        self.graph_hashes.pop(bot_id, None)

        config = get_bot_config(bot_id)
        if config and 'data_db_path' in config:
            db_path = config['data_db_path']
            if os.path.exists(db_path):
                threading.Timer(3.0, self._delete_file, args=[db_path]).start()
                logger.info(f"Scheduled deletion of {db_path} in 3 seconds")

    async def request_exchange(self, requester_id: int, target_id: int,
                               mapping: dict) -> ExchangeHandle:
        """
        Creates or returns an existing ExchangeHandle for data exchange.
        mapping: { "local_name": ["list", "keywords", "targets"] }
        """
        requester = self.bots.get(requester_id)
        target = self.bots.get(target_id)
        if not requester:
            raise KeyError(f"Requester bot {requester_id} not found")
        if not target:
            raise KeyError(f"Target bot {target_id} not found")

        # If there is already a ready handle, we return it
        if target_id in requester.dynamics and isinstance(requester.dynamics[target_id], ExchangeHandle):
            return requester.dynamics[target_id]

        caps = target.get_capabilities()   #synchronous call
        handle = ExchangeHandle(target_id)

        for local_name, keywords in mapping.items():
            matched = None
            for internal_key, cap in caps.items():
                if any(kw in cap["keywords"] for kw in keywords):
                    matched = internal_key
                    break
            if not matched:
                raise ValueError(f"No capability matching keywords {keywords} in bot {target_id}")

            cap = caps[matched]
            handle.add_data_access(local_name, cap.get("getter"), cap.get("setter"))

        # We save the handle in the requesting bot
        requester.dynamics[target_id] = handle
        return handle
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
        if bot_id in self.bots and self.loop:
            asyncio.run_coroutine_threadsafe(self.bots[bot_id].start(), self.loop)

    def stop_bot(self, bot_id: int):
        if bot_id in self.bots and self.loop:
            asyncio.run_coroutine_threadsafe(self.bots[bot_id].stop(), self.loop)

    def shutdown(self):
        for bot in self.bots.values():
            if self.loop:
                asyncio.run_coroutine_threadsafe(bot.stop(), self.loop)