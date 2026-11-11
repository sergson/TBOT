# modules/collector/models.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import asyncio

from core import auto_reg, BaseBot
from core.database import (
    Model, Integer, Float, Char,
    get_bot_config, update_bot_config, update_bot_status, get_all_bots,
)
from core.logger import perf_logger
from .lib.utils import safe_table_name

FAST_MODE_AVAILABLE = True

# TF < 1s excluded: SQLite write amplification at 10ms/50ms/100ms/
# 500ms exceeds the sustainable rate (~10k-20k writes/sec kills WAL).
# Broker deque (60s raw) is plenty for 1s+ aggregation.
FAST_TIMEFRAMES = (
    '1s', '5s', '15s', '30s',
)
SLOW_TIMEFRAMES = (
    '1m', '5m', '15m', '30m', '1h', '2h', '4h', '1d',
)


def timeframe_to_ms(tf: str) -> int:
    if not tf:
        raise ValueError("Empty timeframe")
    if tf.endswith('ms'):
        return int(tf[:-2])
    if tf.endswith('s'):
        return int(tf[:-1]) * 1_000
    if tf.endswith('m'):
        return int(tf[:-1]) * 60_000
    if tf.endswith('h'):
        return int(tf[:-1]) * 3_600_000
    if tf.endswith('d'):
        return int(tf[:-1]) * 86_400_000
    raise ValueError(f"Unsupported timeframe: {tf}")


def is_fast_timeframe(tf: str) -> bool:
    """True iff FAST_MODE_AVAILABLE and tf belongs to FAST_TIMEFRAMES.

    Membership check (not threshold) — '1m' is in SLOW_TIMEFRAMES
    even though it equals 60 000 ms.
    """
    if not FAST_MODE_AVAILABLE:
        return False
    return tf in FAST_TIMEFRAMES


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

        # Coerce unsupported TF (fast-mode-off OR removed from
        # FAST_TIMEFRAMES, e.g. old 500ms config from a previous version).
        tf = self.config.get('timeframe', '1m')
        valid_tfs = set(SLOW_TIMEFRAMES)
        if FAST_MODE_AVAILABLE:
            valid_tfs |= set(FAST_TIMEFRAMES)
        if tf not in valid_tfs:
            self.logger.warning(
                f"Timeframe {tf!r} is not supported — coercing to '1m'")
            tf = '1m'
            self.config['timeframe'] = tf
            try:
                update_bot_config(self.bot_id, {'timeframe': tf})
            except Exception as e:
                self.logger.warning(
                    f"Persisting coerced timeframe failed: {e}")

        self._broker_id = self._find_broker_id()
        if self._broker_id is None:
            self.logger.error(
                "No broker bot configured — collector cannot start")
            update_bot_status(self.bot_id, 'stopped')
            self.running = False
            return

        symbol = self.config.get('symbol')
        if symbol:
            self._candle_table_name = safe_table_name(symbol)

        fast = is_fast_timeframe(tf)
        current_mode = 'fast' if fast else 'slow'

        # ---- Table disposition on start ----
        #
        # Matrix (v9):
        #   cross-mode         (fast↔slow)      → DROP + full REST reload
        #   fast → fast coarser (new_ms > old)  → AGGREGATE (rebuild)
        #   fast → fast finer  (new_ms < old)   → DROP (finer history is
        #                                          unrecoverable; new finer
        #                                          candles would collide
        #                                          with old coarser rows
        #                                          via PK)
        #   slow → slow         (intra-mode)    → DROP + full REST reload
        #   no TF change                        → keep table as-is
        #
        last_mode = self.config.get('_last_run_mode')
        last_tf = self.config.get('_last_run_tf')
        drop = False
        rebuild_coarser = False
        old_tf_ms = new_tf_ms = 0
        reason = ''

        if self._candle_table_name:
            if last_mode is None:
                try:
                    mgr = self._get_candle_manager()
                    if mgr.search_count([]) > 0:
                        drop = True
                        reason = (
                            f"no _last_run_mode persisted but table "
                            f"{self._candle_table_name!r} already has data")
                except Exception as e:
                    self.logger.warning(
                        f"First-start table check failed: {e}")
            elif last_mode != current_mode:
                drop = True
                reason = (
                    f"mode change {last_mode} → {current_mode}")
            elif last_tf and last_tf != tf:
                if fast:
                    try:
                        old_tf_ms = timeframe_to_ms(last_tf)
                        new_tf_ms = timeframe_to_ms(tf)
                    except (ValueError, TypeError) as e:
                        self.logger.warning(
                            f"Cannot compare TFs {last_tf!r} → {tf!r}: "
                            f"{e} — dropping table")
                        drop = True
                        reason = (
                            f"invalid TF comparison "
                            f"{last_tf!r} → {tf!r}: {e}")
                        old_tf_ms = new_tf_ms = 0

                    if not drop and new_tf_ms > old_tf_ms > 0:
                        rebuild_coarser = True
                        reason = (
                            f"TF {last_tf} ({old_tf_ms}ms) → "
                            f"{tf} ({new_tf_ms}ms): aggregate")
                    elif not drop and 0 < new_tf_ms < old_tf_ms:
                        drop = True
                        reason = (
                            f"TF {last_tf} ({old_tf_ms}ms) → "
                            f"{tf} ({new_tf_ms}ms): finer — drop "
                            f"(fine history unrecoverable, PK "
                            f"collision with old coarser rows)")
                    elif not drop and new_tf_ms == old_tf_ms:
                        self.logger.info(
                            f"TF {last_tf!r} → {tf!r}: same duration, "
                            f"no-op")
                else:
                    drop = True
                    reason = (
                        f"slow TF change {last_tf} → {tf}")

        if drop:
            self.logger.warning(
                f"Dropping candle table {self._candle_table_name!r}: "
                f"{reason}")
            try:
                self.env.drop_table(self._candle_table_name)
            except Exception as e:
                self.logger.warning(f"Dropping table failed: {e}")
        elif rebuild_coarser:
            self.logger.info(
                f"Rebuilding candle table {self._candle_table_name!r}: "
                f"{reason}")
            try:
                self._rebuild_candles_for_tf(old_tf_ms, new_tf_ms)
            except Exception as e:
                self.logger.error(
                    f"Rebuild failed — dropping table as fallback: {e}",
                    exc_info=True)
                try:
                    self.env.drop_table(self._candle_table_name)
                except Exception as e2:
                    self.logger.warning(f"Dropping table failed: {e2}")
                # Verify table is actually empty before trusting
                # _last_run_tf. If both rebuild AND drop failed, aborting
                # start() is safer than persisting a new TF over a
                # mixed-TF table.
                try:
                    mgr = self._get_candle_manager()
                    if mgr.search_count([]) > 0:
                        self.logger.error(
                            "rebuild+drop both failed; table still has "
                            "rows — aborting start to avoid mixed-TF data. "
                            "Manual intervention required: fix DB or "
                            "delete the table.")
                        self.running = False
                        try:
                            update_bot_status(self.bot_id, 'stopped')
                        except Exception as ee:
                            self.logger.warning(
                                f"update_bot_status(stopped) failed: {ee}")
                        return
                except Exception as e_check:
                    # Either table is gone (desired) or unqueryable —
                    # proceed; next start() will retry the check.
                    self.logger.debug(
                        f"post-drop verification: {e_check}")
                drop = True

        # Cross-mode / drop / rebuild → table is empty. Reset the
        # initial-load flag so REST _run() fetches full history.
        if drop or rebuild_coarser:
            self.initial_load_done = False

        try:
            update_bot_config(self.bot_id, {
                '_last_run_mode': current_mode,
                '_last_run_tf': tf,
            })
        except Exception as e:
            self.logger.warning(
                f"Persisting _last_run_mode/_last_run_tf failed: {e}")

        if fast:
            await self.setup_exchange(self._broker_id, {
                "get_trades": ["trades", "recent_trades", "raw_trades"],
                "get_broker_status": ["broker_status", "status", "health"],
            })
            self._broker_handle = self.get_exchange(self._broker_id)
            self.task = asyncio.create_task(self._run_fast())
            self.logger.info(
                f"Collector {self.bot_id} started in FAST mode "
                f"(tf={tf}, WS trades)")
        else:
            await self.setup_exchange(self._broker_id, {
                "get_ohlcv": ["ohlcv", "candles"],
                "get_symbols": ["symbols"],
                "get_markets": ["markets"],
                "get_supported_market_types": ["market_types"],
                "get_broker_status": ["broker_status", "status", "health"],
            })
            self._broker_handle = self.get_exchange(self._broker_id)
            self.task = asyncio.create_task(self._run())
            self.logger.info(
                f"Collector {self.bot_id} started in SLOW mode "
                f"(tf={tf}, REST ohlcv)")

        update_bot_status(self.bot_id, 'running')

    async def _run_fast(self):
        """Fast-mode aggregation loop.

        Broker returns a COPY of raw trades. Collector groups them into
        target-TF windows by exchange_ts_ns and REPLACES the
        corresponding DB row (idempotent — re-aggregating the same
        source yields the same OHLCV, unlike the old merge which
        doubled volume).

        Rule G (Gap rule — critical):
          Missing trades over a window DOES NOT mean a zero candle.
          A gap in the DB is correct and expected: consumers must
          handle sparse ohlcv_data. Writing O=H=L=C=V=0 would corrupt
          grid and execution levels. Never fill, never extrapolate,
          never reuse stale values.

        safety_ns = max(1s, min(2s, window_ns)): margin for WS lag
        only, not a full window. Without the 2s cap, 30s TF would need
        30s of safety — incompatible with the broker's 60s history
        and would make 30s TF write nothing reliably.

        lookback_ns = max(2*window_ns, window_ns + safety_ns + 1s):
        enough to cover a full window + late-arrival catchup.
        """
        tf = self.config.get('timeframe', '1s')
        tf_ms = timeframe_to_ms(tf)
        window_ns = tf_ms * 1_000_000
        poll_ms = self._fast_poll_interval_ms()
        safety_ns = max(1_000_000_000, min(2_000_000_000, window_ns))
        lookback_ns = max(2 * window_ns,
                          window_ns + safety_ns + 1_000_000_000)

        self.logger.info(
            f"_run_fast: tf={tf} window={window_ns/1e9:.3f}s "
            f"poll={poll_ms}ms lookback={lookback_ns/1e9:.1f}s "
            f"safety={safety_ns/1e9:.1f}s")

        while self.running:
            if self.config_dirty:
                await self.on_config_updated()
                self.config_dirty = False
                if not self.running:
                    self.logger.info(
                        "fast loop: stopping (running=False set by "
                        "on_config_updated)")
                    break

            if not await self._ensure_broker():
                await asyncio.sleep(1.0)
                continue

            try:
                now_tbot = self.get_time_ns()
                to_ns = now_tbot - safety_ns
                since_ns = now_tbot - lookback_ns

                resp = await self._poll_trades(since_ns, to_ns)
                trades = resp.get('trades') or []
                if not trades:
                    await asyncio.sleep(poll_ms / 1000.0)
                    continue

                first_exch = resp['first_trade_exchange_ts_ns']
                oldest_exch = resp['oldest_exchange_ts_ns']

                if first_exch > 0:
                    drift_ns = first_exch - now_tbot
                    if drift_ns > safety_ns * 2:
                        self.logger.warning(
                            f"_run_fast: exchange clock leads TBOT by "
                            f"{drift_ns / 1e9:.1f}s (safety={safety_ns / 1e9:.1f}s) — "
                            f"trades from the future will be dropped. "
                            f"Check time_sync telemetry and exchange server time.")

                by_window: dict = {}
                for t in trades:
                    ex_ts = t['exchange_ts_ns']
                    w = ex_ts - (ex_ts % window_ns)
                    by_window.setdefault(w, []).append(t)

                for w in sorted(by_window.keys()):
                    # (1) window not fully covered by safety cutoff
                    if w + window_ns > to_ns:
                        continue
                    # (2) window started before worker's first trade
                    #     (replay/restart — partial data)
                    if first_exch > 0 and w < first_exch:
                        continue
                    # (3) window older than deque — data evicted
                    if oldest_exch > 0 and w < oldest_exch:
                        continue
                    bucket = by_window[w]
                    if not bucket:                        # Rule G
                        continue
                    bucket.sort(key=lambda b: b['exchange_ts_ns'])
                    vals = self._aggregate_trades_into_candle(bucket, w)
                    if vals:
                        self._replace_candle(vals)

                # Prune to candles_limit (10% over-trigger, same as slow).
                try:
                    limit = int(self.config.get('candles_limit', 100) or 100)
                    mgr = self._get_candle_manager()
                    count = mgr.search_count([])
                    if count > limit * 1.1:
                        excess = count - limit
                        old = mgr.search(
                            [], order='timestamp ASC', limit=excess)
                        if old:
                            old.unlink()
                except Exception as e:
                    self.logger.debug(f"fast mode prune: {e}")

            except Exception as e:
                self.logger.error(f"fast loop: {e}", exc_info=True)

            await asyncio.sleep(poll_ms / 1000.0)

        self.logger.info("_run_fast: exiting")

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
                # on_config_updated handles its own exceptions internally
                # and sets running=False on any TF change.
                await self.on_config_updated()
                self.config_dirty = False
                if not self.running:
                    self.logger.info(
                        "slow loop: stopping (running=False set by "
                        "on_config_updated)")
                    break
                continue

            if not await self._ensure_broker():
                await asyncio.sleep(5)
                continue

            try:
                symbol = self.config.get('symbol')
                timeframe = self.config.get('timeframe')
                candles_limit = int(self.config.get('candles_limit') or 0)
                if not (symbol and timeframe and candles_limit > 0):
                    self.logger.error(
                        f"Invalid config: symbol={symbol!r} "
                        f"timeframe={timeframe!r} "
                        f"candles_limit={candles_limit}")
                    await asyncio.sleep(10)
                    continue
                fetch_interval_sec = timeframe_to_seconds(timeframe)
            except (TypeError, ValueError) as e:
                self.logger.error(f"Invalid config: {e}")
                await asyncio.sleep(10)
                continue

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

    # ---------- fast mode helpers ----------

    def _fast_poll_interval_ms(self) -> int:
        """Poll cadence: 1/4 of the timeframe, clamped to [10, 1000] ms."""
        try:
            tf_ms = timeframe_to_ms(self.config.get('timeframe', '1s'))
        except (ValueError, TypeError):
            tf_ms = 1000
        return max(10, min(tf_ms // 4, 1000))

    async def _poll_trades(self, since_ns: int, to_ns: int) -> dict:
        """Fetch raw trades from broker in [since_ns, to_ns).

        Filters and sorts on exchange_ts_ns (market clock) — the
        correct clock for TF-window grouping. TBOT clock is used by
        the caller to compute since/to; small drift vs exchange clock
        is absorbed by safety_ns margin.
        """
        ex = self.config.get('exchange')
        mt = self.config.get('market_type')
        sym = self.config.get('symbol')
        if not (ex and mt and sym):
            self.logger.warning("_poll_trades: incomplete config")
            return {}
        try:
            return await self._broker_handle.get(
                "get_trades",
                exchange=ex, market_type=mt, symbol=sym,
                from_ns=since_ns, to_ns=to_ns,
                time_scale='exchange')
        except Exception as e:
            self.logger.warning(f"get_trades failed: {e}")
            return {}

    def _aggregate_trades_into_candle(self, trades, win_start_ns):
        """Aggregate trades (sorted by exchange_ts_ns, non-empty).

        Returns a dict for _replace_candle, or None if empty.
        Caller MUST NOT call this with an empty list — Rule G.
        """
        if not trades:
            return None
        prices = [t['price'] for t in trades]
        vols = [t['amount'] for t in trades]
        return {
            'timestamp':         win_start_ns,
            'open':              prices[0],
            'high':              max(prices),
            'low':               min(prices),
            'close':             prices[-1],
            'volume':            sum(vols),
            'tbot_utc_stamp_ns': self.get_time_ns(),
        }

    def _replace_candle(self, vals):
        """REPLACE (overwrite) the candle row at vals['timestamp'].

        Idempotent — re-aggregation of the same base trades yields
        the same OHLCV. Contrast with slow-mode `_upsert_candles`
        (also overwrite, but source is a full exchange candle, not
        an incremental delta).
        """
        if not vals:
            return
        mgr = self._get_candle_manager()
        existing = mgr.search(
            [('timestamp', '=', vals['timestamp'])], limit=1)
        if existing:
            payload = {k: v for k, v in vals.items() if k != 'timestamp'}
            existing[0].write(payload)
        else:
            mgr.create([vals])

    def _rebuild_candles_for_tf(self, old_tf_ms: int, new_tf_ms: int) -> int:
        """Aggregate historical candles when TF changes coarser inside
        fast mode.

        Contract:
          * Precondition: new_tf_ms > old_tf_ms. Caller must handle
            finer separately (finer → drop, see start() — finer
            history is unrecoverable; new finer candles would
            collide with old coarser rows via PK).
          * Aggregation:
              open   — open of the first fine candle in the new window
              high   — max over the window
              low    — min over the window
              close  — close of the last fine candle in the window
              volume — sum over the window

        Returns number of rows after the call (or -1 if precondition
        fails).

        Edge cases:
          * OHLCV fields may be null on legacy rows — bucket init uses
            a close→open→0.0 fallback so merged high/low are always
            floats.
          * Deletion is chunked with explicit ORDER BY for determinism
            and bounded memory (normally ≤ candles_limit × 1.1 rows).
        """
        if new_tf_ms <= old_tf_ms:
            return -1

        mgr = self._get_candle_manager()
        rows = list(mgr.search([], order='timestamp ASC'))
        if not rows:
            return 0

        new_window_ns = new_tf_ms * 1_000_000
        buckets: dict = {}

        for r in rows:
            ts = int(r.timestamp or 0)
            if ts <= 0:
                continue
            w = ts - (ts % new_window_ns)
            b = buckets.get(w)
            if b is None:
                px_ref = (r.close if r.close is not None
                          else (r.open if r.open is not None else 0.0))
                buckets[w] = {
                    'timestamp': w,
                    'open': r.open if r.open is not None else px_ref,
                    'high': r.high if r.high is not None else px_ref,
                    'low': r.low if r.low is not None else px_ref,
                    'close': r.close if r.close is not None else px_ref,
                    'volume': float(r.volume or 0.0),
                    'tbot_utc_stamp_ns': int(r.tbot_utc_stamp_ns or 0),
                }
            else:
                if r.high is not None and r.high > b['high']:
                    b['high'] = r.high
                if r.low is not None and r.low < b['low']:
                    b['low'] = r.low
                if r.close is not None:
                    b['close'] = r.close
                b['volume'] += float(r.volume or 0.0)
                stamp = int(r.tbot_utc_stamp_ns or 0)
                if stamp > b['tbot_utc_stamp_ns']:
                    b['tbot_utc_stamp_ns'] = stamp

        new_vals = [buckets[w] for w in sorted(buckets.keys())]

        with self.env.transaction():
            while True:
                chunk = mgr.search(
                    [], order='timestamp ASC', limit=500)
                if not chunk:
                    break
                chunk.unlink()
            if new_vals:
                mgr.create(new_vals)

        self.logger.info(
            f"Rebuilt candles: {len(rows)} rows @ {old_tf_ms}ms → "
            f"{len(new_vals)} rows @ {new_tf_ms}ms")
        return len(new_vals)

    async def on_config_updated(self):
        """Apply config changes from bot_settings.

        Policy (v9):
          * symbol change → drop old dynamic table (side effect only).
          * TF change (ANY kind: cross-mode, fast→fast, slow→slow) →
            warning + stop the bot. The user must restart; on next
            start() the table will be dropped or rebuilt as appropriate.
          * Other fields only (e.g. candles_limit) → apply, keep running.
            `initial_load_done` is reset only if a full reload is
            actually warranted (symbol changed, TF changed, or
            candles_limit grew).

        Contract: NEVER raises. All exceptions are caught and logged
        internally. Callers (`_run`, `_run_fast`, external API) rely on
        this — they do not wrap this call in try/except.

        This method is safe to call from anywhere.
        """
        old_config = self.config
        old_symbol = old_config.get('symbol')
        old_table = self._candle_table_name
        old_tf = old_config.get('timeframe')
        old_fast = is_fast_timeframe(old_tf)
        old_candles_limit = old_config.get('candles_limit') or 0

        try:
            new_config = get_bot_config(self.bot_id) or {}
        except Exception as e:
            # get_bot_config is a DB read and can raise
            # (OperationalError, OSError). Contract is "never raises" —
            # log and return so the caller's loop survives.
            self.logger.error(
                f"on_config_updated: get_bot_config failed: {e}",
                exc_info=True)
            return
        if not new_config:
            self.logger.warning(
                "on_config_updated: get_bot_config returned empty — skipping")
            return

        new_symbol = new_config.get('symbol')
        new_tf = new_config.get('timeframe')
        new_candles_limit = new_config.get('candles_limit') or 0

        # Defensive coercion: stale TF in a build with FAST disabled
        # (or TF removed from FAST_TIMEFRAMES). Done BEFORE any
        # side-effects so the rest sees a consistent TF.
        valid_tfs = set(SLOW_TIMEFRAMES)
        if FAST_MODE_AVAILABLE:
            valid_tfs |= set(FAST_TIMEFRAMES)
        if new_tf not in valid_tfs:
            self.logger.warning(
                f"Timeframe {new_tf!r} is not supported — "
                f"coercing to '1m'")
            new_tf = '1m'
            new_config['timeframe'] = new_tf
            try:
                update_bot_config(self.bot_id, {'timeframe': '1m'})
            except Exception as e:
                self.logger.warning(
                    f"Persisting coerced timeframe failed: {e}")

        new_fast = is_fast_timeframe(new_tf)

        # --- Side effect: symbol change → drop old dynamic table ---
        new_table = safe_table_name(new_symbol) if new_symbol else None
        symbol_changed = bool(old_table and new_table and old_table != new_table)
        if symbol_changed:
            try:
                self.env.drop_table(old_table)
                self.logger.info(f"Dropped old table {old_table}")
            except Exception as e:
                self.logger.warning(f"Dropping {old_table} failed: {e}")

        # --- Apply new config ---
        self.config = new_config
        try:
            self._resolve_exchange_provider()
        except Exception as e:
            self.logger.warning(f"_resolve_exchange_provider failed: {e}")
        self._candle_table_name = new_table

        # Only force a full reload when it is actually needed.
        tf_changed = (old_tf != new_tf)
        limit_grew = (new_candles_limit > old_candles_limit)
        if symbol_changed or tf_changed or limit_grew:
            self.initial_load_done = False

        # --- TF-change handling ---
        if not tf_changed:
            # Only non-TF fields changed — keep running.
            return

        mode_change = (old_fast != new_fast)
        if mode_change:
            self.logger.error(
                f"Timeframe {old_tf!r} → {new_tf!r} switches "
                f"{'WS→REST' if old_fast else 'REST→WS'} mode — "
                f"restart required; stopping. On next start() the candle "
                f"table will be dropped and history reloaded.")
        else:
            self.logger.error(
                f"Timeframe {old_tf!r} → {new_tf!r} within "
                f"{'fast' if new_fast else 'slow'} mode — "
                f"restart required; stopping. On next start() the candle "
                f"table will be dropped or aggregated as appropriate.")

        self.running = False
        try:
            update_bot_status(self.bot_id, 'stopped')
        except Exception as e:
            self.logger.warning(
                f"update_bot_status(stopped) failed: {e}")

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