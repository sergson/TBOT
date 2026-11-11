# modules/broker/models.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import os

from core import auto_reg, BaseBot
from core.database import get_bot_config, update_bot_status
from core.logger import perf_logger

from .lib.key_vault import KeyVault
from .lib.order_updates import OrderUpdateQueue
from .lib.broker_service import BrokerService
from .lib.exchange_directory import (
    get_exchange_options as _get_exchange_options
)
from .lib.exchange_directory import build_client_id_params, get_workaround

logger = perf_logger.get_logger('broker_bot', 'app')


@auto_reg
class BrokerBot(BaseBot):
    _name = "broker.bot"
    _inherit = "base.bot"

    def __init__(self, bot_id: int, manager=None):
        super().__init__(bot_id, manager)
        self.config = get_bot_config(bot_id)
        self.logger = perf_logger.get_logger(f"broker_{bot_id}", "app")

        self._ui_password: str | None = None
        self._vault = KeyVault(
            env=self.env, logger=self.logger,
            password_provider=self._password_provider)
        self._order_updates = OrderUpdateQueue(self.logger)
        self._service = BrokerService(
            broker_bot=self, key_vault=self._vault,
            order_update_queue=self._order_updates)

    def _password_provider(self):
        if self._ui_password:
            return self._ui_password
        return os.environ.get('TBOT_KEY_PASSWORD')

    async def start(self):
        if self.running:
            return
        self.running = True
        await self._service.start()
        update_bot_status(self.bot_id, 'running')
        self.logger.info("Broker bot started")

    async def stop(self):
        self.running = False
        await self._service.stop()
        update_bot_status(self.bot_id, 'stopped')
        self.logger.info("Broker bot stopped")

    async def on_config_updated(self):
        self.config = get_bot_config(self.bot_id) or self.config
        self.logger.info("Broker config updated")

    def _close_db(self):
        if hasattr(self, 'env'):
            self.env.close()

    # ---------- capabilities (plan v8 §2.6) ----------

    def get_capabilities(self):
        return {
            # Public
            "get_ohlcv": {
                "keywords": ["ohlcv", "candles"],
                "getter": self._cap_get_ohlcv, "setter": None},
            "get_latest_price": {
                "keywords": ["latest_price", "current_price", "price"],
                "getter": self._cap_get_latest_price, "setter": None},
            "get_trades": {
                "keywords": ["trades", "recent_trades", "raw_trades"],
                "getter": self._cap_get_trades, "setter": None},
            "get_markets": {
                "keywords": ["markets"], "getter": self._cap_get_markets,
                "setter": None},
            "get_market": {
                "keywords": ["market"], "getter": self._cap_get_market,
                "setter": None},
            "get_symbols": {
                "keywords": ["symbols"], "getter": self._cap_get_symbols,
                "setter": None},
            "get_supported_market_types": {
                "keywords": ["market_types"],
                "getter": self._cap_get_supported_market_types,
                "setter": None},
            "get_exchange_options": {
                "keywords": ["exchange_options"],
                "getter": self._cap_get_exchange_options, "setter": None},
            "get_broker_status": {
                "keywords": ["broker_status", "status", "health"],
                "getter": self._cap_get_broker_status, "setter": None},
            # Private
            "create_market_order": {
                "keywords": ["create_market_order"],
                "getter": self._cap_create_market_order, "setter": None},
            "create_limit_order": {
                "keywords": ["create_limit_order"],
                "getter": self._cap_create_limit_order, "setter": None},
            "cancel_order": {
                "keywords": ["cancel_order"],
                "getter": self._cap_cancel_order, "setter": None},
            "fetch_order": {
                "keywords": ["fetch_order"],
                "getter": self._cap_fetch_order, "setter": None},
            "fetch_open_orders": {
                "keywords": ["fetch_open_orders"],
                "getter": self._cap_fetch_open_orders, "setter": None},
            "fetch_positions": {
                "keywords": ["fetch_positions"],
                "getter": self._cap_fetch_positions, "setter": None},
            "fetch_balance": {
                "keywords": ["fetch_balance"],
                "getter": self._cap_fetch_balance, "setter": None},
            "set_leverage": {
                "keywords": ["set_leverage"],
                "getter": self._cap_set_leverage, "setter": None},
            "panic_close": {
                "keywords": ["panic_close"],
                "getter": self._cap_panic_close, "setter": None},
            # Order updates
            "get_order_updates": {
                "keywords": ["get_order_updates", "order_updates"],
                "getter": self._cap_get_order_updates, "setter": None},
            "cleanup_order_updates": {
                "keywords": ["cleanup_order_updates"],
                "getter": self._cap_cleanup_order_updates, "setter": None},
            # Keys
            "list_exchange_keys": {
                "keywords": ["list_exchange_keys", "keys"],
                "getter": self._cap_list_keys, "setter": None},
            "get_key_info": {
                "keywords": ["get_key_info"],
                "getter": self._cap_get_key_info, "setter": None},
            "unlock_keys": {
                "keywords": ["unlock_keys"],
                "getter": None, "setter": self._cap_unlock_keys},
            "invalidate_key_cache": {
                "keywords": ["invalidate_key_cache"],
                "getter": None, "setter": self._cap_invalidate_key_cache},
            "add_key": {
                "keywords": ["add_key"],
                "getter": None, "setter": self._cap_add_key},
            "delete_key": {
                "keywords": ["delete_key"],
                "getter": None, "setter": self._cap_delete_key},
            "register_key_owner": {
                "keywords": ["register_key_owner"],
                "getter": None,
                "setter": self._cap_register_key_owner},
            "unregister_key_owner": {
                "keywords": ["unregister_key_owner"],
                "getter": None,
                "setter": self._cap_unregister_key_owner},
        }

    # --- public getters ---

    async def _cap_get_ohlcv(self, exchange, market_type, symbol,
                              timeframe, limit=500):
        r = await self._service.rest_call(
            exchange, market_type, 'fetch_ohlcv',
            {'symbol': symbol, 'timeframe': timeframe, 'limit': limit})
        return [
            {'timestamp': int(row[0]) * 1_000_000,
             'open': row[1], 'high': row[2], 'low': row[3],
             'close': row[4], 'volume': row[5],
             'timestamp_scale': 'exchange_ns'}
            for row in (r.result or [])
        ]

    async def _cap_get_latest_price(self, exchange, market_type, symbol):
        return await self._service.get_latest_price(
            exchange, market_type, symbol)

    async def _cap_get_trades(self, exchange, market_type, symbol,
                              from_ns=0, to_ns=0, time_scale='exchange'):
        """Return a COPY of raw trades from the broker's deque.

        Response schema:
          {
            'trades': [
              {'exchange_ts_ns', 'tbot_utc_ns',
               'price', 'amount', 'side', 'trade_id'}, ...
            ],
            'first_trade_exchange_ts_ns': int,     # 0 if none yet (session)
            'first_trade_tbot_utc_ns':    int,
            'oldest_exchange_ts_ns':      int,     # 0 if deque empty
            'oldest_tbot_utc_ns':         int,
            'worker_started_at_tbot_utc_ns': int,
          }

        time_scale ∈ {'exchange', 'tbot'} — selects filter/sort clock.
        """
        return await self._service.get_trades(
            exchange, market_type, symbol,
            from_ns=from_ns, to_ns=to_ns, time_scale=time_scale)

    async def _cap_get_markets(self, exchange, market_type):
        key = self._key_for(exchange, market_type, 'rest')
        try:
            client = await self._service.requests.get_client(key, None)
        except Exception as e:
            self.logger.warning(
                f"get_markets({exchange}/{market_type}) failed: {e}")
            return {}
        return dict(client.markets) if client else {}

    async def _cap_get_market(self, exchange, market_type, symbol):
        markets = await self._cap_get_markets(exchange, market_type)
        return markets.get(symbol)

    async def _cap_get_symbols(self, exchange, market_type):
        markets = await self._cap_get_markets(exchange, market_type)
        mt_ccxt = self._ccxt_market_type(exchange, market_type)
        return sorted(s for s, m in markets.items()
                      if m.get('type', '').lower() == mt_ccxt)

    async def _cap_get_supported_market_types(self, exchange):
        # Probe via markets of the default market_type; union of types.
        markets = await self._cap_get_markets(exchange, 'futures')
        if not markets:
            markets = await self._cap_get_markets(exchange, 'spot')
        unified = set()
        for m in markets.values():
            t = m.get('type', '').lower()
            if t == 'spot':
                unified.add('spot')
            elif t in ('future', 'swap', 'linear', 'delivery'):
                unified.add('futures')
        return sorted(unified) or ['spot']

    @staticmethod
    async def _cap_get_exchange_options():
        return _get_exchange_options()

    async def _cap_get_broker_status(self):
        st = self._service.get_status()
        st['keys_count'] = len(await self._vault.list_keys())
        st['healthy'] = st['running']
        return st

    async def _cap_add_key(self, data):
        key_id = await self._vault.add_key(
            data['exchange'], data['market_type'],
            data.get('label', ''),
            data['api_key'], data['api_secret'],
            data.get('mode', 'plain'),
            data.get('password'))
        return {'key_id': key_id}

    async def _cap_delete_key(self, data):
        await self._vault.delete_key(data['key_id'])
        return {'ok': True}

    async def _cap_register_key_owner(self, data):
        self._service.register_key_owner(
            data['key_id'], data['owner_bot_id'])
        return {'ok': True}

    async def _cap_unregister_key_owner(self, data):
        self._service.unregister_key_owner(
            data['key_id'], caller_id=data.get('owner_bot_id'))
        return {'ok': True}

    # --- private getters ---

    async def _cap_create_market_order(self, key_id, symbol, side, amount,
                                        position_id, signal_number,
                                        attempt_n, reduce_only=False,
                                        cid: str | None = None):
        info = await self._vault.get_key_info(key_id)
        if not info:
            raise ValueError(f"Unknown broker key_id: {key_id}")
        ex, mt = info['exchange'], info['market_type']
        params = {}
        if reduce_only:
            params['reduceOnly'] = True
        if cid:
            params.update(build_client_id_params(ex, 'create', cid))
        r = await self._service.rest_call(
            ex, mt, 'create_order',
            {'symbol': symbol, 'type': 'market', 'side': side,
             'amount': amount, 'price': None, 'params': params},
            key_id=key_id, timeout_sec=30.0)
        return self._normalize_order(r.result)

    async def _cap_create_limit_order(self, key_id, symbol, side, amount,
                                       price, position_id, signal_number,
                                       attempt_n, reduce_only=False,
                                       cid: str | None = None):
        info = await self._vault.get_key_info(key_id)
        if not info:
            raise ValueError(f"Unknown broker key_id: {key_id}")
        ex, mt = info['exchange'], info['market_type']
        params = {}
        if reduce_only:
            params['reduceOnly'] = True
        if cid:
            params.update(build_client_id_params(ex, 'create', cid))
        r = await self._service.rest_call(
            ex, mt, 'create_order',
            {'symbol': symbol, 'type': 'limit', 'side': side,
             'amount': amount, 'price': price, 'params': params},
            key_id=key_id, timeout_sec=30.0)
        return self._normalize_order(r.result)

    async def _cap_cancel_order(self, key_id, symbol, order_id, cid=None):
        info = await self._vault.get_key_info(key_id)
        if not info:
            raise ValueError(f"Unknown broker key_id: {key_id}")
        ex, mt = info['exchange'], info['market_type']
        params = build_client_id_params(ex, 'cancel', cid)
        try:
            r = await self._service.rest_call(
                ex, mt, 'cancel_order',
                {'id': order_id, 'symbol': symbol, 'params': params},
                key_id=key_id, timeout_sec=30.0)
        except Exception as e:
            err_str = str(e)
            # Order already gone (filled/canceled/expired) — this is not a
            # failure. Caller (_await_terminal_state) will fetch and set
            # the real terminal state (filled/canceled/etc.).
            if ('-2011' in err_str
                    or 'Unknown order' in err_str
                    or type(e).__name__ == 'OrderNotFound'):
                self.logger.info(
                    f"cancel_order: {order_id} already gone (-2011) "
                    f"— returning success, caller will fetch state")
                return True
            self.logger.warning(
                f"cancel_order {order_id} failed: {type(e).__name__}: {e}")
            raise
        return True

    async def _cap_fetch_order(self, key_id, symbol, order_id, cid=None):
        info = await self._vault.get_key_info(key_id)
        if not info:
            raise ValueError(f"Unknown broker key_id: {key_id}")
        ex, mt = info['exchange'], info['market_type']
        params = build_client_id_params(ex, 'fetch', cid)
        r = await self._service.rest_call(
            ex, mt, 'fetch_order',
            {'id': order_id, 'symbol': symbol, 'params': params},
            key_id=key_id)
        return r.result

    async def _cap_fetch_open_orders(self, key_id, symbol):
        info = await self._vault.get_key_info(key_id)
        if not info:
            raise ValueError(f"Unknown broker key_id: {key_id}")
        ex, mt = info['exchange'], info['market_type']
        r = await self._service.rest_call(
            ex, mt, 'fetch_open_orders',
            {'symbol': symbol}, key_id=key_id)
        return r.result or []

    async def _cap_fetch_positions(self, key_id, symbols=None):
        info = await self._vault.get_key_info(key_id)
        if not info:
            raise ValueError(f"Unknown broker key_id: {key_id}")
        ex, mt = info['exchange'], info['market_type']
        params = {}
        if symbols:
            params['symbols'] = symbols
        r = await self._service.rest_call(
            ex, mt, 'fetch_positions', params, key_id=key_id)
        return r.result or []

    async def _cap_fetch_balance(self, key_id, currency=None):
        info = await self._vault.get_key_info(key_id)
        if not info:
            raise ValueError(f"Unknown broker key_id: {key_id}")
        ex, mt = info['exchange'], info['market_type']
        balance_type = get_workaround(ex, mt, 'balance_type', None)
        # ccxt fetch_balance(self, params={}) — type must be INSIDE params
        # dict, not as top-level kwarg.
        ccxt_params = {'params': {'type': balance_type}} if balance_type \
                      else {'params': {}}
        r = await self._service.rest_call(
            ex, mt, 'fetch_balance', ccxt_params, key_id=key_id)
        if currency and isinstance(r.result, dict):
            return r.result.get(currency, {})
        return r.result

    async def _cap_set_leverage(self, key_id, symbol, leverage):
        info = await self._vault.get_key_info(key_id)
        if not info:
            raise ValueError(f"Unknown broker key_id: {key_id}")
        ex, mt = info['exchange'], info['market_type']
        r = await self._service.rest_call(
            ex, mt, 'set_leverage',
            {'leverage': leverage, 'symbol': symbol}, key_id=key_id)
        return True

    async def _cap_panic_close(self, key_id, symbol):
        """Composite: cancel all + fetch positions + market reduce-only close."""
        info = await self._vault.get_key_info(key_id)
        if not info:
            raise ValueError(f"Unknown broker key_id: {key_id}")
        ex, mt = info['exchange'], info['market_type']

        # 1. cancel all open
        try:
            r = await self._service.rest_call(
                ex, mt, 'fetch_open_orders', {'symbol': symbol},
                key_id=key_id)
            orders = r.result or []
        except Exception:
            orders = []
        orders_canceled = 0
        for o in orders:
            oid = o.get('id')
            if not oid:
                continue
            try:
                await self._service.rest_call(
                    ex, mt, 'cancel_order',
                    {'id': oid, 'symbol': symbol, 'params': {}},
                    key_id=key_id)
                orders_canceled += 1
            except Exception:
                pass

        # 2. fetch positions
        try:
            r = await self._service.rest_call(
                ex, mt, 'fetch_positions', {}, key_id=key_id)
            positions = r.result or []
        except Exception:
            positions = []

        size = 0.0
        close_side = 'sell'
        for p in positions:
            try:
                contracts = float(p.get('contracts') or 0)
            except (TypeError, ValueError):
                contracts = 0.0
            if abs(contracts) > 0:
                size = abs(contracts)
                close_side = 'sell' if contracts > 0 else 'buy'
                break

        if size == 0.0:
            return {'closed_size': 0.0, 'errors': [],
                    'orders_canceled': orders_canceled,
                    'avg_fill_price': None}

        try:
            r_close = await self._service.rest_call(
                ex, mt, 'create_order',
                {'symbol': symbol, 'type': 'market', 'side': close_side,
                 'amount': size, 'price': None,
                 'params': {'reduceOnly': True}},
                key_id=key_id, timeout_sec=30.0)
            filled_price = (r_close.result or {}).get('average')
            return {'closed_size': size, 'errors': [],
                    'orders_canceled': orders_canceled,
                    'avg_fill_price': filled_price}
        except Exception as e:
            return {'closed_size': 0.0, 'errors': [str(e)],
                    'orders_canceled': orders_canceled,
                    'avg_fill_price': None}

    # --- order updates ---

    async def _cap_get_order_updates(self, owner_bot_id, since_id,
                                      timeout_sec=0.3):
        records = await self._order_updates.poll(
            owner_bot_id, since_id, timeout_sec)
        return [{'id': r.id, 'cid': r.cid, 'update': r.update}
                for r in records]

    async def _cap_cleanup_order_updates(self, owner_bot_id):
        await self._order_updates.cleanup(owner_bot_id)

    # --- keys ---

    async def _cap_list_keys(self, exchange=None, market_type=None):
        return await self._vault.list_keys(exchange, market_type)

    async def _cap_get_key_info(self, key_id):
        return await self._vault.get_key_info(key_id)

    async def _cap_unlock_keys(self, data):
        password = data.get('password') if isinstance(data, dict) else None
        if not password:
            raise ValueError("password required")
        result = await self._vault.unlock(password)
        self._ui_password = password
        return result

    async def _cap_invalidate_key_cache(self, data):
        key_id = data.get('key_id') if isinstance(data, dict) else None
        if key_id:
            await self._vault.invalidate(key_id)

    # --- helpers ---

    def _key_for(self, exchange, market_type, kind):
        from .lib.broker_service import ConnectionKey
        return ConnectionKey(exchange, market_type, kind,
                              self.config.get('default_provider', 'ccxt'))

    @staticmethod
    def _ccxt_market_type(exchange, market_type):
        from .lib.exchange_directory import get_ccxt_market_type
        return get_ccxt_market_type(exchange, market_type)

    @staticmethod
    def _normalize_order(order):
        if not isinstance(order, dict):
            return order
        return {
            'id': order.get('id'),
            'cid': order.get('clientOrderId'),
            'status': order.get('status'),
            'filled': order.get('filled'),
            'average': order.get('average'),
            'fee': order.get('fee'),
            'info': order.get('info'),
        }