# modules/broker/lib/broker_service.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

"""Broker service: connection carousel, REST worker, WS workers.

Contract (plan v8 §3.1):
  * ConnectionKey/ConnectionInfo/ConnectionCarousel — stable object per
    (key, key_id); reconnect mutates client inside, subscribers preserved.
  * RequestCarousel.submit async -> RESTResponse.
  * Dedup for fetch_* only (TTL 5s, refcount). create_/cancel_/set_leverage
    — no dedup (cid already idempotent on exchange).
  * REST worker: priority cancel(0) > place(1) > fetch(2), event-driven.
  * WS price: per (exchange, market_type, symbol) loop on shared client,
    fanout to _price_cache.
  * WS trades: per (exchange, market_type, symbol) loop, raw deque with
    both clocks (exchange_ts_ns, tbot_utc_ns), no aggregation.
  * WS orders: per key_id loop, push to OrderUpdateQueue[owner_bot_id].
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple
from collections import deque

from .exchange_directory import (
    get_ccxt_market_type,
    resolve_ccxt_exchange_id,
    get_price_stream_method,
)

from .clock import install_exchange_clock

PRIORITY_CANCEL = 0
PRIORITY_PLACE = 1
PRIORITY_FETCH = 2
DEDUP_TTL_NS = 5_000_000_000

MIN_REQUEST_INTERVAL_SEC = 0.05
FETCH_TIMEOUT_SEC = 15.0
PLACE_TIMEOUT_SEC = 30.0
WS_PRICE_TIMEOUT_SEC = 15.0
WS_ORDERS_TIMEOUT_SEC = 300.0
WS_FAILURES_BEFORE_RESET = 5
WS_RESET_SLEEP_SEC = 60.0
WS_RETRY_SLEEP_SEC = 1.0

MAX_PRICE_AGE_IN_WS = 30_000_000_000
MAX_ORDER_AGE_IN_WS = 60_000_000_000

# Trades deque — raw feed, no aggregation in broker.
TRADES_DEQUE_MAXLEN = 200_000           # emergency cap (very high-volume pairs)
TRADES_HISTORY_NS   = 60_000_000_000    # 60 s — time-based eviction
WS_TRADES_TIMEOUT_SEC = 15.0

_CLOCK_ERROR_MARKERS = (
    '-1021', 'InvalidNonce', 'Timestamp for this request',
    'outside of the recvWindow', '-1022', 'Signature for this request',
)

def _is_clock_error(e: Exception) -> bool:
    s = str(e)
    return any(m in s for m in _CLOCK_ERROR_MARKERS)

async def close_tbot_client(client, logger=None):
    """Close ccxt client and its user-supplied aiohttp session.

    ccxt does NOT close user-supplied sessions on .close(); we must
    close them explicitly to avoid 'Unclosed client session' warnings
    and socket leaks after reconnect.
    """
    if client is None:
        return
    try:
        await client.close()
    except Exception as e:
        if logger:
            logger.debug(f"client.close: {e}")
    session = getattr(client, '_tbot_session', None)
    if session is not None:
        try:
            await session.close()
        except Exception as e:
            if logger:
                logger.debug(f"session.close: {e}")
        try:
            client._tbot_session = None
        except Exception:
            pass

@dataclass(frozen=True)
class ConnectionKey:
    exchange: str
    market_type: str
    kind: str  # 'rest' | 'ws_data' | 'ws_orders'
    provider: str = 'ccxt'


@dataclass
class ConnectionInfo:
    key: ConnectionKey
    key_id: Optional[str] = None
    client: Any = None
    healthy: bool = False
    created_ns: int = 0
    last_tick_ns: int = 0
    subscriber_count: int = 0


@dataclass
class RESTResponse:
    request_id: int
    result: Any = None
    error: Optional[Exception] = None
    ts_ns: int = 0


class ConnectionCarousel:
    def __init__(self, logger, time_provider):
        self.logger = logger
        self.get_time_ns = time_provider
        self._conns: Dict[Tuple[ConnectionKey, Optional[str]],
                          ConnectionInfo] = {}
        self._creating_conn: Dict[Tuple, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, key, key_id, make_client):
        cache_key = (key, key_id)
        async with self._lock:
            info = self._conns.get(cache_key)
            if info is not None and info.client is not None:
                return info
            fut = self._creating_conn.get(cache_key)
            if fut is None:
                fut = asyncio.get_running_loop().create_future()
                self._creating_conn[cache_key] = fut
                is_creator = True
                if info is None:
                    info = ConnectionInfo(key=key, key_id=key_id,
                                          created_ns=self.get_time_ns())
                    self._conns[cache_key] = info
            else:
                is_creator = False
                info = info or self._conns.get(cache_key)

        if not is_creator:
            await fut
            return info

        try:
            info.client = await make_client(key, key_id)
            info.healthy = True
        except Exception as e:
            async with self._lock:
                self._conns.pop(cache_key, None)
                self._creating_conn.pop(cache_key, None)
            if not fut.done():
                fut.set_exception(e)
                try:
                    fut.exception()  # retrieve → no warning
                except Exception:
                    pass
            raise
        async with self._lock:
            self._creating_conn.pop(cache_key, None)
        if not fut.done():
            fut.set_result(info)
        return info

    async def get(self, key, key_id):
        async with self._lock:
            return self._conns.get((key, key_id))

    async def release(self, key, key_id):
        cache_key = (key, key_id)
        async with self._lock:
            info = self._conns.pop(cache_key, None)
        if info and info.client:
            await close_tbot_client(info.client, self.logger)

    def all_infos(self) -> list:
        return list(self._conns.values())

    async def close_all(self):
        async with self._lock:
            infos = list(self._conns.values())
            self._conns.clear()
            self._creating_conn.clear()
        for info in infos:
            if info.client:
                await close_tbot_client(info.client, self.logger)


class RequestCarousel:

    WORKER_COUNT = 3

    def __init__(self, logger, make_client: Callable, time_provider):
        self.logger = logger
        self.make_client = make_client
        self.get_time_ns = time_provider
        self._queue = None
        self._workers: list = []
        self._pending: Dict = {}
        self._counter = 0
        self._rate_lock: Optional[asyncio.Lock] = None
        self._last_mono = 0.0
        self._running = False
        self._clients: Dict = {}
        self._client_owners: Dict = {}
        self._client_lock = asyncio.Lock()
        self._creating: Dict[Tuple, asyncio.Future] = {}

    def start(self):
        if self._running:
            return
        self._running = True
        self._queue = asyncio.PriorityQueue()
        self._rate_lock = asyncio.Lock()
        self._workers = [
            asyncio.create_task(
                self._worker_loop(i),
                name=f"broker_rest_worker_{i}")
            for i in range(self.WORKER_COUNT)
        ]

    async def stop(self):
        self._running = False
        for w in self._workers:
            w.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []
        for c in list(self._clients.values()):
            await close_tbot_client(c, self.logger)
        self._clients.clear()
        self._client_owners.clear()
        self._creating.clear()

    async def get_client(self, key, key_id):
        """Public access to cached client. Creates if missing."""
        from core.exchange import get_exchange_owner
        owner = get_exchange_owner() or "internal"
        return await self._get_or_create_client(key, key_id, owner)

    async def _get_or_create_client(self, key, key_id, owner="unknown"):
        cache_key = (key, key_id)

        # Fast path — lock-free
        client = self._clients.get(cache_key)
        if client is not None:
            return client

        async with self._client_lock:
            client = self._clients.get(cache_key)
            if client is not None:
                return client
            fut = self._creating.get(cache_key)
            if fut is None:
                fut = asyncio.get_running_loop().create_future()
                self._creating[cache_key] = fut
                is_creator = True
            else:
                is_creator = False

        if not is_creator:
            return await fut

        try:
            client = await self.make_client(key, key_id)
        except Exception as e:
            async with self._client_lock:
                self._creating.pop(cache_key, None)
            if not fut.done():
                fut.set_exception(e)
                try:
                    fut.exception()  # retrieve → no warning
                except Exception:
                    pass
            raise

        async with self._client_lock:
            self._clients[cache_key] = client
            self._client_owners[cache_key] = owner
            self._creating.pop(cache_key, None)
        if not fut.done():
            fut.set_result(client)
        return client

    async def submit(self, key, method, params,
                     key_id=None, deduplicate=True,
                     timeout_sec=FETCH_TIMEOUT_SEC,
                     owner: str = "unknown"):
        now_ns = self.get_time_ns()
        dedup_key = None
        if deduplicate:
            try:
                items = tuple(sorted(
                    (k, tuple(v) if isinstance(v, (list, dict)) else v)
                    for k, v in params.items()))
            except TypeError:
                items = tuple(sorted((k, str(v)) for k, v in params.items()))
            dedup_key = (key, method, items, key_id)
            entry = self._pending.get(dedup_key)
            if entry is not None:
                future, refcount, expiry = entry
                if now_ns < expiry and not future.done():
                    self._pending[dedup_key] = (future, refcount + 1, expiry)
                    return await future
                self._pending.pop(dedup_key, None)

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        if dedup_key is not None:
            self._pending[dedup_key] = (future, 1, now_ns + DEDUP_TTL_NS)

        priority = PRIORITY_FETCH
        if method.startswith('cancel_'):
            priority = PRIORITY_CANCEL
        elif method.startswith('create_') or method == 'set_leverage':
            priority = PRIORITY_PLACE

        self._counter += 1
        await self._queue.put(
            (priority, self._counter, key, method, params,
             key_id, future, timeout_sec, dedup_key, owner))
        try:
            return await asyncio.wait_for(future, timeout=timeout_sec + 5.0)
        except asyncio.TimeoutError:
            if dedup_key is not None:
                entry = self._pending.get(dedup_key)
                if entry is not None and entry[0] is future:
                    self._pending.pop(dedup_key, None)
            raise

    async def _worker_loop(self, _worker_id):
        while self._running:
            try:
                (priority, seq, key, method, params,
                 key_id, future, timeout_sec, dedup_key, owner) = \
                    await self._queue.get()
            except asyncio.CancelledError:
                break
            try:
                async with self._rate_lock:
                    now = time.monotonic()
                    delta = now - self._last_mono
                    if delta < MIN_REQUEST_INTERVAL_SEC:
                        await asyncio.sleep(MIN_REQUEST_INTERVAL_SEC - delta)
                    self._last_mono = time.monotonic()

                try:
                    client = await self._get_or_create_client(
                        key, key_id, owner)
                    result = await asyncio.wait_for(
                        getattr(client, method)(**params),
                        timeout=timeout_sec)
                    if not future.done():
                        future.set_result(RESTResponse(
                            0, result, None, self.get_time_ns()))
                except Exception as e:
                    if not future.done():
                        future.set_exception(e)
            finally:
                if dedup_key is not None:
                    entry = self._pending.get(dedup_key)
                    if entry is not None and entry[0] is future:
                        _, refcount, expiry = entry
                        if refcount <= 1:
                            self._pending.pop(dedup_key, None)
                        else:
                            self._pending[dedup_key] = (
                                future, refcount - 1, expiry)
                self._queue.task_done()


class BrokerService:
    def __init__(self, broker_bot, key_vault, order_update_queue):
        self.bot = broker_bot
        self.logger = broker_bot.logger
        self.get_time_ns = broker_bot.get_time_ns
        self.vault = key_vault
        self.order_updates = order_update_queue

        self.carousel = ConnectionCarousel(self.logger, self.get_time_ns)
        self.requests = RequestCarousel(
            self.logger, make_client=self._make_rest_client,
            time_provider=self.get_time_ns)

        self._ws_clients: Dict[Tuple, Any] = {}
        self._ws_order_clients: Dict[str, Any] = {}
        self._price_workers: Dict[Tuple, asyncio.Task] = {}
        self._orders_workers: Dict[Tuple, asyncio.Task] = {}
        self._price_cache: Dict[Tuple, dict] = {}
        self._trades: Dict[Tuple, "deque"] = {}
        self._trades_workers: Dict[Tuple, asyncio.Task] = {}
        self._trades_seen: Dict[Tuple, set] = {}
        self._trades_last_tick_ns: Dict[Tuple, int] = {}
        self._first_trade_exchange_ns: Dict[Tuple, int] = {}
        self._first_trade_tbot_ns: Dict[Tuple, int] = {}
        self._worker_started_tbot_ns: Dict[Tuple, int] = {}
        self._key_to_owner: Dict[str, int] = {}
        self._running = False
        self._orders_last_activity_ns = 0
        self._orders_last_tick_ns = 0
        self._clock_task: Optional[asyncio.Task] = None

        self._provider = (broker_bot.config or {}).get('default_provider', 'ccxt')

    async def start(self):
        if self._running:
            return
        self._running = True
        self.requests.start()
        self._clock_task = asyncio.create_task(
            self._clock_resync_loop(), name="broker_clock_resync")
        self.logger.info("BrokerService started")

    async def stop(self):
        self._running = False
        for t in list(self._price_workers.values()):
            t.cancel()
        for t in list(self._orders_workers.values()):
            t.cancel()
        for t in list(self._trades_workers.values()):
            t.cancel()
        await asyncio.gather(
            *self._price_workers.values(),
            *self._orders_workers.values(),
            *self._trades_workers.values(),
            return_exceptions=True)
        self._price_workers.clear()
        self._orders_workers.clear()
        self._trades_workers.clear()
        self._trades.clear()
        self._trades_seen.clear()
        self._trades_last_tick_ns.clear()
        self._first_trade_exchange_ns.clear()
        self._first_trade_tbot_ns.clear()
        self._worker_started_tbot_ns.clear()
        await self.requests.stop()
        if self._clock_task:
            self._clock_task.cancel()
            try:
                await self._clock_task
            except asyncio.CancelledError:
                pass
            self._clock_task = None
        self._ws_clients.clear()
        self._ws_order_clients.clear()
        await self.carousel.close_all()
        self.logger.info("BrokerService stopped")

    # ---------- REST client factory ----------

    async def _make_rest_client(self, key: ConnectionKey, key_id):
        import ccxt.async_support as ccxt_async
        from core.universal_resolver import create_aiohttp_session
        ccxt_type = get_ccxt_market_type(key.exchange, key.market_type)
        cfg = {
            'enableRateLimit': True,
            'timeout': int(FETCH_TIMEOUT_SEC * 1000),
            'options': {
                'defaultType': ccxt_type,
                'recvWindow': 10000,
            },
        }
        session = None
        try:
            session = create_aiohttp_session()
            cfg['session'] = session
        except Exception:
            session = None
        if key_id:
            ak, sk = await self.vault.get_credentials(key_id)
            cfg['apiKey'] = ak
            cfg['secret'] = sk
        ccxt_id = resolve_ccxt_exchange_id(key.exchange, key.market_type)
        client = getattr(ccxt_async, ccxt_id)(cfg)
        use_mono = (self.bot.config or {}).get('use_monotonic_nonce', False)
        install_exchange_clock(client, logger=self.logger, monotonic=use_mono)
        if session is not None:
            client._tbot_session = session
        try:
            await asyncio.wait_for(
                client.load_time_difference(), timeout=5.0)
        except Exception as e:
            self.logger.debug(
                f"load_time_difference failed for {key.exchange}/"
                f"{key.market_type}: {e}")
        await asyncio.wait_for(client.load_markets(),
                                timeout=FETCH_TIMEOUT_SEC)
        return client

    async def _make_ws_client(self, key: ConnectionKey, key_id):
        import ccxt.pro as ccxtpro
        from core.universal_resolver import create_aiohttp_session
        ccxt_type = get_ccxt_market_type(key.exchange, key.market_type)
        cfg = {
            'enableRateLimit': True,
            'options': {
                'defaultType': ccxt_type,
                'recvWindow': 10000,
            },
        }
        session = None
        try:
            session = create_aiohttp_session()
            cfg['session'] = session
        except Exception:
            session = None
        if key_id:
            ak, sk = await self.vault.get_credentials(key_id)
            cfg['apiKey'] = ak
            cfg['secret'] = sk
        ccxt_id = resolve_ccxt_exchange_id(key.exchange, key.market_type)
        client = getattr(ccxtpro, ccxt_id)(cfg)
        use_mono = (self.bot.config or {}).get('use_monotonic_nonce', False)
        install_exchange_clock(client, logger=self.logger, monotonic=use_mono)
        if session is not None:
            client._tbot_session = session
        if key_id:
            try:
                await asyncio.wait_for(
                    client.load_time_difference(), timeout=5.0)
            except Exception as e:
                self.logger.debug(
                    f"load_time_difference (ws) failed for "
                    f"{key.exchange}/{key.market_type}: {e}")
        await asyncio.wait_for(client.load_markets(),
                                timeout=FETCH_TIMEOUT_SEC)
        return client

    # ---------- REST wrapper ----------

    async def rest_call(self, exchange, market_type, method, params,
                         key_id=None,
                         timeout_sec=FETCH_TIMEOUT_SEC):
        from core.exchange import get_exchange_owner
        key = ConnectionKey(exchange, market_type, 'rest', self._provider)
        dedup = method.startswith('fetch_')
        owner = get_exchange_owner() or "internal"
        return await self.requests.submit(
            key, method, params, key_id=key_id,
            deduplicate=dedup, timeout_sec=timeout_sec,
            owner=owner)

    # ---------- public price ----------

    async def get_latest_price(self, exchange, market_type, symbol):
        cache_key = (exchange, market_type, symbol)
        cached = self._price_cache.get(cache_key)
        if cached is None:
            await self._ensure_price_worker(exchange, market_type, symbol)
            cached = self._price_cache.get(cache_key)
        if cached is None:
            return {'bid': None, 'ask': None, 'ts_ns': 0,
                    'stale': True, 'source': 'broker'}
        age_ns = self.get_time_ns() - cached['ts_ns']
        stale = age_ns > 30 * 1_000_000_000
        return {'bid': cached['bid'], 'ask': cached['ask'],
                'ts_ns': cached['ts_ns'],
                'stale': stale, 'source': 'broker'}

    # ---------- public trades ----------

    async def get_trades(
        self, exchange, market_type, symbol,
        from_ns: int = 0,
        to_ns: int = 0,                     # 0 = until now
        time_scale: str = 'exchange',       # 'exchange' | 'tbot'
    ) -> dict:
        """Return a COPY of raw trades in [from_ns, to_ns) on the
        selected time scale.

        Each returned trade always carries BOTH timestamps:
          exchange_ts_ns — trade timestamp from exchange feed
          tbot_utc_ns    — moment of receipt, TBOT UTC

        time_scale selects which timestamp the [from_ns, to_ns) filter
        and the sort key use. Consumers that need gap-free candles
        should filter on 'exchange' (market time); consumers debugging
        WS lag should filter on 'tbot'.

        Lazy-spawns the per-symbol trades worker on first call.
        """
        cache_key = (exchange, market_type, symbol)
        if cache_key not in self._trades_workers:
            self._trades_workers[cache_key] = asyncio.create_task(
                self._ws_trades_loop(exchange, market_type, symbol),
                name=f"ws_trades_{exchange}_{market_type}_{symbol}")
            await asyncio.sleep(0)

        dq = self._trades.get(cache_key)
        if not dq:
            return {
                'trades': [],
                'first_trade_exchange_ts_ns': 0,
                'first_trade_tbot_utc_ns': 0,
                'oldest_exchange_ts_ns': 0,
                'oldest_tbot_utc_ns': 0,
                'worker_started_at_tbot_utc_ns':
                    self._worker_started_tbot_ns.get(cache_key, 0),
            }

        scale_key = ('exchange_ts_ns' if time_scale == 'exchange'
                     else 'tbot_utc_ns')
        upper = to_ns if to_ns > 0 else self.get_time_ns()

        copied = [
            dict(t) for t in dq
            if from_ns <= t[scale_key] < upper
        ]
        copied.sort(key=lambda t: t[scale_key])

        return {
            'trades': copied,
            'first_trade_exchange_ts_ns':
                self._first_trade_exchange_ns.get(cache_key, 0),
            'first_trade_tbot_utc_ns':
                self._first_trade_tbot_ns.get(cache_key, 0),
            'oldest_exchange_ts_ns': dq[0]['exchange_ts_ns'],
            'oldest_tbot_utc_ns':    dq[0]['tbot_utc_ns'],
            'worker_started_at_tbot_utc_ns':
                self._worker_started_tbot_ns.get(cache_key, 0),
        }

    async def _ensure_price_worker(self, exchange, market_type, symbol):
        cache_key = (exchange, market_type, symbol)
        if cache_key in self._price_workers:
            return
        task = asyncio.create_task(
            self._ws_price_loop(exchange, market_type, symbol),
            name=f"ws_price_{exchange}_{market_type}_{symbol}")
        self._price_workers[cache_key] = task

    async def _ws_price_loop(self, exchange, market_type, symbol):
        # Unified kind: shares ccxt.pro client with _ws_trades_loop.
        key = ConnectionKey(exchange, market_type, 'ws_data', self._provider)
        ws_key = (exchange, market_type)
        method = get_price_stream_method(exchange, market_type)
        failures = 0
        while self._running:
            client = None
            try:
                client = self._ws_clients.get(ws_key)
                if client is None:
                    info = await self.carousel.get_or_create(
                        key, None, self._make_ws_client)
                    client = info.client
                    self._ws_clients[ws_key] = client

                if method == 'watch_order_book':
                    ob = await asyncio.wait_for(
                        client.watch_order_book(symbol),
                        timeout=WS_PRICE_TIMEOUT_SEC)
                    bids = ob.get('bids') or []
                    asks = ob.get('asks') or []
                    bid = bids[0][0] if bids else None
                    ask = asks[0][0] if asks else None
                    ts_ms = ob.get('timestamp')
                elif method == 'watch_ticker':
                    t = await asyncio.wait_for(
                        client.watch_ticker(symbol),
                        timeout=WS_PRICE_TIMEOUT_SEC)
                    bid = t.get('bid')
                    ask = t.get('ask')
                    ts_ms = t.get('timestamp')
                else:  # watch_bids_asks
                    t = await asyncio.wait_for(
                        client.watch_bids_asks(symbol),
                        timeout=WS_PRICE_TIMEOUT_SEC)
                    bids = t.get('bids') or []
                    asks = t.get('asks') or []
                    bid = bids[0][0] if bids else None
                    ask = asks[0][0] if asks else None
                    ts_ms = t.get('timestamp')

                if bid and ask:
                    ts_ns = (int(ts_ms * 1_000_000) if ts_ms
                              else self.get_time_ns())
                    self._price_cache[(exchange, market_type, symbol)] = {
                        'bid': bid, 'ask': ask, 'ts_ns': ts_ns,
                    }
                    failures = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                failures += 1
                if _is_clock_error(e):
                    if failures == 1:
                        self.logger.error(
                            f"ws_price: clock error from exchange — "
                            f"TBOT time drifted beyond recvWindow. "
                            f"Signed requests will keep failing until "
                            f"time_sync recovers. "
                            f"Error: {type(e).__name__}: {e!r}")
                    else:
                        self.logger.debug(
                            f"ws_price: clock error #{failures}: "
                            f"{type(e).__name__}")
                else:
                    self.logger.warning(
                        f"ws_price failed ({failures}): "
                        f"{type(e).__name__}: {e!r}")
                if failures >= WS_FAILURES_BEFORE_RESET:
                    await self.carousel.release(key, None)
                    # Only pop the cache entry if it still points to
                    # the client we were using. Another worker may have
                    # already created a fresh one.
                    if client is not None and self._ws_clients.get(ws_key) is client:
                        self._ws_clients.pop(ws_key, None)
                    failures = 0
                    sleep_sec = WS_RESET_SLEEP_SEC
                else:
                    sleep_sec = WS_RETRY_SLEEP_SEC
                try:
                    await asyncio.sleep(sleep_sec)
                except asyncio.CancelledError:
                    break

    async def _ws_trades_loop(self, exchange, market_type, symbol):
        """WS trades → raw deque.

        Filter: px <= 0 or amt <= 0 or ex_ts <= 0 discarded.
        Both clocks stored: exchange_ts_ns (market), tbot_utc_ns (receipt).

        Dedup: keyed by (exchange_ts_ns, trade_id) when trade_id is
        present. ccxt.pro adapters may replay recent trades on reconnect
        or first-subscribe; without dedup every reconnect doubles volume
        in the collector's aggregated candles.

        Eviction: time-based (tbot_utc_ns < now - TRADES_HISTORY_NS),
        robust to exchange-clock drift. `maxlen` is an emergency cap.

        Session bounds (first_trade_*) are monotone — never reset across
        reconnect. worker_started_tbot_ns is diagnostic only.

        Known limitation (documented): after reconnect, if the adapter's
        replay does NOT cover the entire gap, `first_trade_exchange_ns`
        still reflects the very first trade ever seen in this session.
        Windows starting after `first_trade_exchange_ns` are treated as
        complete even if the replay missed some trades — consumer must
        tolerate occasional brief gaps at this boundary.

        Shared client, shared fate on reset: releasing the SHARED
        ws_data connection drops the socket under _ws_price_loop as
        well. _ws_price_loop will observe the failure on its next
        `watch_*` call and recreate a fresh client immediately thanks
        to the `is client` guard on `_ws_clients.pop`.
        """
        key = ConnectionKey(exchange, market_type, 'ws_data', self._provider)
        ws_key = (exchange, market_type)
        cache_key = (exchange, market_type, symbol)
        failures = 0

        dq = self._trades.get(cache_key)
        if dq is None:
            dq = deque(maxlen=TRADES_DEQUE_MAXLEN)
            self._trades[cache_key] = dq
        seen = self._trades_seen.get(cache_key)
        if seen is None:
            seen = set()
            self._trades_seen[cache_key] = seen

        self._worker_started_tbot_ns[cache_key] = self.get_time_ns()

        while self._running:
            client = None
            try:
                client = self._ws_clients.get(ws_key)
                if client is None:
                    info = await self.carousel.get_or_create(
                        key, None, self._make_ws_client)
                    client = info.client
                    self._ws_clients[ws_key] = client

                # Idle timeout — not an error. No trades in the window
                # is normal for low-liquidity symbols. Mirrors
                # _ws_orders_loop.
                try:
                    trades = await asyncio.wait_for(
                        client.watch_trades(symbol),
                        timeout=WS_TRADES_TIMEOUT_SEC)
                except asyncio.TimeoutError:
                    failures = 0
                    self._trades_last_tick_ns[cache_key] = self.get_time_ns()
                    continue

                now_tbot_ns = self.get_time_ns()

                if trades:
                    for t in trades:
                        ts_ms = t.get('timestamp')
                        price = t.get('price')
                        if ts_ms is None or price is None:
                            continue
                        try:
                            ex_ts_ns = int(ts_ms) * 1_000_000
                            px = float(price)
                            amt = float(t.get('amount') or 0.0)
                        except (TypeError, ValueError):
                            continue
                        if px <= 0 or amt <= 0 or ex_ts_ns <= 0:
                            continue

                        trade_id = str(t.get('id') or '')
                        # Dedup only when trade_id is present. ccxt.pro
                        # adapters replay recent trades on reconnect;
                        # without this filter volume doubles.
                        if trade_id:
                            dk = (ex_ts_ns, trade_id)
                            if dk in seen:
                                continue
                            seen.add(dk)

                        dq.append({
                            'exchange_ts_ns': ex_ts_ns,
                            'tbot_utc_ns':    now_tbot_ns,
                            'price':          px,
                            'amount':         amt,
                            'side':           t.get('side'),
                            'trade_id':       trade_id,
                        })

                        # Monotone session bounds — never reset.
                        cur = self._first_trade_exchange_ns.get(cache_key, 0)
                        if cur == 0 or ex_ts_ns < cur:
                            self._first_trade_exchange_ns[cache_key] = ex_ts_ns
                        cur = self._first_trade_tbot_ns.get(cache_key, 0)
                        if cur == 0 or now_tbot_ns < cur:
                            self._first_trade_tbot_ns[cache_key] = now_tbot_ns

                    # Rebuild dedup set when it grows beyond 2× deque
                    # capacity (bounded memory; rebuilt from what's
                    # actually in dq).
                    if len(seen) > TRADES_DEQUE_MAXLEN * 2:
                        seen.clear()
                        for e in dq:
                            tid = e['trade_id']
                            if tid:
                                seen.add((e['exchange_ts_ns'], tid))

                    # Time-based eviction (tbot clock, monotone).
                    cutoff_tbot = now_tbot_ns - TRADES_HISTORY_NS
                    while dq and dq[0]['tbot_utc_ns'] < cutoff_tbot:
                        dq.popleft()

                self._trades_last_tick_ns[cache_key] = now_tbot_ns
                failures = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                failures += 1
                if _is_clock_error(e):
                    if failures == 1:
                        self.logger.error(
                            f"ws_trades {exchange}/{market_type}/{symbol}: "
                            f"clock error — {type(e).__name__}: {e!r}")
                    else:
                        self.logger.debug(
                            f"ws_trades: clock error #{failures}")
                else:
                    self.logger.warning(
                        f"ws_trades {exchange}/{market_type}/{symbol} "
                        f"failed ({failures}): {type(e).__name__}: {e!r}")
                if failures >= WS_FAILURES_BEFORE_RESET:
                    await self.carousel.release(key, None)
                    if client is not None and self._ws_clients.get(ws_key) is client:
                        self._ws_clients.pop(ws_key, None)
                    # dq and _first_trade_* are NOT reset — session history
                    # survives reconnect. Only diagnostic marker updates.
                    self._worker_started_tbot_ns[cache_key] = self.get_time_ns()
                    failures = 0
                    sleep_sec = WS_RESET_SLEEP_SEC
                else:
                    sleep_sec = WS_RETRY_SLEEP_SEC
                try:
                    await asyncio.sleep(sleep_sec)
                except asyncio.CancelledError:
                    break

    # ---------- private orders ----------

    def register_key_owner(self, key_id, owner_bot_id):
        existing = self._key_to_owner.get(key_id)
        if existing is not None and existing != owner_bot_id:
            self.logger.warning(
                f"register_key_owner: key {key_id[:8]}… already owned "
                f"by bot {existing}, overwriting with {owner_bot_id} "
                f"(recommendation: 1 key = 1 executor)")
        self._key_to_owner[key_id] = owner_bot_id
        cache_key = ('orders', key_id)
        existing_task = self._orders_workers.get(cache_key)
        if existing_task is None or existing_task.done():
            self._orders_workers[cache_key] = asyncio.create_task(
                self._ws_orders_loop(key_id),
                name=f"ws_orders_{key_id}")

    def unregister_key_owner(self, key_id, caller_id=None):
        existing = self._key_to_owner.get(key_id)
        if caller_id is not None and existing != caller_id:
            # Non-owner trying to unregister — ignore silently.
            return
        self._key_to_owner.pop(key_id, None)
        self._ws_order_clients.pop(key_id, None)
        task = self._orders_workers.pop(('orders', key_id), None)
        if task:
            task.cancel()

    async def _ws_orders_loop(self, key_id):
        try:
            info_row = await self.vault.get_key_info(key_id)
            if not info_row:
                self.logger.error(f"ws_orders: key {key_id} not found")
                return
            exchange = info_row['exchange']
            market_type = info_row['market_type']
        except Exception as e:
            self.logger.error(f"ws_orders: get_key_info failed: {e}")
            return

        key = ConnectionKey(exchange, market_type, 'ws_orders',
                            self._provider)
        failures = 0
        while self._running:
            try:
                info = await self.carousel.get_or_create(
                    key, key_id, self._make_ws_client)
                client = info.client
                self._ws_order_clients[key_id] = client
                try:
                    orders = await asyncio.wait_for(
                        client.watch_orders(),
                        timeout=WS_ORDERS_TIMEOUT_SEC)
                except asyncio.TimeoutError:
                    # Idle stream: no order updates for
                    # WS_ORDERS_TIMEOUT_SEC. ccxt.pro keeps the socket
                    # alive internally; watch_orders returns only on
                    # updates. NOT a failure — BUT this proves the
                    # socket is alive, so update the liveness tick.
                    self._orders_last_tick_ns = self.get_time_ns()
                    self.logger.debug(
                        f"ws_orders {key_id}: idle "
                        f"{WS_ORDERS_TIMEOUT_SEC:.0f}s — no updates")
                    failures = 0
                    continue
                failures = 0
                self._orders_last_tick_ns = self.get_time_ns()  # liveness
                self._orders_last_activity_ns = self.get_time_ns()  # event
                owner = self._key_to_owner.get(key_id)
                if owner is not None:
                    for order in orders:
                        cid = order.get('clientOrderId')
                        if cid:
                            try:
                                await self.order_updates.push(
                                    owner, cid, order)
                            except Exception as push_err:
                                # Push is defensive (deque ops under
                                # async lock); if it throws, don't kill
                                # the whole WS loop.
                                self.logger.debug(
                                    f"ws_orders {key_id}: push failed: "
                                    f"{type(push_err).__name__}: {push_err}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                failures += 1
                if _is_clock_error(e):
                    if failures == 1:
                        self.logger.error(
                            f"ws_orders {key_id}: clock error from exchange — "
                            f"TBOT time drifted beyond recvWindow. "
                            f"Signed requests will keep failing until "
                            f"time_sync recovers. "
                            f"Error: {type(e).__name__}: {e!r}")
                    else:
                        self.logger.debug(
                            f"ws_orders {key_id}: clock error #{failures}: "
                            f"{type(e).__name__}")
                else:
                    self.logger.warning(
                        f"ws_orders {key_id} failed ({failures}): "
                        f"{type(e).__name__}: {e!r}")
                if failures >= WS_FAILURES_BEFORE_RESET:
                    await self.carousel.release(key, key_id)
                    self._ws_order_clients.pop(key_id, None)
                    failures = 0
                    sleep_sec = WS_RESET_SLEEP_SEC
                else:
                    sleep_sec = WS_RETRY_SLEEP_SEC
                try:
                    await asyncio.sleep(sleep_sec)
                except asyncio.CancelledError:
                    break

    # ---------- status ----------

    def get_status(self) -> dict:
        now_ns = self.get_time_ns()

        # ---------- PRICE ----------
        last_price_ns = max(
            (c['ts_ns'] for c in self._price_cache.values()), default=0)
        price_age = now_ns - last_price_ns if last_price_ns else 0

        if not self._price_workers:
            ws_price_state = 'off'
            ws_price_healthy = None
        elif not self._price_cache:
            ws_price_state = 'connecting'
            ws_price_healthy = False
        elif price_age < MAX_PRICE_AGE_IN_WS:
            ws_price_state = 'active'
            ws_price_healthy = True
        else:
            ws_price_state = 'stalled'
            ws_price_healthy = False

        # ---------- ORDERS ----------
        orders_workers = len(self._orders_workers)
        orders_owners = len(self._key_to_owner)
        orders_with_events = self.order_updates.stats()['owners']
        orders_clients = len(self._ws_order_clients)

        max_tick_age_ns = (
            WS_ORDERS_TIMEOUT_SEC + WS_RETRY_SLEEP_SEC * WS_FAILURES_BEFORE_RESET
        ) * 1_000_000_000

        if orders_workers == 0:
            ws_orders_state = 'off'
            ws_orders_healthy = None
        elif orders_clients < orders_workers:
            # at least one worker is in a retry state — the client
            # has not yet been created
            ws_orders_state = 'connecting'
            ws_orders_healthy = False
        elif self._orders_last_tick_ns == 0:
            ws_orders_state = 'connected'  # client exists, no tick yet
            ws_orders_healthy = True
        elif now_ns - self._orders_last_tick_ns > max_tick_age_ns:
            ws_orders_state = 'stalled'
            ws_orders_healthy = False
        elif self._orders_last_activity_ns == 0:
            ws_orders_state = 'idle'  # WS is alive; no orders yet
            ws_orders_healthy = True
        else:
            ws_orders_state = 'active'
            ws_orders_healthy = True

        # ---------- REST ----------
        rest_client_info = []
        for k in self.requests._clients.keys():
            key, kid = k
            label = f"{key.exchange}/{key.market_type}"
            if kid:
                label += "(private)"
            owner = self.requests._client_owners.get(k, "?")
            rest_client_info.append(f"{label} — {owner}")

        # ---------- TRADES ----------
        trades_workers = len(self._trades_workers)
        trades_symbols = len(self._trades)

        if trades_workers == 0:
            trades_state = 'off'
            trades_healthy = None
        elif not self._trades:
            trades_state = 'connecting'
            trades_healthy = False
        else:
            last_tick = max(self._trades_last_tick_ns.values(), default=0)
            max_tick_age_ns = (
                WS_TRADES_TIMEOUT_SEC
                + WS_RETRY_SLEEP_SEC * WS_FAILURES_BEFORE_RESET
            ) * 1_000_000_000
            if last_tick == 0:
                trades_state = 'connecting'
                trades_healthy = False
            elif (now_ns - last_tick) < max_tick_age_ns:
                trades_state = 'active'
                trades_healthy = True
            else:
                trades_state = 'stalled'
                trades_healthy = False

        last_trade_tick_ns = max(
            self._trades_last_tick_ns.values(), default=0)
        last_trade_age_ns = (
            (now_ns - last_trade_tick_ns)
            if last_trade_tick_ns else 0)

        return {
            'running': self._running,
            'ws_connections': len(self.carousel.all_infos()),
            'rest_clients': len(self.requests._clients),
            'rest_client_keys': rest_client_info,
            'price_workers': len(self._price_workers),
            'ws_price_healthy': ws_price_healthy,
            'price_state': ws_price_state,
            'price_symbols': len(self._price_cache),
            'last_price_tick_ns': last_price_ns,
            'last_price_age_ns': price_age,
            'orders_workers': orders_workers,
            'orders_owners': orders_owners,
            'orders_with_events': orders_with_events,
            'orders_state': ws_orders_state,
            'ws_orders_healthy': ws_orders_healthy,
            'trades_workers': trades_workers,
            'trades_symbols': trades_symbols,
            'trades_state': trades_state,
            'ws_trades_healthy': trades_healthy,
            'last_trade_tick_ns': last_trade_tick_ns,
            'last_trade_age_ns': last_trade_age_ns,
        }

    async def _clock_resync_loop(self):
        """Re-sync TBOT UTC drift with exchange servers every 10 min.

        Even with install_exchange_clock + load_time_difference at start,
        long-running sessions accumulate drift (NTP jumps, sleep/wake).
        Binance -1021 on signed requests will recur without periodic resync.
        """
        while self._running:
            try:
                await asyncio.sleep(600)
                if not self._running:
                    return

                # REST clients
                for cache_key, client in list(self.requests._clients.items()):
                    try:
                        await asyncio.wait_for(
                            client.load_time_difference(), timeout=5.0)
                    except Exception as e:
                        self.logger.debug(
                            f"clock resync REST {cache_key[0].exchange}: {e}")

                # WS order clients
                for key_id, client in list(self._ws_order_clients.items()):
                    try:
                        await asyncio.wait_for(
                            client.load_time_difference(), timeout=5.0)
                    except Exception as e:
                        self.logger.debug(
                            f"clock resync WS orders {key_id[:8]}: {e}")

            except asyncio.CancelledError:
                return
            except Exception as e:
                self.logger.warning(f"clock resync loop: {e}")