# core/exchange.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

from typing import Dict, Callable, Optional, Any

class ExchangeHandle:
    """An intermediary for accessing another bot's data."""
    def __init__(self, target_bot_id: int):
        self.target_id = target_bot_id
        self._getters: Dict[str, Callable] = {}
        self._setters: Dict[str, Callable] = {}

    def add_data_access(self, local_name: str, getter: Optional[Callable], setter: Optional[Callable]):
        if getter is not None:
            self._getters[local_name] = getter
        if setter is not None:
            self._setters[local_name] = setter

    async def get(self, data_name: str, *args, **kwargs) -> Any:
        if data_name not in self._getters:
            raise KeyError(f"No getter for '{data_name}' in exchange with bot {self.target_id}")
        return await self._getters[data_name](*args, **kwargs)

    async def set(self, data_name: str, value) -> None:
        if data_name not in self._setters:
            raise KeyError(f"No setter for '{data_name}' in exchange with bot {self.target_id}")
        await self._setters[data_name](value)