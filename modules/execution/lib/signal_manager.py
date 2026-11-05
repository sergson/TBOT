# modules/execution/lib/signal_manager.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.

import asyncio
import math
from .order_manager import make_client_order_id, DRY_RUN

REL_TOL = 1e-6
STALE_PRICE_NS = 5_000_000_000   # 5s — for stale-price check
MAX_CONSECUTIVE_FAILURES = 3

TERMINAL_STATES = {'filled', 'canceled', 'rejected', 'expired', 'not_found'}

# Fallback used only if bot._panic_signal_number is unavailable
# (e.g. unit-test stub without bot). The live code reads
# bot._panic_signal_number which is loaded from signal_constants at start().
_PANIC_FALLBACK = -999_999

# --- cancel-confirm invariant ---
CANCEL_CONFIRM_TIMEOUT_SEC = 3.0
CANCEL_CONFIRM_POLL_SEC = 0.2

# --- phase model ---
PHASE3_MIN_NS = 2_000_000_000
PHASE3_MAX_NS = 3_000_000_000
PHASE3_RATIO = 0.10
PHASE3_OVERFLOW_RATIO = 0.15
PHASE1_FALLBACK_RATIO = 0.2
PHASE1_MAX_SHARE_OF_PHASE2 = 0.7
PHASE2_MIN_INTERVAL_NS = 50_000_000

# --- market-close remainder (Patch C) ---
MAX_MARKET_CLOSE_ATTEMPTS = 3
CLOSE_MARKET_FRACTION_FALLBACK = 0.15
CLOSE_MARKET_WAIT_SEC = 10.0
FORCE_COMPLETE_REASON = 'forced_complete_after_failure'


class FailReason:
    TIMEOUT = 'timeout'
    ORDER_REJECTED = 'order_rejected'
    ORDER_EXPIRED = 'order_expired'
    INSUFFICIENT_FUNDS = 'insufficient_funds'
    RATE_LIMIT = 'rate_limit'
    NETWORK = 'network'
    EXCHANGE_ERROR = 'exchange_error'
    INVALID_SYMBOL = 'invalid_symbol'
    UNKNOWN = 'unknown'


def classify_exception(exc: Exception) -> str:
    name = type(exc).__name__
    msg = str(exc).lower()
    if 'insufficient' in msg or 'balance' in msg:
        return FailReason.INSUFFICIENT_FUNDS
    if 'rate limit' in msg or 'too many requests' in msg:
        return FailReason.RATE_LIMIT
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError)):
        return FailReason.NETWORK
    if name in ('NetworkError', 'RequestTimeout', 'ExchangeNotAvailable'):
        return FailReason.NETWORK
    if name in ('InvalidOrder', 'BadRequest'):
        return FailReason.EXCHANGE_ERROR
    return FailReason.UNKNOWN

def classify_breaker_reason(exc: Exception) -> str:
    """Classification for a breaker. Returns '' if it is not a breaker class.

    Differs from `classify_exception`: a breaker operates on *categories* 
    of errors that trigger a pause, rather than on the specific details 
    intended for database logging.
    """
    name = type(exc).__name__
    msg = str(exc).lower()

    if (name in ('AuthenticationError', 'PermissionDenied')
            or 'auth' in msg
            or 'signature' in msg
            or 'api key' in msg
            or 'api-key' in msg
            or 'permission' in msg
            or 'invalid key' in msg):
        return 'auth'
    if 'insufficient' in msg or 'balance' in msg:
        return 'funds'
    if name in ('InvalidOrder', 'BadRequest') or 'invalid order' in msg:
        return 'invalid_order'
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError)):
        return 'network'
    if name in ('NetworkError', 'RequestTimeout', 'ExchangeNotAvailable',
                'DDoSProtection', 'RateLimitExceeded'):
        return 'network'
    return ''


class SignalManager:
    """Works with `plan` (immutable data from the grid) and `current_order`
    (an `execution.order` record — the current attempt row).

    Invariant: The `SignalManager` is the sole writer to `execution.order`
    for its specific `[pos, sig]` pair.
    """

    def __init__(self, bot, plan: dict, logger, time_provider):
        self.bot = bot
        self.plan = plan
        self.logger = logger
        self.get_time_ns = time_provider

        self.current_order = None
        self.cid_to_row: dict = {}
        # event-based confirm: cid -> asyncio.Event, set upon transition to terminal state
        self._terminal_by_cid: dict = {}

        self.active_order = None
        self.active_client_order_id = None

        self.executed_volume = 0.0
        self.executed_price = 0.0
        self.last_fee = None

        self.filled = False
        self.cancelled = False
        self._cleaned = False

        self.last_bid = None
        self.last_ask = None
        self.last_price_ns = 0
        self.last_price_recv_ns = 0
        self._last_seen_best = None

        self.filled_event = asyncio.Event()
        self.price_changed = asyncio.Event()
        self.order_opened = asyncio.Event()
        self.order_canceled = asyncio.Event()

        self.task = None
        self.reorder_task = None
        self.watchdog_task = None
        self.last_ws_update_ns = 0
        self._no_price_logged = False

        self._compute_phases()

    def _compute_phases(self):
        """Recalculate phase 1/2/3 windows from the plan."""
        sig_ts = self.plan['signal_timestamp']
        deadline_ns = self.plan['deadline_ns']
        total_ns = max(deadline_ns - sig_ts, 0)

        phase3_ns = min(int(total_ns * PHASE3_RATIO), PHASE3_MAX_NS)
        phase3_ns = max(phase3_ns, PHASE3_MIN_NS)
        if phase3_ns >= total_ns:
            phase3_ns = max(int(total_ns * PHASE3_OVERFLOW_RATIO), 1)
        self.phase3_ns = phase3_ns
        self.phase2_end_ns = deadline_ns - phase3_ns

        reserve_sec = float(getattr(self.bot, 'execution_reserve_sec', 0.0) or 0.0)
        phase1_end_ns = sig_ts + int(reserve_sec * 1e9)
        max_phase1_ns = int((self.phase2_end_ns - sig_ts)
                            * PHASE1_MAX_SHARE_OF_PHASE2)
        if phase1_end_ns > sig_ts + max_phase1_ns:
            phase1_end_ns = sig_ts + max_phase1_ns
        if phase1_end_ns <= sig_ts:
            phase1_end_ns = sig_ts + int(total_ns * PHASE1_FALLBACK_RATIO)
        self.phase1_end_ns = phase1_end_ns

        phase2_dur_ns = max(self.phase2_end_ns - self.phase1_end_ns, 0)
        # Reorder cadence follows the same divisor as broker polling:
        # one price tick = one reorder opportunity.
        divisor = max(
            int(self.bot.config.get('poll_divisor', 10) or 10), 1)
        self.phase2_interval_ns = max(
            phase2_dur_ns // divisor, PHASE2_MIN_INTERVAL_NS)

    @classmethod
    def from_row(cls, bot, row, logger, time_provider):
        """Restore the manager from an existing attempt string.

        Does not create a new attempt. The `active_order` is NOT hydrated here —
        this is handled by the caller (`_recover_pair`) after `fetch_order`.
        """
        plan = cls._plan_from_row(bot, row)
        mgr = cls(bot, plan, logger, time_provider)
        mgr.current_order = row
        if row.client_order_id:
            mgr.register_client_id(row.client_order_id, row)
        return mgr

    @staticmethod
    def _plan_from_row(bot, row):
        sig_num = row.signal_number
        panic_number = getattr(bot, '_panic_signal_number', _PANIC_FALLBACK)
        return {
            'position_id': row.position_id,
            'signal_number': sig_num,
            'symbol': row.symbol,
            'side': row.side,
            'order_type': row.order_type or 'limit',
            'price': row.price_requested,
            'signal_vol_rate': row.signal_vol_rate or 0.0,
            'signal_timestamp': row.signal_timestamp or 0,
            'signal_timestamp_scale': row.signal_timestamp_scale or 'tbot_utc_ns',
            'deadline_ns': row.deadline_ns or 0,
            'reduce_only': bool(row.reduce_only),
            'amount_requested': row.amount_requested or 0.0,
            'amount_placed': row.amount_placed or 0.0,
            'is_panic': (isinstance(sig_num, int)
                         and sig_num == panic_number),
            'signal_smart': bool(getattr(row, 'signal_smart', False)),
            'signal_retry': int(getattr(row, 'signal_retry', 0) or 0),
        }

    def _bump_ui(self):
        try:
            self.bot._bump_ui_event()
        except Exception:
            pass

    def _log_prefix(self) -> str:
        """unified log-prefix (pos/sig/attempt).
        'attempt' is taken from 'current_order' if it has been created; otherwise, it is '?'.
        (applies to logs prior to '_new_attempt' during pre-flight).
        """
        pos = self.plan.get('position_id') or '?'
        sig = self.plan.get('signal_number', '?')
        att = (self.current_order.attempt_n
               if self.current_order else '?')
        return f"pos={pos} sig={sig} attempt={att}"

    # ---------- attempt row ----------

    def _total_filled(self) -> float:
        rows = self.bot.env['execution.order'].search([
            ('position_id', '=', self.plan['position_id']),
            ('signal_number', '=', self.plan['signal_number']),
        ])
        return sum(float(r.amount_filled or 0) for r in rows)

    def _weighted_avg_fill_price(self) -> float:
        """Weighted average fill price across all attempts with fill > 0.

        Used in _finalize to ensure the average price is not lost during a reorder
        following a partial fill (i.e., when current_order.amount_filled == 0).
        """
        rows = self.bot.env['execution.order'].search([
            ('position_id', '=', self.plan['position_id']),
            ('signal_number', '=', self.plan['signal_number']),
            ('amount_filled', '>', 0),
        ])
        total_v = sum(float(r.amount_filled or 0) for r in rows)
        if total_v <= 0:
            return (self.current_order.avg_fill_price
                    or self.executed_price
                    or self.plan.get('price')
                    or 0.0)
        total_cost = sum(float(r.amount_filled or 0) * float(r.avg_fill_price or 0)
                         for r in rows)
        return total_cost / total_v

    def _new_attempt(self, amount_placed=None):
        """Create a new attempt row.

        amount_requested — always plan['amount_requested'] (the full signal
        volume, corresponding to signal_vol_rate). amount_placed — the actual
        volume for this attempt (or the remaining balance, in the case of a reorder).

        mark as 'abandoned' only those previous attempts where
        amount_filled == 0. Rows with an actual fill remain untouched—their
        fill is already recorded on the exchange and included in _total_filled;
        feedback will be sent via _finalize(current_order) with the total volume.
        """
        pos = self.plan['position_id']
        sig = self.plan['signal_number']

        prev = self.bot.env['execution.order'].search([
            ('position_id', '=', pos),
            ('signal_number', '=', sig),
            ('feedback_state', 'in', ['none', 'pending']),
        ])
        for r in prev:
            if float(r.amount_filled or 0) > 0:
                continue
            # A distinct terminal state,
            # not to be confused with abandoned-by-age (>24h feedback timeout).
            r.write({
                'feedback_state': 'superseded',
                'feedback_last_error': 'superseded_by_reorder',
            })

        max_rec = self.bot.env['execution.order'].search([
            ('position_id', '=', pos),
            ('signal_number', '=', sig),
        ], order='attempt_n DESC', limit=1)
        next_n = (max_rec[0].attempt_n + 1) if max_rec else 1

        # Clear order_id_index for the cid of previous attempts
        # with the same (pos, sig) that are already terminal.
        old = self.bot.env['execution.order'].search([
            ('position_id', '=', pos),
            ('signal_number', '=', sig),
            ('exchange_state', 'in',
             ['filled', 'canceled', 'rejected', 'expired', 'not_found']),
        ])
        for r in old:
            cid_old = r.client_order_id
            if cid_old:
                self.bot.order_id_index.pop(cid_old, None)

        now = self.get_time_ns()
        row = self.bot.env['execution.order'].create([{
            'position_id': pos,
            'signal_number': sig,
            'attempt_n': next_n,
            'symbol': self.plan['symbol'],
            'side': self.plan['side'],
            'order_type': self.plan['order_type'],
            'reduce_only': self.plan['reduce_only'],
            'price_requested': self.plan.get('price'),
            'amount_requested': self.plan['amount_requested'],
            'amount_placed': (amount_placed if amount_placed is not None
                              else self.plan['amount_placed']),
            'signal_vol_rate': self.plan['signal_vol_rate'],
            'signal_timestamp': self.plan['signal_timestamp'],
            'signal_timestamp_scale': self.plan['signal_timestamp_scale'],
            'signal_smart': bool(self.plan.get('signal_smart', False)),
            'signal_retry': int(self.plan.get('signal_retry', 0) or 0),
            'deadline_ns': self.plan['deadline_ns'],
            'exchange_state': 'not_sent',
            'feedback_state': 'none',
            'created_ns': now,
            'last_sync_ns': now,
        }])[0]

        cid = make_client_order_id(self.bot.bot_id, pos, sig, next_n)
        row.write({'client_order_id': cid})
        self.current_order = row
        self.register_client_id(cid, row)
        return row

    def register_client_id(self, cid: str, row):
        self.cid_to_row[cid] = row
        self.bot.order_id_index[cid] = self

    def unregister_all(self):
        for cid in list(self.cid_to_row.keys()):
            self.bot.order_id_index.pop(cid, None)
        self.cid_to_row.clear()

    # ---------- pre-flight ----------

    async def _apply_preflight(self) -> bool:
        if DRY_RUN:
            placed = float(self.plan.get('amount_requested') or 0.0)
            self.plan['amount_placed'] = placed
            self.current_order.write({
                'amount_placed': placed,
                'last_sync_ns': self.get_time_ns(),
            })
            return True
        placed = await self._compute_auto_scale(self.plan['amount_requested'])
        if placed is None:
            self.current_order.write({
                'exchange_state': 'not_sent',
                'reject_reason': 'scale_impossible',
                'amount_placed': 0.0,
                'last_sync_ns': self.get_time_ns(),
            })
            await self.bot._send_feedback_for_row(self.current_order)
            self._cleanup()
            return False
        self.plan['amount_placed'] = placed
        self.current_order.write({
            'amount_placed': placed,
            'last_sync_ns': self.get_time_ns(),
        })

        if not await self._check_balance(placed):
            self.current_order.write({
                'exchange_state': 'not_sent',
                'reject_reason': 'insufficient_funds',
                'last_sync_ns': self.get_time_ns(),
            })
            await self.bot._send_feedback_for_row(self.current_order)
            # Remove mgr from active_managers before the breaker,
            # otherwise _enter_paused will finalize self and overwrite the reason.
            self._cleanup()
            try:
                await self.bot._register_breaker_failure('funds')
            except Exception:
                pass
            return False
        return True

    async def _compute_auto_scale(self, raw: float):
        """Rounding based on precision.amount / limits.amount.min / limits.cost.min.

        If unit_volume_auto_scale=False, return the raw value without rounding.
        """
        if DRY_RUN:
            try:
                raw_f = float(raw)
            except (TypeError, ValueError):
                return None
            return raw_f if raw_f > 0 else None
 
        try:
            raw_f = float(raw)
        except (TypeError, ValueError):
            return None
        if raw_f <= 0:
            return None

        if not self.bot.config.get('unit_volume_auto_scale', True):
            return raw_f

        try:
            market = await self.bot.broker_handle.get(
                "get_market",
                exchange=self.bot.config['exchange'],
                market_type=self.bot.config['market_type'],
                symbol=self.plan['symbol'])
        except Exception:
            return None
        if not market:
            return None

        precision = (market.get('precision') or {}).get('amount')
        limits = market.get('limits') or {}
        min_amount = (limits.get('amount') or {}).get('min')
        min_cost = (limits.get('cost') or {}).get('min')

        ref_price = self.plan.get('price') or self._get_best_price() or 0.0

        def _floor_step(x, p):
            if not p or p <= 0:
                return x
            return math.floor(x / p) * p

        def _ceil_step(x, p):
            if not p or p <= 0:
                return x
            return math.ceil(x / p) * p

        placed = _floor_step(raw_f, precision)
        if self.plan.get('reduce_only'):
            if placed <= 0:
                return None
            if min_amount and placed < min_amount:
                return None
            return float(placed)

        if min_amount and placed < min_amount:
            placed = min_amount

        if min_cost and ref_price > 0 and placed * ref_price < min_cost:
            placed = _ceil_step(min_cost / ref_price, precision)

        if precision:
            placed = _ceil_step(placed, precision)

        if placed <= 0:
            return None
        if min_amount and placed < min_amount:
            return None
        #max_scale
        max_scale = float(self.bot.config.get('max_scale', 0) or 0)
        if max_scale > 0 and placed > raw_f * max_scale:
            return None

        return float(placed)

    @staticmethod
    def _split_symbol(symbol: str):
        """'BTC/USDT' → ('BTC','USDT'); 'BTC/USDT:USDT' → ('BTC','USDT')."""
        core = symbol.split(':')[0]
        base, quote = core.split('/')
        return base, quote

    async def _check_balance(self, placed: float) -> bool:
        """Check available balance.

        Spend currency depends on the side of the trade:
          spot buy → quote; spot sell → base; futures → quote.
        Buffer — always in the quote currency; converted to the base currency if necessary.
        """
        if DRY_RUN:
            return True
        om = self.bot.order_manager
        base, quote = self._split_symbol(self.plan['symbol'])
        side = self.plan['side']
        market_type = om.market_type

        if market_type == 'futures':
            spend_cur = quote
        else:
            spend_cur = quote if side == 'buy' else base

        try:
            bal = await om.fetch_balance(spend_cur)
            available = float(bal.get('free') or 0.0)
        except Exception as e:
            self.logger.error(f"fetch_balance failed: {e}")
            # We do NOT register the breaker — it’s a "fail-open" scenario. The actual error
            # will be captured in _place_or_replace_order/_execute_market.
            return True

        ref_price = self._get_best_price() or self.plan.get('price') or 0.0
        if ref_price <= 0:
            return True

        buffer_ratio = float(self.bot.config.get('margin_buffer_ratio', 0.05))
        unit_vol = float(self.bot.config.get('unit_volume', 1.0) or 1.0)
        unit_mode = self.bot.config.get('unit_volume_mode', 'quote')

        unit_vol_quote = unit_vol if unit_mode == 'quote' else unit_vol * ref_price
        buffer_quote = buffer_ratio * unit_vol_quote

        if spend_cur == quote:
            if market_type == 'futures':
                if self.plan.get('reduce_only'):
                    # Reduce-only close does not require additional margin.
                    required = buffer_quote
                else:
                    required = placed * ref_price / max(om.leverage, 1) + buffer_quote
            else:
                required = placed * ref_price + buffer_quote
        else:
            buffer_base = buffer_quote / ref_price if ref_price > 0 else 0
            required = placed + buffer_base

        if available < required:
            self.logger.warning(
                f"{self._log_prefix()}: "
                f"insufficient {spend_cur}: need {required:.4f}, "
                f"have {available:.4f}")
            return False
        return True

    # ---------- run ----------

    async def run(self):
        try:
            await self._prefetch_market()
            sig_n = self.plan.get('signal_number')

            if self.plan['is_panic']:
                await self._prepare_panic_plan()
            elif isinstance(sig_n, int) and sig_n == 0:
                # NEW: when entering, take into account the actual remaining balance on the exchange
                # (leftover absorbed from a failed close).
                await self._reconcile_entry_position()

            self._new_attempt()

            if self.plan.get('_reconcile_refuse'):
                self.current_order.write({
                    'exchange_state': 'not_sent',
                    'reject_reason': 'leftover_position_refused',
                    'last_sync_ns': self.get_time_ns(),
                })
                await self.bot._send_feedback_for_row(self.current_order)
                self._cleanup()
                return

            # The position is already equal to the target — do not place an order;
            # send a synthetic fill so that the grid can advance.
            if self.plan.get('_reconcile_skip_fill'):
                self.current_order.write({
                    'exchange_state': 'filled',
                    'amount_filled': float(
                        self.plan['amount_requested'] or 0.0),
                    'avg_fill_price': (self.last_bid or self.last_ask
                                       or self.plan.get('price') or 0.0),
                    'last_sync_ns': self.get_time_ns(),
                })
                await self.bot._send_feedback_for_row(self.current_order)
                return

            if not self.plan['is_panic']:
                if not await self._apply_preflight():
                    return
            if self.plan['is_panic']:
                await self._execute_panic_close()
            elif self.plan['order_type'] == 'market':
                await self._execute_market()
            else:
                await self._execute_limit()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.error(
                f"{self._log_prefix()} unexpected error: {e}",
                exc_info=True)
        finally:
            if not self._cleaned:
                try:
                    await self._finalize()
                except asyncio.CancelledError:
                    if self.current_order is not None:
                        try:
                            self.current_order.write({
                                'feedback_state': 'pending',
                                'feedback_last_error':
                                    'cancelled_in_finalize',
                                'last_sync_ns': self.get_time_ns(),
                            })
                        except Exception:
                            pass
                    raise

    async def resume_from_row(self):
        """Recovery path: does not create a new attempt; uses the current_order.

        Mirrors run(), but without _new_attempt. Ensures _finalize() is called within a finally block.
        Before calling _recover_pair, the active_order MUST be hydrated
        (see model.py); otherwise, _place_or_replace_order will create a second order.
        """
        
        try:
            await self._prefetch_market()
            if self.plan.get('is_panic'):
                self.logger.warning(
                    f"{self._log_prefix()} panic row recovered — "
                    f"finalizing without re-execution")
                try:
                    if self.current_order is not None and \
                            not self.current_order.reject_reason:
                        self.current_order.write({
                            'reject_reason': 'panic_recovery_unconfirmed',
                            'last_sync_ns': self.get_time_ns(),
                        })
                except Exception:
                    pass
                await self._finalize()
                return
            if self.plan['order_type'] == 'market':
                # Market order already sent — perform a reconciliation only,
                # to avoid creating a duplicate.
                await self._reconcile_market_order()
            else:
                await self._execute_limit()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.error(
                f"{self._log_prefix()} resume_from_row error: {e}",
                exc_info=True)
        finally:
            if not self._cleaned:
                try:
                    await self._finalize()
                except asyncio.CancelledError:
                    if self.current_order is not None:
                        try:
                            self.current_order.write({
                                'feedback_state': 'pending',
                                'feedback_last_error': 'cancelled_in_resume_finalize',
                                'last_sync_ns': self.get_time_ns(),
                            })
                        except Exception:
                            pass
                    raise

    async def resume_reorder_loop(self):
        if self.current_order is None or self._cleaned:
            return
        await self._reorder_loop()

    # ---------- panic close ----------

    async def _prepare_panic_plan(self):
        # In run(), _prepare_panic_plan() is called before
        # _new_attempt(). This is intentional: preparing the panic plan
        # (fetching positions via the exchange) does not depend on the
        # attempt identifier. _log_prefix() will return attempt='?' —
        # which is acceptable for the panic-preparation phase.
        # If the attempt identifier is needed in the logs in the future,
        # move _new_attempt() to before _prepare_panic_plan() (see run())
        
        if DRY_RUN:
            # Educational mode: there is no real position to close.
            # The panic branch below will report `position_already_flat`,
            # which the grid handles by resetting locally.
            self.plan['_panic_prepare_failed'] = False
            self.plan['_panic_positions'] = []
            self.plan['side'] = self.plan.get('side') or 'sell'
            self.plan['amount_requested'] = 0.0
            self.plan['amount_placed'] = 0.0
            return
            
        self.plan['_panic_prepare_failed'] = False
        self.plan['_panic_positions'] = []
        try:
            positions = await self.bot.broker_handle.get(
                "fetch_positions",
                key_id=self.bot.config['broker_key_id'],
                symbols=[self.plan['symbol']])
            self.plan['_panic_positions'] = positions or []
        except Exception as e:
            self.logger.error(
                f"{self._log_prefix()} panic prepare failed: {e}")
            self.plan['_panic_prepare_failed'] = True
            self.plan['side'] = self.plan.get('side') or 'sell'
            self.plan['amount_requested'] = 0.0
            self.plan['amount_placed'] = 0.0
            return

        pos_size = 0.0
        close_side = self.plan.get('side') or 'sell'
        for pos in self.plan['_panic_positions']:
            signed = self._signed_position(pos)
            if abs(signed) > 0:
                pos_size = abs(signed)
                close_side = 'sell' if signed > 0 else 'buy'
                break
        self.plan['side'] = close_side
        self.plan['amount_requested'] = pos_size
        self.plan['amount_placed'] = pos_size

    async def _execute_panic_close(self):
        if DRY_RUN:
            # _prepare_panic_plan sets amount_placed=0 in DRY_RUN,
            # so this method reports position_already_flat downstream.
            self.current_order.write({
                'exchange_state': 'rejected',
                'reject_reason': 'position_already_flat',
                'last_sync_ns': self.get_time_ns(),
            })
            await self.bot._send_feedback_for_row(self.current_order)
            self._bump_ui()
            self.logger.info(
                f"{self._log_prefix()} [DRY-RUN] panic close emulated — "
                f"no real position, reporting position_already_flat")
            return
            
        if self.plan.get('_panic_prepare_failed'):
            self.current_order.write({
                'exchange_state': 'rejected',
                'reject_reason': 'panic_prepare_failed',
                'last_sync_ns': self.get_time_ns(),
            })
            await self.bot._send_feedback_for_row(self.current_order)
            return

        if self.plan['amount_placed'] == 0.0:
            self.current_order.write({
                'exchange_state': 'rejected',
                'reject_reason': 'position_already_flat',
                'last_sync_ns': self.get_time_ns(),
            })
            await self.bot._send_feedback_for_row(self.current_order)
            return

        try:
            result = await self.bot.broker_handle.get(
                "panic_close",
                key_id=self.bot.config['broker_key_id'],
                symbol=self.plan['symbol'])
        except Exception as e:
            self.current_order.write({
                'exchange_state': 'rejected',
                'reject_reason': 'broker_unavailable',
                'reject_reason_raw': str(e)[:255],
                'last_sync_ns': self.get_time_ns(),
            })
            await self.bot._send_feedback_for_row(self.current_order)
            self._bump_ui()
            return

        closed = float(result.get('closed_size') or 0.0)
        errors = result.get('errors') or []
        if closed > 0 and not errors:
            avg_fill_price = result.get('avg_fill_price') or self.last_bid or self.plan.get('price') or 0.0
            self.current_order.write({
                'exchange_state': 'filled',
                'amount_filled': closed,
                'avg_fill_price': avg_fill_price,
                'last_sync_ns': self.get_time_ns()})
        elif not errors:
            self.current_order.write({
                'exchange_state': 'filled',
                'reject_reason': 'position_already_flat',
                'amount_filled': 0.0,
                'last_sync_ns': self.get_time_ns()})
        else:
            self.current_order.write({
                'exchange_state': 'rejected',
                'reject_reason': 'panic_close_failed',
                'reject_reason_raw': ('; '.join(errors))[:255],
                'last_sync_ns': self.get_time_ns()})
        await self.bot._send_feedback_for_row(self.current_order)
        self._bump_ui()

    # ---------- market ----------

    async def _prefetch_market(self):
        if DRY_RUN:
            self._cached_market = None
            return
        if getattr(self, '_cached_market', None) is None:
            try:
                self._cached_market = await self.bot.broker_handle.get(
                    "get_market",
                    exchange=self.bot.config['exchange'],
                    market_type=self.bot.config['market_type'],
                    symbol=self.plan['symbol'])
            except Exception:
                self._cached_market = None

    async def _execute_market(self):
        if DRY_RUN:
            price = self._get_best_price() or self.plan.get('price') or 0.0
            await self._emulate_fill(
                price=price,
                amount=self.plan['amount_placed'],
                reason='dry_run_market',
            )
            return
            
        amount = self.plan['amount_placed']
        try:
            order = await self.bot.order_manager.create_market_order(
                self.plan['side'], amount,
                self.plan['position_id'], self.plan['signal_number'],
                self.current_order.attempt_n,
                reduce_only=self.plan['reduce_only'],
            )
            write_vals = {
                'exchange_order_id': (order.get('id')
                                       if isinstance(order, dict) else None),
                'exchange_state': 'open',
                'last_sync_ns': self.get_time_ns(),
            }
            if isinstance(order, dict):
                actual_amount = order.get('amount')
                if actual_amount:
                    try:
                        write_vals['amount_placed'] = float(actual_amount)
                    except (TypeError, ValueError):
                        pass
            self.current_order.write(write_vals)

            # Market orders are usually filled instantly. If the REST API has already returned
            # 'closed'/'filled' — we process it without waiting for the WS update.
            rest_status = (order.get('status')
                           if isinstance(order, dict) else None)
            if rest_status == 'closed':
                await self.process_order_update(order)
                return

            timeout = max(
                0, (self.plan['deadline_ns'] - self.get_time_ns()) // 1_000_000_000)
            try:
                await asyncio.wait_for(self.filled_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                self.logger.warning(
                    f"{self._log_prefix()} market did not confirm, reconciling")
                await self._reconcile_market_order()
        except Exception as e:
            err_str = str(e)
            if self.plan['reduce_only'] and (
                    '-2022' in err_str or 'ReduceOnly Order is rejected' in err_str):
                self.logger.warning(
                    f"{self._log_prefix()}: reduce-only rejected — "
                    f"position already flat")
                self.current_order.write({
                    'exchange_state': 'filled',
                    'reject_reason': 'position_already_flat',
                    'amount_filled': 0.0,
                    'avg_fill_price': self.last_bid or self.plan.get('price') or 0.0,
                    'last_sync_ns': self.get_time_ns(),
                })
                await self.bot._send_feedback_for_row(self.current_order)
                return
            self.logger.error(
                f"{self._log_prefix()} market order failed: {e}",
                exc_info=True)
            reason = classify_exception(e)
            self.current_order.write({
                'exchange_state': 'rejected',
                'reject_reason': reason,
                'reject_reason_raw': err_str[:255],
                'last_sync_ns': self.get_time_ns(),
            })
            if reason == FailReason.RATE_LIMIT:
                await asyncio.sleep(1.0)
            #breaker
            breaker = classify_breaker_reason(e)
            if breaker:
                try:
                    await self.bot._register_breaker_failure(breaker)
                except Exception:
                    pass

    async def _reconcile_market_order(self):
        order_id = self.current_order.exchange_order_id
        if not order_id:
            return
        try:
            order = await self.bot.order_manager.fetch_order(
                order_id, self.current_order.client_order_id)
        except Exception as e:
            self.logger.error(
                f"{self._log_prefix()} reconcile fetch failed: {e}")
            return
        if not order:
            return
        status = order.get('status')
        if status == 'closed':
            filled = float(order.get('filled', 0) or 0)
            avg = order.get('average') or order.get('price')
            fee = order.get('fee')
            fee_cost = None
            if isinstance(fee, dict):
                fee_cost = fee.get('cost')
            elif isinstance(fee, (int, float)):
                fee_cost = fee
            placed = float(self.plan['amount_placed'] or 0)
            threshold = placed * (1 - REL_TOL) if placed > 0 else 0
            new_state = 'filled' if filled >= threshold else 'partial'
            self.current_order.write({
                'exchange_state': new_state,
                'amount_filled': filled,
                'avg_fill_price': avg,
                'fee': fee_cost,
                'last_sync_ns': self.get_time_ns(),
            })
            if filled > self.executed_volume:
                self.executed_volume = filled
                self.executed_price = avg
            if fee_cost is not None:
                self.last_fee = fee_cost
            if filled >= threshold:
                self.filled = True
                self.filled_event.set()
                self._bump_ui()

    # ---------- market-close remainder (Patch C) ----------

    def _close_market_threshold_fraction(self) -> float:
        """A threshold for the remaining amount (as a fraction of `plan.amount_placed`), below which 
        the remaining `reduce-only` position is closed out via a market order."""
        cfg = getattr(self.bot, 'config', None) or {}
        consts = cfg.get('signal_constants') or {}
        try:
            v = float(consts.get(
                'close_market_fraction', CLOSE_MARKET_FRACTION_FALLBACK))
        except (TypeError, ValueError):
            v = CLOSE_MARKET_FRACTION_FALLBACK
        # sanity: 0 < frac <= 0.5 (prevents closing half the plan via a "market" order)
        return max(0.0, min(0.5, v))

    async def _force_complete_feedback(self):
        """Synthetic full-close feedback for the grid.

        Marks the `current_order` as fully executed for the planned volume,
        triggering the grid to close the plan and begin a new cycle. Any
        actual remaining balance on the exchange (if present) will be
        accounted for during the next `signal 0` via `_reconcile_entry_position` —
        no state persistence is required.
        """
        if self.current_order is None:
            return
        forced_fill = float(self.current_order.amount_requested or 0.0)
        if forced_fill <= 0:
            forced_fill = float(self.plan.get('amount_requested') or 0.0)
        self.logger.error(
            f"{self._log_prefix()} force-complete: sending synthetic "
            f"full-close feedback (amount_filled={forced_fill:.8f}, "
            f"signal_vol_rate={self.current_order.signal_vol_rate}). "
            f"Any exchange residual will be absorbed by "
            f"_reconcile_entry_position at next signal 0.")
        self.current_order.write({
            'exchange_state': 'filled',
            'reject_reason': FORCE_COMPLETE_REASON,
            'amount_filled': forced_fill,
            'avg_fill_price': (self.current_order.avg_fill_price
                               or self.last_bid or self.last_ask
                               or self.plan.get('price') or 0.0),
            'last_sync_ns': self.get_time_ns(),
        })
        await self.bot._send_feedback_for_row(self.current_order)

    async def _maybe_market_close_remainder(self) -> bool:
        """Close the remainder with a 'reduce-only' market order.

        Trigger: remaining = plan.amount_placed − _total_filled().

          * remaining ≤ threshold (plan_vol × close_market_fraction):
            Attempt a market-close up to MAX_MARKET_CLOSE_ATTEMPTS times.
          * remaining > threshold: a market-close is too costly/risky
            (due to potential slippage on a large volume) — trigger an immediate 'force-complete' feedback;
            the remainder will be accounted for during the next 'signal 0' event.

        Returns True if the position is flat OR a 'force-complete' feedback has been sent
        (which will trigger a grid reset). Returns False only during shutdown
        (_cleaned / _stopping).
        """
        if DRY_RUN:
            return False
            
        if not self.plan.get('reduce_only'):
            return False
        if self.current_order is None or self._cleaned:
            return False

        placed = float(self.plan['amount_placed'] or 0.0)
        if placed <= 0:
            return False

        frac = self._close_market_threshold_fraction()
        threshold = placed * frac
        side = self.plan['side']
        original_order = self.current_order

        for attempt in range(MAX_MARKET_CLOSE_ATTEMPTS):
            if self._cleaned or getattr(self.bot, '_stopping', False):
                self.current_order = original_order
                return False

            filled_total = self._total_filled()
            remaining = placed - filled_total

            # Already flat (including after a reconcile of the previous attempt)
            if remaining <= max(1e-12, placed * 1e-9):
                self.logger.info(
                    f"{self._log_prefix()} market-close: already flat "
                    f"(total_filled={filled_total:.8f}/{placed:.8f})")
                self.current_order = original_order
                return True

            # A large remaining position is not what we want. Closing at market would
            # result in significant slippage; we’ll absorb it into the next signal 0.
            if remaining > threshold:
                self.logger.warning(
                    f"{self._log_prefix()} market-close: remaining="
                    f"{remaining:.8f} > threshold={threshold:.8f} — "
                    f"skipping market, force-completing grid feedback")
                self.current_order = original_order
                await self._force_complete_feedback()
                return True

            self.logger.warning(
                f"{self._log_prefix()} market-close attempt "
                f"{attempt + 1}/{MAX_MARKET_CLOSE_ATTEMPTS}: "
                f"remaining={remaining:.8f} <= threshold={threshold:.8f}")

            row = self._new_attempt(amount_placed=remaining)
            row.write({
                'price_requested': self.last_bid or self.last_ask,
                'last_sync_ns': self.get_time_ns(),
            })

            try:
                order = await self.bot.order_manager.create_market_order(
                    side, remaining,
                    self.plan['position_id'], self.plan['signal_number'],
                    row.attempt_n,
                    reduce_only=True)
                row.write({
                    'exchange_order_id': (order.get('id')
                                           if isinstance(order, dict)
                                           else None),
                    'exchange_state': 'open',
                    'last_sync_ns': self.get_time_ns(),
                })
            except Exception as e:
                err = str(e)
                if '-2022' in err or 'ReduceOnly' in err:
                    self.logger.warning(
                        f"{self._log_prefix()} market-close: reduce-only "
                        f"rejected — position already flat on exchange")
                    row.write({
                        'exchange_state': 'filled',
                        'reject_reason': 'position_already_flat',
                        'amount_filled': 0.0,
                        'last_sync_ns': self.get_time_ns(),
                    })
                    await self.bot._send_feedback_for_row(row)
                    self.current_order = row
                    return True

                self.logger.error(
                    f"{self._log_prefix()} market-close attempt "
                    f"{attempt + 1} failed: {e}", exc_info=True)
                reason = classify_exception(e)
                row.write({
                    'exchange_state': 'rejected',
                    'reject_reason': reason,
                    'reject_reason_raw': err[:255],
                    'last_sync_ns': self.get_time_ns(),
                })
                # We don't send feedback — the grid must not see a 0-fill retry
                # (otherwise, the any_close-guard will break).
                breaker = classify_breaker_reason(e)
                if breaker:
                    try:
                        await self.bot._register_breaker_failure(breaker)
                    except Exception:
                        pass
                if reason == FailReason.RATE_LIMIT:
                    await asyncio.sleep(1.0)
                continue

            # We wait for a 'fill' via the existing event channel.
            self.filled_event.clear()
            try:
                await asyncio.wait_for(
                    self.filled_event.wait(), timeout=CLOSE_MARKET_WAIT_SEC)
            except asyncio.TimeoutError:
                self.current_order = row
                await self._reconcile_market_order()
                row = self.current_order or row

            if row.exchange_state in TERMINAL_STATES:
                await self.bot._send_feedback_for_row(row)
                self.current_order = row
                new_total = self._total_filled()
                if new_total >= placed * (1 - REL_TOL):
                    self.logger.info(
                        f"{self._log_prefix()} market-close: fully "
                        f"closed (total={new_total:.8f}/{placed:.8f})")
                    return True
                self.logger.warning(
                    f"{self._log_prefix()} market-close: still partial "
                    f"after attempt {attempt + 1} "
                    f"(filled={row.amount_filled})")
                continue

            # Non-terminal — abort and retry.
            row.write({
                'exchange_state': 'canceled',
                'reject_reason': 'close_retry_timeout',
                'last_sync_ns': self.get_time_ns(),
            })
            self.current_order = row
            continue

        # Exhausted attempts — force-complete: the grid will perform a reset,
        # and the remainder will be absorbed upon the next signal 0.
        self.logger.error(
            f"{self._log_prefix()} market-close: exhausted "
            f"{MAX_MARKET_CLOSE_ATTEMPTS} attempts — forcing feedback "
            f"so grid can advance. Residual will be absorbed at next "
            f"signal 0 via _reconcile_entry_position.")
        self.current_order = original_order
        await self._force_complete_feedback()
        return True

    async def _reconcile_entry_position(self):
        """signal 0: align actual exchange position to target_signed.

        - same direction, current > target -> reduce-only excess
        - same direction, current < target -> normal entry for missing part
        - opposite direction, small leftover -> flip in one order
        - opposite direction, large leftover -> refuse
        """
        if DRY_RUN:
            return

        if self.plan.get('signal_number') != 0:
            return

        try:
            positions = await self.bot.broker_handle.get(
                "fetch_positions",
                key_id=self.bot.config['broker_key_id'],
                symbols=[self.plan['symbol']])
        except Exception as e:
            self.logger.warning(
                f"{self._log_prefix()} reconcile entry: "
                f"fetch_positions failed: {e} — proceeding as-is")
            return

        nonzero = []
        for p in positions or []:
            s = self._signed_position(p)
            if abs(s) > 1e-12:
                nonzero.append(s)

        if len(nonzero) > 1:
            self.logger.error(
                f"{self._log_prefix()} reconcile entry: multiple non-zero "
                f"positions {nonzero} — hedge mode or ambiguous state; "
                f"refusing reconcile")
            self.plan['_reconcile_refuse'] = True
            return

        current_signed = nonzero[0] if nonzero else 0.0
        if abs(current_signed) < 1e-9:
            return

        entry_sign = 1 if self.plan['side'] == 'buy' else -1
        signal_amount = float(self.plan['amount_placed'] or 0.0)
        if signal_amount <= 0:
            return

        target_signed = entry_sign * signal_amount
        delta = target_signed - current_signed

        if abs(delta) < 1e-9:
            self.logger.info(
                f"{self._log_prefix()} reconcile entry: position already "
                f"at target ({target_signed:.8f}) — synthetic fill")
            self.plan['_reconcile_skip_fill'] = True
            return

        # Do not flip a large opposite leftover in one entry order.
        if (current_signed * target_signed < 0
                and abs(current_signed) > 2 * signal_amount):
            self.logger.error(
                f"{self._log_prefix()} reconcile entry: opposite leftover "
                f"{current_signed:.8f} > 2x entry {signal_amount:.8f}; "
                f"refusing — close it manually or via -2/-3")
            self.plan['_reconcile_refuse'] = True
            return

        same_dir = current_signed * target_signed > 0

        if same_dir and abs(current_signed) > abs(target_signed):
            # Leftover is larger than target: reduce only the excess.
            new_side = 'buy' if current_signed < 0 else 'sell'
            new_amount = abs(current_signed) - abs(target_signed)
            reduce_only = True
        else:
            # No position, leftover smaller than target, or small flip.
            new_side = 'buy' if delta > 0 else 'sell'
            new_amount = abs(delta)
            reduce_only = False

        self.logger.warning(
            f"{self._log_prefix()} reconcile entry: current="
            f"{current_signed:.8f}, target={target_signed:.8f}, "
            f"delta={delta:.8f} → {new_side} {new_amount:.8f} "
            f"reduce_only={reduce_only}")

        self.plan['side'] = new_side
        self.plan['amount_placed'] = new_amount
        self.plan['amount_requested'] = new_amount
        self.plan['reduce_only'] = reduce_only

    # ---------- limit ----------

    async def _execute_limit(self):
        if DRY_RUN:
            price = (self._determine_price()
                     or self.plan.get('price')
                     or 0.0)
            await self._emulate_fill(
                price=price,
                amount=self.plan['amount_placed'],
                reason='dry_run_limit',
            )
            return
            
        self.reorder_task = asyncio.create_task(self._reorder_loop())
        self.watchdog_task = asyncio.create_task(self._order_watchdog_loop())
        try:
            timeout_sec = max(0.0,
                              (self.phase2_end_ns - self.get_time_ns()) / 1e9)
            await asyncio.wait_for(self.filled_event.wait(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            self.logger.info(
                f"{self._log_prefix()} phase 2 expired, "
                f"entering phase 3 (cancel+feedback)")
        except asyncio.CancelledError:
            raise
        finally:
            for t in (self.reorder_task, self.watchdog_task):
                if t:
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass

    async def _reorder_loop(self):
        if DRY_RUN:
            self.logger.debug("[DRY-RUN] reorder loop disabled")
            return
            
        while not self.filled and self.get_time_ns() < self.phase2_end_ns:
            if self._cleaned:
                break
            try:
                await asyncio.wait_for(
                    self.price_changed.wait(),
                    timeout=self.phase2_interval_ns / 1e9)
            except asyncio.TimeoutError:
                pass
            self.price_changed.clear()
            if self.filled or self._cleaned:
                break
            ok = await self._place_or_replace_order()
            if not ok and self._cleaned:
                break

    async def _place_or_replace_order(self):
        if DRY_RUN:
            self.logger.debug("[DRY-RUN] place/replace disabled")
            return False
            
        if self._cleaned or getattr(self.bot, '_stopping', False):
            return False
        if self.active_order:
            intended = self._determine_price()
            if intended is None:
                return True
            if self.active_order.get('price') == intended:
                return True

            old_order_id = self.active_order['id']
            old_cid = self.active_client_order_id

            try:
                await self.bot.order_manager.cancel_order(old_order_id, old_cid)
            except Exception as e:
                # critical error log — prefix is ​​mandatory.
                self.logger.error(
                    f"{self._log_prefix()} Cancel in reorder failed: {e}",
                    exc_info=True)
                reason = classify_exception(e)
                self.current_order.write({
                    'exchange_state': 'rejected',
                    'reject_reason': reason,
                    'reject_reason_raw': str(e)[:255],
                    'last_sync_ns': self.get_time_ns(),
                })
                if self.reorder_task and not self.reorder_task.done():
                    self.reorder_task.cancel()
                return False

            confirmed = await self._await_terminal_state(old_order_id, old_cid)

            if not confirmed:
                self.logger.warning(
                    f"{self._log_prefix()} cancel not confirmed for "
                    f"{CANCEL_CONFIRM_TIMEOUT_SEC}s, stopping the reorder"
                )
                if self.reorder_task and not self.reorder_task.done():
                    self.reorder_task.cancel()
                return False

            if self.current_order.exchange_state in ('filled', 'partial'):
                self.logger.info(
                    f"{self._log_prefix()} cancel confirmed fill — "
                    f"reorder is not needed"
                )
                self.active_client_order_id = None
                self.active_order = None
                total = self._total_filled()
                threshold = float(self.plan['amount_placed']) * (1 - REL_TOL)
                if total >= threshold:
                    self.filled = True
                    self.filled_event.set()
                if self.reorder_task and not self.reorder_task.done():
                    self.reorder_task.cancel()
                return True

            self.active_client_order_id = None
            self.active_order = None

        price = self._determine_price()
        if price is None:
            if self.plan.get('price') is None:
                self.logger.warning(
                    f"{self._log_prefix()} no price: best=stale, "
                    f"signal_price=None — order not placed "
                    f"(deadline={self.plan['deadline_ns']})")
            elif not getattr(self, '_no_price_logged', False):
                self.logger.warning(
                    f"{self._log_prefix()} price is None "
                    f"(stream stall + no signal_price), cannot place order")
                self._no_price_logged = True
            return True

        total = self._total_filled()
        amount = float(self.plan['amount_placed']) - total
        if amount <= 0:
            return True

        # Reuse only if the order has not yet been sent (exchange_order_id is NULL).
        # Otherwise, create a new attempt. This prevents a collision in cases where the WS
        # has not yet confirmed the 'open' status, even though the order has already been created on the exchange.
        cur_state = (self.current_order.exchange_state
                     if self.current_order else None)
        cur_eid = (self.current_order.exchange_order_id
                   if self.current_order else None)
        if cur_state == 'not_sent' and not cur_eid:
            row = self.current_order
            row.write({
                'price_requested': price,
                'amount_placed': amount,
                'last_sync_ns': self.get_time_ns(),
            })
        else:
            row = self._new_attempt(amount_placed=amount)
            row.write({
                'price_requested': price,
                'last_sync_ns': self.get_time_ns(),
            })

        try:
            self.order_opened.clear()
            self.active_order = await self.bot.order_manager.create_limit_order(
                self.plan['side'], amount, price,
                self.plan['position_id'], self.plan['signal_number'],
                row.attempt_n,
                reduce_only=self.plan['reduce_only'],
            )
            self.active_client_order_id = row.client_order_id

            # Record as 'open' immediately — otherwise, a subsequent 'reorder' might reuse it.
            # Capture the actual amount from CCXT (as it may have been rounded).
            write_vals = {
                'exchange_order_id': (self.active_order.get('id')
                                      if isinstance(self.active_order, dict)
                                      else None),
                'exchange_state': 'open',
                'last_sync_ns': self.get_time_ns(),
            }
            if isinstance(self.active_order, dict):
                actual_amount = self.active_order.get('amount')
                if actual_amount:
                    try:
                        write_vals['amount_placed'] = float(actual_amount)
                    except (TypeError, ValueError):
                        pass
            row.write(write_vals)

           # The REST call has already returned a status — if it’s a terminal status, we process it immediately.
            rest_status = (self.active_order.get('status')
                           if isinstance(self.active_order, dict) else None)
            if rest_status in ('closed', 'canceled', 'rejected', 'expired'):
                await self.process_order_update(self.active_order)
                return True
            return True
        except Exception as e:
            err_str = str(e)
            if self.plan['reduce_only'] and (
                    '-2022' in err_str or 'ReduceOnly Order is rejected' in err_str):
                self.logger.warning(
                    f"{self._log_prefix()} reduce-only rejected — "
                    f"position already flat")
                row.write({
                    'exchange_state': 'filled',
                    'reject_reason': 'position_already_flat',
                    'amount_filled': 0.0,
                    'avg_fill_price': self.last_bid or price or 0.0,
                    'last_sync_ns': self.get_time_ns(),
                })
                await self.bot._send_feedback_for_row(row)
                if self.reorder_task and not self.reorder_task.done():
                    self.reorder_task.cancel()
                return False

            #E2-dop: critical error log — prefix is ​​mandatory.
            self.logger.error(
                f"{self._log_prefix()} Place order failed: {e}",
                exc_info=True)
            reason = classify_exception(e)
            row.write({
                'exchange_state': 'rejected',
                'reject_reason': reason,
                'reject_reason_raw': err_str[:255],
                'last_sync_ns': self.get_time_ns(),
            })
            if reason == FailReason.RATE_LIMIT:
                await asyncio.sleep(1.0)
            #breaker
            breaker = classify_breaker_reason(e)
            if breaker:
                try:
                    await self.bot._register_breaker_failure(breaker)
                except Exception:
                    pass
            if self.reorder_task and not self.reorder_task.done():
                self.reorder_task.cancel()
            return False

    # ---------- cancel-confirm invariant ----------

    def _terminal_event_for(self, cid):
        ev = self._terminal_by_cid.get(cid)
        if ev is None:
            ev = asyncio.Event()
            self._terminal_by_cid[cid] = ev
        return ev

    def _signal_terminal(self, cid):
        ev = self._terminal_by_cid.get(cid)
        if ev is not None:
            ev.set()

    async def _await_terminal_state(self, order_id, cid,
                                    timeout_sec=CANCEL_CONFIRM_TIMEOUT_SEC):
        """Wait for the order to reach a terminal state.

        Event-based: A WebSocket event transitions the record to a terminal state → immediate
        return. A fallback `fetch_order` call is performed every `CANCEL_CONFIRM_POLL_SEC`
        as a safeguard against missed WebSocket events.
        """
        if DRY_RUN:
            return True
            
        if (timeout_sec == CANCEL_CONFIRM_TIMEOUT_SEC
                and getattr(self.bot, '_stopping', False)):
            timeout_sec = 1.0
        row = self.cid_to_row.get(cid) or self.current_order
        if row is not None and row.exchange_state in TERMINAL_STATES:
            return True

        ev = self._terminal_event_for(cid)
        deadline_ns = self.get_time_ns() + int(timeout_sec * 1e9)
        last_fetch_ns = 0

        while self.get_time_ns() < deadline_ns:
            #Quick path: WS event
            try:
                await asyncio.wait_for(ev.wait(), timeout=CANCEL_CONFIRM_POLL_SEC)
                if row is not None and row.exchange_state in TERMINAL_STATES:
                    return True
                ev.clear()
            except asyncio.TimeoutError:
                pass

           # Fallback: fetch_order, but no more frequently than once every CANCEL_CONFIRM_POLL_SEC
            now = self.get_time_ns()
            if (now - last_fetch_ns) / 1e9 >= CANCEL_CONFIRM_POLL_SEC:
                last_fetch_ns = now
                try:
                    order = await self.bot.order_manager.fetch_order(order_id, cid)
                    if order:
                        await self.process_order_update(order)
                        if row is not None and row.exchange_state in TERMINAL_STATES:
                            return True
                except Exception as e:
                    self.logger.warning(
                        f"{self._log_prefix()} await_terminal fetch failed "
                        f"for {order_id}: {e}")
        return False

        # ---------- watchdog ----------

    async def _order_watchdog_loop(self):
        if DRY_RUN:
            self.logger.debug("[DRY-RUN] watchdog disabled")
            return
            
        interval = max(self.bot.reorder_interval / 2, 0.05)
        threshold_ns = int(self.bot.reorder_interval * 2 * 1_000_000_000)
        consecutive_failures = 0

        while not self.filled and not self._cleaned:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            if self._cleaned or self.filled:
                break
            if not self.active_order or not self.active_client_order_id:
                consecutive_failures = 0
                continue
            order_id = self.active_order.get('id')
            if not order_id:
                consecutive_failures = 0
                continue
            silent_ns = self.get_time_ns() - self.last_ws_update_ns
            if silent_ns < threshold_ns:
                consecutive_failures = 0
                continue
            try:
                order = await self.bot.order_manager.fetch_order(
                    order_id, self.active_client_order_id)
                if order:
                    await self.process_order_update(order)
                    self.last_ws_update_ns = self.get_time_ns()
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                self.logger.warning(
                    f"{self._log_prefix()} order watchdog fetch failed "
                    f"({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): {e}")
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    return

    # ---------- price helpers ----------

    def _get_best_price(self):
        stale_sec = int(self.bot.config.get('stale_price_sec', 5) or 0)
        if stale_sec > 0:
            stale_ns = stale_sec * 1_000_000_000
            now_ns = self.get_time_ns()
            if self.last_price_recv_ns and \
                    (now_ns - self.last_price_recv_ns) > stale_ns:
                return None
        if self.plan['side'] == 'buy':
            return self.last_ask
        return self.last_bid

    def _determine_price(self):
        signal_price = self.plan.get('price')

        # Close/liquidation (< 0): best price, fallback to signal_price.
        if isinstance(self.plan['signal_number'], int) and self.plan['signal_number'] < 0:
            return self._get_best_price() or signal_price

        current_time_ns = self.get_time_ns()

        if current_time_ns < self.phase1_end_ns:
            best = self._get_best_price()
            # The price stream may be unavailable (WS stall) — we use the
            # grid price as a fallback. Otherwise, the order will never be placed.
            if best is None:
                return signal_price
            if signal_price is not None:
                if self.plan['side'] == 'buy':
                    return signal_price if signal_price < best else best
                else:
                    return signal_price if signal_price > best else best
            return best

        # Phase 2 — best, fallback to signal_price.
        return self._get_best_price() or signal_price

    @staticmethod
    def _signed_position(p):
        """Return signed position size.

        Binance USDT-M via ccxt may return contracts > 0 for both sides,
        with direction in p['side']. Prefer info.positionAmt (signed),
        fallback to side.
        """
        info = p.get('info') or {}
        for key in ('positionAmt', 'positionAmtStr'):
            if key in info:
                try:
                    return float(info[key])
                except (TypeError, ValueError):
                    pass

        try:
            c = float(p.get('contracts') or 0)
        except (TypeError, ValueError):
            c = 0.0

        side = (p.get('side') or '').lower()
        if side == 'short':
            return -abs(c)
        if side == 'long':
            return abs(c)
        return c

    def update_price(self, bid, ask, timestamp_ns):
        self.last_bid = bid
        self.last_ask = ask
        self.last_price_ns = timestamp_ns
        # TBOT UTC of receive — used for stale check
        self.last_price_recv_ns = self.get_time_ns()
        best = self._get_best_price()
        if best is not None and best != self._last_seen_best:
            self._last_seen_best = best
            self.price_changed.set()

    # ---------- order updates ----------

    def _classify_fill_state(self, filled: float, placed_hint: float) -> str:
        """'Filled' vs. 'partial' status, accounting for CCXT rounding.

        If 'filled' and 'placed' are close—within a single precision step—
        we consider it 'filled'; otherwise, it is 'partial'.
        """
        if placed_hint <= 0:
            return 'filled' if filled > 0 else 'partial'
        step = None
        market = getattr(self, '_cached_market', None)
        if market:
            step = (market.get('precision') or {}).get('amount')
        if step and filled + step * 0.5 >= placed_hint:
            return 'filled'
        if filled >= placed_hint * (1 - REL_TOL):
            return 'filled'
        return 'partial'

    async def process_order_update(self, order_data):
        if self._cleaned:
            return
        self.last_ws_update_ns = self.get_time_ns()

        cid = order_data.get('clientOrderId')
        row = self.cid_to_row.get(cid)
        if row is None:
            return

        # guard — do not overwrite the terminal line if
        # `cid` no longer matches `current_order` (reorder-race).
        if (self.current_order is not None
                and row.id != self.current_order.id
                and row.exchange_state in TERMINAL_STATES):
            return

        status = order_data.get('status')
        now = self.get_time_ns()

        if status == 'closed':
            filled = float(order_data.get('filled', 0) or 0)
            placed = float(row.amount_placed or 0)
            new_state = self._classify_fill_state(filled, placed)
            fee = order_data.get('fee')
            fee_cost = None
            if isinstance(fee, dict):
                fee_cost = fee.get('cost')
            elif isinstance(fee, (int, float)):
                fee_cost = fee
            row.write({
                'exchange_state': new_state,
                'amount_filled': filled,
                'avg_fill_price': order_data.get('average'),
                'fee': fee_cost,
                'last_sync_ns': now,
            })
            # A successful fill resets the session-scoped breaker counter.
            # Do not reset while in a 'paused' state. A late WebSocket fill
            # for an order that has already been canceled—arriving during a pause—
            # must not prematurely "clear" an 'invalid_order_persistent' status.
            if (filled > 0
                    and not getattr(self.bot, 'paused', False)
                    and hasattr(self.bot, '_on_order_success')):
                self.bot._on_order_success()
            if new_state in TERMINAL_STATES:
                self._signal_terminal(cid)
            if row.id == getattr(self.current_order, 'id', None):
                if filled > self.executed_volume:
                    self.executed_volume = filled
                    self.executed_price = order_data.get('average')
                if fee_cost is not None:
                    self.last_fee = fee_cost
                self.active_order = None
                total = self._total_filled()
                threshold = float(self.plan['amount_placed']) * (1 - REL_TOL)
                if total >= threshold:
                    self.filled = True
                    self.filled_event.set()
                self._bump_ui()

        elif status == 'canceled':
            row.write({
                'exchange_state': 'canceled',
                'amount_filled': float(order_data.get('filled', 0) or 0),
                'last_sync_ns': now,
            })
            self._signal_terminal(cid)
            if row.id == getattr(self.current_order, 'id', None):
                self.active_order = None
                self.order_canceled.set()

        elif status == 'open':
            row.write({
                'exchange_state': 'open',
                'last_sync_ns': now,
            })
            if row.id == getattr(self.current_order, 'id', None):
                self.active_order = order_data
                self.order_opened.set()
                self._record_ws_latency('open', order_data)

        elif status == 'rejected':
            info = order_data.get('info', {}) or {}
            row.write({
                'exchange_state': 'rejected',
                'reject_reason': 'order_rejected',
                'reject_reason_raw': str(info.get('rejectReason', ''))[:255],
                'last_sync_ns': now,
            })
            self._signal_terminal(cid)
            if row.id == getattr(self.current_order, 'id', None):
                self.filled = False
                self.filled_event.set()
                if self.reorder_task and not self.reorder_task.done():
                    self.reorder_task.cancel()

        elif status == 'expired':
            row.write({
                'exchange_state': 'expired',
                'last_sync_ns': now,
            })
            self._signal_terminal(cid)
            if row.id == getattr(self.current_order, 'id', None):
                self.filled = False
                self.filled_event.set()
                if self.reorder_task and not self.reorder_task.done():
                    self.reorder_task.cancel()

        elif status == 'partially_filled':
            filled = float(order_data.get('filled', 0) or 0)
            placed = float(row.amount_placed or 0)
            new_state = self._classify_fill_state(filled, placed)
            row.write({
                'exchange_state': new_state,
                'amount_filled': filled,
                'avg_fill_price': order_data.get('average'),
                'last_sync_ns': now,
            })
            if row.id == getattr(self.current_order, 'id', None):
                if filled > self.executed_volume:
                    self.executed_volume = filled
                    self.executed_price = order_data.get('average')
                total = self._total_filled()
                threshold = float(self.plan['amount_placed']) * (1 - REL_TOL)
                if total >= threshold:
                    self.filled = True
                    self.filled_event.set()

    def _record_ws_latency(self, action, order_data):
        ws_ts_ms = order_data.get('timestamp')
        if not ws_ts_ms:
            return
        ws_ts_ns = int(ws_ts_ms * 1_000_000)
        delta_ns = self.get_time_ns() - ws_ts_ns
        try:
            self.bot.env['execution.stats'].create([{
                'action': f'ws_{action}',
                'latency_ms': delta_ns / 1e6,
                'timestamp': self.get_time_ns(),
                'tbot_utc_stamp_ns': self.get_time_ns(),
            }])
        except Exception:
            pass

    # ---------- finalize ----------

    async def _finalize(self):
        if self._cleaned:
            return
        if self.current_order is None:
            self._cleanup()
            return

        # 1. Canceling an active order with confirmation
        state = self.current_order.exchange_state
        if (self.active_order and not self.filled
                and state not in TERMINAL_STATES):
            old_oid = self.active_order['id']
            old_cid = self.active_client_order_id
            try:
                await self.bot.order_manager.cancel_order(old_oid, old_cid)
            except Exception as e:
                self.logger.error(f"Final cancel failed: {e}", exc_info=True)
            self.active_order = None

            confirmed = await self._await_terminal_state(old_oid, old_cid)
            if not confirmed:
                self.logger.warning(
                    f"{self._log_prefix()} finalize: condition not "
                    f"confirmed, leaving as pending"
                )
                self.current_order.write({
                    'feedback_state': 'pending',
                    'feedback_last_error': 'cancel_unconfirmed',
                    'last_sync_ns': self.get_time_ns(),
                })
                self._cleanup()
                return

        # 2. Terminal state — just need to add the finishing touch (feedback)
        state = self.current_order.exchange_state
        if state in TERMINAL_STATES:
            # For a 'reduce-only' close that is only partially filled on the exchange —
            # complete the position using a market order *before* the feedback reaches the grid. 
            # Otherwise, the grid
            # will get stuck indefinitely in the 'any_close-guard' state.
            if (self.plan.get('reduce_only')
                    and state in ('canceled', 'expired')
                    and float(self.current_order.amount_filled or 0) > 0):
                closed = await self._maybe_market_close_remainder()
                if closed:
                    self._cleanup()
                    return
                # closed=False only during shutdown — fall back to the standard behavior
                # partial-forward remains as is
            if self.current_order.feedback_state != 'sent':
                await self.bot._send_feedback_for_row(self.current_order)
            self._cleanup()
            return

        # 3. Standard finalization — based on the total sum of fills
        total_filled = self._total_filled()
        placed = float(self.plan['amount_placed'] or 0)
        if total_filled > 0:
            threshold = placed * (1 - REL_TOL) if placed > 0 else 0
            result = 'filled' if total_filled >= threshold else 'partial'

            # partial reduce-only — an attempt to close the remaining position via a market order.
            if result == 'partial' and self.plan.get('reduce_only'):
                closed = await self._maybe_market_close_remainder()
                if closed:
                    self._cleanup()
                    return
                # closed=False → shutdown; keep 'partial' as is

            self.current_order.write({
                'exchange_state': result,
                'amount_filled': total_filled,
                'avg_fill_price': self._weighted_avg_fill_price(),
                'fee': self.last_fee,
                'last_sync_ns': self.get_time_ns(),
            })
            await self.bot._send_feedback_for_row(self.current_order)
        else:
            self.current_order.write({
                'exchange_state': 'canceled',
                'reject_reason': 'timeout',
                'last_sync_ns': self.get_time_ns(),
            })
            await self.bot._send_feedback_for_row(self.current_order)
        self._cleanup()

    # ---------- cleanup ----------

    def _cleanup(self):
        if self._cleaned:
            return
        self._cleaned = True
        self.filled_event.set()
        self.order_opened.set()
        self.order_canceled.set()

        self.unregister_all()
        self._terminal_by_cid.clear()
        self.current_order = None

        key = (self.plan['position_id'], self.plan['signal_number'])
        self.bot.active_managers.pop(key, None)
        self.bot.signal_tasks.pop(key, None)
        
    async def _emulate_fill(self, price: float, amount: float,
                             reason: str = 'dry_run',
                             delay_sec: float = 0.05) -> None:
        """Synthesize a filled execution without touching the exchange.

        Educational mode is the only caller. Writes a synthetic `filled`
        row, signals all waiters (`filled_event`, `order_opened`,
        `order_canceled`, terminal-by-cid), and lets the normal
        `_finalize` flow send feedback to the grid.

        Never calls OrderManager or the broker.
        """
        if self.current_order is None:
            self.logger.error(
                f"{self._log_prefix()} _emulate_fill: no current_order")
            return
        if self.current_order.exchange_state in TERMINAL_STATES:
            self.logger.debug(
                f"{self._log_prefix()} _emulate_fill: already terminal")
            return
        try:
            await asyncio.sleep(delay_sec)
        except asyncio.CancelledError:
            raise

        cid = self.current_order.client_order_id
        self.current_order.write({
            'exchange_order_id': f"dry-run-{cid}",
            'exchange_state': 'filled',
            'amount_filled': float(amount),
            'amount_placed': float(amount),
            'avg_fill_price': float(price),
            'last_sync_ns': self.get_time_ns(),
        })
        self.executed_volume = float(amount)
        self.executed_price = float(price)
        self.filled = True
        self.filled_event.set()
        self.order_opened.set()
        self.order_canceled.set()
        if cid:
            self._signal_terminal(cid)
        self._bump_ui()
        self.logger.info(
            f"{self._log_prefix()} [DRY-RUN] synthetic fill: "
            f"price={price} amount={amount} reason={reason}")