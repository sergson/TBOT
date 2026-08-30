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

@auto_reg
class BaseBot(ABC):
    """Base class for all bots."""
    _name = "base.bot"

    def __init__(self, bot_id: int, manager=None):
        self.bot_id = bot_id
        self.running = False
        self.task = None
        self.manager = manager
        self.dynamics: Dict[int, Any] = {}
        self.config_dirty = False
        self.db_path = f"data/bot_{bot_id}.db"

        # Initialize ORM environment
        self.env = DBSQLite3(self.db_path, registry=MODEL_REGISTRY)

        # Explicitly initialize the settings table (created on first access)
        _ = self.env['bot.settings']

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