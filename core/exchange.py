# core/exchange.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

from typing import Dict, Callable, Optional, Any
from .logger import perf_logger
import contextvars

logger = perf_logger.get_logger('exchange', 'app')

_owner_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    'exchange_owner', default=None)

def get_exchange_owner() -> Optional[str]:
    return _owner_ctx.get()

class ExchangeHandle:
    def __init__(self, target_id: int):
        self.target_id = target_id
        self.owner_label: Optional[str] = None   # NEW
        self.data_access: Dict[str, Any] = {}
        self.setter_access: Dict[str, Any] = {}

    def add_data_access(self, local_name, getter, setter):
        if getter is not None:
            self.data_access[local_name] = getter
        if setter is not None:
            self.setter_access[local_name] = setter

    async def get(self, data_name: str, *args, **kwargs) -> Any:
        if data_name not in self.data_access:
            raise ValueError(
                f"No getter for '{data_name}' in exchange with bot "
                f"{self.target_id}")
        getter = self.data_access[data_name]
        token = None
        if self.owner_label:
            token = _owner_ctx.set(self.owner_label)
        try:
            return await getter(*args, **kwargs)
        finally:
            if token is not None:
                _owner_ctx.reset(token)

    async def set(self, data_name: str, value: Any) -> Any:
        if data_name not in self.setter_access:
            raise ValueError(
                f"No setter for '{data_name}' in exchange with bot "
                f"{self.target_id}")
        setter = self.setter_access[data_name]
        token = None
        if self.owner_label:
            token = _owner_ctx.set(self.owner_label)
        try:
            return await setter(value)
        finally:
            if token is not None:
                _owner_ctx.reset(token)
