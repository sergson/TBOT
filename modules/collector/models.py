# modules/collector/models.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import asyncio
import time
from typing import Dict, Any

from core import auto_reg, BaseBot
from core.database import (
    Model, Integer, Float, Char,
    get_bot_config, update_bot_status
)
from core.logger import perf_logger
from .lib.fetcher import AsyncExchangeFetcher

def timeframe_to_seconds(tf: str) -> int:
    unit = tf[-1]
    value = int(tf[:-1])
    if unit == 'm': return value * 60
    elif unit == 'h': return value * 3600
    elif unit == 'd': return value * 86400
    else: raise ValueError(f"Unsupported timeframe: {tf}")

# ----------------------------------------------------------------------
# Dynamic model for OHLCV candles
# ----------------------------------------------------------------------
class Candle(Model):
    _name = 'collector.candle'
    _table = None          # table name is dynamic, based on symbol
    _dynamic = True        # instruct the metaclass not to auto-generate table name

    timestamp = Integer(primary_key=True, required=True)
    open = Float()
    high = Float()
    low = Float()
    close = Float()
    volume = Float()

# ----------------------------------------------------------------------
@auto_reg
class CollectorBot(BaseBot):
    _name = "collector.bot"
    _inherit = "base.bot"

    def __init__(self, bot_id: int, manager=None):
        super().__init__(bot_id, manager)
        self.config = get_bot_config(bot_id)
        self.logger = perf_logger.get_logger(f'collector_{bot_id}', 'collector')
        self.fetcher = None
        self.initial_load_done = False

        try:
            self._get_candle_manager()
        except Exception as e:
            # The table will be created at startup if it failed now
            self.logger.warning(f"Could not create candle table on init: {e}")

    def _get_candle_manager(self):
        """Return ORM manager for the current symbol's candle table."""
        symbol = self.config.get('symbol')
        if not symbol:
            raise ValueError("Symbol not set")
        table_name = symbol.replace('/', '_').replace('-', '_')
        return self.env.get_model_manager('collector.candle', table_name)

    async def start(self):
        if self.running:
            return
        self.running = True
        self.task = asyncio.create_task(self._run())
        update_bot_status(self.bot_id, 'running')
        self.logger.info(f"Bot {self.bot_id} started, status updated to 'running'")

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.fetcher:
            await self.fetcher.close()
        update_bot_status(self.bot_id, 'stopped')
        self.logger.info(f"Bot {self.bot_id} stopped")

    async def _run(self):
        self.logger.info(f"Collector bot {self.bot_id} starting with config: {self.config}")

        while self.running:
            if self.config_dirty:
                await self.on_config_updated()
                self.config_dirty = False
                continue

            if not self.fetcher:
                try:
                    self.fetcher = AsyncExchangeFetcher(self.config['exchange'], self.config['market_type'])
                    await self.fetcher.initialize()
                except Exception as e:
                    self.logger.error(f"Failed to initialize fetcher: {e}")
                    await self.stop()
                    return

            symbol = self.config['symbol']
            timeframe = self.config['timeframe']
            candles_limit = self.config['candles_limit']
            fetch_interval = timeframe_to_seconds(timeframe)

            candle_manager = self._get_candle_manager()

            try:
                # Initial history load
                if not self.initial_load_done:
                    count = candle_manager.search_count([])
                    if count < candles_limit:
                        self.logger.info(f"Initial history load: {candles_limit} candles")
                        df = await self.fetcher.fetch_ohlcv(symbol, timeframe=timeframe, limit=candles_limit)
                    else:
                        df = await self.fetcher.fetch_ohlcv(symbol, timeframe=timeframe, limit=5)
                    self.initial_load_done = True
                else:
                    df = await self.fetcher.fetch_ohlcv(symbol, timeframe=timeframe, limit=5)

                if df.empty:
                    await asyncio.sleep(fetch_interval)
                    continue

                # Precompute nanosecond array for all rows
                ts_ns = df['timestamp'].astype('int64')  # int64 nanoseconds
                for idx, row in df.iterrows():
                    ts = int(ts_ns[idx])  # nanoseconds
                    existing = candle_manager.search([('timestamp', '=', ts)], limit=1)
                    if existing:
                        existing[0].write({
                            'open': row['open'],
                            'high': row['high'],
                            'low': row['low'],
                            'close': row['close'],
                            'volume': row['volume']
                        })
                    else:
                        candle_manager.create([{
                            'timestamp': ts,
                            'open': row['open'],
                            'high': row['high'],
                            'low': row['low'],
                            'close': row['close'],
                            'volume': row['volume']
                        }])

                # Enforce candle limit
                count = candle_manager.search_count([])
                if count > candles_limit * 1.1:
                    # Get oldest timestamp to keep
                    last_records = candle_manager.search([], order='timestamp DESC', limit=candles_limit)
                    if last_records:
                        min_ts = last_records[-1].timestamp
                        old_records = candle_manager.search([('timestamp', '<', min_ts)])
                        old_records.unlink()

                self.logger.info(f"Processed candles for {symbol}")
            except Exception as e:
                self.logger.error(f"Error in collector loop: {e}", exc_info=True)

            await asyncio.sleep(fetch_interval)

    def get_capabilities(self):
        return {
            "ohlcv_data": {
                "keywords": ["candles", "ohlcv", "quotes", "market_data", "stonks"],
                "getter": self._get_ohlcv_data,
                "setter": None,
            },
            "symbol": {
                "keywords": ["symbol", "pair", "ticker"],
                "getter": self._get_symbol,
                "setter": None,
            }
        }

    async def on_config_updated(self):
        """Reload history and parameters after settings change."""
        old_symbol = self.config.get('symbol')
        old_table_name = old_symbol.replace('/', '_').replace('-', '_') if old_symbol else None

        # Update config from DB
        self.config = get_bot_config(self.bot_id)

        # Close old fetcher
        if self.fetcher:
            await self.fetcher.close()
            self.fetcher = None

        # Reset initial load flag
        self.initial_load_done = False

        # Drop old candle table if symbol changed
        if old_table_name:
            self.env.drop_table(old_table_name)
            self.logger.info(f"Dropped old table {old_table_name}")

        # The new table will be created when _get_candle_manager is called in _run

    async def _get_ohlcv_data(self, limit=500):
        """Returns the last limit candles as a list of dictionaries."""
        candle_manager = self._get_candle_manager()
        records = candle_manager.search([], order='timestamp DESC', limit=limit)
        # Convert records to list of dicts
        return records.read()

    async def _get_symbol(self):
        return self.config['symbol']

    def _close_db(self):
        if hasattr(self, 'env'):
            self.env.close()