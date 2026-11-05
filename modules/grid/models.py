# modules/grid/models.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import asyncio
import json
import uuid
from typing import Dict, Any, List

from core import auto_reg, BaseBot
from core.database import (
    Model, Char, Integer, Boolean, Float, Selection, Json,
    get_bot_config, update_bot_config, update_bot_status
)
from core.logger import perf_logger

logger = perf_logger.get_logger('grid_bot', 'analytics')

# How long grid waits for the execution bot to confirm a force-reset
# (panic close) before giving up and resetting locally anyway.
FORCE_RESET_TIMEOUT_NS = 30 * 1_000_000_000

# Signal constants. Stored in grid/models.py and propagated to the
# executor via the "constants" capability (the executor reads them upon
# startup and caches them in bot_settings['signal_constants']).
PANIC_SIGNAL_NUMBER = -999_999
SMART_REVERSE_BASE  = -100

# Retry limit for zero-fill feedback when signal_number >= 0.
MAX_SIGNAL_RETRIES = 5

# Limit on consecutive position resets without a successful fill entry.
MAX_ENTRY_RESET_STREAK = 5

# A fraction of `plan_vol` below which a remaining `close` value
# is considered a rounding artifact (due to `precision`) and is collapsed to zero.
# This is applied in `_apply_feedback_to_record` (initial cutoff, Fix 1)
# and in `_get_effective_avg_vol` (as a fallback for legacy data, Fix 5).
# It can be customized via `config['close_dust_fraction']`.
CLOSE_DUST_FRACTION_DEFAULT = 0.01   # 1%

_COLLECTOR_STATUS_CACHE: dict = {}
_COLLECTOR_STATUS_TTL_NS = 2_000_000_000


def _get_cached_collector_status(manager, collector_id: int,
                                  now_ns: int) -> str:
    cached = _COLLECTOR_STATUS_CACHE.get(collector_id)
    if cached and (now_ns - cached[0]) < _COLLECTOR_STATUS_TTL_NS:
        return cached[1]
    status = manager.get_bot_status(collector_id)
    _COLLECTOR_STATUS_CACHE[collector_id] = (now_ns, status)
    return status

# ----------------------------------------------------------------------
# Helper functions
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
    dt = (timestamps[-1] - timestamps[-2])/ 1_000_000_000
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
    current_timestamp = Integer() # exchange time of the reference candle
    tbot_utc_stamp_ns = Integer(index=True)   #TBOT UTC timestamp of the row entry
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
    signal_timestamp = Integer() # TBOT UTC of the signal's semantic moment (may be in the future)
    signal_retry = Integer(default=0)   # A.0: counter for repeated plan attempts
    signal_smart = Boolean(default=False)
    signal_smart_reversed = Integer(default=0)
    real_timestamp = Integer() # TBOT UTC timestamp of execution (from the execution bot)
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
    current_timestamp = Integer()
    tbot_utc_stamp_ns = Integer(index=True)
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
    signal_timestamp = Integer()
    signal_retry = Integer(default=0)   # A.0
    signal_smart = Boolean(default=False)
    signal_smart_reversed = Integer(default=0)
    real_timestamp = Integer()
    real_vol_rate = Float()
    real_price = Float()
    real_pnl = Float()
    real_position_volume = Float()
    real_flag = Boolean(default=False)
    real_avg_entry_price = Float()
    moved_to_history_at =Integer()
    created_at = Char(default="CURRENT_TIMESTAMP")

class PriceHistory(Model):
    _name = 'price.history'
    _table = 'price_history'

    timestamp = Integer(primary_key=True, required=True) # candle time (exchange)
    open = Float()
    high = Float()
    low = Float()
    close = Float()
    volume = Float()
    tbot_utc_stamp_ns = Integer(index=True)

# ----------------------------------------------------------------------
# GridBot class with all methods converted to ORM
# ----------------------------------------------------------------------
@auto_reg
class GridBot(BaseBot):
    _name = "grid.bot"
    _inherit = "base.bot"

    MAX_AVERAGING_HARD_CAP = 500

    def _truncate_max_count(self):
        cap = self.MAX_AVERAGING_HARD_CAP
        initial = self.config.get('max_averaging_count_initial')
        if initial is None:
            initial = self.config.get('max_averaging_count', 1)
        if initial is not None and int(initial) > cap:
            self.logger.warning(
                f"max_averaging_count_initial={initial} exceeds cap "
                f"{cap} — clamped")
            initial = cap
            self.config['max_averaging_count_initial'] = cap
        self.config['max_averaging_count'] = (
            initial if initial is not None else 1)
        smart = self.config.get('smart_averaging_count', 0)
        try:
            smart = int(smart or 0)
        except (TypeError, ValueError):
            smart = 0
        if smart > cap:
            self.logger.warning(
                f"smart_averaging_count={smart} exceeds cap {cap} — clamped")
            self.config['smart_averaging_count'] = cap
        elif smart < 0:
            self.config['smart_averaging_count'] = 0

    def __init__(self, bot_id: int, manager=None):
        super().__init__(bot_id, manager)  # init env
        self.config = get_bot_config(bot_id)
        self._db_path = self.db_path  # alias
        self.logger = perf_logger.get_logger(f"grid_{bot_id}", "analytics")
        # F0.7.9.D: select native provider (needs self.config + self.logger).
        self._resolve_exchange_provider()
        self._collector_handle = None
        self._analyst_handle = None

        self._truncate_max_count()

        self._position_id = self._get_current_position_id()
        self.config['current_position_id'] = self._position_id
        self._price_history = []
        self._last_price = None
        self._last_ts = None
        self._need_new_position = False
        self._force_reset_pending = False
        self._force_reset_deadline_ns = 0
        # A streak of consecutive position resets without a successful
		# fill entry. If the limit is exceeded, the grid is paused.
        self._reset_no_fill_streak = 0
        # C2: explicit initialization — a guard against re-invocation
		# of _close_position while it is already executing.
        self._close_in_progress = False
        # F-grid.6: {position_id: (ts_ns, vol)}, TTL=1s.
        self._vol_cache = {}

        # ensure max_averaging_count present
        if self.config.get('max_averaging_count') is None:
            self._save_max_averaging_count_to_bot_settings()

        # Incremental price-history sync state. 
		# - _initial_price_load_done: False — the next call will perform a full
		#   reconcile (fetch with limit + upsert + prune), then set to True. 
		# - _last_processed_ts: anchor — the most recent timestamp already
		#   processed and written to the database. Initialized from the database
		#   upon the first call.
        self._initial_price_load_done = False
        self._last_processed_ts = 0

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
            "timeout": {
                "keywords": ["execution_timeout"],
                "getter": self._get_timeout,
                "setter": None,
            },
            "execution_reserve_sec": {
                "keywords": ["execution_reserve", "signal_lookahead"],
                "getter": self._get_execution_reserve_sec,
                "setter": None,
            },
            "request_close_position": {
                "keywords": ["request_close_position", "grid_close", "reset_position"],
                "getter": None,
                "setter": self._set_request_close_position,
            },
            "constants": {
                "keywords": ["signal_constants", "panic_constants"],
                "getter": self._get_constants,
                "setter": None,
            },
        }

    # ------------------------------------------------------------------
    # Data access methods (converted)
    # ------------------------------------------------------------------
    @staticmethod
    async def _get_constants():
        """Propagation of panic/SMART constants + thresholds to the executor."""
        return {
            "panic_signal_number": PANIC_SIGNAL_NUMBER,
            "smart_reverse_base": SMART_REVERSE_BASE,
			# NEW: 'market-close' trigger for the 'reduce-only' remainder. 
			# Semantically distinct from 'close_dust_fraction':
			#   dust    (1–2%)  → treated as 0; triggers a grid reset
			#   market  (15%)   → closed via a market order by the executor
			#   > market        → 'force-complete' feedback + absorption
			#                     (processed during the next 'signal 0')
            "close_market_fraction": 0.15,
        }

    async def _get_pending_signals(self):
        records = self.env['levels.signals'].search([
            ('signal_flag', '=', True),
            ('real_flag', '=', False)
        ])
        result = records.read()
        for row in result:
            # Declare the scales according to which the grid generates the signal
            row['signal_timestamp_scale'] = 'tbot_utc_ns'
            row['current_timestamp_scale'] = 'exchange_ns'
        return result

    async def _set_execution_result(self, data: Dict[str, Any]):
        # Perform the null check before the scale check — otherwise, in a feedback scenario,
		# a false error regarding the scale might be logged when signal_number is missing.
        signal_number = data.get("signal_number")
        signal_ts = data.get("signal_timestamp")
        if signal_number is None or signal_ts is None:
            return

        # Strict scale contract. Executor always sends tbot_utc_ns.
        scale = data.get("real_timestamp_scale", "tbot_utc_ns")
        if scale != 'tbot_utc_ns':
            self.logger.error(
                f"Feedback pos={data.get('position_id')} sig={signal_number}: "
                f"unexpected real_timestamp_scale={scale!r} — dropped")
            return
        real_timestamp = self.time_scale_normalizer['tbot_utc_ns'](
            data.get("real_timestamp", self.get_time_ns()))

        # Position_id filter.
        fb_position_id = data.get("position_id")
        if fb_position_id and fb_position_id != self._position_id:
            self.logger.warning(
                f"Feedback pos_id={fb_position_id} != current "
                f"{self._position_id} for sig={signal_number} — ignored")
            return
        if not fb_position_id:
            self.logger.warning(
                f"Feedback for sig={signal_number} without position_id — "
                f"using self._position_id")
            fb_position_id = self._position_id

        # Panic close
        if signal_number == PANIC_SIGNAL_NUMBER:
            if self._force_reset_pending:
                self.logger.warning(
                    "force_reset_position: execution confirmed panic close — "
                    "resetting locally")
                self._force_reset_pending = False
                await self._close_position()
                self._need_new_position = True
            else:
                self.logger.info(
                    "force_reset_position: late feedback for panic ignored")
            return

        exchange_state = data.get('exchange_state')
        signal_smart_fb = bool(data.get('signal_smart', False))

        # Strict search. Patch: removed ('real_flag','=',False) — this allows
		# repeated feedback for a partially closed row (multiple attempts
		# by _maybe_market_close_remainder in the executor send feedback
		# for the same plan-row -2).
        record = self.env['levels.signals'].search([
            ('position_id', '=', fb_position_id),
            ('signal_number', '=', signal_number),
            ('signal_timestamp', '=', signal_ts),
        ], order='id DESC', limit=1)

        # Repeated feedback for an already processed ("real") row. 
		#   * Entry/averaging (signal_number >= 0) — ignored (idempotent). 
		#   * Close/liq — processed as long as the remaining balance > dust. 
		#   * 0-fill retry (e.g., a market-close attempt was rejected) —
		#     do NOT modify the state; otherwise, the `any_close-guard` in
		#     `_check_grid_levels` will break (causing the grid to spam new -2 signals).
        if record and record[0].real_flag:
            if signal_number >= 0:
                self.logger.debug(
                    f"Feedback pos={fb_position_id} sig={signal_number}: "
                    f"entry/averaging already real — ignored")
                return
            prev_vol = float(record[0].real_position_volume or 0.0)
            plan_vol = float(record[0].signal_vol_rate or 0.0)
            eps = float(self.config.get(
                'close_dust_fraction', CLOSE_DUST_FRACTION_DEFAULT) or 0.0)
            if prev_vol <= max(1e-9, plan_vol * eps):
                self.logger.debug(
                    f"Feedback pos={fb_position_id} sig={signal_number}: "
                    f"already flat (prev_vol={prev_vol:.6g}) — ignored")
                return
            try:
                fb_vol_peek = float(data.get("real_volume", 0.0))
            except (TypeError, ValueError):
                fb_vol_peek = 0.0
            if fb_vol_peek == 0:
                self.logger.info(
                    f"Feedback pos={fb_position_id} sig={signal_number}: "
                    f"retry with zero fill "
                    f"(state={exchange_state!r}) — "
                    f"keeping partial-close state")
                return

        # Loose-search adopts executor's ts.
        if not record:
            loose = self.env['levels.signals'].search([
                ('position_id', '=', fb_position_id),
                ('signal_number', '=', signal_number),
                ('real_flag', '=', False),
            ], limit=1)
            if loose:
                loose[0].write({
                    'signal_timestamp': signal_ts,
                    'signal_flag': True,
                })
                self.logger.warning(
                    f"loose-search adopted executor ts for "
                    f"pos={fb_position_id} sig={signal_number}")
                record = loose

        # Fallback — the string is already in deals_history.
        if not record:
            hist = self.env['deals.history'].search([
                ('position_id', '=', fb_position_id),
                ('signal_number', '=', signal_number),
                ('signal_timestamp', '=', signal_ts),
            ], limit=1)
            if hist and not hist[0].real_flag:
                await self._apply_feedback_to_record(hist[0], data)
                return
            self.logger.debug(
                f"Feedback pos={fb_position_id} sig={signal_number} "
                f"ts={signal_ts}: no plan row (already executed or removed)")
            return

        rec = record[0]
        try:
            feedback_volume = float(data.get("real_volume", 0.0))
        except (TypeError, ValueError):
            feedback_volume = 0.0

		# Nnegative signal, zero fill - branch on exchange_state. 
		# None and unknown → keep position (safe default).
        if signal_number < 0 and feedback_volume == 0:
            rec_smart = bool(rec.signal_smart or signal_smart_fb)
            if rec_smart:
                rec.write({
                    'real_flag': True,
                    'real_vol_rate': 0.0,
                    'real_timestamp': real_timestamp,
                    'tbot_utc_stamp_ns': self.get_time_ns(),
                })
                self._maybe_reset_lost_smart_parent(rec)
                self.logger.info(
                    f"Smart reverse sig={signal_number} canceled "
                    f"(state={exchange_state}) — no position reset")
                return
            if exchange_state in ('canceled', 'rejected', 'expired',
                                  'not_found', 'not_sent'):
                rec.write({
                    'signal_flag': False,
                    'signal_retry': (rec.signal_retry or 0) + 1,
                    'tbot_utc_stamp_ns': self.get_time_ns(),
                })
                self.logger.info(
                    f"Close sig={signal_number} canceled "
                    f"(state={exchange_state}) — position kept")
                return
            if exchange_state == 'filled':
                # If `rec` is from `deals_history` (C.3 fallback), the position
				# is already closed; do not reset the current one.			
                if rec.position_id == self._position_id:
                    remaining = self.env['levels.signals'].search([
                        ('position_id', '=', self._position_id),
                    ], limit=1)
                    if remaining:
                        self.logger.warning(
                            f"Signal #{signal_number}: zero fill with "
                            f"state=filled — resetting position")
                        await self._close_position()
                        self._need_new_position = True
                return
            # D12: None / unknown — safe default: keep position.
            self.logger.warning(
                f"Signal #{signal_number}: zero fill with unknown state="
                f"{exchange_state!r} — keeping position")
            rec.write({
                'signal_flag': False,
                'signal_retry': (rec.signal_retry or 0) + 1,
            })
            return

        # F.2 / D5: non-close signal, zero fill — retry limit.
        if signal_number >= 0 and feedback_volume == 0:
            reject_reason = data.get('reject_reason') or ''
            # Terminal rejects — the signal cannot be retried. Reset and,
            # for averaging on a live position, close/panic-close to
            # avoid an orphaned exchange position.
            if reject_reason in ('stale_signal', 'superseded',
                                 'recovery_no_ref'):
                rec.write({
                    'signal_flag': False,
                    'signal_retry': MAX_SIGNAL_RETRIES,
                    'tbot_utc_stamp_ns': self.get_time_ns(),
                })
                self.logger.warning(
                    f"Signal #{signal_number}: terminal reject "
                    f"({reject_reason}) — not retrying")
                if signal_number == 0:
                    await self._close_position()
                    self._need_new_position = True
                else:
                    last_exec = self._get_last_executed()
                    if last_exec is not None:
                        timeout_sec = self.config.get(
                            'execution_timeout_sec', 0)
                        if timeout_sec > 0:
                            self.logger.error(
                                f"Signal #{signal_number}: stale averaging "
                                f"on live position (two-bot) — panic close")
                            await self.force_reset_position()
                        else:
                            self.logger.warning(
                                f"Signal #{signal_number}: stale averaging "
                                f"on live position (single-bot) — closing "
                                f"locally")
                            await self._close_position()
                            self._need_new_position = True
                return

            # Retryable zero-fill — increment, give up at MAX.
            retry = (rec.signal_retry or 0) + 1
            if retry >= MAX_SIGNAL_RETRIES:
                rec.write({
                    'signal_flag': False,
                    'signal_retry': retry,
                    'tbot_utc_stamp_ns': self.get_time_ns(),
                })
                self.logger.error(
                    f"Signal #{signal_number}: retry limit ({retry}) "
                    f"reached with zero fill — giving up")
                if signal_number == 0:
                    await self._close_position()
                    self._need_new_position = True
                return
            rec.write({
                'signal_retry': retry,
                'tbot_utc_stamp_ns': self.get_time_ns(),
            })
            self.logger.warning(
                f"Signal #{signal_number}: zero fill, retry={retry} "
                f"(limit {MAX_SIGNAL_RETRIES})")
            return

        # Normal path — non-zero fill.
        await self._apply_feedback_to_record(rec, data)

    async def _apply_feedback_to_record(self, rec, data: Dict[str, Any]):
        """Apply fill to a plan or history line."""
        signal_number = rec.signal_number
        real_price = data.get("real_price", 0.0)
        try:
            feedback_volume = float(data.get("real_volume", 0.0))
        except (TypeError, ValueError):
            feedback_volume = 0.0
        real_timestamp = self.time_scale_normalizer['tbot_utc_ns'](
            data.get("real_timestamp", self.get_time_ns()))

        fb_position_id = data.get("position_id") or self._position_id
        direction = rec.signal_direction

		# Container by PID. The PK for `deals.history` is `history_id`,
		# not `id`; sorting must be done using the correct field, otherwise
		# a `sqlite3.OperationalError: no such column: id` will occur.
        is_active = rec.position_id == self._position_id
        container = (self.env['levels.signals'] if is_active
                     else self.env['deals.history'])
        order_field = 'id' if is_active else 'history_id'

        prev_records = container.search([
            ('position_id', '=', fb_position_id),
            ('real_flag', '=', True),
        ], order=f'{order_field} DESC', limit=1)
        if prev_records:
            old_vol = float(prev_records[0].real_position_volume or 0.0)
            old_avg = float(prev_records[0].real_avg_entry_price or 0.0)
        else:
            # History-fallback — the `real_*` properties of the string itself are the source of truth.
            old_vol = float(rec.real_position_volume or 0.0)
            old_avg = float(rec.real_avg_entry_price or 0.0)

        entry_rec = container.search([
            ('position_id', '=', fb_position_id),
            ('signal_number', '=', 0),
        ], limit=1)
        if entry_rec:
            sign = 1 if entry_rec[0].signal_direction == 'buy' else -1
        elif (rec.signal_number or 0) < 0:
            # The close/liq/panic row has direction='sell' for
			# long positions and 'buy' for short positions. We reconstruct the sign
			# based on the inverted direction of the row.
            sign = 1 if rec.signal_direction == 'sell' else -1
        else:
            # Entry/averaging in history without entry_rec —
			# direction matches the open direction (is not inverted).
            sign = 1 if rec.signal_direction == 'buy' else -1

        new_avg, new_vol, _ = self._apply_trade_to_position(
            old_vol, old_avg, direction, real_price, feedback_volume, sign)

        # Precision-dust - close remainder is less than ε × plan_vol 
		# considered an artifact of precision rounding and collapses 
		# to zero. Only for non-smart closes (signal_number < 0).
        if signal_number < 0 < new_vol and not rec.signal_smart:
            plan_vol = float(rec.signal_vol_rate or 0.0)
            eps_ratio = float(self.config.get(
                'close_dust_fraction',
                CLOSE_DUST_FRACTION_DEFAULT) or 0.0)
            dust_threshold = max(1e-9, plan_vol * eps_ratio)
            if new_vol <= dust_threshold:
                self.logger.info(
                    f"Close sig={signal_number} dust: remaining="
                    f"{new_vol:.6g} <= {dust_threshold:.6g} "
                    f"({eps_ratio * 100:.2f}% of plan={plan_vol:.6g}) — "
                    f"treating as fully closed")
                new_vol = 0.0
                new_avg = 0.0

        lev = self.config['leverage']

        if signal_number < 0:
            total_pnl = (sign * (real_price - old_avg) / old_avg * lev * 100
                         if old_avg > 0 else 0.0)
        else:
            total_pnl = (sign * (real_price - new_avg) / new_avg * lev * 100
                         if new_avg > 1e-12 else 0.0)

        rec.write({
            'real_timestamp': real_timestamp,
            'real_vol_rate': feedback_volume,
            'real_price': real_price,
            'real_pnl': total_pnl,
            'real_position_volume': new_vol,
            'real_flag': True,
            'real_avg_entry_price': new_avg if new_vol > 1e-12 else 0.0,
            'tbot_utc_stamp_ns': self.get_time_ns(),
        })
        self._vol_cache.pop(self._position_id, None)

        #Successful fill entry/averaging — streak reset.
        if signal_number >= 0 and feedback_volume > 0:
            self._reset_no_fill_streak = 0

        # Successful close/liquidation with a positive fill — finalize. 
		# Guard: apply _close_position only if the record originates from an active
		# position. A history entry (C.3 fallback) must not close a new active position.
        if signal_number < 0 and not rec.signal_smart:
            if new_vol <= 1e-9:
                if rec.position_id == self._position_id:
                    remaining = self.env['levels.signals'].search([
                        ('position_id', '=', self._position_id),
                    ], limit=1)
                    if remaining:
                        self.logger.info(
                            f"Close sig={signal_number} completed — "
                            f"resetting")
                        await self._close_position()
                        self._need_new_position = True
                else:
                    self.logger.info(
                        f"Close sig={signal_number} from history — "
                        f"no position reset (pos mismatch)")
                return
            rec.write({'signal_flag': True})
            self.logger.info(
                f"Partial close sig={signal_number}, remaining="
                f"{new_vol} — waiting for remainder")
            return

        # Recalculation applies only to the active position. 
		# A history row must not trigger a recalculation of the levels for a new cycle.
        if signal_number >= 0 and rec.position_id == self._position_id:
            await self._apply_execution_and_recalc(signal_number)

    def _maybe_reset_lost_smart_parent(self, smart_rec):
        """Reset signal_smart_reversed from parent if none 
		child-smart failed with positive fill."""
        parent_number = smart_rec.signal_smart_reversed
        if not parent_number or parent_number < 0:
            return
        level = self.env['levels.signals']
        parent = level.search([
            ('position_id', '=', smart_rec.position_id),
            ('signal_number', '=', parent_number),
            ('signal_smart', '=', False),
        ], limit=1)
        if not parent:
            return
        parent = parent[0]
        if parent.signal_smart_reversed != -1:
            return
        children = level.search([
            ('position_id', '=', smart_rec.position_id),
            ('signal_smart', '=', True),
            ('signal_smart_reversed', '=', parent_number),
        ])
        if not children or all(
            c.real_flag and (c.real_vol_rate or 0) == 0 for c in children
        ):
            parent.write({'signal_smart_reversed': 0})
            self.logger.info(
                f"Lost smart parent #{parent_number} reset")

    async def _get_position_id(self):
        return self._position_id

    async def _get_signals_by_position(self, position_id: str):
        active = self.env['levels.signals'].search([('position_id', '=', position_id)]).read()
        historical = self.env['deals.history'].search([('position_id', '=', position_id)]).read()
        return active + historical

    async def _get_timeout(self):
        return self.config.get('execution_timeout_sec', 0)

    async def _get_execution_reserve_sec(self):
        return self.config.get('execution_reserve_sec', 0) or 0

    async def _set_request_close_position(self, data):
        """External request (from execution bot) to close current position.

        Called when execution detects a stale entry (signal_number=0) and
        blocks the position. In that state, execution will ignore any -2
        signal we send (position is blocked on its side), so sending one
        would create a deadlock that requires manual user action.

        Instead, reset locally: move the current plan to history, generate
        a new position_id. Grid will then emit a fresh entry, execution
        will pick it up normally, and the cycle restarts.

        Note: if the position was actually opened on the exchange in some
        previous session but execution has lost its state, this reset will
        not close it. That rare case requires manual intervention or a
        future reconciliation loop.
        """
        reason = (data or {}).get('reason', 'external')
        self.logger.warning(
            f"External close_position requested (reason={reason}) — "
            f"hard reset of pos={self._position_id}"
        )
        await self.force_reset_position()

    # ------------------------------------------------------------------
    # Price history update (converted)
    # ------------------------------------------------------------------
    async def _update_price_history(self, limit=2000):
        """Incremental price-history sync.

        First call after start/restart fetches a full window and reconciles
        with the DB (fills gaps, refreshes the currently forming candle).
        Subsequent calls fetch only the tail (5 candles) — the collector is
        the source of truth, so re-reading a handful of candles covers the
        forming one plus any just-closed ones. Closed candles are written
        once and never touched again.
        """
        if not self._collector_handle:
            return
        try:
            price_hist = self.env['price.history']
            now_ns = self.get_time_ns()

            # Anchor _last_processed_ts to the newest DB timestamp on the
            # first call after start/restart. Fresh DB → stays 0, whole
            # fetched window is inserted. Restart → only newer candles pass
            # the filter below (gap catch-up limited to `limit` rows).
            if not self._initial_price_load_done:
                last = price_hist.search([], order='timestamp DESC', limit=1)
                self._last_processed_ts = last[0].timestamp if last else 0

            fetch_limit = limit if not self._initial_price_load_done else 5
            candles = await self._collector_handle.get(
                "candles", limit=fetch_limit)
            if not candles:
                return

            # >= (not >): the newest candle may be the currently forming one
            # whose timestamp is unchanged until it closes — its OHLCV must
            # be refreshed on every tick.
            new_candles = [
                c for c in candles if c['timestamp'] >= self._last_processed_ts
            ]

            if new_candles:
                # Batch existence-check in chunks — SQLite default
                # SQLITE_MAX_VARIABLE_NUMBER is 999 on many builds; 500 is
                # safe.
                ts_list = [c['timestamp'] for c in new_candles]
                existing = {}
                chunk_size = 500
                for i in range(0, len(ts_list), chunk_size):
                    chunk = ts_list[i:i + chunk_size]
                    for r in price_hist.search([('timestamp', 'in', chunk)]):
                        existing[r.timestamp] = r

                to_create = []
                for c in new_candles:
                    ts = c['timestamp']
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
                    # Single batch INSERT on the initial pass, 0–1 on the
                    # tail pass.
                    price_hist.create(to_create)

                self._last_processed_ts = max(
                    c['timestamp'] for c in new_candles)
                self._initial_price_load_done = True

                # Prune to `limit` rows, only when 10% over. Delete the
                # excess oldest rows directly — no need to fetch the
                # newest `limit` just to compute a threshold.
                count = price_hist.search_count([])
                if count > limit * 1.1:
                    excess = count - limit
                    to_delete = price_hist.search(
                        [], order='timestamp ASC', limit=excess)
                    if to_delete:
                        to_delete.unlink()

            # In-memory tail for extrapolation. Assumes the collector returns
            # candles in DESC order (`order='timestamp DESC'` in
            # CollectorBot._get_ohlcv_data) — candles[0] is the newest.
            self._last_price = candles[0]['close']
            self._last_ts = candles[0]['timestamp']
            if len(candles) >= 2:
                self._price_history = [
                    (candles[1]['timestamp'], candles[1]['close']),
                    (candles[0]['timestamp'], candles[0]['close']),
                ]
            else:
                self._price_history = [
                    (candles[0]['timestamp'], candles[0]['close'])
                ]
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
        level = self.env['levels.signals']
        # remove all existing levels for this position
        level.search([('position_id', '=', self._position_id)]).unlink()

        # Entry (signal_number=0)
        #
        # In two-bot mode (execution_timeout_sec > 0) the entry is
        # created as PLAN ONLY — real_* fields are left empty and will be
        # filled when the execution bot sends feedback with the actual
        # fill. In single-bot mode (execution_timeout_sec == 0) there is
        # no execution bot, so grid emulates the fill immediately.
        entry_vol = 1.0
        entry_cost = entry_price * entry_vol
        timeout_sec = cfg.get('execution_timeout_sec', 0)
        emulate_entry = (timeout_sec == 0)

        # `signal_timestamp` is defined as TBOT UTC — the semantic moment
        # of the signal. `current_timestamp` keeps exchange candle time.
        # The two must not share the same source: candle open time can be
        # up to one full timeframe in the past, which makes the entry
        # look already-stale to the execution bot on its first poll.
        entry_vals = {
            'position_id': self._position_id,
            'signal_number': 0,
            'current_price': entry_price,
            'current_timestamp': entry_ts,  # exchange candle time
            'tbot_utc_stamp_ns': self.get_time_ns(),
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
            'signal_timestamp': self.get_time_ns(),  # TBOT UTC now
        }
        if emulate_entry:
            entry_vals.update({
                'real_timestamp': entry_ts,
                'real_vol_rate': entry_vol,
                'real_price': entry_price,
                'real_pnl': 0.0,
                'real_position_volume': entry_vol,
                'real_flag': True,
                'real_avg_entry_price': entry_price,
            })
        all_vals = [entry_vals]
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

            all_vals.append({
                'position_id': self._position_id,
                'signal_number': n,
                'current_price': entry_price,
                'current_timestamp': entry_ts,
                'tbot_utc_stamp_ns': self.get_time_ns(),
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
            })
            prev_avg_price = new_avg_price
            prev_position_vol = new_position_vol
            prev_position_cost = new_position_cost

        # Liquidation
        liq_pnl = cfg.get('liquidation_pnl', 90)
        liq_price = entry_price * (1 - sign * (liq_pnl / 100.0) / lev)
        liq_order_type = cfg.get('liquidation_order_type', 'market')
        all_vals.append({
            'tbot_utc_stamp_ns': self.get_time_ns(),
            'position_id': self._position_id,
            'signal_number': -1,
            'signal_timestamp': self.get_time_ns(),  # C.1: exact-search ключ
            'signal_direction': 'sell' if direction == 'long' else 'buy',
            'signal_price': liq_price,
            'signal_liquidation': True,
            'signal_order_type': liq_order_type,
            'signal_flag': False,
            'real_flag': False,
        })

        # Close reference
        close_pnl = cfg.get('close_pnl', 50.0)
        close_price = entry_price * (1 + sign * (close_pnl / 100.0) / lev)
        all_vals.append({
            'tbot_utc_stamp_ns': self.get_time_ns(),
            'position_id': self._position_id,
            'signal_number': -2,
            'signal_timestamp': self.get_time_ns(),  # C.1
            'signal_direction': 'sell' if direction == 'long' else 'buy',
            'signal_price': close_price,
            'signal_close': True,
            'signal_flag': False,
            'real_flag': False,
        })
        level.create(all_vals)
        self.logger.info(f"Rebuilt levels for {self._position_id}, direction {direction}")

    async def _update_levels(self):
        self.logger.debug(f"_update_levels: last_price={self._last_price}, position_id={self._position_id}")
        if self._last_price is None:
            self.logger.warning("_update_levels: no current price, skipping")
            return

        conn = self.env['levels.signals']  # manager
        entry_rec = conn.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '=', 0)
        ], limit=1)
        if not entry_rec:
            self.logger.warning(f"_update_levels: no entry record in position {self._position_id}")
            return

        sign = 1 if entry_rec[0].signal_direction == 'buy' else -1

        last_exec = self._get_last_executed()
        if not last_exec:
            self.logger.debug(f"_update_levels: no last executed in position {self._position_id}")
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

        cfg = self.config
        max_count = cfg.get('max_averaging_count', 1)
        threshold_pct = cfg['averaging_threshold_pnl']
        lev = cfg['leverage']
        strategy = cfg['averaging_strategy']
        volumes = get_volume_sequence(strategy, max_count, entry_vol=1.0)

        prev_avg = effective_avg
        prev_vol = effective_vol
        prev_cost = effective_cost
        direction_str = 'buy' if sign == 1 else 'sell'
        all_vals = []

        with self.env.transaction():
            # N-2: purge any unexecuted levels beyond hard cap
            # (plan v8 §PR-GRID-4)
            conn.search([
                ('position_id', '=', self._position_id),
                ('signal_number', '>', self.MAX_AVERAGING_HARD_CAP),
                ('real_flag', '=', False),
                ('signal_smart', '=', False),
            ]).unlink()

            # remove non-executed levels > last_number
            conn.search([
                ('position_id', '=', self._position_id),
                ('signal_number', '>', last_number),
                ('real_flag', '=', False),
                ('signal_smart', '=', False)
            ]).unlink()

            for n in range(last_number + 1, max_count + 1):
                vol = (volumes[n - 1] if n - 1 < len(volumes)
                       else volumes[-1])
                level_price = prev_avg * (
                        1 - sign * threshold_pct / 100.0 / lev
                )
                new_pos_vol = prev_vol + vol
                new_pos_cost = prev_cost + level_price * vol
                new_avg = (
                    0.0 if new_pos_vol == 0
                    else new_pos_cost / new_pos_vol
                )
                pnl = (
                    0.0 if new_avg == 0
                    else sign * (level_price / new_avg - 1) * lev * 100
                )

                all_vals.append({
                    'position_id': self._position_id,
                    'signal_number': n,
                    'current_price': self._last_price or None,
                    'current_timestamp': self._last_ts,
                    'tbot_utc_stamp_ns': self.get_time_ns(),
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
                })

                prev_avg = new_avg
                prev_vol = new_pos_vol
                prev_cost = new_pos_cost

            if all_vals:
                conn.create(all_vals)

        # update liquidation and close
        liq_price = effective_avg * (
                1 - sign * cfg['liquidation_pnl'] / 100.0 / lev
        )
        close_price = effective_avg * (
                1 + sign * cfg['close_pnl'] / 100.0 / lev
        )

        liq_recs = conn.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '=', -1)
        ])
        if liq_recs:
            liq_recs[0].write({'signal_price': liq_price})

        close_recs = conn.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '=', -2)
        ])
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
        cfg = self.config
        lev = cfg['leverage']
        reserve_sec = cfg['execution_reserve_sec']
        timeout_sec = cfg['execution_timeout_sec']
        recalc_strategy = cfg['recalc_strategy']

        # A unified scale — TBOT UTC — is used for all comparisons. 
		# `candle_ts` is not needed here for `_create_signal`, but
		# it is retained for `current_timestamp` in the event of an
		# entry-timeout-rebuild (C4) — `_rebuild_levels` expects exchange time.
        tbot_now = self.get_time_ns()
        current_ts = tbot_now
        candle_ts = (self._price_history[-1][0]
                     if self._price_history else tbot_now)

        level = self.env['levels.signals']

        # entry check
        entry_rec = level.search([('position_id', '=', self._position_id), ('signal_number', '=', 0)], limit=1)
        if not entry_rec:
            self._need_new_position = True
            return
        sign = 1 if entry_rec[0].signal_direction == 'buy' else -1

        # last executed
        last_exec = self._get_last_executed()
        if not last_exec:
            # Two-bot mode: entry is planned but not yet confirmed by the
            # execution bot. Do not average, close, or liquidate until
            # feedback arrives. Do not trigger new-position either —
            # unless the entry has outlived its timeout, in which case
            # reset the position and rebuild from current price.
            entry_check = level.search([
                ('position_id', '=', self._position_id),
                ('signal_number', '=', 0)
            ], limit=1)
            if entry_check and entry_check[0].signal_flag \
                    and not entry_check[0].real_flag:
                if timeout_sec > 0:
                    entry_ts = entry_check[0].signal_timestamp or 0
                    age_ns = current_ts - entry_ts
                    if age_ns > timeout_sec * 1_000_000_000:
                        self.logger.error(
                            f"Entry #0 timed out (age {age_ns / 1e9:.1f}s > "
                            f"{timeout_sec}s) — resetting position and "
                            f"rebuilding levels from current price"
                        )
                        # Unified helper (streak + pause). 
						# Removed redundant return — the helper
						# either performed a rebuild or parked the grid.
                        await self._handle_entry_timeout_rebuild(
                            direction='long' if sign == 1 else 'short',
                            price=current_price,
                            signal_ts=tbot_now,
                            candle_ts=candle_ts)
                        return
                return  # still within timeout — keep waiting
            self._need_new_position = True
            return

        effective_avg, effective_vol = self._get_effective_avg_vol(last_exec)
        if effective_avg == 0 or effective_vol == 0:
            self.logger.warning("Effective avg or vol is zero, no active position.")
            self._need_new_position = True
            return

        pnl = sign * (current_price / effective_avg - 1) * lev * 100

        # Liquidation
        existing_liq = level.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '=', -1),
            ('signal_flag', '=', True),
            ('real_flag', '=', False)
        ], limit=1)
        if not existing_liq and pnl <= -cfg['liquidation_pnl']:
            total_vol = effective_vol
            vol_rates_row = level.search([
                ('position_id', '=', self._position_id),
                ('real_flag', '=', True)
            ], order='signal_number DESC', limit=1)
            base = (vol_rates_row[0].signal_volume_rates
                    if vol_rates_row and vol_rates_row[0].signal_volume_rates
                    else [1.0])
            # Create a copy — protection against mutation of the ORM cache for a JSON field.
            vol_rates_list = list(base)
            vol_rates_list.append(total_vol)
            created = self._create_signal(
                signal_number=-1,
                direction='sell' if sign == 1 else 'buy',
                price=current_price,
                timestamp=tbot_now,
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
            if not created:
                self.logger.debug(
                    "_check_grid_levels: liquidation -1 refused "
                    "(already real) — see _create_signal")
                await self._on_signal_refused(signal_number=-1)
                return
            if timeout_sec == 0:
                await self._close_position()
                self._need_new_position = True
            else:
                self.logger.info("Liquidation signal generated, waiting for execution")
            return

        # Guard — if -2 has already been executed (real_flag=True), do not
		# initiate the retry loop. In this case, the reset is handled by Fix 1
		# (via _close_position, called from _apply_feedback_to_record). A
		# protective return is needed here to avoid spamming the ERROR log
		# with "Close already executed" and to prevent re-triggering the reset.
        any_close = level.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '=', -2),
            ('real_flag', '=', True),
        ], order='id DESC', limit=1)
        if any_close:
            return

        # Close
        existing_close = level.search([
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
                vol_rates_row = level.search([
                    ('position_id', '=', self._position_id),
                    ('real_flag', '=', True)
                ], order='signal_number DESC', limit=1)
                base = (vol_rates_row[0].signal_volume_rates
                        if vol_rates_row
                        and vol_rates_row[0].signal_volume_rates
                        else [1.0])
                # Create a copy — protection against mutation of the ORM cache for a JSON field.
                vol_rates_list = list(base)
                vol_rates_list.append(total_vol)
                close_direction = 'sell' if sign == 1 else 'buy'
                created = self._create_signal(
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
                if not created:
                    self.logger.debug(
                        "_check_grid_levels: close -2 refused "
                        "(already real) — see _create_signal")
                    await self._on_signal_refused(signal_number=-2)
                    return
                if timeout_sec == 0:
                    await self._close_position()
                    self._need_new_position = True
                else:
                    self.logger.info("Close signal generated, waiting for execution")
                return

        # Averaging levels
        levels = level.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '>=', 1),
            ('real_flag', '=', False),
            ('signal_flag', '=', False),
            ('signal_retry', '<', MAX_SIGNAL_RETRIES),   #Do not activate abandon
        ], order='signal_number ASC')

        for lvl in levels:
            level_price = lvl.signal_price
            # immediate crossing
            if (sign == 1 and current_price <= level_price) or \
               (sign == -1 and current_price >= level_price):
                created = self._create_signal(
                    signal_number=lvl.signal_number,
                    direction=lvl.signal_direction,
                    price=level_price,
                    timestamp=current_ts,
                    vol_rate=lvl.signal_vol_rate,
                    position_volume=lvl.signal_position_volume,
                    emulate_execution=(timeout_sec == 0),
                    current_price=current_price,
                    signal_volume_rates=lvl.signal_volume_rates,  # already list
                    avg_entry_price=lvl.avg_entry_price,
                    position_cost=lvl.position_cost,
                    signal_pnl=lvl.signal_pnl,
                    order_type=lvl.signal_order_type if lvl.signal_order_type else 'limit'
                )
                if not created:
                    self.logger.debug(
                        f"_check_grid_levels: averaging "
                        f"#{lvl.signal_number} refused (already real)")
                    await self._on_signal_refused(signal_number=lvl.signal_number)
                    return
                if timeout_sec == 0:
                    await self._apply_execution_and_recalc(lvl.signal_number)
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
                        signal_ts = tbot_now + time_to_cross * 1_000_000_000  # ← TBOT UTC in the future
                        created = self._create_signal(
                            signal_number=lvl.signal_number,
                            direction=lvl.signal_direction,
                            price=level_price,
                            timestamp=signal_ts,
                            vol_rate=lvl.signal_vol_rate,
                            position_volume=lvl.signal_position_volume,
                            emulate_execution=(timeout_sec == 0)
                        )
                        if not created:
                            self.logger.debug(
                                f"_check_grid_levels: extrapolated "
                                f"#{lvl.signal_number} refused (already real)")
                            return
                        if timeout_sec == 0:
                            await self._apply_execution_and_recalc(lvl.signal_number)
                        return

        # Timeout of pending signals
        if timeout_sec > 0:
            pending = level.search([
                ('position_id', '=', self._position_id),
                ('signal_flag', '=', True),
                ('real_flag', '=', False),
                ('signal_timestamp', '!=', None)
            ])
            timeout_ns = timeout_sec * 1_000_000_000
            for sig in pending:
                # Retry limit — do not recreate indefinitely.
                if (sig.signal_retry or 0) >= MAX_SIGNAL_RETRIES:
                    continue
                if (current_ts - sig.signal_timestamp) > timeout_ns:
                    if sig.signal_number < 0:
                        # Close / liquidation: refresh the signal price
                        # at current market so it gets a fresh attempt.
                        if recalc_strategy == 'recalc_grid':
                            created = self._create_signal(
                                signal_number=sig.signal_number,
                                direction=sig.signal_direction,
                                price=current_price,
                                timestamp=current_ts,
                                vol_rate=sig.signal_vol_rate,
                                position_volume=0.0,
                                emulate_execution=(timeout_sec == 0),
                                current_price=current_price,
                                signal_volume_rates=sig.signal_volume_rates,
                                avg_entry_price=sig.avg_entry_price,
                                position_cost=sig.position_cost,
                                signal_pnl=sig.signal_pnl,
                                order_type=sig.signal_order_type if sig.signal_order_type else 'limit'
                            )
                            if created:
                                self.logger.info(
                                    f"Reset timeout for signal "
                                    f"#{sig.signal_number} (recalc_grid)")
                            else:
                                self.logger.warning(
                                    f"Reset timeout refused for signal "
                                    f"#{sig.signal_number} — unexpected "
                                    f"(pending negative returned False)")
                            break
                        elif recalc_strategy == 'market_order':
                            # Signal_timestamp - TBOT UTC (tbot_utc_ns scale), 
							# and not current_ts (which may be an exchange 
							# candle). Without updating, execution will return noop.
                            sig.write({
                                'signal_order_type': 'market',
                                'signal_timestamp': tbot_now,
                            })
                            self.logger.info(
                                f"Changed signal #{sig.signal_number} to "
                                f"market order (ts refreshed)")
                        break

                    elif sig.signal_number == 0:
                        # ENTRY TIMED OUT — no confirmed fill from execution.
                        # There is no real position to average from, so the
                        # whole plan is invalid. Reset: move current plan to
                        # history, generate a new position_id, and rebuild
                        # levels from current price on the next iteration.
                        #
                        # A new position_id is required (not just a new
                        # timestamp on the same entry) because the execution
                        # bot deduplicates by (source_bot_id, position_id,
                        # signal_number) — reusing the same id would make it
                        # ignore the rebuilt entry as already-seen.
                        if recalc_strategy == 'recalc_grid':
                            self.logger.error(
                                "Entry signal #0 timed out — resetting "
                                "position and rebuilding levels from "
                                "current price"
                            )
                            # Unified helper (streak + pause).
                            if not await self._handle_entry_timeout_rebuild(
                                    direction='long' if sign == 1
                                    else 'short',
                                    price=current_price,
                                    signal_ts=tbot_now,
                                    candle_ts=candle_ts):
                                return
                            return
                        elif recalc_strategy == 'market_order':
                            # TBOT UTC semantic timestamp—not an exchange-provided candle.
                            sig.write({
                                'signal_order_type': 'market',
                                'signal_timestamp': tbot_now,
                            })
                            self.logger.info(
                                "Entry signal #0 timed out — switching to "
                                "market order (ts refreshed)"
                            )
                        break

                    else:
                        # Averaging level timed out. Recalc from that level
                        # is meaningful only when we already have a real
                        # position; otherwise _recalc_levels_from() will
                        # return early (no last_exec).
                        if recalc_strategy == 'recalc_grid':
                            await self._recalc_levels_from(sig.signal_number)
                            self.logger.info(
                                f"Recalc levels from #{sig.signal_number} "
                                f"due to timeout"
                            )
                        elif recalc_strategy == 'market_order':
                            # TBOT UTC semantic moment.
                            sig.write({
                                'signal_order_type': 'market',
                                'signal_timestamp': tbot_now,
                            })
                            self.logger.info(
                                f"Changed signal #{sig.signal_number} to "
                                f"market order (ts refreshed)"
                            )
                        break

    # ------------------------------------------------------------------
    # Create signal (converted)
    # ------------------------------------------------------------------
    def _create_signal(self, signal_number, direction, price, timestamp,
                       vol_rate, position_volume, emulate_execution=False,
                       current_price=None, signal_volume_rates=None,
                       avg_entry_price=None, position_cost=None,
                       signal_pnl=None, order_type=None):
        """Create or update a plan signal.

		Returns True if the signal is updated; False if the update
		is rejected (e.g., it has already been executed, a 'close' already exists, etc.).
		"""
        level = self.env['levels.signals']

        if price is None:
            self.logger.error(
                f"_create_signal: price is None for sig={signal_number} "
                f"pos={self._position_id} — refusing to create plan row "
                f"(executor would see signal_price=None and produce noop)")
            return False

        record = level.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '=', signal_number)
        ], limit=1)

        # D8: retry-limit — do not recreate a signal that has already exhausted
		# its retry attempts. Otherwise, averaging-#3 with retry=MAX would be reactivated.
        if record and not record[0].real_flag:
            if (record[0].signal_retry or 0) >= MAX_SIGNAL_RETRIES:
                self.logger.warning(
                    f"_create_signal: refusing sig={signal_number} — "
                    f"retry limit {MAX_SIGNAL_RETRIES} reached")
                if signal_number == 0:
                    self._need_new_position = True
                return False

        if record and record[0].real_flag:
            if signal_number < 0:
                # The close/liquidation/panic has already played out — it’s an anomaly.
                if signal_number == PANIC_SIGNAL_NUMBER:
                    kind = "Panic"
                elif signal_number == -2:
                    kind = "Close"
                elif signal_number == -1:
                    kind = "Liquidation"
                else:
                    kind = "Unexpected-negative"
                self.logger.error(
                    f"{kind} already executed for pos={self._position_id} "
                    f"(sig={signal_number}, "
                    f"real_vol={record[0].real_vol_rate or 0.0}, "
                    f"plan_vol={vol_rate}). Refusing to recreate. "
                    f"Forcing position reset.")
                self._need_new_position = True
                return False
            else:
                # Entry/averaging has already been executed — do not touch the 'plan' fields.
                self.logger.warning(
                    f"_create_signal: entry/averaging sig={signal_number} "
                    f"already real — plan update ignored")
                return False

        is_new = not record
        if is_new:
            created = level.create([{
                'position_id': self._position_id,
                'signal_number': signal_number,
                'signal_direction': direction,
                'signal_price': price,
                'signal_flag': True,
                'signal_timestamp': timestamp,
                'tbot_utc_stamp_ns': self.get_time_ns(),
                'signal_liquidation': signal_number == -1,
                'signal_close': signal_number == -2,
            }])
            if not created:
                # create returned an empty Recordset — defensive fallback
                fallback = level.search([
                    ('position_id', '=', self._position_id),
                    ('signal_number', '=', signal_number),
                ], order='id DESC', limit=1)
                if not fallback:
                    self.logger.error(
                        f"_create_signal: create returned empty AND "
                        f"fallback empty for sig={signal_number} "
                        f"pos={self._position_id} — aborting")
                    return False
                self.logger.warning(
                    f"_create_signal: create returned empty, "
                    f"fallback search found id={fallback[0].id}")
                rec = fallback[0]
            else:
                rec = created[0]
        else:
            rec = record[0]

        updates = {
            'signal_flag': True,
            'tbot_utc_stamp_ns': self.get_time_ns(),
            'signal_price': price,
            'signal_liquidation': signal_number == -1,
            'signal_close': signal_number == -2,
            'current_timestamp': self._last_ts,   # exchange time
        }
        # pending — do not overwrite signal_timestamp.
        if is_new:
            updates['signal_timestamp'] = timestamp
        elif rec.signal_flag and not rec.real_flag:
            updates['signal_retry'] = (rec.signal_retry or 0) + 1
        else:
            updates['signal_timestamp'] = timestamp
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
        if position_volume is not None:
            updates['signal_position_volume'] = position_volume
        rec.write(updates)

        if emulate_execution:
            self._emulate_execution_for_signal(
                rec, signal_number, direction, price, vol_rate)

        return True

    def _emulate_execution_for_signal(self, rec, signal_number, direction,
                                      price, vol_rate):
        """Emulation of a 'fill' in single-bot mode (execution_timeout_sec == 0).

		Extracted from _create_signal to avoid duplicating logic and
		to keep _create_signal concise.

		Fix 1 (dust-threshold) is NOT applied: in single-bot mode, the amount
		does not undergo exchange-side rounding to the precision step.
		Since vol_rate is an exact value derived from the grid plan,
		a 'phantom residual' is physically impossible. The threshold is
		applied only within _apply_feedback_to_record (the two-bot workflow).
		"""
        level = self.env['levels.signals']
        entry_rec = level.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '=', 0)
        ], limit=1)
        sign = 1 if (entry_rec and entry_rec[0].signal_direction == 'buy') else -1

        prev = level.search([
            ('position_id', '=', self._position_id),
            ('real_flag', '=', True)
        ], order='id DESC', limit=1)
        old_vol = (prev[0].real_position_volume
                   if prev and prev[0].real_position_volume is not None
                   else 0.0)
        old_avg = (prev[0].real_avg_entry_price
                   if prev and prev[0].real_avg_entry_price is not None
                   else 0.0)

        new_avg, new_vol, _ = self._apply_trade_to_position(
            old_vol, old_avg, direction, price, vol_rate, sign)
        lev = self.config['leverage']
        # F-grid.3.1 (mirror in emulate path):
        if signal_number < 0:
            total_pnl = (sign * (price - old_avg) / old_avg * lev * 100
                         if old_avg > 0 else 0.0)
        else:
            total_pnl = (sign * (price - new_avg) / new_avg * lev * 100
                         if new_avg > 1e-12 else 0.0)

        rec.write({
            'real_timestamp': self.get_time_ns(),
            'real_vol_rate': vol_rate,
            'real_price': price,
            'real_position_volume': new_vol,
            'real_avg_entry_price': new_avg if new_vol > 1e-12 else 0.0,
            'real_flag': True,
            'real_pnl': total_pnl,
        })
        self._vol_cache.pop(self._position_id, None)

    async def _apply_execution_and_recalc(self, signal_number: int):
        level = self.env['levels.signals']
        rec = level.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '=', signal_number)
        ], limit=1)
        if not rec:
            return
        rec = rec[0]
        avg_after, pos_vol_after = self._get_effective_avg_vol(rec)
        pos_cost_after = pos_vol_after * avg_after

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
        all_vals = []

        with self.env.transaction():
            # N-2: purge any unexecuted plan rows beyond hard cap
            # (plan v8 §PR-GRID-4).
            level.search([
                ('position_id', '=', self._position_id),
                ('signal_number', '>', self.MAX_AVERAGING_HARD_CAP),
                ('real_flag', '=', False),
                ('signal_smart', '=', False),
            ]).unlink()

            # unlink + batch create — atomic (N-1b)
            level.search([
                ('position_id', '=', self._position_id),
                ('signal_number', '>', signal_number),
                ('real_flag', '=', False),
                ('signal_smart', '=', False)
            ]).unlink()

            for n in range(next_number, max_count + 1):
                vol = volumes[n - 1] if n - 1 < len(volumes) else volumes[-1]
                level_price = prev_avg * (1 - sign * threshold_pct / 100.0 / lev)
                new_pos_vol = prev_vol + vol
                new_pos_cost = prev_cost + level_price * vol
                new_avg = new_pos_cost / new_pos_vol
                pnl = sign * (level_price / new_avg - 1) * lev * 100

                all_vals.append({
                    'position_id': self._position_id,
                    'signal_number': n,
                    'current_price': self._last_price or rec.current_price,
                    'current_timestamp': self._last_ts,
                    'tbot_utc_stamp_ns': self.get_time_ns(),
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
                })

                prev_avg = new_avg
                prev_vol = new_pos_vol
                prev_cost = new_pos_cost

            if all_vals:
                level.create(all_vals)

        liq_pnl = cfg.get('liquidation_pnl', 90)
        liq_price = avg_after * (1 - sign * liq_pnl / 100.0 / lev)
        liq_rec = level.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '=', -1)
        ])
        if liq_rec:
            liq_rec[0].write({'signal_price': liq_price})

        close_pnl = cfg.get('close_pnl', 50.0)
        close_price = avg_after * (1 + sign * close_pnl / 100.0 / lev)
        close_rec = level.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '=', -2)
        ])
        if close_rec:
            close_rec[0].write({'signal_price': close_price})

        self.logger.info(
            f"Applied execution for level #{signal_number} "
            f"and recalculated subsequent levels"
        )

    async def _recalc_levels_from(self, start_number: int):
        if start_number < 0:
            return

        level = self.env['levels.signals']
        entry_rec = level.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '=', 0)
        ], limit=1)
        sign = 1 if (entry_rec and entry_rec[0].signal_direction == 'buy') else -1

        last_exec = self._get_last_executed()
        if not last_exec:
            return

        effective_avg, effective_vol = self._get_effective_avg_vol(last_exec)
        effective_cost = effective_vol * effective_avg

        cfg = self.config
        max_count = cfg.get('max_averaging_count', 1)
        threshold_pct = cfg['averaging_threshold_pnl']
        lev = cfg['leverage']
        strategy = cfg['averaging_strategy']
        volumes = get_volume_sequence(strategy, max_count, entry_vol=1.0)

        prev_avg = effective_avg
        prev_vol = effective_vol
        prev_cost = effective_cost
        all_vals = []

        with self.env.transaction():
            # unlink + batch create — atomic (N-1c)
            level.search([
                ('position_id', '=', self._position_id),
                ('signal_number', '>=', start_number),
                ('real_flag', '=', False),
                ('signal_smart', '=', False)
            ]).unlink()

            for n in range(start_number, max_count + 1):
                vol = volumes[n - 1] if n - 1 < len(volumes) else volumes[-1]
                level_price = prev_avg * (1 - sign * threshold_pct / 100.0 / lev)
                new_pos_vol = prev_vol + vol
                new_pos_cost = prev_cost + level_price * vol
                new_avg = new_pos_cost / new_pos_vol
                pnl = sign * (level_price / new_avg - 1) * lev * 100

                all_vals.append({
                    'position_id': self._position_id,
                    'signal_number': n,
                    'current_price': self._last_price or None,
                    'current_timestamp': self._last_ts,
                    'tbot_utc_stamp_ns': self.get_time_ns(),
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
                })

                prev_avg = new_avg
                prev_vol = new_pos_vol
                prev_cost = new_pos_cost

            if all_vals:
                level.create(all_vals)

        liq_price = effective_avg * (
                1 - sign * cfg['liquidation_pnl'] / 100.0 / lev
        )
        close_price = effective_avg * (
                1 + sign * cfg['close_pnl'] / 100.0 / lev
        )

        liq_rec = level.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '=', -1)
        ])
        if liq_rec:
            liq_rec[0].write({'signal_price': liq_price})

        close_rec = level.search([
            ('position_id', '=', self._position_id),
            ('signal_number', '=', -2)
        ])
        if close_rec:
            close_rec[0].write({'signal_price': close_price})

        self.logger.info(
            f"Levels recalculated from #{start_number} due to timeout"
        )

    # ------------------------------------------------------------------
    # Smart averaging (converted)
    # ------------------------------------------------------------------
    async def _check_smart_averaging(self):
        # Reset "lost" parents — BEFORE the `smart_count` check. 
		# Otherwise, if `smart_averaging_count == 0` (e.g., due to a
		# configuration change on the fly), any stuck `signal_smart_reversed = -1`
		# values ​​would never be reset.
        await self._reset_lost_smart_parents()

        cfg = self.config
        smart_count = cfg.get('smart_averaging_count', 0)
        if smart_count == 0:
            return
        max_count = self._get_max_averaging_count()
        smart_count = min(smart_count, max_count)

        level = self.env['levels.signals']
        all_rows = level.search([('position_id', '=', self._position_id)])
        smart_min_number = max_count - smart_count + 1
        eligible = all_rows.filtered(
            lambda r: r.signal_number >= smart_min_number
                      and r.real_flag
                      and r.signal_flag
                      and not r.signal_smart
                      and not r.signal_smart_reversed
        ).sorted(key=lambda r: r.signal_number, reverse=True)
        levels = eligible[:smart_count]
        current_price = self._price_history[-1][1] if self._price_history else 0
        threshold_pct = cfg['averaging_threshold_pnl']
        leverage = cfg['leverage']
        timeout_sec = cfg['execution_timeout_sec']

        # Calculated based on the minimum of the existing negative values, excluding 'panic'. 
		# This ensures that after a parent is reset, a new 'smart' instance will not
		# take an already existing number.
        neg_numbers = [
            r.signal_number for r in all_rows
            if r.signal_number < 0
            and r.signal_number != PANIC_SIGNAL_NUMBER
        ]
        min_neg = min(neg_numbers) if neg_numbers else -2
        new_signal_number = min(min_neg - 1, SMART_REVERSE_BASE - 1)

        for lvl in levels:
            direction = lvl.signal_direction
            real_price = lvl.real_price
            if real_price is None:
                continue
            if direction == 'buy':
                trigger_price = real_price * (1 + threshold_pct / 2 / 100 / leverage)
                if current_price >= trigger_price:
                    self._create_smart_reverse(
                        lvl, new_signal_number=new_signal_number,
                        emulate_execution=(timeout_sec == 0))
                    new_signal_number -= 1
            else:
                trigger_price = real_price * (1 - threshold_pct / 2 / 100 / leverage)
                if current_price <= trigger_price:
                    self._create_smart_reverse(
                        lvl, new_signal_number=new_signal_number,
                        emulate_execution=(timeout_sec == 0))
                    new_signal_number -= 1

    async def _reset_lost_smart_parents(self):
        """G.1: Reset parents with signal_smart_reversed=-1 for all
		children that have not yet executed.
		"""
        level = self.env['levels.signals']
        lost = level.search([
            ('position_id', '=', self._position_id),
            ('signal_smart_reversed', '=', -1),
            ('signal_smart', '=', False),
        ])
        for parent in lost:
            children = level.search([
                ('position_id', '=', self._position_id),
                ('signal_smart', '=', True),
                ('signal_smart_reversed', '=', parent.signal_number),
            ])
            if not children or all(
                c.real_flag and (c.real_vol_rate or 0) == 0
                for c in children
            ):
                parent.write({'signal_smart_reversed': 0})

    async def _check_smart_executions(self):
        level = self.env['levels.signals']
        executed_smarts = level.search([
            ('position_id', '=', self._position_id),
            ('signal_smart', '=', True),
            ('real_flag', '=', True),
            ('signal_smart_reversed', '>', 0),
            ('real_vol_rate', '>', 0),   # exclude canceled smarts
        ])
        if not executed_smarts:
            return
        for smart in executed_smarts:
            parent_number = smart.signal_smart_reversed
            parent = level.search([
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
        # collision with panic — do not generate a signal.
        if new_signal_number == PANIC_SIGNAL_NUMBER:
            self.logger.error(
                f"_create_smart_reverse: refused — signal_number "
                f"collides with PANIC_SIGNAL_NUMBER={PANIC_SIGNAL_NUMBER}")
            return
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

        level = self.env['levels.signals']
        vals = {
            'position_id': self._position_id,
            'signal_number': new_signal_number,
            'current_price': self._last_price or parent_real_price,
            'current_timestamp': self._last_ts,
            'tbot_utc_stamp_ns': self.get_time_ns(),
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
            'signal_timestamp': self.get_time_ns(),
            'signal_smart': True,
            'signal_smart_reversed': parent_number,
            'real_flag': False,
        }
        if emulate_execution:
            entry_rec = level.search([('position_id', '=', self._position_id), ('signal_number', '=', 0)], limit=1)
            sign = 1 if (entry_rec and entry_rec[0].signal_direction == 'buy') else -1
            prev = level.search([
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
                'real_timestamp': self.get_time_ns(),
                'real_vol_rate': parent_vol,
                'real_price': reverse_price,
                'real_pnl': total_pnl,
                'real_position_volume': new_vol,
                'real_avg_entry_price': new_avg if new_vol > 1e-12 else 0.0,
            })
        # mark parent as waiting for smart execution
        parent_rec = level.search([
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
        # Idempotency — a repeated call during
		# execution must not generate a second position_id. 
		# The entire logic is wrapped in a try/finally block with correct indentation.
        if getattr(self, '_close_in_progress', False):
            self.logger.debug("_close_position: already in progress — skip")
            return
        self._close_in_progress = True
        try:
            level = self.env['levels.signals']
            history = self.env['deals.history']
            records = level.search([('position_id', '=', self._position_id)])
            if not records:
                return

            new_pos_id = None
            try:
                with self.env.transaction():
                    history_vals = []
                    skipped = []
                    _cached_weighted_avg = None

                    for rec in records:
                        if (rec.signal_number >= 0 and rec.real_flag
                                and rec.real_avg_entry_price):
                            plan_avg = rec.avg_entry_price
                            real_avg = rec.real_avg_entry_price
                            is_anomaly = False
                            if plan_avg and plan_avg > 0:
                                deviation = abs(real_avg - plan_avg) / plan_avg
                                if deviation > 0.5:
                                    is_anomaly = True
                                    self.logger.error(
                                        f"Sanity: pos={rec.position_id} "
                                        f"sig={rec.signal_number} "
                                        f"real_avg={real_avg} deviates from "
                                        f"plan_avg={plan_avg} by "
                                        f"{deviation * 100:.1f}%")
                            else:
                                cur = self._last_price or 0
                                if cur > 0 and (real_avg > cur * 10
                                                or real_avg < cur * 0.1):
                                    is_anomaly = True
                                    self.logger.error(
                                        f"Sanity: pos={rec.position_id} "
                                        f"sig={rec.signal_number} "
                                        f"real_avg={real_avg} far from "
                                        f"current={cur} (>10×)")
                            if is_anomaly:
                                if _cached_weighted_avg is None:
                                    _cached_weighted_avg = \
                                        self._recompute_weighted_avg(records)
                                weighted_avg = _cached_weighted_avg
                                cur = self._last_price or 0
                                if (weighted_avg > 0
                                        and (cur <= 0
                                             or (cur * 0.1 <= weighted_avg
                                                 <= cur * 10))):
                                    rec.write({
                                        'real_avg_entry_price': weighted_avg,
                                        'tbot_utc_stamp_ns': self.get_time_ns(),
                                    })
                                    self.logger.info(
                                        f"Sanity: recomputed avg={weighted_avg} "
                                        f"for pos={rec.position_id} "
                                        f"sig={rec.signal_number}")
                                else:
                                    self.logger.error(
                                        f"Sanity: cannot recover avg for "
                                        f"pos={rec.position_id} "
                                        f"sig={rec.signal_number} "
                                        f"(weighted={weighted_avg}, cur={cur}).")
                                    skipped.append(rec)
                                    continue

                        data = rec.read()
                        data.pop('id', None)
                        data.pop('created_at', None)
                        data['moved_to_history_at'] = self.get_time_ns()
                        data['tbot_utc_stamp_ns'] = self.get_time_ns()
                        history_vals.append(data)

                    if history_vals:
                        history.create(history_vals)

                    skipped_ids = {r.id for r in skipped}
                    transferable_ids = [
                        r.id for r in records if r.id not in skipped_ids
                    ]
                    if transferable_ids:
                        level.browse(transferable_ids).unlink()

                    if skipped:
                        self.logger.error(
                            f"Sanity: {len(skipped)} rows kept in "
                            f"levels.signals for position {self._position_id} "
                            f"— manual review required")
                        try:
                            settings = self.env['bot.settings']
                            rec = settings.search(
                                [('key', '=', 'skipped_position_ids')], limit=1)
                            cur = []
                            if rec:
                                try:
                                    cur = json.loads(rec[0].value or '[]')
                                except Exception:
                                    cur = []
                            if self._position_id not in cur:
                                cur.append(self._position_id)
                            new_val = json.dumps(cur)
                            if rec:
                                rec[0].write({'value': new_val})
                            else:
                                settings.create([{
                                    'key': 'skipped_position_ids',
                                    'value': new_val,
                                }])
                        except Exception as e:
                            self.logger.warning(
                                f"Failed to persist skipped_position_ids: {e}")

                    new_pos_id = str(uuid.uuid4())
                # end transaction

                self._set_position_id(new_pos_id)
                self._vol_cache = {}
                self.logger.info(
                    f"Position closed, new position_id {new_pos_id}")

                initial = self.config.get('max_averaging_count_initial')
                if initial is None:
                    initial = self.config.get('max_averaging_count', 1)
                self.config['max_averaging_count'] = (
                    initial if initial is not None else 1)
                update_bot_config(self.bot_id, self.config)
            except Exception:
                self.logger.error(
                    f"_close_position: failed during reset pos="
                    f"{self._position_id} — see traceback", exc_info=True)
                raise
        finally:
            self._close_in_progress = False

    @staticmethod
    def _recompute_weighted_avg(records):
        """Weighted avg by executed entry/averaging records."""
        total_cost = 0.0
        total_vol = 0.0
        for r in records:
            # Signal_number can be None.
            if ((r.signal_number or 0) >= 0 and r.real_flag
                    and r.real_vol_rate and r.real_price):
                total_cost += float(r.real_vol_rate) * float(r.real_price)
                total_vol += float(r.real_vol_rate)
        return total_cost / total_vol if total_vol > 1e-12 else 0.0

    async def request_close_position(self):
        """Close current position.

        If no real execution has been recorded — the grid moves the plan
        rows to history locally and creates a new position_id, without
        sending anything to the execution bot. Otherwise a close signal
        (-2) is created and the execution bot handles it.
        """
        self.logger.info(
            f"request_close_position: pos={self._position_id}, "
            f"last_price={self._last_price}, "
            f"ph_len={len(self._price_history) if self._price_history else 0}"
        )

        # --- local fallbacks: nothing to send to execution ---
        if not self._price_history or self._last_price is None:
            self.logger.warning(
                "request_close_position: no price data — resetting plan locally"
            )
            await self._close_position()
            self._need_new_position = True
            return

        level = self.env['levels.signals']
        last_exec = self._get_last_executed()
        if not last_exec:
            self.logger.warning(
                "request_close_position: no real executions — "
                "resetting plan locally (no signal sent to execution)"
            )
            await self._close_position()
            self._need_new_position = True
            return

        current_price = self._price_history[-1][1]
        current_ts = self._price_history[-1][0]
        timeout_sec = self.config.get('execution_timeout_sec', 0)

        effective_avg, effective_vol = self._get_effective_avg_vol(last_exec)
        if effective_avg == 0 or effective_vol == 0:
            self.logger.warning(
                "request_close_position: zero avg/vol — resetting plan locally"
            )
            await self._close_position()
            self._need_new_position = True
            return

        total_vol = effective_vol

        vol_rates_row = level.search([
            ('position_id', '=', self._position_id),
            ('real_flag', '=', True)
        ], order='signal_number DESC', limit=1)
        vol_rates_list = vol_rates_row[0].signal_volume_rates if vol_rates_row and vol_rates_row[
            0].signal_volume_rates else [1.0]
        vol_rates_list.append(total_vol)

        direction = last_exec.signal_direction
        sign = 1 if direction == 'buy' else -1
        pnl_close = sign * (current_price / effective_avg - 1) * self.config['leverage'] * 100

        created = self._create_signal(
            signal_number=-2,
            direction='sell' if sign == 1 else 'buy',
            price=current_price,
            timestamp=current_ts,
            vol_rate=total_vol,
            position_volume=0.0,
            emulate_execution=(timeout_sec == 0),
            current_price=current_price,
            signal_volume_rates=vol_rates_list,
            avg_entry_price=effective_avg,
            position_cost=effective_avg * effective_vol,
            signal_pnl=pnl_close,
            order_type='limit'
        )

        if not created:
            self.logger.warning(
                "request_close_position: _create_signal(-2) refused — "
                "close already executed, position will reset on next tick")
            return

        if timeout_sec == 0:
            await self._close_position()
            self._need_new_position = True
            self.logger.info("Manual close executed immediately")
        else:
            self.logger.info("Manual close signal created, waiting for execution")

    async def force_reset_position(self):
        """Panic close via execution + local reset.

        Sends a special signal (PANIC_SIGNAL_NUMBER, default -999999) to the
        execution bot asking it to cancel every open order
        on the exchange for this symbol and
        market-close any non-zero position (reduce-only). Grid then
        waits up to FORCE_RESET_TIMEOUT_NS for feedback:

          * feedback received  → execution confirmed; grid resets
          * timeout reached    → grid resets anyway (data moved to
                                 history as-is)

        If execution is not running, the exchange position (if any)
        will NOT be closed by this call. Only a manual check on the
        exchange can recover that — the user is warned.
        """
        # The strictest guard — _close_position() is invoked. 
		# Even if _force_reset_pending == False (having been reset prior to
		# _close_position within the timeout branch of _run), a new panic signal
		# arriving in the middle of the critical section would create a race condition.
        """
        Single-bot mode (execution_timeout_sec == 0): no executor exists
        to receive a panic signal — reset locally and immediately.

        Two-bot mode: send panic to executor, wait for feedback
        or timeout.
        """
        if self._close_in_progress:
            self.logger.info(
                "force_reset_position: close already in progress — skipped")
            return
        if self._force_reset_pending:
            self.logger.warning(
                "force_reset_position: already pending, ignoring duplicate")
            return

        timeout_sec = self.config.get('execution_timeout_sec', 0)

        # --- Single-bot: no executor to close anything, reset immediately
        if timeout_sec == 0:
            self.logger.warning(
                "force_reset_position: single-bot mode — "
                "immediate local reset")
            await self._close_position()
            self._need_new_position = True
            return

        # --- Two-bot: send panic, wait for feedback
        current_price = (self._price_history[-1][1]
                         if self._price_history else 0.0)
        current_ts = self.get_time_ns()

        created = self._create_signal(
            signal_number=PANIC_SIGNAL_NUMBER,
            direction='sell',
            price=current_price,
            timestamp=current_ts,
            vol_rate=0.0,
            position_volume=0.0,
            emulate_execution=False,
            current_price=current_price,
            order_type='market',
        )

        if not created:
            self.logger.error(
                "force_reset_position: _create_signal refused — "
                "position already reset locally or reset pending")
            return

        self._force_reset_pending = True
        self._force_reset_deadline_ns = current_ts + FORCE_RESET_TIMEOUT_NS
        self.logger.warning(
            f"force_reset_position: sent panic-close signal "
            f"({PANIC_SIGNAL_NUMBER}) for pos={self._position_id}, "
            f"waiting up to {FORCE_RESET_TIMEOUT_NS / 1e9:.0f}s "
            f"for execution feedback")

    # ------------------------------------------------------------------
    # Helper methods (some converted)
    # ------------------------------------------------------------------
    def _get_effective_avg_vol(self, row):
        """Effective avg and vol for the current position.
		avg — derived from the provided string (this represents the entry/averaging price — the cost basis).
		vol — calculated dynamically: the sum of the 'real_vol_rate' for all increases minus the sum of the 
		'real_vol_rate' for all decreases.
		A cache (with a TTL of 1s) keyed by 'position_id' is used to prevent redundant ORM searches when 
		called from '_check_grid_levels' or '_update_levels' within the same processing cycle.
		"""
        avg = (row.real_avg_entry_price
               if row.real_avg_entry_price is not None
               else row.avg_entry_price) or 0.0

        cache_key = self._position_id
        now = self.get_time_ns()
        cached = self._vol_cache.get(cache_key)
        if cached and (now - cached[0]) < 1_000_000_000:  # 1s TTL
            return avg, cached[1]

        rows = self.env['levels.signals'].search([
            ('position_id', '=', self._position_id),
            ('real_flag', '=', True),
        ])
        total_in = 0.0
        total_out = 0.0
        for r in rows:
            v = float(r.real_vol_rate or 0)
            # Signal_number can be None (legacy) —
			# we use `or 0` as a safeguard.
            if (r.signal_number or 0) >= 0:
                total_in += v
            else:
                total_out += v
        vol = max(0.0, total_in - total_out)

        # Safety net — if a "dust" remainder has already been recorded in the database
		# (due to legacy entries, manual edits, or a missed Fix 1), we collapse
		# it to a value below ε × total_in. This is aligned with Fix 1 via a
		# single configuration key, `close_dust_fraction`.
        if total_in > 0:
            eps_ratio = float(self.config.get(
                'close_dust_fraction',
                CLOSE_DUST_FRACTION_DEFAULT) or 0.0)
            if vol < total_in * eps_ratio:
                if vol > 0:
                    self.logger.info(
                        f"Residual {vol:.6f} < {eps_ratio * 100:.2f}% of "
                        f"entry {total_in:.6f} — treating as flat")
                vol = 0.0

        self._vol_cache[cache_key] = (now, vol)
        return avg, vol

    def _get_last_executed(self):
        """The most recently executed position *increase*.

		Explicitly exclude close/liq (signal_number < 0); otherwise,
		a partial close might be returned as the "most recent execution."
		"""
        records = self.env['levels.signals'].search([
            ('position_id', '=', self._position_id),
            ('signal_number', '>=', 0),
            ('real_flag', '=', True),
            ('real_position_volume', '>', 0),
        ], order='id DESC', limit=1)
        return records[0] if records else None

    @staticmethod
    def _apply_trade_to_position(current_vol, current_avg,
                                 trade_direction, trade_price,
                                 trade_volume, sign):
        """Update position after a fill.

        increase: weighted average entry price.
        decrease (close): entry avg is unchanged — this is cost basis of
        the remaining part. Realized PnL is computed separately in
        _set_execution_result (see F-grid.3.1).
        """
        increase = ((sign == 1 and trade_direction == 'buy') or
                    (sign == -1 and trade_direction == 'sell'))
        if increase:
            new_vol = current_vol + trade_volume
            new_cost = current_avg * current_vol + trade_price * trade_volume
            new_avg = new_cost / new_vol if new_vol > 1e-12 else 0.0
        else:
            new_vol = max(0.0, current_vol - trade_volume)
            new_avg = current_avg if new_vol > 1e-12 else 0.0
            new_cost = new_avg * new_vol
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
        val = int(val)
        self.config['max_averaging_count'] = val
        #Initial — an immutable snapshot of a user-provided value.
        if self.config.get('max_averaging_count_initial') is None:
            self.config['max_averaging_count_initial'] = val
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
        rec = self.env['bot.settings'].search(
            [('key', '=', 'current_position_id')], limit=1)
        if rec:
            rec[0].write({'value': new_id})
        else:
            self.env['bot.settings'].create(
                [{'key': 'current_position_id', 'value': new_id}])
        self._position_id = new_id
        self.config['current_position_id'] = new_id

    async def _ensure_clean_levels_table(self):
        level = self.env['levels.signals']
        history = self.env['deals.history']
        rows = level.execute(
            "SELECT DISTINCT position_id FROM levels_signals "
            "WHERE position_id IS NOT NULL")
        unique_ids = {r['position_id'] for r in rows}
        current_id = self._position_id
        other_ids = [pid for pid in unique_ids if pid != current_id]
        if not other_ids:
            return

        now_ns = self.get_time_ns()
        chunk_size = 500

        def _move_to_history(h_records):
            if not h_records:
                return
            vals = []
            for rec in h_records:
                key = (rec.position_id, rec.signal_number,
                       rec.signal_timestamp)
                if key in existing_keys:
                    continue
                existing_keys.add(key)
                data = rec.read()
                data.pop('id', None)
                data.pop('created_at', None)
                data['moved_to_history_at'] = now_ns
                data['tbot_utc_stamp_ns'] = now_ns
                vals.append(data)
            if vals:
                history.create(vals)

        # chunked IN — known set
        known = set()
        for i in range(0, len(other_ids), chunk_size):
            chunk_ids = other_ids[i:i + chunk_size]
            known.update(history.search([
                ('position_id', 'in', chunk_ids)
            ]).mapped('position_id'))

        # pre-load existing (pos, sig, ts) for O(1) dedup
        existing_keys = set()
        for i in range(0, len(other_ids), chunk_size):
            chunk_ids = other_ids[i:i + chunk_size]
            for r in history.search([('position_id', 'in', chunk_ids)]):
                existing_keys.add(
                    (r.position_id, r.signal_number, r.signal_timestamp))

        unknown = [pid for pid in other_ids if pid not in known]

        if known:
            known_list = list(known)
            for i in range(0, len(known_list), chunk_size):
                chunk_ids = known_list[i:i + chunk_size]
                known_recs = level.search([('position_id', 'in', chunk_ids)])
                _move_to_history(known_recs)
                known_recs.unlink()

        if len(unknown) == 1:
            orphan_recs = level.search([('position_id', 'in', unknown)])
            self.logger.warning(
                f"Found orphan position {unknown[0]} — moving to history")
            _move_to_history(orphan_recs)
            orphan_recs.unlink()
            new_pos_id = str(uuid.uuid4())
            self._vol_cache = {}
            self._set_position_id(new_pos_id)
        elif len(unknown) > 1:
            self.logger.warning(
                f"Found multiple orphan positions: {unknown} — "
                f"moving to history")
            for i in range(0, len(unknown), chunk_size):
                chunk_ids = unknown[i:i + chunk_size]
                records = level.search([('position_id', 'in', chunk_ids)])
                _move_to_history(records)
                records.unlink()

    async def _get_initial_direction(self):
        if self._analyst_handle:
            pass
        if len(self._price_history) >= 2:
            prev_price = self._price_history[0][1]
            last_price = self._price_history[1][1]
            return "long" if last_price > prev_price else "short"
        return "long"

    @staticmethod
    async def _get_analyst_signal():
        return None

    async def _on_signal_refused(self, signal_number: int):
        """_create_signal failed for 'negative' — this means
		a close/liq operation has already been executed, and the position
		on the exchange is closed.

		P15: For 'negative', there are two scenarios:
		1) The close/liq operation has already been successfully executed
		(is 'real') — the position is closed on the exchange. A reset is appropriate.
		2) The retry limit has been reached — the position might still be
		active on the exchange, following 5 unsuccessful attempts to close/liquidate.
		A local reset is a measure of last resort and requires manual review.
		For signal_number >= 0 — this is the standard case (averaging is already 'real'),
		so it is a no-op.
		"""
        if signal_number >= 0:
            return
        real = self.env['levels.signals'].search([
            ('position_id', '=', self._position_id),
            ('real_flag', '=', True),
        ], limit=1)
        if real:
            await self._close_position()
            self._need_new_position = True

    async def _handle_entry_timeout_rebuild(self, direction, price,
                                            signal_ts, candle_ts):
        """Unified handling of entry-timeout — streak + pause.

		Called from both branches of _check_grid_levels:
		* "last_exec is None" (entry has not yet executed at all);
		* The timeout block for a pending entry with signal_number == 0.

		signal_ts — TBOT UTC (for logs); candle_ts — exchange time,
		which is passed to _rebuild_levels(entry_ts=...) as the current_timestamp.

		Returns True if the rebuild was performed; False if the grid has been parked.
		"""
        self._reset_no_fill_streak += 1
        # «> MAX_ENTRY_RESET_STREAK» — 5 successful rebuilds
		# before a pause on the 6th. Mirrors the semantics of "N consecutive failures."
        if self._reset_no_fill_streak > MAX_ENTRY_RESET_STREAK:
            self.logger.error(
                f"Entry cannot be executed after "
                f"{self._reset_no_fill_streak} rebuilds "
                f"(last signal_ts={signal_ts}) — pausing grid")
            self.config['paused_reason'] = 'entry_cannot_execute'
            #Store the pause timestamp for diagnostic purposes
			# (analogous to execution_paused.since_ns in the executor).
            self.config['paused_since_ns'] = self.get_time_ns()
            update_bot_config(self.bot_id, self.config)
            update_bot_status(self.bot_id, 'paused')
            self.running = False
            return False

        # Recalc direction from freshest price data — caller's direction
        # was derived from old entry_rec, which is about to move to history
        recalc_direction = await self._get_initial_direction()
        if recalc_direction:
            if recalc_direction != direction:
                self.logger.info(
                    f"Entry-timeout rebuild: direction {direction} → "
                    f"{recalc_direction} (based on latest candles)")
            direction = recalc_direction

        await self._close_position()
        await self._rebuild_levels(direction, price, candle_ts)
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self):
        if self.running:
            return
        self.running = True
        # Streak reset upon manual start. The instance is preserved
		# between Stop → Start; without a reset, a parked session would
		# immediately return to 'pause' upon the very first entry-timeout.
        self._reset_no_fill_streak = 0

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

        # Recover from a force-reset that was pending across a restart.
        force_reset_rec = self.env['levels.signals'].search([
            ('position_id', '=', self._position_id),
            ('signal_number', '=', PANIC_SIGNAL_NUMBER),
        ], limit=1)
        if force_reset_rec:
            self.logger.warning(
                "force_reset_position: unfinished reset found on start — "
                "completing local reset"
            )
            await self._close_position()

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
        collector_id = self.config.get("collector_bot_id")
        last_warn_ns = 0
        while self.running:
            try:
                if self.config_dirty:
                    await self.on_config_updated()
                    self.config_dirty = False

                # Collector unavailable → skip cycle and
				# issue a throttled warning. `unknown` — proceed with caution; 
				# anything else (stopped) — skip. Unified throttle block.
                now = self.get_time_ns()
                if self.manager and collector_id:
                    coll_status = _get_cached_collector_status(self.manager, collector_id, now)
                    if coll_status not in ('running', 'paused'):
                        if (now - last_warn_ns) > 300 * 1_000_000_000:
                            if coll_status == 'unknown':
                                self.logger.warning(
                                    f"Collector #{collector_id} "
                                    f"status unknown — proceeding "
                                    f"with caution")
                            else:
                                self.logger.warning(
                                    f"Collector #{collector_id} "
                                    f"status={coll_status} — "
                                    f"grid loop skipped")
                            last_warn_ns = now
                        if coll_status != 'unknown':
                            await asyncio.sleep(interval)
                            continue

                await self._update_price_history(limit=2000)
                await self._check_grid_levels()
                # If the grid is parked within _check_grid_levels
				# (due to a pause triggered by a streak), do not complete the current measure.
                if not self.running:
                    break
                await self._check_smart_averaging()
                await self._check_smart_executions()

                # Force reset timeout: execution never confirmed.
                if self._force_reset_pending and \
                        self.get_time_ns() >= self._force_reset_deadline_ns:
                    self.logger.error(
                        "force_reset_position: timeout waiting for execution "
                        "feedback — resetting locally as-is. If the exchange "
                        "position was real, close it manually."
                    )
                    self._force_reset_pending = False
                    await self._close_position()
                    self._need_new_position = True

                if self._need_new_position:
                    self._need_new_position = False
                    # If the current position contains any "real" rows
					# (whether complete or partial), move the entire position to
					# history via `_close_position` — this function automatically
					# retrieves all rows associated with the `pid` and generates a new one. 
					# Otherwise, simply perform a `rebuild` on the current `pid`
					# without altering the history.
                    real = self.env['levels.signals'].search([
                        ('position_id', '=', self._position_id),
                        ('real_flag', '=', True),
                    ], limit=1)
                    if real:
                        await self._close_position()
                    direction = await self._get_initial_direction()
                    if direction and self._last_price:
                        await self._rebuild_levels(
                            direction, self._last_price, self._last_ts)
            except Exception as e:
                self.logger.error(f"Error in main loop: {e}", exc_info=True)
            await asyncio.sleep(interval)

    async def on_config_updated(self):
        try:
            self.logger.info("on_config_updated: updating config")
            new_config = get_bot_config(self.bot_id)
            provider_changed = (
                new_config.get('exchange_provider')
                != self.config.get('exchange_provider')
            )
            self.config = new_config
            if provider_changed:
               # Provider change — re-resolve the factory.
                self._resolve_exchange_provider()
            self._truncate_max_count()
            if not self.config.get('max_averaging_count'):
                self.config['max_averaging_count'] = self.config.get(
                    'max_averaging_count_initial', 1)
            update_bot_config(self.bot_id, self.config)
            await self._update_levels()
            self.logger.info("on_config_updated: levels successfully recalculated")
        except Exception as e:
            self.logger.error(f"on_config_updated: error updating levels: {e}", exc_info=True)

    def _close_db(self):
        if hasattr(self, 'env'):
            self.env.close()