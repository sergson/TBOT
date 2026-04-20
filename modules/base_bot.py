# modules/base_bot.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

from abc import ABC, abstractmethod

class BaseBot(ABC):
    """Base class for all bots."""
    def __init__(self, bot_id: int):
        self.bot_id = bot_id

    @abstractmethod
    async def start(self):
        """Start the bot."""
        pass

    @abstractmethod
    async def stop(self):
        """Stop the bot."""
        pass
