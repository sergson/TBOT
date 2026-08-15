# core/bot_manager.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import asyncio
import os
import threading
import time
from .database import get_all_bots, get_bot_config, DATA_DIR
from .logger import perf_logger
from .registry import bot_registry
from .exchange import ExchangeHandle

logger = perf_logger.get_logger('bot_manager', 'app')

class BotManager:
    def __init__(self):
        self.bots = {}
        self.loop = None
        self._loop_thread = None
        self._shutdown_event = threading.Event()
        self.graph_hashes = {}

    def start_loop_in_thread(self):
        self.loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._run_loop, name="BotEventLoop", daemon=False)
        self._loop_thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    def load_bots(self):
        bots_data = get_all_bots()
        for bot in bots_data:
            # Create instance (without starting)
            if bot['id'] not in self.bots:
                self._create_instance(bot['id'])
            # If bot should be running, start it
            if bot['status'] == 'running':
                self.start_bot(bot['id'])

    def _create_instance(self, bot_id: int):
        """Creates a bot instance and stores it in self.bots, but does not start it."""
        config = get_bot_config(bot_id)
        if not config:
            logger.error(f"Bot {bot_id} config not found")
            return
        bot_type = config['bot_type']
        bot_class = bot_registry.get_model(f"{bot_type}.bot")
        if not bot_class:
            logger.error(f"Unknown bot model: {bot_type}.bot")
            return

        bot_instance = bot_class(bot_id, manager=self)
        self.bots[bot_id] = bot_instance

    def _on_bot_start_done(self, future):
        try:
            future.result()
        except Exception as e:
            logger.error(f"Bot start failed: {e}")

    def add_bot(self, bot_id: int):
        """Creates and immediately starts the bot (used when adding a new bot)."""
        self._create_instance(bot_id)
        self.start_bot(bot_id)

    def remove_bot(self, bot_id: int, delete_db: bool = True):
        if bot_id in self.bots:
            self.stop_bot(bot_id)
            bot = self.bots[bot_id]
            if hasattr(bot, '_close_db'):
                bot._close_db()
            del self.bots[bot_id]

        # Clean up links (dynamics of other bots and graph hashes)
        for req_bot in self.bots.values():
            req_bot.dynamics.pop(bot_id, None)
        self.graph_hashes.pop(bot_id, None)

        # Database file is now deleted only on next startup
        # or via the "Clean orphan DBs" button. This prevents errors
        # due to locked files.

    def _delete_file(self, path: str, max_attempts: int = 5, delay: float = 1.0):
        for attempt in range(1, max_attempts + 1):
            if not os.path.exists(path):
                return
            try:
                os.remove(path)
                logger.info(f"Deleted database file: {path}")
                return
            except OSError as e:
                if attempt == max_attempts:
                    logger.error(f"Failed to delete {path} after {max_attempts} attempts: {e}")
                else:
                    time.sleep(delay)

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

        caps = target.get_capabilities()
        if asyncio.iscoroutinefunction(getattr(target, 'get_capabilities', None)):
            caps = await caps

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

    def start_bot(self, bot_id: int):
        if bot_id in self.bots and self.loop:
            bot = self.bots[bot_id]
            if not bot.running:  # additional protection against double start
                future = asyncio.run_coroutine_threadsafe(bot.start(), self.loop)
                future.add_done_callback(self._on_bot_start_done)
        else:
            logger.warning(f"Bot {bot_id} not found in manager or event loop not ready")

    def stop_bot(self, bot_id: int):
        if bot_id in self.bots:
            bot = self.bots[bot_id]
            if self.loop:
                future = asyncio.run_coroutine_threadsafe(bot.stop(), self.loop)
                try:
                    future.result(timeout=5)
                except Exception as e:
                    logger.error(f"Error stopping bot {bot_id}: {e}")

    def shutdown(self):
        self._shutdown_event.set()
        for bot_id in list(self.bots.keys()):
            self.stop_bot(bot_id)
        self.bots.clear()
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._loop_thread.join(timeout=10)