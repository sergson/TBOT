# core/base_bot.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
import asyncio
from .registry import auto_reg
from .database import DBSQLite3, MODEL_REGISTRY
from core.time_sync import time_sync_service
import time
from .logger import perf_logger

@auto_reg
class BaseBot(ABC):
    """Base class for all bots."""
    _name = "base.bot"

    def __init__(self, bot_id: int, manager=None):
        self.bot_id = bot_id
        self.logger = perf_logger.get_logger(f"base_bot_{bot_id}", "app")
        self.running = False
        self.task = None
        self.manager = manager
        self.dynamics: Dict[int, Any] = {}
        self.config_dirty = False
        self.db_path = f"data/bot_{bot_id}.db"
        self.time_sync = time_sync_service

        # Initialize ORM environment
        self.env = DBSQLite3(self.db_path, registry=MODEL_REGISTRY)

        # Explicitly initialize the settings table (created on first access)
        _ = self.env['bot.settings']

        self.time_scale_normalizer = {
            'tbot_utc_ns': lambda x: int(x)
        }

        # F0.7.9.A: native provider selection state. Default ccxt.
        # Populated by _resolve_exchange_provider() in subclasses AFTER
        # self.config and self.logger are set.
        self.tbotex_provider_name = 'ccxt'
        self._provider_factory = None

    def _resolve_exchange_provider(self):
        """F0.7.9.B: pick native provider for self.config.

        Call from subclass AFTER self.config = get_bot_config(...) and
        AFTER self.logger = perf_logger.get_logger(...).
        """
        from core.tbotex import get_provider, has_provider
        provider_name = (self.config or {}).get('exchange_provider', 'ccxt')
        self.tbotex_provider_name = provider_name
        self._provider_factory = None

        if provider_name == 'ccxt':
            return

        exchange = ((self.config or {}).get('exchange') or '').lower()
        market_type = ((self.config or {}).get('market_type') or 'spot').lower()

        if not has_provider(exchange, market_type):
            self.logger.warning(
                f"Bot {self.bot_id}: provider '{provider_name}' not "
                f"registered for {exchange}/{market_type} — "
                f"falling back to ccxt")
            return

        factory = get_provider(exchange, market_type)
        if factory:
            self._provider_factory = factory
            self.logger.info(
                f"Bot {self.bot_id}: using tbotex provider "
                f"'{provider_name}' for {exchange}/{market_type}")

    @abstractmethod
    async def start(self):
        """Start the bot."""
        pass

    @abstractmethod
    async def stop(self):
        """Stop the bot. Subclasses should call super().stop() or clean up."""
        pass

    @abstractmethod
    def get_capabilities(self) -> Dict[str, Dict]:
        """Return capabilities."""
        pass

    async def setup_exchange(self, target_bot_id: int, mapping: dict) -> 'ExchangeHandle':
        if not self.manager:
            raise RuntimeError("BotManager reference is not set")
        return await self.manager.request_exchange(self.bot_id, target_bot_id, mapping)

    def get_exchange(self, target_bot_id: int) -> Optional['ExchangeHandle']:
        return self.dynamics.get(target_bot_id)

    def _cleanup_dynamics(self):
        self.dynamics.clear()

    async def on_config_updated(self):
        """Called after configuration changes via the web interface."""
        pass

    @staticmethod
    def get_time_ns() -> int:
        if time_sync_service.is_synced_event.is_set():
            return time_sync_service.get_utc_ns()
        return time.time_ns()