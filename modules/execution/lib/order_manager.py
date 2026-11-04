# modules/execution/lib/order_manager.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

"""Executor-side OrderManager shim.

All exchange actions live in broker (plan v8 D7). This class only
proxies to broker capabilities. It holds no client, no queue, no
workers.

Kept for backward-compat with SignalManager until PR-CLEAN-1.
"""

DRY_RUN = True

def _assert_live_allowed(method_name: str) -> None:
    if DRY_RUN:
        raise RuntimeError(f"{method_name}")

def make_client_order_id(bot_id, position_id, signal_number: int,
                          attempt_n: int) -> str:
    """Deterministic client_order_id.

    Format: {bot_id}-{pos[:8]}-{sig}-{att}, truncated to 36 chars
    (Binance max). bot_id prefix prevents collisions between executors
    sharing one broker.
    """
    pos = position_id or 'orphan'
    return f"{bot_id}-{pos[:8]}-{signal_number}-{attempt_n}"[:36]


class OrderManager:
    """Thin proxy to broker. Does not connect to any exchange directly."""

    def __init__(self, broker_handle, key_id, symbol, config, logger,
                 exchange_name, market_type, bot_id):
        self.broker = broker_handle
        self.key_id = key_id
        self.symbol = symbol
        self.logger = logger
        self.bot_id = bot_id
        self.exchange_name = exchange_name
        self.market_type = market_type
        self.leverage = int(config.get('leverage', 0) or 0)

    # ---------- private ops (proxy to broker) ----------

    async def create_market_order(self, side, amount, position_id,
                                   signal_number, attempt_n,
                                   reduce_only=False):
        _assert_live_allowed("create_market_order")
        cid = make_client_order_id(
            self.bot_id, position_id, signal_number, attempt_n)
        return await self.broker.get(
            "create_market_order",
            key_id=self.key_id, symbol=self.symbol, side=side,
            amount=amount, position_id=position_id,
            signal_number=signal_number, attempt_n=attempt_n,
            reduce_only=reduce_only, cid=cid)

    async def create_limit_order(self, side, amount, price, position_id,
                                  signal_number, attempt_n,
                                  reduce_only=False):
        _assert_live_allowed("create_limit_order")
        cid = make_client_order_id(
            self.bot_id, position_id, signal_number, attempt_n)
        return await self.broker.get(
            "create_limit_order",
            key_id=self.key_id, symbol=self.symbol, side=side,
            amount=amount, price=price, position_id=position_id,
            signal_number=signal_number, attempt_n=attempt_n,
            reduce_only=reduce_only, cid=cid)

    async def cancel_order(self, order_id, client_order_id=None):
        _assert_live_allowed("cancel_order")
        return await self.broker.get(
            "cancel_order",
            key_id=self.key_id, symbol=self.symbol,
            order_id=order_id, cid=client_order_id)

    async def fetch_order(self, order_id, client_order_id=None):
        _assert_live_allowed("fetch_order")
        return await self.broker.get(
            "fetch_order",
            key_id=self.key_id, symbol=self.symbol,
            order_id=order_id, cid=client_order_id)

    async def fetch_open_orders(self):
        _assert_live_allowed("fetch_open_orders")
        return await self.broker.get(
            "fetch_open_orders",
            key_id=self.key_id, symbol=self.symbol)

    async def fetch_positions(self, symbols=None):
        _assert_live_allowed("fetch_positions")
        return await self.broker.get(
            "fetch_positions",
            key_id=self.key_id, symbols=symbols or [self.symbol])

    async def fetch_balance(self, currency=None):
        _assert_live_allowed("fetch_balance")
        return await self.broker.get(
            "fetch_balance",
            key_id=self.key_id, currency=currency)

    async def set_leverage(self, leverage):
        _assert_live_allowed("set_leverage")
        return await self.broker.get(
            "set_leverage",
            key_id=self.key_id, symbol=self.symbol, leverage=leverage)

    # ---------- no-op / removed ----------

    async def wait_rate_limit(self):
        # Rate limiting lives in broker.
        return

    async def close(self):
        # Nothing to close — broker owns the client.
        return

    @property
    def exchange(self):
        raise RuntimeError(
            "OrderManager is a shim; exchange lives in broker. "
            "Use broker capability get_market/get_markets instead.")

    @property
    def markets(self):
        raise RuntimeError(
            "OrderManager is a shim; use broker.get_market capability.")

    async def _ensure_exchange(self):
        raise RuntimeError(
            "OrderManager is a shim; use broker capabilities instead.")