# modules/grid/models.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

"""
Grid Bot – full-featured grid bot with levels, recalculation, extrapolation,
smart averaging, and trade history.
"""
import asyncio
import sqlite3
import json
import time
import math
import os
import uuid
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple

from core import auto_reg, BaseBot
from core.database import get_bot_config, update_bot_status
from core.logger import perf_logger
from core.registry import bot_registry

logger = perf_logger.get_logger('grid_bot', 'analytics')


# ----------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------
def timeframe_to_seconds(tf: str) -> int:
    unit = tf[-1]
    value = int(tf[:-1])
    if unit == 'm':
        return value * 60
    elif unit == 'h':
        return value * 3600
    elif unit == 'd':
        return value * 86400
    else:
        raise ValueError(f"Unsupported timeframe: {tf}")


def linear_extrapolate_price(prices: List[float], timestamps: List[float], future_sec: float) -> float:
    """Linear extrapolation using the last two points."""
    if len(prices) < 2:
        return prices[-1] if prices else 0.0
    dt = timestamps[-1] - timestamps[-2]
    if dt == 0:
        return prices[-1]
    slope = (prices[-1] - prices[-2]) / dt
    return prices[-1] + slope * future_sec


def get_volume_sequence(strategy: str, max_count: int, entry_vol: float = 1.0) -> List[float]:
    if strategy == "equal":
        return [entry_vol] * max_count
    elif strategy == "sum_prev":
        return [entry_vol * (2 ** i) for i in range(max_count)]
    elif strategy == "double":
        return [entry_vol * (2 ** (i + 1)) for i in range(max_count)]
    elif strategy == "triple":
        seq = [entry_vol]
        for _ in range(1, max_count):
            seq.append(seq[-1] * 3)
        return seq
    else:
        return [entry_vol] * max_count


# ----------------------------------------------------------------------
@auto_reg
class GridBot(BaseBot):
    _name = "grid.bot"
    _inherit = "base.bot"

    def __init__(self, bot_id: int, manager=None):
        super().__init__(bot_id, manager)
        self.config = get_bot_config(bot_id)
        self.logger = perf_logger.get_logger(f"grid_{bot_id}", "analytics")
        self._db_path = f"data/bot_{bot_id}.db"
        self._init_db()

        self._collector_handle = None
        self._analyst_handle = None

        self._position_id = self._get_current_position_id()
        self._price_history = []
        self._last_price = None
        self._last_ts = None
        self._need_new_position = False   # flag indicating need to open a new position after closing

    # ------------------------------------------------------------------
    # Database initialization
    # ------------------------------------------------------------------
    def _init_db(self):
        os.makedirs("data", exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")

        # Table for active levels and signals
        conn.execute("""
            CREATE TABLE IF NOT EXISTS levels_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id TEXT NOT NULL,
                signal_number INTEGER NOT NULL,
                current_price REAL,
                current_timestamp REAL,
                signal_volume_rates TEXT,
                signal_direction TEXT,
                signal_vol_rate REAL,
                signal_order_type TEXT,
                signal_price REAL,
                signal_liquidation INTEGER DEFAULT 0,
                signal_close INTEGER DEFAULT 0,
                signal_position_volume REAL,
                avg_entry_price REAL,
                position_cost REAL,
                signal_pnl REAL,
                signal_flag INTEGER DEFAULT 0,
                signal_timestamp REAL,
                signal_smart INTEGER DEFAULT 0,
                signal_smart_reversed INTEGER DEFAULT 0,
                real_timestamp REAL,
                real_vol_rate REAL,
                real_price REAL,
                real_pnl REAL,
                real_position_volume REAL,
                real_flag INTEGER DEFAULT 0,
                real_avg_entry_price REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Attempt to add new columns for existing tables
        for col, col_type in [
            ("real_flag", "INTEGER DEFAULT 0"),
            ("real_avg_entry_price", "REAL"),
        ]:
            try:
                conn.execute(f"ALTER TABLE levels_signals ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError:
                pass

        # Table for completed positions history
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deals_history (
                history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id TEXT,
                signal_number INTEGER,
                current_price REAL,
                current_timestamp REAL,
                signal_volume_rates TEXT,
                signal_direction TEXT,
                signal_vol_rate REAL,
                signal_order_type TEXT,
                signal_price REAL,
                signal_liquidation INTEGER DEFAULT 0,
                signal_close INTEGER DEFAULT 0,
                signal_position_volume REAL,
                avg_entry_price REAL,
                position_cost REAL,
                signal_pnl REAL,
                signal_flag INTEGER,
                signal_timestamp REAL,
                signal_smart INTEGER,
                signal_smart_reversed INTEGER,
                real_timestamp REAL,
                real_vol_rate REAL,
                real_price REAL,
                real_pnl REAL,
                real_position_volume REAL,
                real_flag INTEGER DEFAULT 0,
                real_avg_entry_price REAL,
                moved_to_history_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        for col in ["signal_close", "signal_liquidation"]:
            try:
                conn.execute(f"ALTER TABLE levels_signals ADD COLUMN {col} INTEGER DEFAULT 0")
                conn.execute(f"ALTER TABLE deals_history ADD COLUMN {col} INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass

        for col, col_type in [
            ("real_flag", "INTEGER DEFAULT 0"),
            ("real_avg_entry_price", "REAL"),
        ]:
            try:
                conn.execute(f"ALTER TABLE deals_history ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError:
                pass

        # Table for price quotes
        conn.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                timestamp REAL PRIMARY KEY,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL
            )
        """)
        # Table for bot settings (internal state)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------
    # Helper methods for position_id
    # ------------------------------------------------------------------
    def _get_current_position_id(self) -> str:
        conn = sqlite3.connect(self._db_path)
        cur = conn.execute("SELECT value FROM bot_settings WHERE key='current_position_id'")
        row = cur.fetchone()
        if row:
            pos_id = row[0].strip("'\"")
        else:
            pos_id = str(uuid.uuid4())
            self.logger.debug(f"_get_current_position_id: read from DB = {pos_id}")
            conn.execute("INSERT INTO bot_settings (key, value) VALUES (?, ?)",
                         ("current_position_id", pos_id))
            conn.commit()
            self.logger.debug(f"_get_current_position_id: created new = {pos_id}")
        conn.close()
        return pos_id

    def _set_position_id(self, new_id: str):
        self.logger.debug(f"_set_position_id: setting position_id = {new_id}")
        new_id = str(new_id).strip("'\"")
        conn = sqlite3.connect(self._db_path)
        conn.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
                     ("current_position_id", new_id))
        conn.commit()
        conn.close()
        self._position_id = new_id

    async def _ensure_clean_levels_table(self):
        """
        Brings the levels_signals table to a consistent state.

        Logic:
          - If the table contains only the current position_id – do nothing.
          - If there are other ids already present in deals_history – delete them (garbage).
          - If there is exactly one "foreign" id not present in history – treat it
            as the current active position (e.g., after a restart) and restore it.
          - If there are several such "foreign" ids – move them to history and clear,
            so that a new position can be started.
        """
        conn = sqlite3.connect(self._db_path)
        try:
            cur = conn.execute("SELECT DISTINCT position_id FROM levels_signals")
            active_ids = [row[0] for row in cur.fetchall()]
            if not active_ids:
                return  # empty

            current_id = self._position_id
            other_ids = [pid for pid in active_ids if pid != current_id]
            if not other_ids:
                return  # only current position – all is well

            # Split foreign ids into those already known in history and unknown ones
            placeholders = ','.join('?' * len(other_ids))
            cur = conn.execute(
                f"SELECT DISTINCT position_id FROM deals_history WHERE position_id IN ({placeholders})",
                other_ids
            )
            known_ids = {row[0] for row in cur.fetchall()}
            unknown_ids = [pid for pid in other_ids if pid not in known_ids]

            # Delete records already in history (they are not needed in the active table)
            if known_ids:
                del_ph = ','.join('?' * len(known_ids))
                conn.execute(
                    f"DELETE FROM levels_signals WHERE position_id IN ({del_ph})",
                    list(known_ids)
                )
                conn.commit()

            if len(unknown_ids) == 1:
                # Restore the only orphan position as current
                recovered_id = unknown_ids[0]
                self.logger.warning(
                    f"Found lost active position {recovered_id}. "
                    f"Restoring as current."
                )
                self.logger.debug(f"Current self._position_id before restore = {self._position_id}")
                # Update bot_settings and self._position_id
                self._set_position_id(recovered_id)
                self.logger.debug(f"self._position_id after restore = {self._position_id}")

                # Delete records of the previous current id (if any) and any others,
                # so that only records of the restored position remain
                conn.execute(
                    "DELETE FROM levels_signals WHERE position_id != ?",
                    (recovered_id,)
                )
                conn.commit()
                self.logger.info(
                    f"Levels table cleaned, restored position {recovered_id}"
                )

            elif len(unknown_ids) > 1:
                # Several unknown positions – move them to history and clear
                self.logger.warning(
                    f"Found multiple orphan positions: {unknown_ids}. "
                    f"Moving to history and clearing table."
                )
                miss_ph = ','.join('?' * len(unknown_ids))

                conn.execute(
                    f"""INSERT INTO deals_history (
                            position_id, signal_number, current_price, current_timestamp,
                            signal_volume_rates, signal_direction, signal_vol_rate,
                            signal_order_type, signal_price, signal_liquidation, signal_close,
                            signal_position_volume, avg_entry_price, position_cost,
                            signal_pnl, signal_flag, signal_timestamp, signal_smart,
                            signal_smart_reversed, real_timestamp, real_vol_rate,
                            real_price, real_pnl, real_position_volume, real_flag,
                            real_avg_entry_price
                        )
                        SELECT
                            position_id, signal_number, current_price, current_timestamp,
                            signal_volume_rates, signal_direction, signal_vol_rate,
                            signal_order_type, signal_price, signal_liquidation, signal_close,
                            signal_position_volume, avg_entry_price, position_cost,
                            signal_pnl, signal_flag, signal_timestamp, signal_smart,
                            signal_smart_reversed, real_timestamp, real_vol_rate,
                            real_price, real_pnl, real_position_volume, real_flag,
                            real_avg_entry_price
                        FROM levels_signals
                        WHERE position_id IN ({miss_ph})""",
                    unknown_ids
                )
                conn.execute(
                    f"DELETE FROM levels_signals WHERE position_id IN ({miss_ph})",
                    unknown_ids
                )
                conn.commit()
                self.logger.info(
                    f"Moved to history positions {unknown_ids}, table cleared."
                )

            # If unknown_ids is empty – we just deleted known_ids, nothing more is required

        except Exception as e:
            conn.rollback()
            self.logger.error(f"Error in _ensure_clean_levels_table: {e}", exc_info=True)
            raise
        finally:
            conn.close()

    def _close_db(self):
        pass

    # ------------------------------------------------------------------
    # Inter-bot communication (capabilities)
    # ------------------------------------------------------------------
    def get_capabilities(self) -> Dict[str, Dict]:
        return {
            "signal": {
                "keywords": ["grid_signal", "signal", "order_signal"],
                "getter": self._get_pending_signals,
                "setter": None,
            },
            "record_execution": {
                "keywords": ["grid_fill", "execution", "fill_order"],
                "getter": None,
                "setter": self._set_execution_result,
            },
            "get_position_id": {
                "keywords": ["position_id"],
                "getter": self._get_position_id,
                "setter": None,
            },
            "get_signals_by_position": {
                "keywords": ["position_signals"],
                "getter": self._get_signals_by_position,
                "setter": None,
            },
        }

    async def _get_pending_signals(self):
        def query():
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM levels_signals WHERE signal_flag=1 AND real_flag=0 ORDER BY signal_timestamp ASC"
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, query)

    async def _set_execution_result(self, data: Dict[str, Any]):
        loop = asyncio.get_running_loop()
        def update():
            conn = sqlite3.connect(self._db_path)
            signal_number = data.get("signal_number")
            signal_ts = data.get("signal_timestamp")
            if signal_number is not None and signal_ts is not None:
                conn.execute(
                    """UPDATE levels_signals SET real_timestamp=?, real_vol_rate=?, real_price=?,
                       real_pnl=?, real_position_volume=?, real_flag=1,
                       real_avg_entry_price=?
                       WHERE signal_number=? AND ABS(signal_timestamp - ?) < 1 AND real_flag=0""",
                    (
                        data.get("real_timestamp", time.time()),
                        data.get("real_vol_rate", 1.0),
                        data.get("real_price"),
                        data.get("real_pnl", 0.0),
                        data.get("real_position_volume", 1.0),
                        data.get("real_avg_entry_price", data.get("real_price")),  # simplified
                        signal_number,
                        signal_ts,
                    ),
                )
                conn.commit()
            conn.close()
        await loop.run_in_executor(None, update)

    async def _get_position_id(self):
        return self._position_id

    async def _get_signals_by_position(self, position_id: str):
        def query():
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM levels_signals WHERE position_id=? UNION SELECT * FROM deals_history WHERE position_id=?",
                (position_id, position_id)
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, query)

    # ------------------------------------------------------------------
    # Get price from collector
    # ------------------------------------------------------------------
    async def _update_price_history(self, limit=2000):
        if not self._collector_handle:
            return
        try:
            candles = await self._collector_handle.get("candles", limit=limit)
            if not candles:
                return
            conn = sqlite3.connect(self._db_path)
            for c in candles:
                conn.execute(
                    "INSERT OR REPLACE INTO price_history (timestamp, open, high, low, close, volume) VALUES (?,?,?,?,?,?)",
                    (c['timestamp'], c['open'], c['high'], c['low'], c['close'], c['volume'])
                )
            cur = conn.execute('SELECT COUNT(*) FROM price_history')
            count = cur.fetchone()[0]
            if count > limit * 1.1:
                conn.execute('''
                    DELETE FROM price_history WHERE timestamp < (
                        SELECT MIN(timestamp) FROM (
                            SELECT timestamp FROM price_history ORDER BY timestamp DESC LIMIT ?
                        )
                    )
                ''', (limit,))
            conn.commit()
            conn.close()

            if candles:
                self._last_price = candles[0]['close']
                self._last_ts = candles[0]['timestamp']
                if len(candles) >= 2:
                    self._price_history = [
                        (candles[1]['timestamp'], candles[1]['close']),
                        (candles[0]['timestamp'], candles[0]['close'])
                    ]
                else:
                    self._price_history = [(candles[0]['timestamp'], candles[0]['close'])]
        except Exception as e:
            self.logger.error(f"Failed to update price history: {e}")

    # ------------------------------------------------------------------
    # Level calculation (initial grid building)
    # ------------------------------------------------------------------
    async def _rebuild_levels(self, direction: str, entry_price: float, entry_ts: float):
        await self._ensure_clean_levels_table()  # ← check and clean
        cfg = self.config
        max_count = cfg['max_averaging_count']
        threshold_pct = cfg['averaging_threshold_pnl']
        lev = cfg['leverage']
        strategy = cfg['averaging_strategy']
        volumes = get_volume_sequence(strategy, max_count, entry_vol=1.0)

        sign = 1 if direction == 'long' else -1
        conn = sqlite3.connect(self._db_path)
        conn.execute("DELETE FROM levels_signals WHERE position_id=?", (self._position_id,))
        conn.commit()

        # ---- Entry (signal_number=0) – immediately executed ----
        entry_vol = 1.0
        entry_cost = entry_price * entry_vol
        conn.execute(
            """INSERT INTO levels_signals
               (position_id, signal_number, current_price, current_timestamp, signal_volume_rates,
                signal_direction, signal_vol_rate, signal_order_type, signal_price, signal_liquidation,
                signal_position_volume, avg_entry_price, position_cost, signal_pnl, signal_flag, signal_timestamp,
                real_timestamp, real_vol_rate, real_price, real_pnl, real_position_volume, real_flag, real_avg_entry_price)
               VALUES (?,0,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,1,?)""",
            (self._position_id, entry_price, entry_ts, json.dumps([entry_vol]),
             'buy' if direction == 'long' else 'sell', entry_vol, 'limit', entry_price, 0.0,
             entry_vol, entry_price, entry_cost, 0.0, entry_ts,
             entry_ts, entry_vol, entry_price, 0.0, entry_vol, entry_price)
        )

        # ---- Averaging levels 1..max_count ----
        prev_avg_price = entry_price
        prev_position_vol = entry_vol
        prev_position_cost = entry_cost

        for n in range(1, max_count + 1):
            vol = volumes[n - 1]
            level_price = prev_avg_price * (1 - sign * (threshold_pct / 100.0) / lev)
            new_position_vol = prev_position_vol + vol
            new_position_cost = prev_position_cost + level_price * vol
            new_avg_price = new_position_cost / new_position_vol
            pnl = sign * (level_price / new_avg_price - 1) * lev * 100

            volume_rates_json = json.dumps(volumes[:n])
            conn.execute(
                """INSERT INTO levels_signals
                   (position_id, signal_number, current_price, current_timestamp, signal_volume_rates,
                    signal_direction, signal_vol_rate, signal_order_type, signal_price, signal_liquidation,
                    signal_position_volume, avg_entry_price, position_cost, signal_pnl, signal_flag, real_flag)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0)""",
                (self._position_id, n, entry_price, entry_ts, volume_rates_json,
                 'buy' if direction == 'long' else 'sell', vol, 'limit', level_price, 0.0,
                 new_position_vol, new_avg_price, new_position_cost, pnl)
            )
            prev_avg_price = new_avg_price
            prev_position_vol = new_position_vol
            prev_position_cost = new_position_cost

        # ---- Liquidation ----
        liq_pnl = cfg.get('liquidation_pnl', 90)
        liq_price = entry_price * (1 - sign * (liq_pnl / 100.0) / lev)
        liq_order_type = cfg.get('liquidation_order_type', 'market')
        conn.execute(
            "INSERT INTO levels_signals (position_id, signal_number, signal_direction, signal_price, signal_liquidation, signal_order_type, signal_flag, real_flag) "
            "VALUES (?, -1, ?, ?, 1, ?, 0, 0)",
            (self._position_id, 'sell' if direction == 'long' else 'buy', liq_price, liq_order_type)
        )

        # ---- Close (reference price) ----
        close_pnl = cfg.get('close_pnl', 50.0)
        close_price = entry_price * (1 + sign * (close_pnl / 100.0) / lev)  # higher for long, lower for short
        conn.execute(
            "INSERT INTO levels_signals (position_id, signal_number, signal_direction, signal_price, signal_flag, real_flag, signal_close) "
            "VALUES (?, -2, ?, ?, 0, 0, 1)",
            (self._position_id, 'sell' if direction == 'long' else 'buy', close_price)
        )

        conn.commit()
        conn.close()
        self.logger.info(f"Rebuilt levels for {self._position_id}, direction {direction}")

    # ------------------------------------------------------------------
    # Update levels without deleting executed ones (configuration change)
    # ------------------------------------------------------------------
    async def _update_levels(self):
        """Updates prices of levels, liquidation and close without deleting executed signals."""

        self.logger.debug(f"_update_levels: last_price={self._last_price}, position_id={self._position_id}")
        if self._last_price is None:
            self.logger.warning("_update_levels: no current price, skipping recalculation")
            return

        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row

        last_exec = conn.execute(
            "SELECT * FROM levels_signals WHERE position_id=? AND (real_flag=1 OR (signal_number=0 AND real_flag=1)) "
            "ORDER BY signal_number DESC LIMIT 1",
            (self._position_id,)
        ).fetchone()
        if not last_exec:
            last_exec = conn.execute(
                "SELECT * FROM levels_signals WHERE position_id=? AND signal_number=0",
                (self._position_id,)
            ).fetchone()
            if not last_exec:
                conn.close()
                return

        direction = last_exec['signal_direction']
        effective_avg = last_exec['real_avg_entry_price'] or last_exec['avg_entry_price']
        effective_vol = last_exec['real_position_volume'] or last_exec['signal_position_volume']
        effective_cost = effective_vol * effective_avg


        last_number = last_exec['signal_number']
        # delete all future unexecuted levels
        conn.execute(
            "DELETE FROM levels_signals WHERE position_id=? AND signal_number>? AND real_flag=0",
            (self._position_id, last_number)
        )

        # create new ones from last_number+1 to max_count
        sign = 1 if last_exec['signal_direction'] == 'buy' else -1
        cfg = self.config
        max_count = cfg['max_averaging_count']
        threshold_pct = cfg['averaging_threshold_pnl']
        lev = cfg['leverage']
        strategy = cfg['averaging_strategy']
        volumes = get_volume_sequence(strategy, max_count, entry_vol=1.0)

        prev_avg = effective_avg
        prev_vol = effective_vol
        prev_cost = effective_cost

        for n in range(last_number + 1, max_count + 1):
            vol = volumes[n - 1] if n - 1 < len(volumes) else volumes[-1]
            level_price = prev_avg * (1 - sign * threshold_pct / 100.0 / lev)
            new_pos_vol = prev_vol + vol
            new_pos_cost = prev_cost + level_price * vol
            new_avg = new_pos_cost / new_pos_vol
            pnl = sign * (level_price / new_avg - 1) * lev * 100

            conn.execute(
                """INSERT INTO levels_signals
                   (position_id, signal_number, current_price, current_timestamp, signal_volume_rates,
                    signal_direction, signal_vol_rate, signal_order_type, signal_price, signal_liquidation,
                    signal_position_volume, avg_entry_price, position_cost, signal_pnl, signal_flag, real_flag,
                    signal_close)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,0)""",
                (self._position_id, n, self._last_price or None, time.time(),
                 json.dumps(volumes[:n]), last_exec['signal_direction'], vol, 'limit', level_price, 0,
                 new_pos_vol, new_avg, new_pos_cost, pnl)
            )
            prev_avg = new_avg
            prev_vol = new_pos_vol
            prev_cost = new_pos_cost

        # update liquidation and close
        liq_price = effective_avg * (1 - sign * cfg['liquidation_pnl'] / 100.0 / lev)
        close_price = effective_avg * (1 + sign * cfg['close_pnl'] / 100.0 / lev)
        conn.execute("UPDATE levels_signals SET signal_price=? WHERE position_id=? AND signal_number=-1",
                     (liq_price, self._position_id))
        conn.execute("UPDATE levels_signals SET signal_price=? WHERE position_id=? AND signal_number=-2",
                     (close_price, self._position_id))
        conn.commit()
        conn.close()
        self.logger.info("Levels updated after configuration change")

    # ------------------------------------------------------------------
    # Check and generate signals
    # ------------------------------------------------------------------
    async def _check_grid_levels(self):
        if not self._price_history:
            return
        current_price = self._price_history[-1][1]
        current_ts = self._price_history[-1][0]
        cfg = self.config
        lev = cfg['leverage']
        reserve_sec = cfg['execution_reserve_sec']
        timeout_sec = cfg['execution_timeout_sec']
        recalc_strategy = cfg['recalc_strategy']

        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row

        try:
            # Find the last executed level (or entry) to calculate PNL
            last = conn.execute(
                "SELECT * FROM levels_signals WHERE position_id=? AND (real_flag=1 OR signal_number=0) "
                "ORDER BY signal_number DESC LIMIT 1",
                (self._position_id,)
            ).fetchone()
            if not last:
                # position completely missing
                conn.close()
                self._need_new_position = True
                return

            effective_avg = last['real_avg_entry_price'] or last['avg_entry_price']
            direction = last['signal_direction']
            sign = 1 if direction == 'buy' else -1
            pnl = sign * (current_price / effective_avg - 1) * lev * 100

            # --- Liquidation ---
            if pnl <= -cfg['liquidation_pnl']:
                # effective_vol – real or signal position volume
                effective_vol = last['real_position_volume'] or last['signal_position_volume']

                total_vol = effective_vol  # total volume to be closed

                # List of volumes for signal_volume_rates
                vol_rates_row = conn.execute(
                    "SELECT signal_volume_rates FROM levels_signals WHERE position_id=? AND (real_flag=1 OR signal_number=0) ORDER BY signal_number DESC LIMIT 1",
                    (self._position_id,)
                ).fetchone()
                if vol_rates_row:
                    vol_rates_list = json.loads(vol_rates_row['signal_volume_rates'])
                else:
                    vol_rates_list = [1.0]
                vol_rates_list.append(total_vol)
                vol_rates_json = json.dumps(vol_rates_list)

                liq_order_type = cfg.get('liquidation_order_type', 'market')

                self._create_signal(
                    conn, -1, 'sell' if direction == 'buy' else 'buy', current_price, current_ts,
                    vol_rate=total_vol,
                    position_volume=0.0,
                    emulate_execution=(timeout_sec == 0),
                    current_price=current_price,
                    signal_volume_rates=vol_rates_json,
                    avg_entry_price=effective_avg,
                    position_cost=effective_avg * effective_vol,  # effective_vol defined now
                    signal_pnl=pnl,
                    order_type=liq_order_type
                )
                conn.commit()
                conn.close()
                if timeout_sec == 0:
                    await self._close_position()
                    self._need_new_position = True
                else:
                    self.logger.info("Liquidation signal generated, waiting for execution")
                return

            # --- Close by PNL ---
            if pnl >= cfg['breakeven_pnl']:
                use_analyst = cfg.get('use_analyst_close', 1) and cfg.get('analyst_bot_id')
                if use_analyst:
                    analyst_signal = await self._get_analyst_signal()
                    if not analyst_signal:
                        conn.close()
                        return
                if pnl >= cfg['close_pnl']:
                    # Determine effective position volume
                    effective_vol = last['real_position_volume'] or last['signal_position_volume']
                    total_vol = effective_vol

                    vol_rates_row = conn.execute(
                        "SELECT signal_volume_rates FROM levels_signals WHERE position_id=? AND (real_flag=1 OR signal_number=0) ORDER BY signal_number DESC LIMIT 1",
                        (self._position_id,)
                    ).fetchone()
                    if vol_rates_row:
                        vol_rates_list = json.loads(vol_rates_row['signal_volume_rates'])
                    else:
                        vol_rates_list = [1.0]
                    vol_rates_list.append(total_vol)
                    vol_rates_json = json.dumps(vol_rates_list)

                    self._create_signal(
                        conn, -2, 'close', current_price, current_ts,
                        vol_rate=total_vol,
                        position_volume=0.0,
                        emulate_execution=(timeout_sec == 0),
                        current_price=current_price,
                        signal_volume_rates=vol_rates_json,
                        avg_entry_price=effective_avg,
                        position_cost=effective_avg * effective_vol,  # effective_vol defined now
                        signal_pnl=pnl,
                        order_type='limit'
                    )
                    conn.commit()
                    conn.close()
                    if timeout_sec == 0:
                        await self._close_position()
                        self._need_new_position = True
                    else:
                        self.logger.info("Close signal generated, waiting for execution")
                    return

            # --- Averaging levels ---
            levels = conn.execute(
                "SELECT * FROM levels_signals WHERE position_id=? AND signal_number>=1 AND real_flag=0 AND signal_flag=0 "
                "ORDER BY signal_number ASC",
                (self._position_id,)
            ).fetchall()

            for level in levels:
                level_price = level['signal_price']

                # 1) Price already crossed the level – immediate signal
                if (direction == 'buy' and current_price <= level_price) or \
                        (direction == 'sell' and current_price >= level_price):
                    signal_ts = current_ts
                    self._create_signal(conn, level['signal_number'], level['signal_direction'],
                                        level_price, signal_ts, level['signal_vol_rate'],
                                        level['signal_position_volume'],
                                        emulate_execution=(timeout_sec == 0))
                    conn.commit()
                    if timeout_sec == 0:
                        await self._apply_execution_and_recalc(conn, level['signal_number'])
                        conn.commit()
                        conn.close()
                    else:
                        conn.close()
                    return

                # 2) Price hasn't reached yet – extrapolation
                if len(self._price_history) >= 2:
                    timestamps = [p[0] for p in self._price_history[-2:]]
                    prices = [p[1] for p in self._price_history[-2:]]
                    extrap_price = linear_extrapolate_price(prices, timestamps, reserve_sec)

                    if (direction == 'buy' and extrap_price <= level_price <= current_price) or \
                            (direction == 'sell' and extrap_price >= level_price >= current_price):
                        delta_price = extrap_price - current_price
                        if abs(delta_price) > 1e-8:
                            dt = reserve_sec * (level_price - current_price) / delta_price
                            time_to_cross = max(0.0, dt)
                        else:
                            time_to_cross = 0.0
                    else:
                        continue

                    if time_to_cross <= reserve_sec:
                        signal_ts = current_ts + time_to_cross
                        self._create_signal(conn, level['signal_number'], level['signal_direction'],
                                            level_price, signal_ts, level['signal_vol_rate'],
                                            level['signal_position_volume'],
                                            emulate_execution=(timeout_sec == 0))
                        conn.commit()
                        if timeout_sec == 0:
                            await self._apply_execution_and_recalc(conn, level['signal_number'])
                            conn.commit()
                            conn.close()
                        else:
                            conn.close()
                        return

            # --- Timeout of pending signals (only if timeout_sec > 0) ---
            if timeout_sec > 0:
                pending = conn.execute(
                    "SELECT * FROM levels_signals WHERE position_id=? AND signal_flag=1 AND real_flag=0 AND signal_timestamp IS NOT NULL",
                    (self._position_id,)
                ).fetchall()
                now = time.time()
                conn.close()

                for sig in pending:
                    if now - sig['signal_timestamp'] > timeout_sec:
                        if recalc_strategy == 'recalc_grid':
                            await self._recalc_levels_from(sig['signal_number'])
                            self.logger.info(f"Recalc levels from #{sig['signal_number']} due to timeout")
                        elif recalc_strategy == 'market_order':
                            with sqlite3.connect(self._db_path) as c2:
                                c2.execute("UPDATE levels_signals SET signal_order_type='market' WHERE id=?",
                                           (sig['id'],))
                                c2.commit()
                            self.logger.info(f"Changed signal #{sig['signal_number']} to market order")
                        break
            else:
                conn.close()

        except Exception:
            conn.close()
            raise

    def _create_signal(self, conn, signal_number: int, direction: str, price: float, timestamp: float,
                       vol_rate: float, position_volume: float, smart: bool = False, emulate_execution: bool = False,
                       current_price: float = None, signal_volume_rates: str = None,
                       avg_entry_price: float = None, position_cost: float = None,
                       signal_pnl: float = None, order_type: str = None):
        """Sets a signal (signal_flag=1) and fills additional fields."""
        updates = {
            'signal_flag': 1,
            'signal_timestamp': timestamp,
            'signal_price': price,
            'signal_smart': 1 if smart else 0,
        }
        # Set liquidation / close flags
        if signal_number == -1:
            updates['signal_liquidation'] = 1
            updates['signal_close'] = 0
        elif signal_number == -2:
            updates['signal_liquidation'] = 0
            updates['signal_close'] = 1
        else:
            updates['signal_liquidation'] = 0
            updates['signal_close'] = 0
        if current_price is not None:
            updates['current_price'] = current_price
        if signal_volume_rates is not None:
            updates['signal_volume_rates'] = signal_volume_rates
        if avg_entry_price is not None:
            updates['avg_entry_price'] = avg_entry_price
        if position_cost is not None:
            updates['position_cost'] = position_cost
        if signal_pnl is not None:
            updates['signal_pnl'] = signal_pnl
        if order_type is not None:
            updates['signal_order_type'] = order_type
        if vol_rate is not None:
            updates['signal_vol_rate'] = vol_rate

        set_clause = ', '.join([f"{col}=?" for col in updates.keys()])
        values = list(updates.values()) + [self._position_id, signal_number]
        conn.execute(
            f"UPDATE levels_signals SET {set_clause} WHERE position_id=? AND signal_number=?",
            values
        )

        if emulate_execution:
            # Fill real fields
            real_updates = {
                'real_timestamp': timestamp,
                'real_vol_rate': vol_rate,
                'real_price': price,
                'real_pnl': signal_pnl if signal_pnl is not None else 0.0,
                'real_position_volume': position_volume,
                'real_flag': 1,
                'real_avg_entry_price': avg_entry_price if avg_entry_price is not None else price,
            }
            real_set = ', '.join([f"{col}=?" for col in real_updates.keys()])
            real_values = list(real_updates.values()) + [self._position_id, signal_number]
            conn.execute(
                f"UPDATE levels_signals SET {real_set} WHERE position_id=? AND signal_number=?",
                real_values
            )

        self.logger.info(f"Signal created: #{signal_number} {direction} at {price} (ts {timestamp})")

    async def _apply_execution_and_recalc(self, conn, signal_number: int):
        """Emulates execution of a level and then recalculates subsequent levels."""
        cur = conn.execute(
            "SELECT * FROM levels_signals WHERE position_id=? AND signal_number=?",
            (self._position_id, signal_number)
        ).fetchone()
        if not cur:
            return

        avg_after = cur['real_avg_entry_price'] or cur['avg_entry_price']
        pos_vol_after = cur['real_position_volume'] or cur['signal_position_volume']
        pos_cost_after = pos_vol_after * avg_after

        conn.execute(
            "DELETE FROM levels_signals WHERE position_id=? AND signal_number>? AND real_flag=0 AND signal_flag=0",
            (self._position_id, signal_number)
        )
        conn.commit()

        cfg = self.config
        max_count = cfg['max_averaging_count']
        threshold_pct = cfg['averaging_threshold_pnl']
        lev = cfg['leverage']
        strategy = cfg['averaging_strategy']
        volumes = get_volume_sequence(strategy, max_count, entry_vol=1.0)
        direction = cur['signal_direction']
        sign = 1 if direction == 'buy' else -1

        prev_avg = avg_after
        prev_vol = pos_vol_after
        prev_cost = pos_cost_after

        next_number = signal_number + 1
        for n in range(next_number, max_count + 1):
            vol = volumes[n - 1] if n - 1 < len(volumes) else volumes[-1]
            level_price = prev_avg * (1 - sign * threshold_pct / 100.0 / lev)
            new_pos_vol = prev_vol + vol
            new_pos_cost = prev_cost + level_price * vol
            new_avg = new_pos_cost / new_pos_vol
            pnl = sign * (level_price / new_avg - 1) * lev * 100

            volume_rates_json = json.dumps(volumes[:n])
            conn.execute(
                """INSERT INTO levels_signals
                   (position_id, signal_number, current_price, current_timestamp, signal_volume_rates,
                    signal_direction, signal_vol_rate, signal_order_type, signal_price, signal_liquidation,
                    signal_position_volume, avg_entry_price, position_cost, signal_pnl, signal_flag, real_flag)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0)""",
                (self._position_id, n, self._last_price or cur['current_price'], time.time(),
                 volume_rates_json, direction, vol, 'limit', level_price, 0.0,
                 new_pos_vol, new_avg, new_pos_cost, pnl)
            )
            prev_avg = new_avg
            prev_vol = new_pos_vol
            prev_cost = new_pos_cost

        liq_pnl = cfg.get('liquidation_pnl', 90)
        liq_price = avg_after * (1 - sign * liq_pnl / 100.0 / lev)
        conn.execute(
            "UPDATE levels_signals SET signal_price=? WHERE position_id=? AND signal_number=-1",
            (liq_price, self._position_id)
        )
        self.logger.info(f"Applied execution for level #{signal_number} and recalculated subsequent levels")

    async def _recalc_levels_from(self, start_number: int):
        """Recalculates levels starting from the specified number based on the last executed level."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row

        last_exec = conn.execute(
            "SELECT * FROM levels_signals WHERE position_id=? AND signal_number < ? AND real_flag=1 "
            "ORDER BY signal_number DESC LIMIT 1",
            (self._position_id, start_number)
        ).fetchone()
        if not last_exec:
            last_exec = conn.execute(
                "SELECT * FROM levels_signals WHERE position_id=? AND signal_number=0",
                (self._position_id,)
            ).fetchone()
            if not last_exec:
                conn.close()
                return

        direction = last_exec['signal_direction']
        sign = 1 if direction == 'buy' else -1
        effective_avg = last_exec['real_avg_entry_price'] or last_exec['avg_entry_price']
        effective_vol = last_exec['real_position_volume'] or last_exec['signal_position_volume']
        effective_cost = effective_vol * effective_avg

        conn.execute(
            "DELETE FROM levels_signals WHERE position_id=? AND signal_number >= ? AND real_flag=0",
            (self._position_id, start_number)
        )
        conn.commit()

        cfg = self.config
        max_count = cfg['max_averaging_count']
        threshold_pct = cfg['averaging_threshold_pnl']
        lev = cfg['leverage']
        strategy = cfg['averaging_strategy']
        volumes = get_volume_sequence(strategy, max_count, entry_vol=1.0)

        prev_avg = effective_avg
        prev_vol = effective_vol
        prev_cost = effective_cost

        for n in range(start_number, max_count + 1):
            vol = volumes[n - 1] if n - 1 < len(volumes) else volumes[-1]
            level_price = prev_avg * (1 - sign * threshold_pct / 100.0 / lev)
            new_pos_vol = prev_vol + vol
            new_pos_cost = prev_cost + level_price * vol
            new_avg = new_pos_cost / new_pos_vol
            pnl = sign * (level_price / new_avg - 1) * lev * 100

            conn.execute(
                """INSERT INTO levels_signals
                   (position_id, signal_number, current_price, current_timestamp, signal_volume_rates,
                    signal_direction, signal_vol_rate, signal_order_type, signal_price, signal_liquidation,
                    signal_position_volume, avg_entry_price, position_cost, signal_pnl, signal_flag, real_flag)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0)""",
                (self._position_id, n, self._last_price or None, time.time(),
                 json.dumps(volumes[:n]), direction, vol, 'limit', level_price, 0.0,
                 new_pos_vol, new_avg, new_pos_cost, pnl)
            )
            prev_avg = new_avg
            prev_vol = new_pos_vol
            prev_cost = new_pos_cost

        liq_pnl = cfg.get('liquidation_pnl', 90)
        liq_price = effective_avg * (1 - sign * liq_pnl / 100.0 / lev)
        conn.execute(
            "UUPDATE levels_signals SET signal_price=? WHERE position_id=? AND signal_number=-1",
            (liq_price, self._position_id)
        )
        conn.commit()
        conn.close()
        self.logger.info(f"Levels recalculated from #{start_number} due to timeout")

    # ------------------------------------------------------------------
    # Smart averaging (unchanged)
    # ------------------------------------------------------------------
    async def _check_smart_averaging(self):
        cfg = self.config
        smart_count = cfg['smart_averaging_count']
        if smart_count == 0:
            return
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        levels = conn.execute(
            "SELECT * FROM levels_signals WHERE position_id=? AND signal_flag=1 AND signal_smart=0 AND real_flag=0 ORDER BY signal_number DESC LIMIT ?",
            (self._position_id, smart_count)
        ).fetchall()
        if not levels:
            conn.close()
            return
        current_price = self._price_history[-1][1] if self._price_history else 0
        for level in levels:
            direction = level['signal_direction']
            price = level['signal_price']
            if direction == 'buy':
                target = price * (1 + cfg['close_pnl'] / 100 / cfg['leverage'])
                if current_price >= target:
                    self._create_smart_reverse(conn, level)
            else:
                target = price * (1 - cfg['close_pnl'] / 100 / cfg['leverage'])
                if current_price <= target:
                    self._create_smart_reverse(conn, level)
        conn.commit()
        conn.close()

    def _create_smart_reverse(self, conn, original_level):
        reverse_dir = 'sell' if original_level['signal_direction'] == 'buy' else 'buy'
        conn.execute(
            """INSERT INTO levels_signals
               (position_id, signal_number, signal_direction, signal_vol_rate, signal_price,
                signal_flag, signal_timestamp, signal_smart, signal_smart_reversed, real_flag)
               VALUES (?,?,?,?,?,1,?,1,1,0)""",
            (self._position_id, original_level['signal_number'], reverse_dir,
             original_level['signal_vol_rate'], original_level['signal_price'],
             time.time())
        )
        self.logger.info(f"Smart reverse created for level #{original_level['signal_number']}")

    # ------------------------------------------------------------------
    # Close position and move to history
    # ------------------------------------------------------------------
    async def _close_position(self):
        """Moves all rows of the current position to deals_history and clears levels_signals."""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                """INSERT INTO deals_history (
                    position_id, signal_number, current_price, current_timestamp, signal_volume_rates,
                    signal_direction, signal_vol_rate, signal_order_type, signal_price, signal_liquidation, signal_close,
                    signal_position_volume, avg_entry_price, position_cost, signal_pnl, signal_flag,
                    signal_timestamp, signal_smart, signal_smart_reversed,
                    real_timestamp, real_vol_rate, real_price, real_pnl, real_position_volume, real_flag, real_avg_entry_price
                )
                SELECT
                    position_id, signal_number, current_price, current_timestamp, signal_volume_rates,
                    signal_direction, signal_vol_rate, signal_order_type, signal_price, signal_liquidation, signal_close,
                    signal_position_volume, avg_entry_price, position_cost, signal_pnl, signal_flag,
                    signal_timestamp, signal_smart, signal_smart_reversed,
                    real_timestamp, real_vol_rate, real_price, real_pnl, real_position_volume, real_flag, real_avg_entry_price
                FROM levels_signals WHERE position_id=?""",
                (self._position_id,)
            )
            conn.execute("DELETE FROM levels_signals WHERE position_id=?", (self._position_id,))

            # Delete old positions, keeping the last deals_display_count
            max_deals = 10000
            if max_deals > 0:
                old_positions = conn.execute(
                    "SELECT position_id FROM deals_history GROUP BY position_id ORDER BY MAX(moved_to_history_at) DESC LIMIT ?",
                    (max_deals,)
                ).fetchall()
                keep_ids = [row[0] for row in old_positions]
                if keep_ids:
                    conn.execute(
                        "DELETE FROM deals_history WHERE position_id NOT IN ({})".format(','.join('?' * len(keep_ids))),
                        keep_ids
                    )
                else:
                    conn.execute("DELETE FROM deals_history")
            else:
                conn.execute("DELETE FROM deals_history")

            conn.commit()
            conn.close()

            new_pos_id = str(uuid.uuid4())
            self._set_position_id(new_pos_id)
            self.logger.info(f"Position closed, new position_id {new_pos_id}")
        except Exception as e:
            conn.rollback()
            self.logger.error(f"Error in _close_position: {e}", exc_info=True)
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Analyst (placeholder)
    # ------------------------------------------------------------------
    async def _get_analyst_signal(self) -> Optional[str]:
        return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self):
        if self.running:
            return
        self.running = True

        collector_id = self.config.get("collector_bot_id")
        if not collector_id:
            self.logger.error("No collector bot, cannot start")
            update_bot_status(self.bot_id, "stopped")
            self.running = False
            return

        await self.setup_exchange(collector_id, {"candles": ["candles", "ohlcv"]})
        self._collector_handle = self.get_exchange(collector_id)

        analyst_id = self.config.get("analyst_bot_id")
        if analyst_id:
            await self.setup_exchange(analyst_id, {"signal": ["close", "direction"]})
            self._analyst_handle = self.get_exchange(analyst_id)

        await self._update_price_history(limit=2000)
        if self._last_price is None:
            await self._update_price_history(limit=5)
            if self._last_price is None:
                self.logger.error("Cannot get current price from collector")
                update_bot_status(self.bot_id, "stopped")
                self.running = False
                return
        # Restore consistency of the active levels table
        await self._ensure_clean_levels_table()

        conn = sqlite3.connect(self._db_path)
        cur = conn.execute("SELECT COUNT(*) FROM levels_signals WHERE signal_flag=1 OR real_timestamp IS NOT NULL")
        has_trades = cur.fetchone()[0] > 0
        conn.close()

        if not has_trades:
            direction = await self._get_initial_direction()
            if direction:
                entry_price = self._last_price
                entry_ts = self._last_ts
                await self._rebuild_levels(direction, entry_price, entry_ts)
            else:
                self.logger.info("Waiting for analyst signal")
        else:
            self.logger.info("Restoring existing position from DB")

        self.task = asyncio.create_task(self._run())
        update_bot_status(self.bot_id, "running")
        cfg_check = get_bot_config(self.bot_id, include_status=True)
        self.logger.info(f"Grid bot {self.bot_id} status after update: {cfg_check.get('status')}")
        self.logger.info("Grid bot started")

    async def _get_initial_direction(self) -> Optional[str]:
        if self._analyst_handle:
            pass
        if len(self._price_history) >= 2:
            prev_price = self._price_history[0][1]
            last_price = self._price_history[1][1]
            return "long" if last_price > prev_price else "short"
        return "long"

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        update_bot_status(self.bot_id, "stopped")
        self.logger.info("Grid bot stopped")

    async def _run(self):
        interval = self.config.get("poll_interval_sec", 10)
        while self.running:
            try:
                if self.config_dirty:
                    await self.on_config_updated()
                    self.config_dirty = False

                await self._update_price_history(limit=2000)
                await self._check_grid_levels()
                await self._check_smart_averaging()

                if self._need_new_position:
                    self._need_new_position = False
                    direction = await self._get_initial_direction()
                    if direction and self._last_price:
                        await self._rebuild_levels(direction, self._last_price, self._last_ts)
            except Exception as e:
                self.logger.error(f"Error in main loop: {e}", exc_info=True)
            await asyncio.sleep(interval)

    async def on_config_updated(self):
        try:
            self.logger.info("on_config_updated: updating config and recalculating levels")
            self.config = get_bot_config(self.bot_id)
            await self._update_levels()
            self.logger.info("on_config_updated: levels successfully recalculated")
        except Exception as e:
            self.logger.error(f"on_config_updated: error updating levels: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # External method to request close (Close Position button)
    # ------------------------------------------------------------------
    async def request_close_position(self):
        if not self._price_history or self._last_price is None:
            self.logger.warning("No price data to close position")
            return

        current_price = self._price_history[-1][1]
        current_ts = self._price_history[-1][0]
        timeout_sec = self.config.get('execution_timeout_sec', 0)

        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            last = conn.execute(
                "SELECT * FROM levels_signals WHERE position_id=? AND (real_flag=1 OR signal_number=0) ORDER BY signal_number DESC LIMIT 1",
                (self._position_id,)
            ).fetchone()
            if not last:
                self.logger.warning("No active position to close")
                conn.close()
                return

            effective_avg = last['real_avg_entry_price'] or last['avg_entry_price']
            effective_vol = last['real_position_volume'] or last['signal_position_volume']
            total_vol = effective_vol

            vol_rates_row = conn.execute(
                "SELECT signal_volume_rates FROM levels_signals WHERE position_id=? AND (real_flag=1 OR signal_number=0) ORDER BY signal_number DESC LIMIT 1",
                (self._position_id,)
            ).fetchone()
            if vol_rates_row:
                vol_rates_list = json.loads(vol_rates_row['signal_volume_rates'])
            else:
                vol_rates_list = [1.0]
            vol_rates_list.append(total_vol)
            vol_rates_json = json.dumps(vol_rates_list)

            direction = last['signal_direction']
            sign = 1 if direction == 'buy' else -1
            pnl_close = sign * (current_price / effective_avg - 1) * self.config['leverage'] * 100

            self._create_signal(
                conn, -2, 'close', current_price, current_ts,
                vol_rate=total_vol,
                position_volume=0.0,
                emulate_execution=(timeout_sec == 0),
                current_price=current_price,
                signal_volume_rates=vol_rates_json,
                avg_entry_price=effective_avg,
                position_cost=effective_avg * effective_vol,
                signal_pnl=pnl_close,
                order_type='limit'
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            self.logger.error(f"Error in request_close_position: {e}", exc_info=True)
            return
        finally:
            conn.close()

        # Immediately close position in local logic
        await self._close_position()
        self._need_new_position = True
        self.logger.info("Manual close executed immediately")