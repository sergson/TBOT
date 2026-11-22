# modules/execution/models.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.

from core.database import (
    Model, Char, Integer, Float, Boolean, Selection,
    get_bot_config, update_bot_config, update_bot_status, get_all_bots,
)
import asyncio
from collections import deque
from core import auto_reg, BaseBot
from core.logger import perf_logger
from .lib.order_manager import OrderManager, DRY_RUN
from .lib.signal_manager import SignalManager

# --- feedback ---
FEEDBACK_MAX_ATTEMPTS = 20
FEEDBACK_MAX_AGE_NS = 24 * 3600 * 1_000_000_000
FEEDBACK_RETRY_INTERVAL_SEC = 5
FEEDBACK_BATCH_LIMIT = 10

# --- cleanup / retention ---
CLEANUP_INTERVAL_NS = 60 * 1_000_000_000
DEFAULT_MAX_ORDER_ROWS = 10000
TERMINAL_RETENTION_SEC = 24 * 3600

PRICE_TIERS = [
    (0, 3 * 60, 0),
    (3 * 60, 60 * 60, 30),
    (60 * 60, 6 * 60 * 60, 300),
    (6 * 60 * 60, 24 * 60 * 60, 1800),
]
PRICE_RETENTION_SEC = 24 * 60 * 60
PRICE_MAX_ROWS = 10000
STATS_SAMPLE_SIZE = 500

LATENCY_AGGREGATE_INTERVAL_SEC = 300
LATENCY_AGGREGATE_SAMPLE_N = 200

ATTENTION_DOMAIN = [
    '&',
    '|', '|',
    ('exchange_state', '=', 'not_found'),
    '&', ('exchange_state', '=', 'filled'),
         ('feedback_state', 'in', ['abandoned', 'delivery_failed']),
    '&', ('exchange_state', '=', 'not_sent'),
         ('reject_reason', '=', 'recovery_no_ref'),
    ('ui_acknowledged_at_ns', '=', 0),
]
ATTENTION_FIELDS = ['id', 'position_id', 'signal_number', 'attempt_n',
                     'exchange_state', 'reject_reason']

RECOVERY_MAX_PAIRS = 20
RECOVERY_TIMEOUT_SEC = 60

PHASE4_RATIO = 0.05
PHASE4_MIN_SEC = 1.0

BREAKER_INVALID_ORDER_THRESHOLD = 3
BREAKER_INSUFFICIENT_FUNDS_THRESHOLD = 1
BREAKER_NETWORK_THRESHOLD = 5
PAUSED_COOLDOWN_SEC = 60
ORDER_RECONCILE_INTERVAL_SEC = 5

REPAUSE_THRESHOLD = 3

PANIC_SIGNAL_NUMBER_FALLBACK = -999_999

logger = perf_logger.get_logger('execution_bot', 'execution')


class ExecutionOrder(Model):
    _name = 'execution.order'
    _table = 'execution_orders'
    id = Integer(primary_key=True, autoincrement=True, required=True)
    position_id = Char(required=True, index=True)
    signal_number = Integer(index=True)
    attempt_n = Integer(required=True)
    exchange_order_id = Char(index=True)
    client_order_id = Char()
    symbol = Char(required=True)
    side = Selection([('buy', 'Buy'), ('sell', 'Sell')], required=True)
    order_type = Selection([('limit', 'Limit'), ('market', 'Market')])
    reduce_only = Boolean(default=False)
    price_requested = Float()
    amount_requested = Float()
    amount_placed = Float()
    amount_filled = Float()
    avg_fill_price = Float()
    fee = Float()
    signal_vol_rate = Float(default=0.0)
    signal_timestamp_scale = Char(default='tbot_utc_ns')
    signal_smart = Boolean(default=False)
    signal_retry = Integer(default=0)
    exchange_state = Selection([
        ('not_sent', 'Not sent'), ('open', 'Open'), ('partial', 'Partial'),
        ('filled', 'Filled'), ('canceled', 'Canceled'),
        ('expired', 'Expired'), ('rejected', 'Rejected'),
        ('not_found', 'Not found'),
    ], default='not_sent', index=True)
    reject_reason = Char()
    reject_reason_raw = Char()
    signal_timestamp = Integer()
    deadline_ns = Integer()
    created_ns = Integer()
    last_sync_ns = Integer()
    feedback_state = Selection([
        ('none', 'None'), ('pending', 'Pending'), ('sent', 'Sent'),
        ('abandoned', 'Abandoned'), ('delivery_failed', 'Delivery failed'),
        ('superseded', 'Superseded'),
    ], default='none', index=True)
    feedback_attempts = Integer(default=0)
    feedback_last_error = Char()
    ui_acknowledged_at_ns = Integer(default=0)


class PriceTick(Model):
    _name = 'execution.price'
    _table = 'execution_prices'
    id = Integer(primary_key=True, autoincrement=True, required=True)
    timestamp = Integer(required=True, index=True)
    tbot_utc_stamp_ns = Integer(index=True)
    bid = Float()
    ask = Float()
    symbol = Char(required=True)


class ExecutionStats(Model):
    _name = 'execution.stats'
    _table = 'execution_stats'
    id = Integer(primary_key=True, autoincrement=True, required=True)
    action = Char()
    latency_ms = Float()
    timestamp = Integer(required=True)
    tbot_utc_stamp_ns = Integer(index=True)


@auto_reg
class ExecutionBot(BaseBot):
    _name = "execution.bot"
    _inherit = "base.bot"

    def __init__(self, bot_id, manager=None):
        super().__init__(bot_id, manager)
        self.config = get_bot_config(bot_id)
        self.logger = perf_logger.get_logger(
            f"execution_{bot_id}", "execution")
        self._resolve_exchange_provider()

        self.source_handle = None
        self.broker_handle = None
        self._broker_id = None
        self._broker_restart_last_ns = 0
        self.order_manager = None

        self.active_managers = {}
        self.signal_tasks = {}
        self.order_id_index = {}

        self.price_buffer = deque(maxlen=10000)

        self.source_timeout = float(self.config.get('source_timeout', 30))
        self.poll_interval = self._calc_poll_interval()
        self.reorder_interval = self._calc_reorder_interval()
        self.task = None
        self.flush_task = None
        self.feedback_handle = None
        self.feedback_capabilities = None
        self.ui_event_counter = 0
        self.feedback_task = None
        self.latency_task = None
        self.time_scale_name = 'tbot_utc_ns'
        self.poll_price_task = None
        self.poll_updates_task = None

        self.blocked_positions = set()
        self.execution_reserve_sec = 0.0

        self.paused = False
        self.paused_reason = None
        self.paused_since_ns = 0
        self.paused_check_at_ns = 0

        self._stopping = False
        self.reconciler_task = None
        self._breaker_counters = {
            'auth': 0, 'funds': 0, 'invalid_order': 0, 'network': 0,
        }
        self.paused_task = None
        self.balance_task = None
        self._balance_err_logged = False
        self._paused_attempt = 0
        self._last_paused_log_ns = 0
        self._feedback_tasks: set = set()
        self._balance_snapshot_counter = 0
        self._session_invalid_order_count = 0

        self._panic_signal_number = int(
            (self.config.get('signal_constants') or {}).get(
                'panic_signal_number', PANIC_SIGNAL_NUMBER_FALLBACK))

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
                self.broker_handle.get("get_broker_status"), timeout=5)
            return bool(info.get('healthy'))
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

    async def _register_with_broker(self) -> bool:
        if DRY_RUN:
            return True
        if not self.broker_handle:
            return False
        key_id = self.config.get('broker_key_id')
        if not key_id:
            return False
        try:
            await self.broker_handle.set("register_key_owner", {
                'key_id': key_id,
                'owner_bot_id': self.bot_id,
            })
            return True
        except Exception as e:
            self.logger.warning(f"register_key_owner failed: {e}")
            return False

    # ---------- capabilities (unchanged, still relevant) ----------

    def _bump_ui_event(self):
        self.ui_event_counter += 1

    def get_capabilities(self):
        return {
            "execution_stats": {
                "keywords": ["execution_stats", "latency", "fills"],
                "getter": self._get_execution_stats, "setter": None},
            "active_signals": {
                "keywords": ["active_signals", "execution_active"],
                "getter": self._get_active_signals, "setter": None},
            "attention_signals": {
                "keywords": ["attention_signals", "needs_attention"],
                "getter": self._get_attention_signals, "setter": None},
            "failures_summary": {
                "keywords": ["failures", "errors", "telemetry"],
                "getter": self._get_failures_summary, "setter": None},
            "latency_stats": {
                "keywords": ["latency_stats", "latency"],
                "getter": self._get_latency_stats, "setter": None},
            "db_fill_stats": {
                "keywords": ["db_fill_stats", "db_fill", "retention"],
                "getter": self._get_db_fill_stats, "setter": None},
        }

    async def _get_execution_stats(self):
        return self.env['execution.stats'].search([]).read()

    async def _get_active_signals(self):
        return self.env['execution.order'].search([
            ('exchange_state', 'in', ['open', 'partial'])
        ]).read()

    async def _get_attention_signals(self):
        try:
            cand = self.env['execution.order'].search(
                ATTENTION_DOMAIN, order='id DESC', limit=50)
            if not cand:
                return []
            return cand.read(ATTENTION_FIELDS)
        except Exception as e:
            self.logger.error(f"attention_signals failed: {e}", exc_info=True)
            return []

    async def _get_db_fill_stats(self):
        now = self.get_time_ns()
        cached = getattr(self, '_db_fill_cache', None)
        if cached and (now - cached[0]) < 5_000_000_000:
            return cached[1]
        try:
            result = {
                'orders': {
                    'rows': self.env['execution.order'].search_count([]),
                    'max': int(self.config.get('max_order_rows',
                                                DEFAULT_MAX_ORDER_ROWS)),
                },
                'prices': {
                    'rows': self.env['execution.price'].search_count([]),
                    'max': PRICE_MAX_ROWS,
                },
                'stats': {
                    'rows': self.env['execution.stats'].search_count([]),
                    'max': int(self.config.get('stats_sample_size',
                                                STATS_SAMPLE_SIZE)),
                },
            }
            self._db_fill_cache = (now, result)
            return result
        except Exception as e:
            self.logger.error(f"db_fill_stats: {e}", exc_info=True)
            return {}

    async def _get_failures_summary(self):
        from collections import Counter
        recs = self.env['execution.order'].search([
            ('exchange_state', 'in',
             ['rejected', 'expired', 'not_found', 'not_sent'])
        ]).read()
        return dict(Counter(r.get('reject_reason') or 'unknown' for r in recs))

    async def _get_latency_stats(self):
        cfg = get_bot_config(self.bot_id) or {}
        return cfg.get('latency_stats') or {}

    # ---------- helpers ----------

    def _calc_poll_interval(self):
        divisor = max(
            int(self.config.get('poll_divisor') or 10), 1)
        return max(self.source_timeout / divisor, 0.001)

    def _calc_reorder_interval(self):
        # Watchdog interval for WS-stream silence detection only.
        # Reorder cadence itself is driven by broker_poll_divisor via
        # SignalManager._compute_phases; this value feeds
        # _order_watchdog_loop, which detects stalls in the WS order
        # stream and forces a fetch_order. Hardcoded /5 — internal,
        # not user-facing.
        return max(self.source_timeout / 5, 0.001)

    async def _get_target_capabilities(self, target_id):
        if not self.manager:
            return None
        target = self.manager.bots.get(target_id)
        if not target:
            return None
        try:
            caps = target.get_capabilities()
            if asyncio.iscoroutine(caps):
                caps = await caps
            return caps or None
        except Exception as e:
            self.logger.warning(
                f"Failed to get capabilities from {target_id}: {e}")
            return None

    def _latest_ref_price(self, sig):
        sp = sig.get('signal_price')
        if sp is not None:
            try:
                sp = float(sp)
                if sp > 0:
                    return sp
            except (TypeError, ValueError):
                pass
        if self.price_buffer:
            _ts, bid, ask, _tbot = self.price_buffer[-1]
            try:
                if bid and ask:
                    return (float(bid) + float(ask)) / 2.0
                if bid:
                    return float(bid)
                if ask:
                    return float(ask)
            except (TypeError, ValueError):
                pass
        return None

    # ---------- feedback (unchanged) ----------

    def _build_feedback_payload(self, row) -> dict:
        planned = float(row.amount_requested or 0.0)
        if planned > 0 and row.signal_vol_rate:
            real_vol_rate = (float(row.signal_vol_rate)
                             * float(row.amount_filled or 0.0) / planned)
        else:
            real_vol_rate = 0.0
        return {
            'position_id': row.position_id,
            'signal_number': row.signal_number,
            'signal_timestamp': row.signal_timestamp,
            'signal_timestamp_scale':
                row.signal_timestamp_scale or 'tbot_utc_ns',
            'real_price': row.avg_fill_price or row.price_requested or 0.0,
            'real_volume': real_vol_rate,
            'real_timestamp': self.get_time_ns(),
            'real_timestamp_scale': self.time_scale_name,
            'exchange_state': row.exchange_state,
            'reject_reason': row.reject_reason,
            'is_partial': row.exchange_state == 'partial',
            'signal_smart': bool(getattr(row, 'signal_smart', False)),
            'signal_retry': int(getattr(row, 'signal_retry', 0) or 0),
        }

    async def _build_feedback_payload_async(self, row) -> dict:
        """Build the feedback payload, correcting reduce-only fills
        against the actual exchange position.

        Binance reports a reduce-only order as fully 'filled' even when
        the real position was smaller than amount_placed (the excess
        cannot be opened). In that case `order.filled` overstates the
        true closed volume, and the feedback formula
            real_vol_rate = signal_vol_rate * amount_filled / amount_requested
        under-reports the close to the grid. Grid then keeps the -2 signal
        alive waiting for a remainder that no longer exists.
        """
        payload = self._build_feedback_payload(row)
        if DRY_RUN:
            return payload
        if not row.reduce_only or row.exchange_state != 'filled':
            return payload
        if not self.order_manager:
            return payload

        try:
            positions = await self.order_manager.fetch_positions([row.symbol])
        except Exception as e:
            self.logger.warning(
                f"feedback position probe for order {row.id}: {e}")
            return payload

        if positions is None:
            self.logger.warning(
                f"feedback position probe for order {row.id}: "
                f"returned None — keep payload as is")
            return payload

        net = 0.0
        nonzero = 0
        for p in positions:
            info = p.get('info') or {}
            amt = info.get('positionAmt')
            try:
                if amt is not None:
                    signed = float(amt)
                else:
                    c = float(p.get('contracts') or 0)
                    if (p.get('side') or '').lower() == 'short':
                        signed = -abs(c)
                    else:
                        signed = abs(c)
            except (TypeError, ValueError):
                continue

            if abs(signed) > 1e-12:
                nonzero += 1
            net += signed

        if nonzero > 1:
            self.logger.warning(
                f"feedback order {row.id}: multiple non-zero positions — "
                f"ambiguous, keep payload as is")
            return payload

        if abs(net) < 1e-9:
            payload['real_volume'] = float(row.signal_vol_rate or 0.0)
            self.logger.info(
                f"Feedback order {row.id} (pos={row.position_id} "
                f"sig={row.signal_number}): exchange flat — "
                f"overriding real_volume to {payload['real_volume']}")
        return payload

    async def _send_feedback_for_row(self, row) -> bool:
        if not self.feedback_handle:
            return False
        if 'record_execution' not in (self.feedback_capabilities or {}):
            return False
        payload = await self._build_feedback_payload_async(row)
        for attempt in range(3):
            try:
                await self.feedback_handle.set("record_execution", payload)
                row.write({
                    'feedback_state': 'sent',
                    'feedback_attempts': (row.feedback_attempts or 0) + 1,
                })
                self._bump_ui_event()
                return True
            except Exception as e:
                row.write({
                    'feedback_attempts': (row.feedback_attempts or 0) + 1,
                    'feedback_last_error': str(e)[:255],
                })
                self.logger.warning(
                    f"Feedback attempt {attempt+1} failed for order "
                    f"{row.id}: {e}")
                await asyncio.sleep(0.1 * (2 ** attempt))
        row.write({'feedback_state': 'pending'})
        return False

    def _send_feedback_fire_and_forget(self, row,
                                        name_prefix: str = "fb_ff") -> None:
        async def _run():
            try:
                await self._send_feedback_for_row(row)
            except Exception as _e:
                self.logger.warning(
                    f"fire-and-forget feedback failed for {row.id}: {_e}")
        try:
            task = asyncio.create_task(_run(), name=f"{name_prefix}_{row.id}")
            self._feedback_tasks.add(task)
            task.add_done_callback(self._feedback_task_done)
        except RuntimeError as e:
            self.logger.warning(
                f"fire-and-forget skipped: {e}; row {row.id} pending")

    def _feedback_task_done(self, task):
        self._feedback_tasks.discard(task)
        try:
            exc = task.exception()
        except (asyncio.CancelledError, Exception):
            return
        if exc is not None:
            self.logger.error(f"feedback task crashed: {exc}", exc_info=exc)

    async def _safe_finalize(self, mgr):
        try:
            await mgr._finalize()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            pos = mgr.plan.get('position_id') if mgr.plan else '?'
            sig = mgr.plan.get('signal_number') if mgr.plan else '?'
            self.logger.error(
                f"Finalize failed pos={pos} sig={sig}: {e}", exc_info=True)

    # ---------- breaker / paused (unchanged) ----------

    async def _register_breaker_failure(self, reason: str):
        if reason not in self._breaker_counters:
            return
        self._breaker_counters[reason] += 1
        threshold = self._breaker_threshold(reason)
        self.logger.warning(
            f"BREAKER: failure[{reason}] = {self._breaker_counters[reason]}"
            f"/{threshold}")
        if self._breaker_counters[reason] >= threshold:
            await self._enter_paused(reason)

    def _breaker_threshold(self, reason: str) -> int:
        spec = {
            'auth': (None, 1),
            'funds': ('breaker_insufficient_funds',
                      BREAKER_INSUFFICIENT_FUNDS_THRESHOLD),
            'invalid_order': ('breaker_invalid_order',
                              BREAKER_INVALID_ORDER_THRESHOLD),
            'network': ('breaker_network', BREAKER_NETWORK_THRESHOLD),
        }
        cfg_key, default = spec.get(reason, (None, 999))
        if cfg_key is None:
            return default
        try:
            v = int(self.config.get(cfg_key) or default)
            return v if v >= 1 else default
        except (TypeError, ValueError):
            return default

    async def _enter_paused(self, reason: str):
        if self.paused:
            return
        self.paused = True
        self.paused_reason = reason
        self.paused_since_ns = self.get_time_ns()
        self.paused_check_at_ns = (
            self.paused_since_ns + PAUSED_COOLDOWN_SEC * 1_000_000_000)
        self._paused_attempt = 0
        if reason == 'invalid_order':
            self._session_invalid_order_count += 1
            if self._session_invalid_order_count >= REPAUSE_THRESHOLD:
                self.paused_reason = 'invalid_order_persistent'
                self.paused_check_at_ns = 0
                self.logger.error(
                    f"BREAKER: invalid_order persistent "
                    f"({self._session_invalid_order_count} pauses) — "
                    f"auto-unpause disabled")
        try:
            update_bot_config(self.bot_id, {
                'execution_paused': {
                    'reason': self.paused_reason,
                    'since_ns': self.paused_since_ns,
                    'check_at_ns': self.paused_check_at_ns,
                    'attempt': 0,
                    'repause_count': self._session_invalid_order_count,
                }
            })
        except Exception as e:
            self.logger.error(f"Failed to persist paused state: {e}")
        update_bot_status(self.bot_id, 'paused')
        self.logger.error(f"BREAKER: entering PAUSED (reason={reason})")
        self._bump_ui_event()
        managers = list(self.active_managers.values())
        if managers:
            self._stopping = True
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *[self._safe_finalize(m) for m in managers],
                        return_exceptions=True),
                    timeout=5.0)
            except asyncio.TimeoutError:
                self.logger.warning(
                    f"BREAKER: {len(managers)} managers did not finalize in 5s")
            finally:
                self._stopping = False

    async def _can_unpause(self) -> bool:
        reason = self.paused_reason
        if reason == 'collector_unavailable':
            # Probe the broker via _ensure_broker without touching state.
            try:
                info = await asyncio.wait_for(
                    self.broker_handle.get("get_broker_status"), timeout=5)
                return bool(info.get('healthy'))
            except Exception:
                return False
        if reason == 'invalid_order':
            return True
        if reason == 'invalid_order_persistent':
            return False
        if reason in ('auth', 'funds', 'network'):
            try:
                await self.order_manager.fetch_balance()
                return True
            except Exception as e:
                self.logger.info(f"Unpause probe failed: {e}")
                return False
        return True

    async def _exit_paused(self):
        self.paused = False
        self.paused_reason = None
        self.paused_since_ns = 0
        self.paused_check_at_ns = 0
        self._paused_attempt = 0
        self._breaker_counters = {k: 0 for k in self._breaker_counters}
        try:
            update_bot_config(self.bot_id, {'execution_paused': None})
        except Exception as e:
            self.logger.warning(f"Failed to clear paused state: {e}")
        update_bot_status(self.bot_id, 'running')
        self.logger.warning("BREAKER: resumed — leaving PAUSED")
        self._bump_ui_event()
        try:
            await self._flush_pending_feedback_once(fire_and_forget=False)
            await self._reconcile_orders()
        except Exception as e:
            self.logger.warning(f"Post-unpause reconcile failed: {e}")

    def _on_order_success(self):
        if self._session_invalid_order_count:
            self.logger.info(
                f"Breaker: successful fill — resetting invalid_order "
                f"counter ({self._session_invalid_order_count})")
        self._session_invalid_order_count = 0

    async def _paused_watchdog_loop(self):
        while self.running:
            try:
                await asyncio.sleep(5.0)
                if not self.running:
                    break
                if not self.paused:
                    continue
                if self.paused_reason == 'invalid_order_persistent':
                    continue
                now_ns = self.get_time_ns()
                if now_ns < self.paused_check_at_ns:
                    continue
                if await self._can_unpause():
                    await self._exit_paused()
                else:
                    self.paused_check_at_ns = (
                        now_ns + PAUSED_COOLDOWN_SEC * 1_000_000_000)
                    self._paused_attempt += 1
                    try:
                        update_bot_config(self.bot_id, {
                            'execution_paused': {
                                'reason': self.paused_reason,
                                'since_ns': self.paused_since_ns,
                                'check_at_ns': self.paused_check_at_ns,
                                'attempt': self._paused_attempt,
                                'repause_count':
                                    self._session_invalid_order_count,
                            }
                        })
                    except Exception:
                        pass
                    self.logger.info(
                        f"BREAKER: still paused ({self.paused_reason}), "
                        f"next check in {PAUSED_COOLDOWN_SEC}s")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.error(f"Paused watchdog error: {e}",
                                   exc_info=True)

    # ---------- feedback retry ----------

    async def _flush_pending_feedback_once(self, fire_and_forget: bool = True):
        try:
            now_ns = self.get_time_ns()
            min_created_ns = (
                now_ns - FEEDBACK_RETRY_INTERVAL_SEC * 1_000_000_000)
            pending = self.env['execution.order'].search([
                '&', '&',
                ('feedback_attempts', '<', FEEDBACK_MAX_ATTEMPTS),
                ('created_ns', '<', min_created_ns),
                '|',
                ('feedback_state', '=', 'pending'),
                '&',
                ('feedback_state', '=', 'none'),
                ('exchange_state', 'in', ['filled', 'canceled', 'rejected',
                                          'expired', 'not_found']),
            ], order='id ASC', limit=FEEDBACK_BATCH_LIMIT)
            for row in pending:
                if row.created_ns and \
                        (now_ns - row.created_ns) > FEEDBACK_MAX_AGE_NS:
                    self.logger.warning(
                        f"Feedback abandoned (age > 24h): order_id={row.id}")
                    row.write({
                        'feedback_state': 'delivery_failed',
                        'feedback_last_error':
                            'delivery_failed: age > 24h',
                    })
                    continue
                if fire_and_forget:
                    self._send_feedback_fire_and_forget(
                        row, name_prefix="fb_retry")
                else:
                    await self._send_feedback_for_row(row)
        except Exception as e:
            self.logger.error(
                f"Pending feedback flush: {e}", exc_info=True)

    # ---------- recovery (unchanged) ----------

    async def _recover_in_flight(self):
        if DRY_RUN:
            self.logger.info("in-flight recovery skipped")
            return
        rows = self.env['execution.order'].search([
            '&',
            ('exchange_state', 'in', ['open', 'partial', 'not_sent']),
            '!',
            ('feedback_state', 'in',
             ['sent', 'abandoned', 'delivery_failed', 'superseded']),
        ], order='id ASC', limit=RECOVERY_MAX_PAIRS * 8)
        groups = {}
        for r in rows:
            key = (r.position_id, r.signal_number)
            groups.setdefault(key, []).append(r)
        keys = list(groups.keys())[:RECOVERY_MAX_PAIRS]
        if not keys:
            return
        self.logger.info(f"Recovery: {len(keys)} in-flight groups")
        sem = asyncio.Semaphore(5)

        async def _recover_one(_key):
            async with sem:
                if _key in self.active_managers:
                    return
                grp = sorted(groups[_key],
                             key=lambda _r: _r.attempt_n or 0, reverse=True)
                latest = grp[0]
                try:
                    await self._recover_pair(latest)
                except Exception as e:
                    self.logger.error(
                        f"Recovery failed pos={_key[0]} sig={_key[1]}: {e}",
                        exc_info=True)
                    try:
                        latest.write({
                            'reject_reason': 'recovery_no_ref',
                            'last_sync_ns': self.get_time_ns(),
                        })
                    except Exception:
                        pass

        await asyncio.gather(
            *[_recover_one(k) for k in keys], return_exceptions=True)

    async def _recover_pair(self, row):
        pos = row.position_id
        sig = row.signal_number
        key = (pos, sig)

        if row.exchange_state in ('open', 'partial'):
            mgr = SignalManager.from_row(
                self, row, self.logger, time_provider=self.get_time_ns)
            if not row.exchange_order_id:
                self.logger.warning(
                    f"Recovery: open/partial without order_id "
                    f"pos={pos} sig={sig} — skip, reconciler will pick up")
                row.write({
                    'reject_reason': 'recovery_no_ref',
                    'last_sync_ns': self.get_time_ns(),
                })
                return
            try:
                order = await self.order_manager.fetch_order(
                    row.exchange_order_id, row.client_order_id)
            except Exception as e:
                self.logger.warning(
                    f"Recovery fetch failed pos={pos} sig={sig}: {e}")
                return
            if not order:
                return
            status = order.get('status')
            if status in ('open', 'partially_filled'):
                mgr.active_order = order
                mgr.active_client_order_id = row.client_order_id
            else:
                now = self.get_time_ns()
                if status == 'closed':
                    filled = float(order.get('filled', 0) or 0)
                    placed = float(row.amount_placed or 0)
                    threshold = placed * (1 - 1e-6) if placed > 0 else 0
                    new_state = 'filled' if filled >= threshold else 'partial'
                    row.write({
                        'exchange_state': new_state,
                        'amount_filled': filled,
                        'avg_fill_price': order.get('average')
                                           or order.get('price'),
                        'last_sync_ns': now,
                    })
                else:
                    row.write({
                        'exchange_state': status,
                        'amount_filled': float(order.get('filled', 0) or 0),
                        'last_sync_ns': now,
                    })
                await self._send_feedback_for_row(row)
                return
            self.active_managers[key] = mgr
            task = asyncio.create_task(
                mgr.resume_from_row(), name=f"recover_{pos}_{sig}")
            mgr.task = task
            task.add_done_callback(
                lambda t, k=key: self._on_signal_task_done(k, t))
            self.signal_tasks[key] = task
            self.logger.info(
                f"Recovered open/partial pos={pos} sig={sig} "
                f"att={row.attempt_n}")
            return

        now_ns = self.get_time_ns()
        deadline_ns = row.deadline_ns or 0
        if deadline_ns and now_ns >= deadline_ns:
            row.write({
                'exchange_state': 'not_sent',
                'reject_reason': 'stale_recovery',
                'last_sync_ns': now_ns,
            })
            await self._send_feedback_for_row(row)
            return
        mgr = SignalManager.from_row(
            self, row, self.logger, time_provider=self.get_time_ns)
        self.active_managers[key] = mgr
        task = asyncio.create_task(
            mgr.run(), name=f"recover_{pos}_{sig}")
        mgr.task = task
        task.add_done_callback(
            lambda t, k=key: self._on_signal_task_done(k, t))
        self.signal_tasks[key] = task
        self.logger.info(f"Recovered not_sent pos={pos} sig={sig}")

    # ---------- reconciler ----------

    async def _order_reconciler_loop(self):
        if DRY_RUN:
            while self.running:
                await asyncio.sleep(5.0)
            return
        while self.running:
            try:
                await asyncio.sleep(ORDER_RECONCILE_INTERVAL_SEC)
                if not self.running:
                    break
                await self._reconcile_orders()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.error(f"Reconciler loop: {e}", exc_info=True)

    async def _reconcile_orders(self):
        if DRY_RUN:
            return
        open_rows = self.env['execution.order'].search([
            '&',
            ('exchange_state', 'in', ['open', 'partial']),
            '|',
            ('feedback_state', 'in', ['none', 'pending']),
            ('reject_reason', '=', 'recovery_no_ref'),
        ], order='id ASC', limit=200)
        for r in open_rows:
            if (r.position_id, r.signal_number) in self.active_managers:
                continue
            try:
                await self._reconcile_row(r)
            except Exception as e:
                self.logger.error(f"Reconcile row {r.id}: {e}",
                                   exc_info=True)
        orphan_rows = self.env['execution.order'].search([
            ('reject_reason', '=', 'recovery_no_ref'),
            ('feedback_state', 'in', ['none', 'pending']),
        ], order='id ASC', limit=20)
        for r in orphan_rows:
            try:
                await self._reconcile_row(r)
            except Exception as e:
                self.logger.error(f"Reconcile orphan {r.id}: {e}")
        fb_rows = self.env['execution.order'].search([
            ('exchange_state', '=', 'filled'),
            ('feedback_state', 'in', ['none', 'pending']),
        ], order='id ASC', limit=20)
        for r in fb_rows:
            if r.feedback_attempts and \
                    r.feedback_attempts >= FEEDBACK_MAX_ATTEMPTS:
                continue
            try:
                await self._send_feedback_for_row(r)
            except Exception as e:
                self.logger.error(f"Reconcile feedback {r.id}: {e}")

    async def _reconcile_row(self, row):
        if DRY_RUN:
            return
        order_id = row.exchange_order_id
        cid = row.client_order_id
        now = self.get_time_ns()
        if not order_id:
            if row.feedback_state == 'sent':
                return
            if row.exchange_state not in ('canceled', 'rejected', 'not_found'):
                row.write({
                    'exchange_state': 'not_found',
                    'reject_reason': row.reject_reason or 'recovery_no_ref',
                    'last_sync_ns': now,
                })
            await self._send_feedback_for_row(row)
            return
        try:
            order = await self.order_manager.fetch_order(order_id, cid)
        except Exception as e:
            self.logger.warning(f"Reconcile fetch failed for {row.id}: {e}")
            return
        if not order:
            return
        status = order.get('status')
        if status == 'closed':
            filled = float(order.get('filled', 0) or 0)
            avg = order.get('average') or order.get('price')
            placed = float(row.amount_placed or 0)
            threshold = placed * (1 - 1e-6) if placed > 0 else 0
            new_state = 'filled' if filled >= threshold else 'partial'
            row.write({
                'exchange_state': new_state,
                'amount_filled': filled,
                'avg_fill_price': avg,
                'last_sync_ns': now,
            })
            if new_state == 'filled':
                await self._send_feedback_for_row(row)
        elif status == 'canceled':
            row.write({
                'exchange_state': 'canceled',
                'amount_filled': float(order.get('filled', 0) or 0),
                'last_sync_ns': now,
            })
            await self._send_feedback_for_row(row)
        elif status == 'rejected':
            row.write({
                'exchange_state': 'rejected',
                'reject_reason': 'order_rejected',
                'last_sync_ns': now,
            })
            await self._send_feedback_for_row(row)
        elif status == 'expired':
            row.write({'exchange_state': 'expired', 'last_sync_ns': now})
            await self._send_feedback_for_row(row)

    async def _await_row_terminal(self, row, timeout_sec=2.0):
        if DRY_RUN:
            return True
        terminal = {'filled', 'canceled', 'rejected', 'expired', 'not_found'}
        order_id = row.exchange_order_id
        cid = row.client_order_id
        deadline_ns = self.get_time_ns() + int(timeout_sec * 1e9)
        while self.get_time_ns() < deadline_ns:
            if row.exchange_state in terminal:
                return True
            try:
                order = await self.order_manager.fetch_order(order_id, cid)
                if order:
                    status = order.get('status')
                    now = self.get_time_ns()
                    if status == 'closed':
                        filled = float(order.get('filled', 0) or 0)
                        placed = float(row.amount_placed or 0)
                        threshold = placed * (1 - 1e-6) if placed > 0 else 0
                        new_state = 'filled' if filled >= threshold else 'partial'
                        row.write({
                            'exchange_state': new_state,
                            'amount_filled': filled,
                            'avg_fill_price': order.get('average')
                                              or order.get('price'),
                            'last_sync_ns': now,
                        })
                        return True
                    if status in ('canceled', 'rejected', 'expired'):
                        row.write({
                            'exchange_state': status, 'last_sync_ns': now})
                        return True
            except Exception as e:
                self.logger.debug(f"await_row_terminal fetch error: {e}")
            await asyncio.sleep(0.2)
        return False

    @staticmethod
    def _decide_on_signal(pos, sig_num, sig_ts, sig_retry, last_row):
        if not last_row:
            return 'new'
        r = last_row
        r_ts = r.signal_timestamp or 0
        r_retry = int(getattr(r, 'signal_retry', 0) or 0)
        st = r.exchange_state
        if sig_ts == r_ts and sig_retry > r_retry:
            if st in ('rejected', 'expired', 'canceled', 'not_found',
                      'not_sent'):
                return 'new'
            return 'skip'
        if sig_ts < r_ts:
            return 'skip'
        if sig_ts == r_ts:
            if st in ('open', 'partial', 'filled'):
                return 'skip'
            if st in ('rejected', 'expired', 'canceled',
                      'not_found', 'not_sent'):
                return 'noop'
        if sig_ts > r_ts:
            if st in ('open', 'partial', 'not_sent'):
                return 'rebuild'
            return 'new'
        return 'skip'

    async def _rebuild_pair(self, old_row, new_sig_ts):
        pos = old_row.position_id
        sig = old_row.signal_number
        cid = old_row.client_order_id
        order_id = old_row.exchange_order_id
        now = self.get_time_ns()
        if not order_id:
            old_row.write({
                'reject_reason': 'superseded',
                'feedback_last_error': 'superseded_by_rebuild',
                'last_sync_ns': now,
            })
            try:
                await self._send_feedback_for_row(old_row)
            except Exception as e:
                self.logger.warning(f"Rebuild feedback failed: {e}")
            return True
        try:
            await self.order_manager.cancel_order(order_id, cid)
        except Exception as e:
            self.logger.warning(
                f"Rebuild cancel failed pos={pos} sig={sig}: {e}")
            return False
        confirmed = await self._await_row_terminal(old_row, timeout_sec=2.0)
        if not confirmed:
            self.logger.warning(
                f"Rebuild cancel unconfirmed pos={pos} sig={sig}")
            return False
        if old_row.exchange_state in ('filled', 'partial'):
            self.logger.info(
                f"Rebuild aborted: old order filled/partial "
                f"pos={pos} sig={sig}")
            return False
        if old_row.exchange_state not in ('canceled', 'rejected',
                                          'expired', 'not_found'):
            old_row.write({
                'exchange_state': 'canceled', 'last_sync_ns': now})
        old_row.write({
            'reject_reason': 'superseded',
            'feedback_last_error': 'superseded_by_rebuild',
            'last_sync_ns': now,
        })
        try:
            await self._send_feedback_for_row(old_row)
        except Exception as e:
            self.logger.warning(f"Rebuild feedback failed: {e}")
        self.logger.info(
            f"Rebuild: pos={pos} sig={sig} old_ts="
            f"{old_row.signal_timestamp} new_ts={new_sig_ts} superseded")
        return True

    async def _close_attention_row(self, row_id: int):
        rows = self.env['execution.order'].search([('id', '=', row_id)])
        if not rows:
            return
        r = rows[0]
        r.write({
            'ui_acknowledged_at_ns': self.get_time_ns(),
            'last_sync_ns': self.get_time_ns(),
        })
        self.logger.info(
            f"attention row {row_id} acknowledged "
            f"(pos={r.position_id} sig={r.signal_number} "
            f"state={r.exchange_state})")
        self._bump_ui_event()

    @staticmethod
    def _split_symbol(symbol: str):
        core = (symbol or 'BTC/USDT').split(':')[0]
        parts = core.split('/')
        if len(parts) == 2:
            return parts[0], parts[1]
        return parts[0], 'USDT'

    async def _balance_snapshot_loop(self):
        try:
            await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            raise
        while self.running:
            try:
                if (not self.paused
                        or (self._balance_snapshot_counter % 5 == 0)):
                    await self._snapshot_balance_once()
                self._balance_snapshot_counter += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self._balance_err_logged:
                    self.logger.warning(f"balance snapshot: {e}")
                    self._balance_err_logged = True
            try:
                await asyncio.sleep(30.0)
            except asyncio.CancelledError:
                raise

    async def _snapshot_balance_once(self):
        if not self.order_manager:
            return
        base, quote = self._split_symbol(self.config.get('symbol', 'BTC/USDT'))
        market_type = (self.config.get('market_type') or 'spot').lower()
        currencies = [quote]
        if market_type != 'futures' and base and base != quote:
            currencies.append(base)
        snapshot = {}
        ok_any = False
        for cur in currencies:
            try:
                bal = await self.order_manager.fetch_balance(cur)
                snapshot[cur] = {
                    'free': float(bal.get('free') or 0.0),
                    'used': float(bal.get('used') or 0.0),
                    'total': float(bal.get('total') or 0.0),
                }
                ok_any = True
            except Exception as e:
                self.logger.debug(f"balance snapshot {cur} failed: {e}")
        if ok_any:
            self._balance_err_logged = False
        if snapshot:
            snapshot['_at_ns'] = self.get_time_ns()
            snapshot['_unit_volume'] = float(
                self.config.get('unit_volume', 1.0) or 1.0)
            snapshot['_unit_volume_mode'] = self.config.get(
                'unit_volume_mode', 'quote')
            snapshot['_margin_buffer_ratio'] = float(
                self.config.get('margin_buffer_ratio', 0.05) or 0.05)
            try:
                update_bot_config(self.bot_id,
                                  {'balance_snapshot': snapshot})
            except Exception as e:
                self.logger.debug(f"balance snapshot persist: {e}")

    async def _feedback_retry_loop(self):
        while self.running:
            await self._flush_pending_feedback_once()
            await asyncio.sleep(FEEDBACK_RETRY_INTERVAL_SEC)

    # ---------- broker poll tasks (new in PR-EXEC-2) ----------

    async def _poll_broker_price(self):
        """Replaces ExchangeWebSocket price feed. Q10: strict bid/ask/stale
        validation."""
        while self.running:
            if not await self._ensure_broker():
                await asyncio.sleep(1.0)
                continue
            try:
                price = await self.broker_handle.get(
                    "get_latest_price",
                    exchange=self.config['exchange'],
                    market_type=self.config['market_type'],
                    symbol=self.config['symbol'])
                if (price and price.get('bid') is not None
                        and price.get('ask') is not None
                        and not price.get('stale')):
                    ts_ns = price['ts_ns']
                    bid = price['bid']
                    ask = price['ask']
                    self.price_buffer.append(
                        (ts_ns, bid, ask, self.get_time_ns()))
                    for mgr in self.active_managers.values():
                        mgr.update_price(bid, ask, ts_ns)
            except Exception as e:
                self.logger.warning(f"broker price poll: {e}")
            interval = (self.config.get('broker_poll_interval_ms', 300)
                        / 1000.0)
            await asyncio.sleep(max(0.05, interval))

    async def _poll_order_updates(self):
        """Replaces ExchangeWebSocket order stream. Re-registers owner on
        broker restart to survive force_restart."""
        if DRY_RUN:
            self.logger.info(
                "order-updates poller is disabled ")
            while self.running:
                await asyncio.sleep(5.0)
            return
        since_id = 0
        registered = False
        while self.running:
            healthy = await self._ensure_broker()
            if not healthy:
                registered = False
                since_id = 0  # N-8: broker may restart → queue IDs reset
                await asyncio.sleep(1.0)
                continue
            if not registered:
                if await self._register_with_broker():
                    registered = True
                    since_id = 0  # N-8: fresh queue on register
                else:
                    await asyncio.sleep(1.0)
                    continue
            try:
                updates = await self.broker_handle.get(
                    "get_order_updates",
                    owner_bot_id=self.bot_id,
                    since_id=since_id,
                    timeout_sec=0.3)
                for u in updates or []:
                    since_id = max(since_id, u['id'])
                    mgr = self.order_id_index.get(u['cid'])
                    if mgr:
                        await mgr.process_order_update(u['update'])
            except Exception as e:
                self.logger.warning(f"broker order_updates poll: {e}")
                registered = False
                await asyncio.sleep(1.0)

    # ---------- lifecycle ----------

    async def start(self):
        if self.running:
            return
        self.running = True
        self._stopping = False
        self._session_invalid_order_count = 0

        source_id = self.config.get('source_bot_id')
        if not source_id:
            self.logger.error("Source bot ID not configured")
            update_bot_status(self.bot_id, "stopped")
            self.running = False
            return

        self._broker_id = self._find_broker_id()
        if self._broker_id is None:
            self.logger.error("Broker bot not configured")
            update_bot_status(self.bot_id, "stopped")
            self.running = False
            return

        await self.setup_exchange(source_id, {
            "signal": ["grid_signal", "signal", "order_signal"],
            "timeout": ["execution_timeout"],
            "execution_reserve_sec":
                ["execution_reserve", "signal_lookahead"],
            "request_close_position":
                ["request_close_position", "grid_close"],
            "constants": ["signal_constants", "panic_constants"],
        })
        self.source_handle = self.get_exchange(source_id)

        await self.setup_exchange(self._broker_id, {
            "get_latest_price": ["latest_price", "current_price", "price"],
            "get_broker_status": ["broker_status", "status", "health"],
            "get_market": ["market"],
            "get_markets": ["markets"],
            "create_market_order": ["create_market_order"],
            "create_limit_order": ["create_limit_order"],
            "cancel_order": ["cancel_order"],
            "fetch_order": ["fetch_order"],
            "fetch_open_orders": ["fetch_open_orders"],
            "fetch_positions": ["fetch_positions"],
            "fetch_balance": ["fetch_balance"],
            "set_leverage": ["set_leverage"],
            "panic_close": ["panic_close"],
            "get_order_updates": ["get_order_updates", "order_updates"],
            "cleanup_order_updates": ["cleanup_order_updates"],
            "register_key_owner": ["register_key_owner"],
            "unregister_key_owner": ["unregister_key_owner"],
        })
        self.broker_handle = self.get_exchange(self._broker_id)

        try:
            consts = await self.source_handle.get("constants")
        except Exception as e:
            self.logger.warning(f"constants capability unavailable: {e}")
            consts = None
        if consts:
            update_bot_config(self.bot_id, {'signal_constants': consts})
            self.config['signal_constants'] = consts
            self._panic_signal_number = int(
                consts.get('panic_signal_number',
                           self._panic_signal_number))

        try:
            if self.source_handle:
                timeout = await self.source_handle.get("timeout")
                if timeout:
                    self.source_timeout = float(timeout)
        except Exception as e:
            self.logger.error(f"Failed to get timeout from source: {e}")
        try:
            if self.source_handle:
                r = await self.source_handle.get("execution_reserve_sec")
                if r is not None:
                    self.execution_reserve_sec = float(r)
        except Exception as e:
            self.logger.warning(f"Failed to get execution_reserve_sec: {e}")

        self.poll_interval = self._calc_poll_interval()
        self.reorder_interval = self._calc_reorder_interval()

        feedback_id = self.config.get('feedback_bot_id') or source_id
        await self.setup_exchange(feedback_id, {
            "record_execution": ["grid_fill", "execution", "fill_order"]})
        self.feedback_handle = self.get_exchange(feedback_id)
        caps = await self._get_target_capabilities(feedback_id)
        if caps is None:
            caps = {'record_execution': {}}
        self.feedback_capabilities = caps

        symbol = self.config.get('symbol', '')
        if not symbol:
            self.logger.error("Trading symbol not set")
            update_bot_status(self.bot_id, "stopped")
            self.running = False
            return

        # OrderManager shim
        self.order_manager = OrderManager(
            broker_handle=self.broker_handle,
            key_id=self.config.get('broker_key_id'),
            symbol=symbol,
            config=self.config,
            logger=self.logger,
            exchange_name=self.config.get('exchange', 'binance'),
            market_type=(self.config.get('market_type') or 'spot').lower(),
            bot_id=self.bot_id,
        )

        if not DRY_RUN and (self.config.get('market_type') or '').lower() == 'futures':
            lev = int(self.config.get('leverage') or 0)
            if lev > 0:
                try:
                    await self.order_manager.set_leverage(lev)
                    self.logger.info(f"Leverage set to {lev}x on exchange")
                except Exception as e:
                    self.logger.error(f"set_leverage({lev}) failed: {e}")
                    # fail-safe: local margin check must not assume exchange leverage
                    self.order_manager.leverage = 1

        # Register with broker for order updates (skip in DRY_RUN:
        # no real orders means no updates to route)
        if DRY_RUN:
            self.logger.info(
                "Skipping register_key_owner — no live orders")
        else:
            if not await self._register_with_broker():
                self.logger.warning(
                    "Failed to register with broker; will retry in "
                    "order_updates poller")

        # paused restore
        paused_state = self.config.get('execution_paused')
        if isinstance(paused_state, dict) and paused_state.get('reason'):
            reason = paused_state.get('reason')
            if reason == 'invalid_order_persistent':
                self.paused = True
                self.paused_reason = reason
                self.paused_since_ns = int(
                    paused_state.get('since_ns') or self.get_time_ns())
                self.paused_check_at_ns = 0
                self._session_invalid_order_count = int(
                    paused_state.get('repause_count') or 0)
                self.logger.error(
                    "BREAKER: restored invalid_order_persistent — "
                    "auto-unpause disabled")
            elif reason in ('auth', 'funds'):
                self.paused = True
                self.paused_reason = reason
                self.paused_since_ns = int(
                    paused_state.get('since_ns') or self.get_time_ns())
                self.paused_check_at_ns = (
                    self.get_time_ns() + PAUSED_COOLDOWN_SEC * 1_000_000_000)
                self.logger.warning(
                    f"BREAKER: restored paused state (reason={reason})")
            else:
                try:
                    update_bot_config(self.bot_id,
                                       {'execution_paused': None})
                except Exception as e:
                    self.logger.warning(f"Failed to clear paused state: {e}")

        await self._flush_pending_feedback_once(fire_and_forget=False)
        
        # Recovery is meaningless when there are no real in-flight rows
        if DRY_RUN:
            self.logger.info("in-flight recovery skipped")
        else:
            if not self.paused:
                await self._recover_in_flight()
            else:
                self.logger.warning(
                    "Recovery skipped: bot started in PAUSED state")

        self.task = asyncio.create_task(self._run())
        self.flush_task = asyncio.create_task(self._flush_price_buffer_loop())
        self.feedback_task = asyncio.create_task(self._feedback_retry_loop())
        self.latency_task = asyncio.create_task(self._latency_aggregate_loop())
        if not DRY_RUN:
            self.reconciler_task = asyncio.create_task(
                self._order_reconciler_loop())
        else:
            self.reconciler_task = None
        self.paused_task = asyncio.create_task(self._paused_watchdog_loop())
        self.balance_task = asyncio.create_task(self._balance_snapshot_loop())
        self.poll_price_task = asyncio.create_task(
            self._poll_broker_price())  
        if not DRY_RUN:
            self.poll_updates_task = asyncio.create_task(
                self._poll_order_updates())
        else:
            self.poll_updates_task = None

        if not self.paused:
            update_bot_status(self.bot_id, "running")
        else:
            update_bot_status(self.bot_id, "paused")

        self.logger.info(
            "ExecutionBot started"
            + (f" (paused: {self.paused_reason})" if self.paused else ""))

    async def stop(self):
        self._stopping = True
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self.logger.error(f"Error waiting for main task: {e}")
        if self.signal_tasks:
            for task in list(self.signal_tasks.values()):
                task.cancel()
            await asyncio.gather(*self.signal_tasks.values(),
                                 return_exceptions=True)
        try:
            await self._flush_pending_feedback_once(fire_and_forget=False)
            await self._reconcile_orders()
        except Exception as e:
            self.logger.warning(
                f"stop(): final flush/reconcile failed: {e}")
        managers = list(self.active_managers.values())
        if managers:
            self.logger.info(f"stop(): finalizing {len(managers)} managers")
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *[self._safe_finalize(m) for m in managers],
                        return_exceptions=True),
                    timeout=5.0)
            except asyncio.TimeoutError:
                self.logger.warning(
                    f"stop(): managers did not finalize in 5s — proceeding")
        for t in (
                getattr(self, 'flush_task', None),
                getattr(self, 'feedback_task', None),
                getattr(self, 'latency_task', None),
                getattr(self, 'reconciler_task', None),
                getattr(self, 'paused_task', None),
                getattr(self, 'balance_task', None),
                getattr(self, 'poll_price_task', None),
                getattr(self, 'poll_updates_task', None)):
            if t:
                t.cancel()
        await asyncio.gather(*[
            t for t in (
                getattr(self, 'flush_task', None),
                getattr(self, 'feedback_task', None),
                getattr(self, 'latency_task', None),
                getattr(self, 'reconciler_task', None),
                getattr(self, 'paused_task', None),
                getattr(self, 'balance_task', None),
                getattr(self, 'poll_price_task', None),
                getattr(self, 'poll_updates_task', None))
            if t], return_exceptions=True)
        if self._feedback_tasks:
            for t in list(self._feedback_tasks):
                t.cancel()
            await asyncio.gather(*self._feedback_tasks,
                                 return_exceptions=True)
            self._feedback_tasks.clear()
        try:
            await self._flush_price_buffer()
        except Exception as e:
            self.logger.error(f"Final flush failed: {e}")
        if self.broker_handle and not DRY_RUN:
            try:
                await self.broker_handle.set(
                    "unregister_key_owner",
                    {'key_id': self.config.get('broker_key_id'),
                     'owner_bot_id': self.bot_id})
            except Exception:
                pass
            try:
                await self.broker_handle.get(
                    "cleanup_order_updates",
                    owner_bot_id=self.bot_id)
            except Exception:
                pass
        if self.order_manager:
            await self.order_manager.close()
        self.signal_tasks.clear()
        self.active_managers.clear()
        self.blocked_positions.clear()
        self.order_id_index.clear()
        self.reconciler_task = None
        self.paused_task = None
        self.balance_task = None
        update_bot_status(self.bot_id, "stopped")
        self._stopping = False
        self.logger.info("ExecutionBot stopped")

    async def _run(self):
        cleanup_counter = 0
        last_cleanup_ns = 0
        while self.running:
            try:
                if self.config_dirty:
                    self.config = get_bot_config(self.bot_id)
                    self.poll_interval = self._calc_poll_interval()
                    self.reorder_interval = self._calc_reorder_interval()
                    self.config_dirty = False
                await self._poll_source()
            except Exception as e:
                self.logger.error(f"Error in main loop: {e}",
                                   exc_info=True)
            await asyncio.sleep(self.poll_interval)
            cleanup_counter += 1
            if cleanup_counter >= 100:
                now = self.get_time_ns()
                if now - last_cleanup_ns >= CLEANUP_INTERVAL_NS:
                    try:
                        await self._cleanup_history()
                    except Exception as e:
                        self.logger.error(
                            f"Cleanup history failed: {e}", exc_info=True)
                    cleanup_counter = 0
                    last_cleanup_ns = now

    async def _poll_source(self):
        try:
            signals = await self.source_handle.get("signal")
            if not signals:
                return
            for sig in signals:
                position_id = sig.get('position_id')
                sig_num = sig.get('signal_number')
                if position_id is None or sig_num is None:
                    continue
                is_panic_pre = (isinstance(sig_num, int)
                                and sig_num == self._panic_signal_number)
                if self.paused and not is_panic_pre:
                    now = self.get_time_ns()
                    if (now - self._last_paused_log_ns) > 60 * 10**9:
                        self.logger.info(
                            f"pos={position_id} sig={sig_num}: skipped "
                            f"(paused, reason={self.paused_reason})")
                        self._last_paused_log_ns = now
                    continue
                last_row = self.env['execution.order'].search([
                    ('position_id', '=', position_id),
                    ('signal_number', '=', sig_num),
                ], order='attempt_n DESC', limit=1)
                now_ns = self.get_time_ns()
                scale = sig.get('signal_timestamp_scale', 'tbot_utc_ns')
                normalize = self.time_scale_normalizer.get(scale)
                if normalize is None:
                    self.logger.error(
                        f"pos={position_id} sig={sig_num}: "
                        f"unknown scale {scale}")
                    continue
                sig_ts = normalize(sig['signal_timestamp'])
                if not sig_ts or sig_ts < 1_500_000_000_000_000_000:
                    sig_ts = now_ns
                safety = max(self.source_timeout * PHASE4_RATIO,
                             PHASE4_MIN_SEC)
                deadline_ns = sig_ts + int(
                    (self.source_timeout - safety) * 1e9)
                if deadline_ns <= now_ns:
                    existing_stale = self.env['execution.order'].search([
                        ('position_id', '=', position_id),
                        ('signal_number', '=', sig_num),
                        ('reject_reason', '=', 'stale_signal'),
                        ('signal_timestamp', '=', sig_ts),
                    ], limit=1)
                    if existing_stale:
                        continue
                    self.logger.warning(
                        f"pos={position_id} sig={sig_num}: "
                        f"deadline passed — stale")
                    is_urgent = isinstance(sig_num, int) and sig_num < 0
                    if not is_urgent:
                        side_val = sig.get('signal_direction') or 'buy'
                        rate = float(sig.get('signal_vol_rate') or 0.0)
                        existing_by_ts = self.env['execution.order'].search([
                            ('position_id', '=', position_id),
                            ('signal_number', '=', sig_num),
                            ('signal_timestamp', '=', sig_ts),
                        ], order='attempt_n DESC', limit=1)
                        if existing_by_ts:
                            last = existing_by_ts[0]
                            if last.exchange_state in ('filled', 'canceled', 'rejected',
                                                       'expired', 'not_found'):
                                if last.feedback_state in ('none', 'pending'):
                                    self._send_feedback_fire_and_forget(last)
                                continue

                            # Non-terminal row exists (not_sent / open / partial) and deadline
                            # has passed. Force-finalize it so grid moves on instead of
                            # remaining stuck in pending forever.
                            if last.exchange_state in ('open', 'partial'):
                                if last.exchange_order_id:
                                    try:
                                        await self.order_manager.cancel_order(
                                            last.exchange_order_id, last.client_order_id)
                                    except Exception as e:
                                        self.logger.warning(
                                            f"stale-cleanup cancel failed for {last.id}: {e}")
                                last.write({
                                    'exchange_state': 'canceled',
                                    'reject_reason': 'stale_signal',
                                    'last_sync_ns': self.get_time_ns(),
                                })
                                await self._send_feedback_for_row(last)
                            else:  # not_sent
                                last.write({
                                    'reject_reason': 'stale_signal',
                                    'last_sync_ns': self.get_time_ns(),
                                })
                                await self._send_feedback_for_row(last)
                            continue

                        max_rec = self.env['execution.order'].search([
                            ('position_id', '=', position_id),
                            ('signal_number', '=', sig_num),
                        ], order='attempt_n DESC', limit=1)
                        next_n = (max_rec[0].attempt_n + 1) if max_rec else 1
                        stale_row = self.env['execution.order'].create([{
                            'position_id': position_id,
                            'signal_number': sig_num,
                            'attempt_n': next_n,
                            'symbol': self.config['symbol'],
                            'side': side_val,
                            'order_type': sig.get('signal_order_type')
                                          or 'market',
                            'reduce_only': (isinstance(sig_num, int)
                                            and sig_num < 0),
                            'price_requested': sig.get('signal_price'),
                            'amount_requested': 0.0,
                            'amount_placed': 0.0,
                            'signal_vol_rate': rate,
                            'signal_timestamp': sig_ts,
                            'signal_timestamp_scale': scale,
                            'deadline_ns': deadline_ns,
                            'exchange_state': 'not_sent',
                            'reject_reason': 'stale_signal',
                            'feedback_state': 'pending',
                            'created_ns': now_ns,
                            'last_sync_ns': now_ns,
                        }])[0]
                        self._send_feedback_fire_and_forget(stale_row)
                        if sig_num == 0:
                            self.blocked_positions.add(position_id)
                            try:
                                await self.source_handle.set(
                                    "request_close_position",
                                    {'reason': 'stale_entry_signal'})
                            except Exception as e:
                                self.logger.error(
                                    f"request_close failed: {e}")
                        self._bump_ui_event()
                        continue
                    else:
                        self.logger.error(
                            f"pos={position_id} sig={sig_num}: "
                            f"stale urgent — forcing market")
                        sig['signal_order_type'] = 'market'
                is_panic = (isinstance(sig_num, int)
                            and sig_num == self._panic_signal_number)
                if position_id in self.blocked_positions and not is_panic:
                    if sig_num != 0:
                        self.logger.warning(
                            f"pos={position_id} sig={sig_num}: blocked, skip")
                        continue
                if sig_num == 0 and position_id in self.blocked_positions:
                    self.blocked_positions.discard(position_id)
                key = (position_id, sig_num)
                if key in self.active_managers:
                    self.logger.debug(
                        f"pos={position_id} sig={sig_num}: manager active")
                    continue
                sig_retry = int(sig.get('signal_retry', 0) or 0)
                decision = self._decide_on_signal(
                    position_id, sig_num, sig_ts, sig_retry,
                    last_row[0] if last_row else None)
                if decision == 'skip':
                    continue
                if decision == 'noop':
                    continue
                if decision == 'rebuild':
                    ok = await self._rebuild_pair(last_row[0], sig_ts)
                    if not ok:
                        continue
                # unit_volume in config is always the *effective* value. Auto-scale in
                # SignalManager._apply_preflight bumps it upward when one quantum would
                # otherwise cost less than the exchange min_cost / min_amount, and
                # persists the bumped value back into bot_settings.
                #
                # Do NOT assume config['unit_volume'] equals the user's original input —
                # it may have grown over time ("ratchet up"). Shrinking is a manual
                # action only.
                #
                # `plan['amount_requested']` below is the *pre-scaling* value; the row
                # is overwritten in _apply_preflight (amount_requested = raw_eff), which
                # keeps the feedback ratio amount_filled / amount_requested in [0, 1].
                unit_volume = float(self.config.get('unit_volume', 1.0) or 1.0)
                mode = self.config.get('unit_volume_mode', 'quote')
                rate = float(sig.get('signal_vol_rate') or 0.0)
                if mode == 'quote':
                    ref_price = self._latest_ref_price(sig)
                    if not ref_price or ref_price <= 0:
                        self.logger.warning(
                            f"pos={position_id} sig={sig_num}: "
                            f"no ref price")
                        continue
                    raw = rate * unit_volume / ref_price
                else:
                    raw = rate * unit_volume
                side_val = sig.get('signal_direction')
                if side_val not in ('buy', 'sell'):
                    side_val = 'buy'
                plan = {
                    'position_id': position_id,
                    'signal_number': sig_num,
                    'symbol': self.config['symbol'],
                    'side': side_val,
                    'order_type': sig.get('signal_order_type') or 'market',
                    'price': sig.get('signal_price'),
                    'signal_vol_rate': rate,
                    'signal_timestamp': sig_ts,
                    'signal_timestamp_scale': scale,
                    'deadline_ns': deadline_ns,
                    'reduce_only': (isinstance(sig_num, int) and sig_num < 0),
                    'amount_requested': raw,
                    'amount_placed': raw,
                    'is_panic': is_panic,
                    'signal_smart': bool(sig.get('signal_smart', False)),
                    'signal_retry': sig_retry,
                }
                mgr = SignalManager(self, plan, self.logger,
                                    time_provider=self.get_time_ns)
                self.active_managers[key] = mgr
                task = asyncio.create_task(
                    mgr.run(), name=f"signal_{position_id}_{sig_num}")
                mgr.task = task
                task.add_done_callback(
                    lambda t, k=key: self._on_signal_task_done(k, t))
                self.signal_tasks[key] = task
        except Exception as e:
            self.logger.error(f"Error polling source: {e}", exc_info=True)

    def _on_signal_task_done(self, key, task):
        try:
            exc = task.exception()
        except (asyncio.CancelledError, Exception):
            return
        if exc is not None:
            self.logger.error(
                f"Signal task {key} crashed: {exc}", exc_info=exc)

    async def _latency_aggregate_loop(self):
        import statistics
        while self.running:
            try:
                await asyncio.sleep(LATENCY_AGGREGATE_INTERVAL_SEC)
                if not self.running:
                    break
                recs = self.env['execution.stats'].search(
                    [], order='id DESC', limit=LATENCY_AGGREGATE_SAMPLE_N)
                rows = recs.read() if recs else []
                if not rows:
                    continue
                by_action = {}
                for r in rows:
                    action = r.get('action') or 'unknown'
                    v = r.get('latency_ms')
                    if v is None:
                        continue
                    by_action.setdefault(action, []).append(float(v))
                out = {}
                for action, vals in by_action.items():
                    vs = sorted(vals)
                    n = len(vs)
                    p95_idx = min(n - 1, int(0.95 * (n - 1)))
                    out[action] = {
                        'count': n,
                        'mean_ms': statistics.fmean(vals),
                        'stdev_ms': statistics.pstdev(vals) if n > 1 else 0.0,
                        'p95_ms': vs[p95_idx],
                    }
                out['_updated_at_ns'] = self.get_time_ns()
                out['_sample_size'] = LATENCY_AGGREGATE_SAMPLE_N
                try:
                    update_bot_config(self.bot_id, {'latency_stats': out})
                except Exception as e:
                    self.logger.error(
                        f"Failed to persist latency_stats: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.error(
                    f"Latency aggregation failed: {e}", exc_info=True)

    async def _cleanup_history(self):
        now_ns = self.get_time_ns()
        price_tick = self.env['execution.price']
        max_rows = int(self.config.get('max_order_rows',
                                        DEFAULT_MAX_ORDER_ROWS))
        total = self.env['execution.order'].search_count([])
        if total > max_rows:
            cutoff_ns = now_ns - TERMINAL_RETENTION_SEC * 1_000_000_000
            old = self.env['execution.order'].search([
                ('exchange_state', 'in',
                 ['filled', 'canceled', 'rejected', 'expired', 'not_found']),
                ('feedback_state', 'in',
                 ['sent', 'abandoned', 'delivery_failed', 'superseded']),
                ('created_ns', '<', cutoff_ns),
            ], order='id ASC', limit=max(0, total - int(max_rows * 0.9)))
            if old:
                old.unlink()
        cutoff_ns = now_ns - PRICE_RETENTION_SEC * 10**9
        price_tick.search([('timestamp', '<', cutoff_ns)]).unlink()
        for tier_from_sec, tier_to_sec, bucket_sec in PRICE_TIERS:
            if bucket_sec == 0:
                continue
            low_ns = now_ns - tier_to_sec * 10**9
            high_ns = now_ns - tier_from_sec * 10**9
            batch = 10_000
            bucket_ns = bucket_sec * 10**9
            cursor_ts = low_ns - 1
            while self.running:
                rows = price_tick.search([
                    ('timestamp', '>', cursor_ts),
                    ('timestamp', '<', high_ns),
                ], order='timestamp ASC', limit=batch)
                if not rows:
                    break
                cursor_ts = rows[-1].timestamp
                buckets = {}
                for r in rows:
                    key = r.timestamp // bucket_ns
                    buckets.setdefault(key, []).append(r)
                for key, group in buckets.items():
                    await asyncio.sleep(0)
                    if len(group) <= 1:
                        continue
                    keeper = group[-1]
                    try:
                        keeper.write({
                            'timestamp': key * bucket_ns,
                            'bid': keeper.bid,
                            'ask': keeper.ask,
                            'tbot_utc_stamp_ns': max(
                                r.tbot_utc_stamp_ns for r in group),
                        })
                    except Exception as e:
                        self.logger.debug(
                            f"retention keeper.write failed: {e}")
                        continue
                    for r in group:
                        if r.id != keeper.id:
                            r.unlink()
                await asyncio.sleep(0)
                if len(rows) < batch:
                    break
        total_price = price_tick.search_count([])
        if total_price > PRICE_MAX_ROWS:
            excess = total_price - PRICE_MAX_ROWS
            price_tick.search([], order='timestamp ASC',
                              limit=excess).unlink()
        stats_limit = int(self.config.get('stats_sample_size',
                                           STATS_SAMPLE_SIZE)
                          or STATS_SAMPLE_SIZE)
        stats = self.env['execution.stats']
        if stats.search_count([]) > stats_limit:
            last_keep = stats.search([], order='id DESC', limit=stats_limit)
            if last_keep:
                min_id = last_keep[-1].id
                stats.search([('id', '<', min_id)]).unlink()

    async def _flush_price_buffer_loop(self):
        while self.running:
            await asyncio.sleep(self.poll_interval)
            await self._flush_price_buffer()

    async def _flush_price_buffer(self):
        if not self.price_buffer:
            return
        ticks = list(self.price_buffer)
        self.price_buffer.clear()
        if not ticks:
            return
        try:
            vals = [{
                'timestamp': ts, 'bid': bid, 'ask': ask,
                'symbol': self.config['symbol'],
                'tbot_utc_stamp_ns': tbot_ts,
            } for ts, bid, ask, tbot_ts in ticks]
            self.env['execution.price'].create(vals)
        except Exception as e:
            self.logger.error(
                f"Failed to flush price buffer: {e}", exc_info=True)

    async def on_config_updated(self):
        # PR-EXEC-5: broker_key_id change → reinit managers.
        new_config = get_bot_config(self.bot_id)
        old_key_id = self.config.get('broker_key_id')
        new_key_id = new_config.get('broker_key_id')
        if old_key_id != new_key_id:
            self.logger.info(
                f"broker_key_id changed: {old_key_id} → {new_key_id}; "
                f"reinitializing")
            if self.active_managers:
                self._stopping = True
                managers = list(self.active_managers.values())
                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            *[self._safe_finalize(m) for m in managers],
                            return_exceptions=True),
                        timeout=5.0)
                except asyncio.TimeoutError:
                    self.logger.error(
                        "broker_key_id change: managers did not finalize "
                        "in 5s — recreating anyway")
                finally:
                    self._stopping = False
            self.config = new_config
            self.order_manager = OrderManager(
                broker_handle=self.broker_handle,
                key_id=new_key_id,
                symbol=self.config['symbol'],
                config=self.config,
                logger=self.logger,
                exchange_name=self.config.get('exchange', 'binance'),
                market_type=(self.config.get('market_type')
                             or 'spot').lower(),
                bot_id=self.bot_id,
            )
            if new_key_id:
                try:
                    await self.broker_handle.set("unregister_key_owner",
                                                   {'key_id': old_key_id})
                except Exception:
                    pass
                await self._register_with_broker()
        else:
            self.config = new_config