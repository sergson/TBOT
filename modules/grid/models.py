# modules/grid/models.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import asyncio
import time
import math
import os
import uuid
import json
from typing import Dict, Any, Optional, List, Tuple

from core import auto_reg, BaseBot
from core.database import (
    Model, Char, Text, Integer, Boolean, Float, Selection, Json,
    get_bot_config, update_bot_config, update_bot_status
)
from core.logger import perf_logger

logger = perf_logger.get_logger('grid_bot', 'analytics')

# ----------------------------------------------------------------------
# Helper functions (unchanged)
# ----------------------------------------------------------------------
def timeframe_to_seconds(tf: str) -> int:
    unit = tf[-1]
    value = int(tf[:-1])
    if unit == 'm': return value * 60
    elif unit == 'h': return value * 3600
    elif unit == 'd': return value * 86400
    else: raise ValueError(f"Unsupported timeframe: {tf}")

def linear_extrapolate_price(prices: List[float], timestamps: List[float], future_sec: float) -> float:
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
# Declarative models for GridBot tables
# ----------------------------------------------------------------------
class LevelsSignals(Model):
    _name = 'levels.signals'
    _table = 'levels_signals'

    id = Integer(primary_key=True, autoincrement=True, required=True)
    position_id = Char(required=True, index=True)
    signal_number = Integer(required=True)
    current_price = Float()
    current_timestamp = Float()
    signal_volume_rates = Json()
    signal_direction = Selection([('buy','Buy'),('sell','Sell'),('close','Close'),('liquidation','Liquidation')])
    signal_vol_rate = Float()
    signal_order_type = Selection([('limit','Limit'),('market','Market')])
    signal_price = Float()
    signal_liquidation = Boolean(default=False)
    signal_close = Boolean(default=False)
    signal_position_volume = Float()
    avg_entry_price = Float()
    position_cost = Float()
    signal_pnl = Float()
    signal_flag = Boolean(default=False)
    signal_timestamp = Float()
    signal_smart = Boolean(default=False)
    signal_smart_reversed = Integer(default=0)
    real_timestamp = Float()
    real_vol_rate = Float()
    real_price = Float()
    real_pnl = Float()
    real_position_volume = Float()
    real_flag = Boolean(default=False)
    real_avg_entry_price = Float()
    created_at = Char(default="CURRENT_TIMESTAMP")  # won't auto-update; fine for now

class DealsHistory(Model):
    _name = 'deals.history'
    _table = 'deals_history'

    history_id = Integer(primary_key=True, autoincrement=True, required=True)
    position_id = Char(index=True)
    signal_number = Integer()
    current_price = Float()
    current_timestamp = Float()
    signal_volume_rates = Json()
    signal_direction = Selection([('buy','Buy'),('sell','Sell'),('close','Close'),('liquidation','Liquidation')])
    signal_vol_rate = Float()
    signal_order_type = Selection([('limit','Limit'),('market','Market')])
    signal_price = Float()
    signal_liquidation = Boolean(default=False)
    signal_close = Boolean(default=False)
    signal_position_volume = Float()
    avg_entry_price = Float()
    position_cost = Float()
    signal_pnl = Float()
    signal_flag = Boolean(default=False)
    signal_timestamp = Float()
    signal_smart = Boolean(default=False)
    signal_smart_reversed = Integer(default=0)
    real_timestamp = Float()
    real_vol_rate = Float()
    real_price = Float()
    real_pnl = Float()
    real_position_volume = Float()
    real_flag = Boolean(default=False)
    real_avg_entry_price = Float()
    moved_to_history_at = Float()
    created_at = Char(default="CURRENT_TIMESTAMP")

class PriceHistory(Model):
    _name = 'price.history'
    _table = 'price_history'

    timestamp = Integer(primary_key=True, required=True)
    open = Float()
    high = Float()
    low = Float()
    close = Float()
    volume = Float()

# ----------------------------------------------------------------------
# GridBot class with all methods converted to ORM
# ----------------------------------------------------------------------
@auto_reg
class GridBot(BaseBot):
    _name = "grid.bot"
    _inherit = "base.bot"

    def __init__(self, bot_id: int, manager=None):
        super().__init__(bot_id, manager)  # init env
        self.config = get_bot_config(bot_id)
        self._db_path = self.db_path  # alias
        self.logger = perf_logger.get_logger(f"grid_{bot_id}", "analytics")
        self._collector_handle = None
        self._analyst_handle = None

        self._position_id = self._get_current_position_id()
        self._price_history = []
        self._last_price = None
        self._last_ts = None
        self._need_new_position = False

        # ensure max_averaging_count present
        if self.config.get('max_averaging_count') is None:
            self._save_max_averaging_count_to_bot_settings()

    # ------------------------------------------------------------------
    # Capabilities (unchanged signature, but using ORM inside)
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

    # ------------------------------------------------------------------
    # Data access methods (converted)
    # ------------------------------------------------------------------
    async def _get_pending_signals(self):
        records = self.env['levels.signals'].search([
            ('signal_flag', '=', True),
            ('real_flag', '=', False)
        ])
        return records.read()

    async def _set_execution_result(self, data: Dict[str, Any]):
        signal_number = data.get("signal_number")
        signal_ts = data.get("signal_timestamp")
        real_price = data.get("real_price", 0.0)
        real_timestamp = data.get("real_timestamp", time.time())

        if signal_number is None or signal_ts is None:
            return

        record = self.env['levels.signals'].search([
            ('signal_number', '=', signal_number),
            ('real_flag', '=', False)
        ], limit=1)
        if not record:
            return
        rec = record[0]
        direction = rec.signal_direction
        vol_rate = rec.signal_vol_rate

        # get previous real state
        prev_records = self.env['levels.signals'].search([
            ('position_id', '=', self._position_id),
            ('real_flag', '=', True)
        ], order='id DESC', limit=1)
        old_vol = prev_records[0].real_position_volume if prev_records and prev_records[0].real_position_volume is not None else 0.0
        old_avg = prev_records[0].real_avg_entry_price if prev_records and prev_records[0].real_avg_entry_price is not None else 0.0

        # determine sign
        entry_rec = self.env['levels.signals'].search([
            ('position_id', '=', self._position_id),
            ('signal_number', '=', 0)
        ], limit=1)
        sign = 1 if (entry_rec and entry_rec[0].signal_direction == 'buy') else -1

        new_avg, new_vol, _ = self._apply_trade_to_position(old_vol, old_avg, direction, real_price, vol_rate, sign)

        lev = self.config['leverage']
        if new_vol > 1e-12:
            total_pnl = sign * (real_price / new_avg - 1) * lev * 100
        else:
            if old_avg > 0:
                total_pnl = (real_price - old_avg) / old_avg * lev * 100 if sign == 1 \
                    else (old_avg - real_price) / old_avg * lev * 100
            else:
                total_pnl = 0.0

        rec.write({
            'real_timestamp': real_timestamp,
            'real_vol_rate': vol_rate,
            'real_price': real_price,
            'real_pnl': total_pnl,
            'real_position_volume': new_vol,
            'real_flag': True,
            'real_avg_entry_price': new_avg if new_vol > 1e-12 else 0.0,
        })

    async def _get_position_id(self):
        return self._position_id

    async def _get_signals_by_position(self, position_id: str):
        active = self.env['levels.signals'].search([('position_id', '=', position_id)]).read()
        historical = self.env['deals.history'].search([('position_id', '=', position_id)]).read()
        return active + historical

    # ------------------------------------------------------------------
    # Price history update (converted)
    # ------------------------------------------------------------------
    async def _update_price_history(self, limit=2000):
        if not self._collector_handle:
            return
        try:
            candles = await self._collector_handle.get("candles", limit=limit)
            if not candles:
                return
            price_hist = self.env['price.history']
            for c in candles:
                ts = c['timestamp']
                existing = price_hist.search([('timestamp', '=', ts)], limit=1)
                if existing:
                    existing[0].write({
                        'open': c['open'],
                        'high': c['high'],
                        'low': c['low'],
                        'close': c['close'],
                        'volume': c['volume']
                    })
                else:
                    price_hist.create([{
                        'timestamp': ts,
                        'open': c['open'],
                        'high': c['high'],
                        'low': c['low'],
                        'close': c['close'],
                        'volume': c['volume']
                    }])
            # limit rows
            count = price_hist.search_count([])
            if count > limit * 1.1:
                # get oldest timestamps beyond limit
                oldest = price_hist.search([], order='timestamp DESC', limit=limit)
                if oldest:
                    min_ts = oldest[-1].timestamp
                    old_records = price_hist.search([('timestamp', '<', min_ts)])
                    old_records.unlink()

            # update last price
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
    # Grid building and level management (converted)
    # ------------------------------------------------------------------
    async def _rebuild_levels(self, direction: str, entry_price: float, entry_ts: float):
        await self._ensure_clean_levels_table()
        cfg = self.config
        max_count = cfg.get('max_averaging_count', 1)
        threshold_pct = cfg['averaging_threshold_pnl']
        lev = cfg['leverage']
        strategy = cfg['averaging_strategy']
        volumes = get_volume_sequence(strategy, max_count, entry_vol=1.0)

        sign = 1 if direction == 'long' else -1
        Level = self.env['levels.signals']
        # remove all existing levels for this position
        Level.search([('position_id', '=', self._position_id)]).unlink()

        # Entry (signal_number=0)
        entry_vol = 1.0
        entry_cost = entry_price * entry_vol
        Level.create([{
            'position_id': self._position_id,
            'signal_number': 0,
            'current_price': entry_price,
            'current_timestamp': entry_ts,
            'signal_volume_rates': [entry_vol],
            'signal_direction': 'buy' if direction == 'long' else 'sell',
            'signal_vol_rate': entry_vol,
            'signal_order_type': 'limit',
            'signal_price': entry_price,
            'signal_liquidation': False,
            'signal_position_volume': entry_vol,
            'avg_entry_price': entry_price,
            'position_cost': entry_cost,
            'signal_pnl': 0.0,
            'signal_flag': True,
            'signal_timestamp': entry_ts,
            'real_timestamp': entry_ts,
            'real_vol_rate': entry_vol,
            'real_price': entry_price,
            'real_pnl': 0.0,
            'real_position_volume': entry_vol,
            'real_flag': True,
            'real_avg_entry_price': entry_price,
        }])

        # Averaging levels
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

            Level.create([{
                'position_id': self._position_id,
                'signal_number': n,
                'current_price': entry_price,
                'current_timestamp': entry_ts,
                'signal_volume_rates': volumes[:n],
                'signal_direction': 'buy' if direction == 'long' else 'sell',
                'signal_vol_rate': vol,
                'signal_order_type': 'limit',
                'signal_price': level_price,
                'signal_liquidation': False,
                'signal_position_volume': new_position_vol,
                'avg_entry_price': new_avg_price,
                'position_cost': new_position_cost,
                'signal_pnl': pnl,
                'signal_flag': False,
                'real_flag': False,
            }])
            prev_avg_price = new_avg_price
            prev_position_vol = new_position_vol
            prev_position_cost = new_position_cost

        # Liquidation
        liq_pnl = cfg.get('liquidation_pnl', 90)
        liq_price = entry_price * (1 - sign * (liq_pnl / 100.0) / lev)
        liq_order_type = cfg.get('liquidation_order_type', 'market')
        Level.create([{
            'position_id': self._position_id,
            'signal_number': -1,
            'signal_direction': 'sell' if direction == 'long' else 'buy',
            'signal_price': liq_price,
            'signal_liquidation': True,
            'signal_order_type': liq_order_type,
            'signal_flag': False,
            'real_flag': False,
        }])

        # Close reference
        close_pnl = cfg.get('close_pnl', 50.0)
        close_price = entry_price * (1 + sign * (close_pnl / 100.0) / lev)
        Level.create([{
            'position_id': self._position_id,
            'signal_number': -2,
            'signal_direction': 'sell' if direction == 'long' else 'buy',
            'signal_price': close_price,
            'signal_close': True,
            'signal_flag': False,
            'real_flag': False,
        }])
        self.logger.info(f"Rebuilt levels for {self._position_id}, direction {direction}")

    async def _update_levels(self):
        self.logger.debug(f"_update_levels: last_price={self._last_price}, position_id={self._position_id}")
        if self._last_price is None:
            self.logger.warning("_update_levels: no current price, skipping")
            return

        conn = self.env['levels.signals']  # manager
        entry_rec = conn.search([('position_id', '=', self._position_id), ('signal_number', '=', 0)], limit=1)
        if not entry_rec:
            return
        sign = 1 if entry_rec[0].signal_direction == 'buy' else -1

        last_exec = self._get_last_executed()
        if not last_exec:
            return
        effective_avg, effective_vol = self._get_effective_avg_vol(last_exec)
        effective_cost = effective_vol * effective_avg

        if effective_avg == 0 or effective_vol == 0:
            self.logger.warning("_update_levels: effective avg or vol is zero, cannot update levels")
            return

        # find last executed normal level number
        exec_levels = conn.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '>', 0),
            ('real_flag', '=', True)
        ])
        last_number = max([r.signal_number for r in exec_levels]) if exec_levels else 0

        # remove non-executed levels > last_number
        conn.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '>', last_number),
            ('real_flag', '=', False),
            ('signal_smart', '=', False)
        ]).unlink()

        cfg = self.config
        max_count = cfg.get('max_averaging_count', 1)
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
            if new_pos_vol == 0:
                new_avg = 0.0
            else:
                new_avg = new_pos_cost / new_pos_vol

            if new_avg == 0:
                pnl = 0.0
            else:
                pnl = sign * (level_price / new_avg - 1) * lev * 100

            direction_str = 'buy' if sign == 1 else 'sell'
            conn.create([{
                'position_id': self._position_id,
                'signal_number': n,
                'current_price': self._last_price or None,
                'current_timestamp': time.time(),
                'signal_volume_rates': volumes[:n],
                'signal_direction': direction_str,
                'signal_vol_rate': vol,
                'signal_order_type': 'limit',
                'signal_price': level_price,
                'signal_position_volume': new_pos_vol,
                'avg_entry_price': new_avg,
                'position_cost': new_pos_cost,
                'signal_pnl': pnl,
                'signal_flag': False,
                'real_flag': False,
                'signal_close': False,
            }])
            prev_avg = new_avg
            prev_vol = new_pos_vol
            prev_cost = new_pos_cost

        # update liquidation and close
        liq_price = effective_avg * (1 - sign * cfg['liquidation_pnl'] / 100.0 / lev)
        close_price = effective_avg * (1 + sign * cfg['close_pnl'] / 100.0 / lev)
        liq_recs = conn.search([('position_id', '=', self._position_id), ('signal_number', '=', -1)])
        if liq_recs:
            liq_recs[0].write({'signal_price': liq_price})
        close_recs = conn.search([('position_id', '=', self._position_id), ('signal_number', '=', -2)])
        if close_recs:
            close_recs[0].write({'signal_price': close_price})
        self.logger.info("Levels updated after configuration change")

    # ------------------------------------------------------------------
    # Signal checking (converted)
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

        Level = self.env['levels.signals']

        # entry check
        entry_rec = Level.search([('position_id', '=', self._position_id), ('signal_number', '=', 0)], limit=1)
        if not entry_rec:
            self._need_new_position = True
            return
        sign = 1 if entry_rec[0].signal_direction == 'buy' else -1

        # last executed
        last_exec = self._get_last_executed()
        if not last_exec:
            self._need_new_position = True
            return
        effective_avg, effective_vol = self._get_effective_avg_vol(last_exec)
        if effective_avg == 0 or effective_vol == 0:
            self.logger.warning("Effective avg or vol is zero, no active position.")
            self._need_new_position = True
            return
        pnl = sign * (current_price / effective_avg - 1) * lev * 100

        # Liquidation
        existing_liq = Level.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '=', -1),
            ('signal_flag', '=', True),
            ('real_flag', '=', False)
        ], limit=1)
        if not existing_liq and pnl <= -cfg['liquidation_pnl']:
            total_vol = effective_vol
            vol_rates_row = Level.search([
                ('position_id', '=', self._position_id),
                ('real_flag', '=', True)
            ], order='signal_number DESC', limit=1)
            # Fix: signal_volume_rates is already deserialized as a list
            vol_rates_list = vol_rates_row[0].signal_volume_rates if vol_rates_row and vol_rates_row[0].signal_volume_rates else [1.0]
            vol_rates_list.append(total_vol)
            self._create_signal(
                signal_number=-1,
                direction='sell' if sign == 1 else 'buy',
                price=current_price,
                timestamp=current_ts,
                vol_rate=total_vol,
                position_volume=0.0,
                emulate_execution=(timeout_sec == 0),
                current_price=current_price,
                signal_volume_rates=vol_rates_list,  # pass list
                avg_entry_price=effective_avg,
                position_cost=effective_avg * effective_vol,
                signal_pnl=pnl,
                order_type=cfg.get('liquidation_order_type', 'market')
            )
            if timeout_sec == 0:
                await self._close_position()
                self._need_new_position = True
            else:
                self.logger.info("Liquidation signal generated, waiting for execution")
            return

        # Close
        existing_close = Level.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '=', -2),
            ('signal_flag', '=', True),
            ('real_flag', '=', False)
        ], limit=1)
        if not existing_close and pnl >= cfg['breakeven_pnl']:
            use_analyst = cfg.get('use_analyst_close', 1) and cfg.get('analyst_bot_id')
            if use_analyst:
                analyst_signal = await self._get_analyst_signal()
                if not analyst_signal:
                    return
            if pnl >= cfg['close_pnl']:
                total_vol = effective_vol
                vol_rates_row = Level.search([
                    ('position_id', '=', self._position_id),
                    ('real_flag', '=', True)
                ], order='signal_number DESC', limit=1)
                # Fix: signal_volume_rates is already deserialized as a list
                vol_rates_list = vol_rates_row[0].signal_volume_rates if vol_rates_row and vol_rates_row[0].signal_volume_rates else [1.0]
                vol_rates_list.append(total_vol)
                close_direction = 'sell' if sign == 1 else 'buy'
                self._create_signal(
                    signal_number=-2,
                    direction=close_direction,
                    price=current_price,
                    timestamp=current_ts,
                    vol_rate=total_vol,
                    position_volume=0.0,
                    emulate_execution=(timeout_sec == 0),
                    current_price=current_price,
                    signal_volume_rates=vol_rates_list,  # pass list
                    avg_entry_price=effective_avg,
                    position_cost=effective_avg * effective_vol,
                    signal_pnl=pnl,
                    order_type='limit'
                )
                if timeout_sec == 0:
                    await self._close_position()
                    self._need_new_position = True
                else:
                    self.logger.info("Close signal generated, waiting for execution")
                return

        # Averaging levels
        levels = Level.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '>=', 1),
            ('real_flag', '=', False),
            ('signal_flag', '=', False)
        ], order='signal_number ASC')

        for level in levels:
            level_price = level.signal_price
            # immediate crossing
            if (sign == 1 and current_price <= level_price) or \
               (sign == -1 and current_price >= level_price):
                self._create_signal(
                    signal_number=level.signal_number,
                    direction=level.signal_direction,
                    price=level_price,
                    timestamp=current_ts,
                    vol_rate=level.signal_vol_rate,
                    position_volume=level.signal_position_volume,
                    emulate_execution=(timeout_sec == 0),
                    current_price=current_price,
                    signal_volume_rates=level.signal_volume_rates,  # already list
                    avg_entry_price=level.avg_entry_price,
                    position_cost=level.position_cost,
                    signal_pnl=level.signal_pnl,
                    order_type=level.signal_order_type if level.signal_order_type else 'limit'
                )
                if timeout_sec == 0:
                    await self._apply_execution_and_recalc(level.signal_number)
                return

            # extrapolation
            if len(self._price_history) >= 2:
                timestamps = [p[0] for p in self._price_history[-2:]]
                prices = [p[1] for p in self._price_history[-2:]]
                extrap_price = linear_extrapolate_price(prices, timestamps, reserve_sec)
                if (sign == 1 and extrap_price <= level_price <= current_price) or \
                   (sign == -1 and extrap_price >= level_price >= current_price):
                    delta_price = extrap_price - current_price
                    if abs(delta_price) > 1e-8:
                        dt = reserve_sec * (level_price - current_price) / delta_price
                        time_to_cross = max(0.0, dt)
                    else:
                        time_to_cross = 0.0
                    if time_to_cross <= reserve_sec:
                        signal_ts = current_ts + time_to_cross
                        self._create_signal(
                            signal_number=level.signal_number,
                            direction=level.signal_direction,
                            price=level_price,
                            timestamp=signal_ts,
                            vol_rate=level.signal_vol_rate,
                            position_volume=level.signal_position_volume,
                            emulate_execution=(timeout_sec == 0)
                        )
                        if timeout_sec == 0:
                            await self._apply_execution_and_recalc(level.signal_number)
                        return

        # Timeout of pending signals
        if timeout_sec > 0:
            pending = Level.search([
                ('position_id', '=', self._position_id),
                ('signal_flag', '=', True),
                ('real_flag', '=', False),
                ('signal_timestamp', '!=', None)
            ])
            now = time.time()
            for sig in pending:
                if now - (sig.signal_timestamp or 0) > timeout_sec:
                    if sig.signal_number < 0:
                        if recalc_strategy == 'recalc_grid':
                            self._create_signal(
                                signal_number=sig.signal_number,
                                direction=sig.signal_direction,
                                price=current_price,
                                timestamp=current_ts,
                                vol_rate=sig.signal_vol_rate,
                                position_volume=0.0,
                                emulate_execution=(timeout_sec == 0),
                                current_price=current_price,
                                signal_volume_rates=sig.signal_volume_rates,  # already list
                                avg_entry_price=sig.avg_entry_price,
                                position_cost=sig.position_cost,
                                signal_pnl=sig.signal_pnl,
                                order_type=sig.signal_order_type if sig.signal_order_type else 'limit'
                            )
                            self.logger.info(f"Reset timeout for signal #{sig.signal_number} (recalc_grid)")
                        elif recalc_strategy == 'market_order':
                            sig.write({'signal_order_type': 'market'})
                            self.logger.info(f"Changed signal #{sig.signal_number} to market order")
                        break
                    else:
                        if recalc_strategy == 'recalc_grid':
                            await self._recalc_levels_from(sig.signal_number)
                            self.logger.info(f"Recalc levels from #{sig.signal_number} due to timeout")
                        elif recalc_strategy == 'market_order':
                            sig.write({'signal_order_type': 'market'})
                            self.logger.info(f"Changed signal #{sig.signal_number} to market order")
                        break

    # ------------------------------------------------------------------
    # Create signal (converted)
    # ------------------------------------------------------------------
    def _create_signal(self, signal_number, direction, price, timestamp,
                       vol_rate, position_volume, emulate_execution=False,
                       current_price=None, signal_volume_rates=None,
                       avg_entry_price=None, position_cost=None,
                       signal_pnl=None, order_type=None):
        Level = self.env['levels.signals']
        record = Level.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '=', signal_number)
        ], limit=1)
        if not record:
            # For negative signals that don't exist, create new
            record = Level.create([{
                'position_id': self._position_id,
                'signal_number': signal_number,
                'signal_direction': direction,
                'signal_price': price,
                'signal_flag': True,
                'signal_timestamp': timestamp,
                'signal_liquidation': signal_number == -1,
                'signal_close': signal_number == -2,
            }])
            rec = record[0]
        else:
            rec = record[0]

        updates = {
            'signal_flag': True,
            'signal_timestamp': timestamp,
            'signal_price': price,
            'signal_liquidation': signal_number == -1,
            'signal_close': signal_number == -2,
        }
        if current_price is not None:
            updates['current_price'] = current_price
        if signal_volume_rates is not None:
            updates['signal_volume_rates'] = signal_volume_rates  # list, ORM serializes
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
        rec.write(updates)

        if emulate_execution:
            # similar to _set_execution_result but for new signal
            entry_rec = Level.search([('position_id', '=', self._position_id), ('signal_number', '=', 0)], limit=1)
            sign = 1 if (entry_rec and entry_rec[0].signal_direction == 'buy') else -1

            prev = Level.search([
                ('position_id', '=', self._position_id),
                ('real_flag', '=', True)
            ], order='id DESC', limit=1)
            old_vol = prev[0].real_position_volume if prev and prev[0].real_position_volume is not None else 0.0
            old_avg = prev[0].real_avg_entry_price if prev and prev[0].real_avg_entry_price is not None else 0.0

            new_avg, new_vol, _ = self._apply_trade_to_position(old_vol, old_avg, direction, price, vol_rate, sign)
            lev = self.config['leverage']
            if new_vol > 1e-12:
                total_pnl = sign * (price / new_avg - 1) * lev * 100
            else:
                if old_avg > 0:
                    total_pnl = (price - old_avg) / old_avg * lev * 100 if sign == 1 \
                        else (old_avg - price) / old_avg * lev * 100
                else:
                    total_pnl = 0.0

            rec.write({
                'real_timestamp': timestamp,
                'real_vol_rate': vol_rate,
                'real_price': price,
                'real_position_volume': new_vol,
                'real_avg_entry_price': new_avg if new_vol > 1e-12 else 0.0,
                'real_flag': True,
                'real_pnl': total_pnl
            })

    async def _apply_execution_and_recalc(self, signal_number: int):
        Level = self.env['levels.signals']
        rec = Level.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '=', signal_number)
        ], limit=1)
        if not rec:
            return
        rec = rec[0]
        avg_after, pos_vol_after = self._get_effective_avg_vol(rec)
        pos_cost_after = pos_vol_after * avg_after

        # remove later non-executed, non-smart levels
        Level.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '>', signal_number),
            ('real_flag', '=', False),
            ('signal_smart', '=', False)
        ]).unlink()

        cfg = self.config
        max_count = cfg.get('max_averaging_count', 1)
        threshold_pct = cfg['averaging_threshold_pnl']
        lev = cfg['leverage']
        strategy = cfg['averaging_strategy']
        volumes = get_volume_sequence(strategy, max_count, entry_vol=1.0)
        direction = rec.signal_direction
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

            Level.create([{
                'position_id': self._position_id,
                'signal_number': n,
                'current_price': self._last_price or rec.current_price,
                'current_timestamp': time.time(),
                'signal_volume_rates': volumes[:n],
                'signal_direction': direction,
                'signal_vol_rate': vol,
                'signal_order_type': 'limit',
                'signal_price': level_price,
                'signal_position_volume': new_pos_vol,
                'avg_entry_price': new_avg,
                'position_cost': new_pos_cost,
                'signal_pnl': pnl,
                'signal_flag': False,
                'real_flag': False,
            }])
            prev_avg = new_avg
            prev_vol = new_pos_vol
            prev_cost = new_pos_cost

        liq_pnl = cfg.get('liquidation_pnl', 90)
        liq_price = avg_after * (1 - sign * liq_pnl / 100.0 / lev)
        liq_rec = Level.search([('position_id', '=', self._position_id), ('signal_number', '=', -1)])
        if liq_rec:
            liq_rec[0].write({'signal_price': liq_price})

        close_pnl = cfg.get('close_pnl', 50.0)
        close_price = avg_after * (1 + sign * close_pnl / 100.0 / lev)
        close_rec = Level.search([('position_id', '=', self._position_id), ('signal_number', '=', -2)])
        if close_rec:
            close_rec[0].write({'signal_price': close_price})
        self.logger.info(f"Applied execution for level #{signal_number} and recalculated subsequent levels")

    async def _recalc_levels_from(self, start_number: int):
        if start_number < 0:
            return
        Level = self.env['levels.signals']
        entry_rec = Level.search([('position_id', '=', self._position_id), ('signal_number', '=', 0)], limit=1)
        sign = 1 if (entry_rec and entry_rec[0].signal_direction == 'buy') else -1
        last_exec = self._get_last_executed()
        if not last_exec:
            return
        effective_avg, effective_vol = self._get_effective_avg_vol(last_exec)
        effective_cost = effective_vol * effective_avg

        Level.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '>=', start_number),
            ('real_flag', '=', False),
            ('signal_smart', '=', False)
        ]).unlink()

        cfg = self.config
        max_count = cfg.get('max_averaging_count', 1)
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

            Level.create([{
                'position_id': self._position_id,
                'signal_number': n,
                'current_price': self._last_price or None,
                'current_timestamp': time.time(),
                'signal_volume_rates': volumes[:n],
                'signal_direction': 'buy' if sign == 1 else 'sell',
                'signal_vol_rate': vol,
                'signal_order_type': 'limit',
                'signal_price': level_price,
                'signal_position_volume': new_pos_vol,
                'avg_entry_price': new_avg,
                'position_cost': new_pos_cost,
                'signal_pnl': pnl,
                'signal_flag': False,
                'real_flag': False,
            }])
            prev_avg = new_avg
            prev_vol = new_pos_vol
            prev_cost = new_pos_cost

        liq_price = effective_avg * (1 - sign * cfg['liquidation_pnl'] / 100.0 / lev)
        close_price = effective_avg * (1 + sign * cfg['close_pnl'] / 100.0 / lev)
        liq_rec = Level.search([('position_id', '=', self._position_id), ('signal_number', '=', -1)])
        if liq_rec:
            liq_rec[0].write({'signal_price': liq_price})
        close_rec = Level.search([('position_id', '=', self._position_id), ('signal_number', '=', -2)])
        if close_rec:
            close_rec[0].write({'signal_price': close_price})
        self.logger.info(f"Levels recalculated from #{start_number} due to timeout")

    # ------------------------------------------------------------------
    # Smart averaging (converted)
    # ------------------------------------------------------------------
    async def _check_smart_averaging(self):
        cfg = self.config
        smart_count = cfg.get('smart_averaging_count', 0)
        if smart_count == 0:
            return
        max_count = self._get_max_averaging_count()
        smart_count = min(smart_count, max_count)

        Level = self.env['levels.signals']
        all_rows = Level.search([('position_id', '=', self._position_id)])
        # eligible: executed normal levels without smart reversed
        eligible = all_rows.filtered(
            lambda r: r.signal_number > 0 and r.real_flag and r.signal_flag and not r.signal_smart and not r.signal_smart_reversed
        )
        # sort by signal_number descending
        eligible = eligible.sorted(key=lambda r: r.signal_number, reverse=True)
        if not eligible:
            return
        # take first smart_count
        levels = eligible[:smart_count]
        current_price = self._price_history[-1][1] if self._price_history else 0
        threshold_pct = cfg['averaging_threshold_pnl']
        leverage = cfg['leverage']
        timeout_sec = cfg['execution_timeout_sec']

        # find minimal negative signal number
        neg_numbers = [r.signal_number for r in all_rows if r.signal_number < 0]
        min_negative = min(neg_numbers) if neg_numbers else -2

        for level in levels:
            direction = level.signal_direction
            real_price = level.real_price
            if real_price is None:
                continue
            trigger_price = None
            if direction == 'buy':
                trigger_price = real_price * (1 + threshold_pct / 2 / 100 / leverage)
                if current_price >= trigger_price:
                    self._create_smart_reverse(level, new_signal_number=min_negative - 1, emulate_execution=(timeout_sec == 0))
                    min_negative -= 1
            else:
                trigger_price = real_price * (1 - threshold_pct / 2 / 100 / leverage)
                if current_price <= trigger_price:
                    self._create_smart_reverse(level, new_signal_number=min_negative - 1, emulate_execution=(timeout_sec == 0))
                    min_negative -= 1

    async def _check_smart_executions(self):
        Level = self.env['levels.signals']
        executed_smarts = Level.search([
            ('position_id', '=', self._position_id),
            ('signal_smart', '=', True),
            ('real_flag', '=', True),
            ('signal_smart_reversed', '>', 0)
        ])
        if not executed_smarts:
            return
        for smart in executed_smarts:
            parent_number = smart.signal_smart_reversed
            parent = Level.search([
                ('position_id', '=', self._position_id),
                ('signal_number', '=', parent_number),
                ('signal_smart', '=', False),
                ('real_flag', '=', True)
            ], limit=1)
            if not parent:
                # Parent not found — mark smart as processed and skip
                smart.write({'signal_smart_reversed': -2})
                continue
            parent = parent[0]
            if parent.signal_smart_reversed == -2:
                continue
            # Check that parent has non-zero real volume
            parent_vol = parent.real_position_volume if parent.real_position_volume is not None else 0.0
            if parent_vol <= 0:
                # Invalid parent — skip
                smart.write({'signal_smart_reversed': -2})
                continue

            parent.write({'signal_smart_reversed': -2})
            # Mark the smart signal itself as processed to avoid repetition
            smart.write({'signal_smart_reversed': -2})

            # Increase max_averaging_count
            new_max = self._get_max_averaging_count() + 1
            self.config['max_averaging_count'] = new_max
            update_bot_config(self.bot_id, self.config)

            # Recalculate levels
            await self._update_levels()
            self.logger.info(
                f"Smart execution processed for parent #{parent_number}, max_averaging_count increased to {new_max}")
            return

    def _create_smart_reverse(self, original_level, new_signal_number: int, emulate_execution: bool = False):
        cfg = self.config
        threshold_pct = cfg['averaging_threshold_pnl']
        leverage = cfg['leverage']
        parent_number = original_level.signal_number
        parent_direction = original_level.signal_direction
        parent_real_price = original_level.real_price
        parent_vol = original_level.signal_vol_rate

        reverse_dir = 'sell' if parent_direction == 'buy' else 'buy'
        if reverse_dir == 'sell':
            reverse_price = parent_real_price * (1 + threshold_pct / 100 / leverage)
        else:
            reverse_price = parent_real_price * (1 - threshold_pct / 100 / leverage)

        Level = self.env['levels.signals']
        vals = {
            'position_id': self._position_id,
            'signal_number': new_signal_number,
            'current_price': self._last_price or parent_real_price,
            'current_timestamp': time.time(),
            'signal_volume_rates': [parent_vol],
            'signal_direction': reverse_dir,
            'signal_vol_rate': parent_vol,
            'signal_order_type': 'limit',
            'signal_price': reverse_price,
            'signal_liquidation': False,
            'signal_close': False,
            'signal_position_volume': 0,
            'avg_entry_price': 0,
            'position_cost': 0,
            'signal_pnl': 0,
            'signal_flag': True,
            'signal_timestamp': time.time(),
            'signal_smart': True,
            'signal_smart_reversed': parent_number,
            'real_flag': False,
        }
        if emulate_execution:
            entry_rec = Level.search([('position_id', '=', self._position_id), ('signal_number', '=', 0)], limit=1)
            sign = 1 if (entry_rec and entry_rec[0].signal_direction == 'buy') else -1
            prev = Level.search([
                ('position_id', '=', self._position_id),
                ('real_flag', '=', True)
            ], order='id DESC', limit=1)
            old_vol = prev[0].real_position_volume if prev and prev[0].real_position_volume is not None else 0.0
            old_avg = prev[0].real_avg_entry_price if prev and prev[0].real_avg_entry_price is not None else 0.0

            new_avg, new_vol, _ = self._apply_trade_to_position(old_vol, old_avg, reverse_dir, reverse_price, parent_vol, sign)
            lev = self.config['leverage']
            if new_vol > 1e-12:
                total_pnl = sign * (reverse_price / new_avg - 1) * lev * 100
            else:
                if old_avg > 0:
                    total_pnl = (reverse_price - old_avg) / old_avg * lev * 100 if sign == 1 \
                        else (old_avg - reverse_price) / old_avg * lev * 100
                else:
                    total_pnl = 0.0
            vals.update({
                'real_flag': True,
                'real_timestamp': time.time(),
                'real_vol_rate': parent_vol,
                'real_price': reverse_price,
                'real_pnl': total_pnl,
                'real_position_volume': new_vol,
                'real_avg_entry_price': new_avg if new_vol > 1e-12 else 0.0,
            })
        new_rec = Level.create([vals])[0]
        # mark parent as waiting for smart execution
        parent_rec = Level.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '=', parent_number),
            ('signal_smart', '=', False)
        ], limit=1)
        if parent_rec:
            parent_rec[0].write({'signal_smart_reversed': -1})
        self.logger.info(f"Smart reverse created for level #{parent_number} at {reverse_price}")

    # ------------------------------------------------------------------
    # Close position and move to history (converted)
    # ------------------------------------------------------------------
    async def _close_position(self):
        Level = self.env['levels.signals']
        History = self.env['deals.history']
        records = Level.search([('position_id', '=', self._position_id)])
        if records:
            history_vals = []
            for rec in records:
                data = rec.read()
                data.pop('id', None)
                data.pop('created_at', None)
                data['moved_to_history_at'] = time.time()
                history_vals.append(data)
            History.create(history_vals)
            records.unlink()

            # reset max_averaging_count
            initial = self.config.get('max_averaging_count_initial')
            if initial is None:
                initial = self.config.get('max_averaging_count', 1)
            self.config['max_averaging_count'] = initial if initial is not None else 1
            update_bot_config(self.bot_id, self.config)

            new_pos_id = str(uuid.uuid4())
            self._set_position_id(new_pos_id)
            self.logger.info(f"Position closed, new position_id {new_pos_id}")

    async def request_close_position(self):
        if not self._price_history or self._last_price is None:
            self.logger.warning("No price data to close position")
            return
        current_price = self._price_history[-1][1]
        current_ts = self._price_history[-1][0]
        timeout_sec = self.config.get('execution_timeout_sec', 0)

        Level = self.env['levels.signals']
        last_exec = self._get_last_executed()
        if not last_exec:
            self.logger.warning("No active position to close")
            return
        effective_avg, effective_vol = self._get_effective_avg_vol(last_exec)
        if effective_avg == 0 or effective_vol == 0:
            self.logger.warning("No active position or zero volume.")
            return
        total_vol = effective_vol

        vol_rates_row = Level.search([
            ('position_id', '=', self._position_id),
            ('real_flag', '=', True)
        ], order='signal_number DESC', limit=1)
        # Fix: signal_volume_rates is already a list
        vol_rates_list = vol_rates_row[0].signal_volume_rates if vol_rates_row and vol_rates_row[0].signal_volume_rates else [1.0]
        vol_rates_list.append(total_vol)

        direction = last_exec.signal_direction
        sign = 1 if direction == 'buy' else -1
        pnl_close = sign * (current_price / effective_avg - 1) * self.config['leverage'] * 100

        self._create_signal(
            signal_number=-2,
            direction='sell' if sign == 1 else 'buy',
            price=current_price,
            timestamp=current_ts,
            vol_rate=total_vol,
            position_volume=0.0,
            emulate_execution=(timeout_sec == 0),
            current_price=current_price,
            signal_volume_rates=vol_rates_list,  # pass list
            avg_entry_price=effective_avg,
            position_cost=effective_avg * effective_vol,
            signal_pnl=pnl_close,
            order_type='limit'
        )
        if timeout_sec == 0:
            await self._close_position()
            self._need_new_position = True
            self.logger.info("Manual close executed immediately")
        else:
            self.logger.info("Manual close signal created, waiting for execution")

    # ------------------------------------------------------------------
    # Helper methods (some converted)
    # ------------------------------------------------------------------
    def _get_effective_avg_vol(self, row):
        avg = row.real_avg_entry_price if row.real_avg_entry_price is not None else row.avg_entry_price
        vol = row.real_position_volume if row.real_position_volume is not None else row.signal_position_volume
        return avg or 0.0, vol or 0.0

    def _get_last_executed(self):
        records = self.env['levels.signals'].search([
            ('position_id', '=', self._position_id),
            ('real_flag', '=', True),
            ('real_position_volume', '>', 0)  # only with non-zero volume
        ], order='id DESC', limit=1)
        return records[0] if records else None

    def _apply_trade_to_position(self, current_vol, current_avg, trade_direction, trade_price, trade_volume, sign):
        increase = (sign == 1 and trade_direction == 'buy') or (sign == -1 and trade_direction == 'sell')
        if increase:
            new_vol = current_vol + trade_volume
            new_cost = current_avg * current_vol + trade_price * trade_volume
        else:
            new_vol = current_vol - trade_volume
            new_cost = current_avg * current_vol - trade_price * trade_volume
        if new_vol > 1e-12:
            new_avg = new_cost / new_vol
        else:
            new_avg = 0.0
            new_vol = 0.0
        return new_avg, new_vol, new_cost

    def _get_max_averaging_count(self) -> int:
        val = self.config.get('max_averaging_count')
        if val is None:
            val = self.config.get('max_averaging_count_initial', 1)
        return int(val)

    def _save_max_averaging_count_to_bot_settings(self):
        val = self.config.get('max_averaging_count')
        if val is None:
            val = self.config.get('max_averaging_count_initial', 1)
        if val is None:
            val = 1
        self.config['max_averaging_count'] = int(val)
        update_bot_config(self.bot_id, self.config)

    def _get_current_position_id(self) -> str:
        rec = self.env['bot.settings'].search([('key', '=', 'current_position_id')], limit=1)
        if rec:
            pos_id = rec[0].value.strip("'\"")
        else:
            pos_id = str(uuid.uuid4())
            self.env['bot.settings'].create([{'key': 'current_position_id', 'value': pos_id}])
        return pos_id

    def _set_position_id(self, new_id: str):
        new_id = str(new_id).strip("'\"")
        rec = self.env['bot.settings'].search([('key', '=', 'current_position_id')], limit=1)
        if rec:
            rec[0].write({'value': new_id})
        else:
            self.env['bot.settings'].create([{'key': 'current_position_id', 'value': new_id}])
        self._position_id = new_id

    async def _ensure_clean_levels_table(self):
        Level = self.env['levels.signals']
        all_ids = Level.search([]).mapped('position_id')
        unique_ids = set(all_ids)
        current_id = self._position_id
        other_ids = [pid for pid in unique_ids if pid != current_id]
        if not other_ids:
            return
        History = self.env['deals.history']
        known = set(History.search([('position_id', 'in', other_ids)]).mapped('position_id'))
        unknown = [pid for pid in other_ids if pid not in known]
        if known:
            Level.search([('position_id', 'in', list(known))]).unlink()
        if len(unknown) == 1:
            recovered_id = unknown[0]
            self.logger.warning(f"Found lost active position {recovered_id}. Restoring as current.")
            self._set_position_id(recovered_id)
            Level.search([('position_id', '!=', recovered_id)]).unlink()
        elif len(unknown) > 1:
            self.logger.warning(f"Found multiple orphan positions: {unknown}. Moving to history and clearing.")
            records = Level.search([('position_id', 'in', unknown)])
            history_vals = []
            for rec in records:
                data = rec.read()
                data.pop('id', None)
                history_vals.append(data)
            History.create(history_vals)
            records.unlink()

    async def _get_initial_direction(self):
        if self._analyst_handle:
            pass
        if len(self._price_history) >= 2:
            prev_price = self._price_history[0][1]
            last_price = self._price_history[1][1]
            return "long" if last_price > prev_price else "short"
        return "long"

    async def _get_analyst_signal(self):
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

        await self._ensure_clean_levels_table()

        # check if entry exists
        entry_records = self.env['levels.signals'].search([('signal_number', '=', 0)], limit=1)
        has_entry = bool(entry_records)

        if has_entry:
            # restore max_averaging_count from bot_settings if present
            settings_rec = self.env['bot.settings'].search([('key', '=', 'max_averaging_count')], limit=1)
            if settings_rec:
                self.config['max_averaging_count'] = int(settings_rec[0].value.strip("'\""))
            else:
                if self.config.get('max_averaging_count') is None:
                    self.config['max_averaging_count'] = self.config.get('max_averaging_count_initial', 1) or 1
        else:
            initial = self.config.get('max_averaging_count_initial')
            if initial is None:
                initial = self.config.get('max_averaging_count', 1)
            self.config['max_averaging_count'] = initial if initial is not None else 1
            self.env['bot.settings'].search([('key', '=', 'max_averaging_count')], limit=1).unlink()
            self.env['bot.settings'].create([{'key': 'max_averaging_count', 'value': str(self.config['max_averaging_count'])}])

        # check if any trades exist
        trade_records = self.env['levels.signals'].search([
            '|',
            ('signal_flag', '=', True),
            ('real_timestamp', '!=', None)
        ])
        has_trades = bool(trade_records)

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
        self.logger.info("Grid bot started")

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
                await self._check_smart_executions()

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
            self.logger.info("on_config_updated: updating config")
            self.config = get_bot_config(self.bot_id)
            if not self.config.get('max_averaging_count'):
                self.config['max_averaging_count'] = self.config.get('max_averaging_count_initial', 1)
            update_bot_config(self.bot_id, self.config)
            await self._update_levels()
            self.logger.info("on_config_updated: levels successfully recalculated")
        except Exception as e:
            self.logger.error(f"on_config_updated: error updating levels: {e}", exc_info=True)

    def _close_db(self):
        if hasattr(self, 'env'):
            self.env.close()