# core/tbotex/protocol.py
# Canonical protocol for tbotex native providers.
#
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.
#
# All methods are optional at runtime — a provider only has to implement
# the subset that OrderManager / ExchangeWebSocket actually call.
from typing import Any, Callable, Optional, Protocol, runtime_checkable


@runtime_checkable
class ExchangeClientProtocol(Protocol):
    """REST + WS interface for a native exchange client.
    Implemented in specific native providers.
    """

    # --- Lifecycle ---
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def load_markets(self) -> None: ...

    # --- REST: orders ---
    async def create_order(self, symbol: str, type_: str, side: str,
                           amount: float, price: Optional[float] = None,
                           params: Optional[dict] = None) -> dict: ...
    async def cancel_order(self, order_id: str, symbol: str,
                           params: Optional[dict] = None) -> Any: ...
    async def fetch_order(self, order_id: str, symbol: str,
                          params: Optional[dict] = None) -> dict: ...
    async def fetch_open_orders(self, symbol: str) -> list: ...

    # --- REST: account ---
    async def fetch_balance(self, params: Optional[dict] = None) -> dict: ...
    async def fetch_positions(
        self, symbols: Optional[list] = None) -> list: ...

    # --- WS (optional) ---
    async def watch_orders(self, symbol: str) -> list: ...
    async def watch_order_book(self, symbol: str) -> dict: ...

    # --- Extra: useing OrderManager ---
    async def set_leverage(self, leverage: int, symbol: str) -> Any: ...
    async def close(self) -> None: ...

    # --- Properties ---
    @property
    def exchange_name(self) -> str: ...
    @property
    def market_type(self) -> str: ...
    @property
    def markets(self) -> dict: ...


# Factory signature: (config: dict, logger) -> ExchangeClientProtocol.
# type alias, not a Protocol.
# isinstance(x, TbotexFactory) not working.
# For runtime checks, use callable(x)
TbotexFactory = Callable[[dict, Any], ExchangeClientProtocol]