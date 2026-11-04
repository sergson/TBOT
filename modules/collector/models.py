# modules/collector/models.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import asyncio

from core import auto_reg, BaseBot
from core.database import (
    Model, Integer, Float, Char,
    get_bot_config, update_bot_status, get_all_bots,
)
from core.logger import perf_logger
from .lib.utils import safe_table_name


def timeframe_to_seconds(tf: str) -> int:
    unit = tf[-1]
    value = int(tf[:-1])
    if unit == 'm': return value * 60
    elif unit == 'h': return value * 3600
    elif unit == 'd': return value * 86400
    else: raise ValueError(f"Unsupported timeframe: {tf}")


class Candle(Model):
    _name = 'collector.candle'
    _table = None
    _dynamic = True

    timestamp = Integer(primary_key=True, required=True)
    open = Float()
    high = Float()
    low = Float()
    close = Float()
    volume = Float()
    tbot_utc_stamp_ns = Integer(index=True)


@auto_reg
class CollectorBot(BaseBot):
    _name = "collector.bot"
    _inherit = "base.bot"

    def __init__(self, bot_id: int, manager=None):
        super().__init__(bot_id, manager)
        self.config = get_bot_config(bot_id)
        self.logger = perf_logger.get_logger(f'collector_{bot_id}',
                                              'collector')
        self._resolve_exchange_provider()

        self.initial_load_done = False
        self._broker_handle = None
        self._broker_id = None
        self._broker_restart_last_ns = 0
        self._candle_table_name = None

    # ---------- broker ----------

    def _find_broker_id(self):
        if self.manager is not None:
            bid = getattr(self.manager, '_broker_id', None)
            if bid is not None:
                return bid
        for b in get_all_bots():
            if b['type'] == 'broker':
                return b['id']
        return None

    async def _ensure_broker(self) -> bool:
        if not self._broker_id or not self.manager:
            return False
        status = self.manager.get_bot_status(self._broker_id)
        if status != 'running':
            now_ns = self.get_time_ns()
            if now_ns - self._broker_restart_last_ns > 10 * 1_000_000_000:
                self._broker_restart_last_ns = now_ns
                self.manager.restart_bot(self.bot_id, self._broker_id)
            await asyncio.sleep(2)
            return False
        try:
            info = await asyncio.wait_for(
                self._broker_handle.get("get_broker_status"), timeout=5)
            if info.get('healthy'):
                return True
            return False
        except asyncio.TimeoutError:
            self.logger.warning("Broker health probe timeout")
            now_ns = self.get_time_ns()
            if now_ns - self._broker_restart_last_ns > 10 * 1_000_000_000:
                self._broker_restart_last_ns = now_ns
                self.manager.force_restart_bot(self.bot_id, self._broker_id)
            return False
        except Exception as e:
            self.logger.warning(f"Broker health probe: {e}")
            return False

    # ---------- lifecycle ----------

    async def start(self):
        if self.running:
            return
        self.running = True

        self._broker_id = self._find_broker_id()
        if self._broker_id is None:
            self.logger.error(
                "No broker bot configured — collector cannot start")
            update_bot_status(self.bot_id, 'stopped')
            self.running = False
            return

        await self.setup_exchange(self._broker_id, {
            "get_ohlcv": ["ohlcv", "candles"],
            "get_symbols": ["symbols"],
            "get_markets": ["markets"],
            "get_supported_market_types": ["market_types"],
            "get_broker_status": ["broker_status", "status", "health"],
        })
        self._broker_handle = self.get_exchange(self._broker_id)

        # Cache table_name
        symbol = self.config.get('symbol')
        if symbol:
            self._candle_table_name = safe_table_name(symbol)

        self.task = asyncio.create_task(self._run())
        update_bot_status(self.bot_id, 'running')
        self.logger.info(f"Collector {self.bot_id} started")

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        update_bot_status(self.bot_id, 'stopped')
        self.logger.info(f"Collector {self.bot_id} stopped")

    def _get_candle_manager(self):
        if not self._candle_table_name:
            symbol = self.config.get('symbol')
            if not symbol:
                raise ValueError("Symbol not set")
            self._candle_table_name = safe_table_name(symbol)
        return self.env.get_model_manager(
            'collector.candle', self._candle_table_name)

    async def _run(self):
        self.logger.info(
            f"Collector bot {self.bot_id} starting with config: {self.config}")

        while self.running:
            if self.config_dirty:
                await self.on_config_updated()
                self.config_dirty = False
                continue

            if not await self._ensure_broker():
                await asyncio.sleep(5)
                continue

            try:
                symbol = self.config.get('symbol')
                timeframe = self.config.get('timeframe')
                candles_limit = int(self.config.get('candles_limit') or 0)
                if not (symbol and timeframe and candles_limit > 0):
                    self.logger.error(...)
                    await asyncio.sleep(10)
                    continue
            except (TypeError, ValueError) as e:
                self.logger.error(f"Invalid config: {e}")
                await asyncio.sleep(10)
                continue

            fetch_interval_sec = timeframe_to_seconds(timeframe)
            # The forming candle must be updated more frequently than once per
            # full timeframe — otherwise, the UI will "freeze" for the entire duration.
            # Constraint: no more often than every 5s (rate-limit) and no less often than every 60s
            # (while not particularly meaningful for 1h/1d timeframes, it does no harm).
            fetch_interval = max(5, min(fetch_interval_sec // 4, 60))

            candle_manager = self._get_candle_manager()

            try:
                if not self.initial_load_done:
                    count = candle_manager.search_count([])
                    if count < candles_limit:
                        self.logger.info(
                            f"Initial history load: {candles_limit} candles")
                        fetch_limit = candles_limit
                    else:
                        fetch_limit = 5
                    self.initial_load_done = True
                else:
                    fetch_limit = 5

                candles = await self._broker_handle.get(
                    "get_ohlcv",
                    exchange=self.config['exchange'],
                    market_type=self.config['market_type'],
                    symbol=symbol,
                    timeframe=timeframe,
                    limit=fetch_limit,
                )
                if not candles:
                    await asyncio.sleep(fetch_interval)
                    continue

                self._upsert_candles(candle_manager, candles)

                # Prune
                count = candle_manager.search_count([])
                if count > candles_limit * 1.1:
                    excess = count - candles_limit
                    to_delete = candle_manager.search(
                        [], order='timestamp ASC', limit=excess)
                    if to_delete:
                        to_delete.unlink()

                self.logger.info(f"Processed candles for {symbol}")

            except Exception as e:
                self.logger.error(
                    f"Error in collector loop: {e}", exc_info=True)

            await asyncio.sleep(fetch_interval)

    def _upsert_candles(self, candle_manager, candles):
        """Batch check + batch create/update. candles — list of dicts with
        int ns timestamps (broker returns exchange_ns)."""
        if not candles:
            return
        ts_list = [int(c['timestamp']) for c in candles]
        existing = {}
        chunk = 500
        for i in range(0, len(ts_list), chunk):
            sub = ts_list[i:i + chunk]
            for r in candle_manager.search([('timestamp', 'in', sub)]):
                existing[r.timestamp] = r

        to_create = []
        now_ns = self.get_time_ns()
        for c in candles:
            ts = int(c['timestamp'])
            vals = {
                'open': c['open'],
                'high': c['high'],
                'low': c['low'],
                'close': c['close'],
                'volume': c['volume'],
                'tbot_utc_stamp_ns': now_ns,
            }
            rec = existing.get(ts)
            if rec is not None:
                rec.write(vals)
            else:
                to_create.append({'timestamp': ts, **vals})
        if to_create:
            candle_manager.create(to_create)

    async def on_config_updated(self):
        old_symbol = self.config.get('symbol')
        old_table = self._candle_table_name

        self.config = get_bot_config(self.bot_id)
        self._resolve_exchange_provider()
        self.initial_load_done = False

        new_symbol = self.config.get('symbol')
        new_table = safe_table_name(new_symbol) if new_symbol else None
        if old_table and new_table and old_table != new_table:
            try:
                self.env.drop_table(old_table)
                self.logger.info(f"Dropped old table {old_table}")
            except Exception as e:
                self.logger.warning(
                    f"Dropping {old_table} failed: {e}")
        self._candle_table_name = new_table

    def get_capabilities(self):
        return {
            "ohlcv_data": {
                "keywords": ["candles", "ohlcv", "quotes",
                              "market_data", "stonks"],
                "getter": self._get_ohlcv_data,
                "setter": None,
            },
            "symbol": {
                "keywords": ["symbol", "pair", "ticker"],
                "getter": self._get_symbol,
                "setter": None,
            },
        }

    async def _get_ohlcv_data(self, limit=500):
        candle_manager = self._get_candle_manager()
        records = candle_manager.search(
            [], order='timestamp DESC', limit=limit)
        result = records.read()
        for row in result:
            row['timestamp_scale'] = 'exchange_ns'
            row['tbot_utc_stamp_ns_scale'] = 'tbot_utc_ns'
        return result

    async def _get_symbol(self):
        return self.config['symbol']

    def _close_db(self):
        if hasattr(self, 'env'):
            self.env.close()