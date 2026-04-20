# modules/collector_bot.py (updated)
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import asyncio
import sqlite3
from .base_bot import BaseBot
from .fetcher import AsyncExchangeFetcher
from .database import get_bot_config, update_bot_status
from .logger import perf_logger

def timeframe_to_seconds(tf: str) -> int:
    unit = tf[-1]
    value = int(tf[:-1])
    if unit == 'm': return value * 60
    elif unit == 'h': return value * 3600
    elif unit == 'd': return value * 86400
    else: raise ValueError(f"Unsupported timeframe: {tf}")

class CollectorBot(BaseBot):
    def __init__(self, bot_id: int):
        super().__init__(bot_id)
        self.config = get_bot_config(bot_id)
        self.running = False
        self.task = None
        self.logger = perf_logger.get_logger(f'collector_{bot_id}', 'collector')
        self.fetcher = None

    async def start(self):
        if self.running:
            return
        self.running = True
        self.task = asyncio.create_task(self._run())
        update_bot_status(self.bot_id, 'running')
        self.logger.info(f"Bot {self.bot_id} started")

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
        self.fetcher = AsyncExchangeFetcher(self.config['exchange'], self.config['market_type'])
        await self.fetcher.initialize()

        db_path = self.config['data_db_path']
        symbol = self.config['symbol']
        table_name = symbol.replace('/', '_').replace('-', '_')
        timeframe = self.config['timeframe']
        candles_limit = self.config['candles_limit']

        # Create table
        with sqlite3.connect(db_path) as conn:
            conn.execute(f'''
                CREATE TABLE IF NOT EXISTS {table_name} (
                    timestamp INTEGER PRIMARY KEY,
                    open REAL, high REAL, low REAL, close REAL, volume REAL
                )
            ''')

        fetch_interval = timeframe_to_seconds(timeframe)
        initial_load_done = False

        while self.running:
            try:
                if not initial_load_done:
                    with sqlite3.connect(db_path) as conn:
                        cur = conn.execute(f'SELECT COUNT(*) FROM {table_name}')
                        count = cur.fetchone()[0]
                    if count < candles_limit:
                        self.logger.info(f"Initial history load: {candles_limit} candles")
                        df = await self.fetcher.fetch_ohlcv(symbol, timeframe=timeframe, limit=candles_limit)
                        initial_load_done = True
                    else:
                        initial_load_done = True
                        df = await self.fetcher.fetch_ohlcv(symbol, timeframe=timeframe, limit=5)
                else:
                    df = await self.fetcher.fetch_ohlcv(symbol, timeframe=timeframe, limit=5)

                if df.empty:
                    await asyncio.sleep(fetch_interval)
                    continue

                with sqlite3.connect(db_path) as conn:
                    cur = conn.execute(f'SELECT MAX(timestamp) FROM {table_name}')
                    last_ts = cur.fetchone()[0] or 0
                    ts_sec = (df['timestamp'].astype('int64') // 10**9).astype(int)
                    new_mask = ts_sec > last_ts
                    new_candles = df[new_mask].copy()
                    if not new_candles.empty:
                        new_candles['timestamp_sec'] = ts_sec[new_mask]
                        data = new_candles[['timestamp_sec', 'open', 'high', 'low', 'close', 'volume']].values.tolist()
                        conn.executemany(
                            f'INSERT OR IGNORE INTO {table_name} (timestamp, open, high, low, close, volume) VALUES (?,?,?,?,?,?)',
                            data
                        )
                        conn.execute(f'''
                            DELETE FROM {table_name} WHERE timestamp NOT IN (
                                SELECT timestamp FROM {table_name} ORDER BY timestamp DESC LIMIT ?
                            )
                        ''', (candles_limit,))
                        conn.commit()
                        self.logger.info(f"Inserted {len(data)} new candles")
            except Exception as e:
                self.logger.error(f"Error in collector loop: {e}", exc_info=True)
            await asyncio.sleep(fetch_interval)