# Broker Bot — Technical Manual

> ## ⚠️ DEMONSTRATION / EDUCATIONAL VERSION
>
> **This is a demonstration and educational version, not a finished product.**
>
> - The module is provided to illustrate the T.B.O.T architecture, exchange-integration patterns, and UI conventions — not as a production-ready component.
> - Behavior, configs, and file layout may change without notice.
> - You are responsible for your own security review, testing, and compliance if you build on this code.

> ## 💰 RISK DISCLAIMER
>
> **Trading cryptocurrencies involves significant risk and may result in the loss of your entire invested capital.**
>
> - Loss of API keys (plain storage mode) means total loss of funds. Use encryption and test on testnet first.
> - Exchange APIs, rate limits, and rules can change at any time; a client that works today may fail tomorrow.
> - This software is provided **for educational and research purposes only**, "as is", without warranty of any kind.
> - The author assumes **no responsibility or liability** for any financial loss, key leak, or other damage incurred through the use of this software.
> - **Never trade with funds you cannot afford to lose.** Test on demo/testnet environments with minimal capital first.

---

## Contents

1. [Role in the system](#1-role-in-the-system)
2. [Architecture](#2-architecture)
3. [BrokerService — connection & request carousels](#3-brokerservice--connection--request-carousels)
4. [REST pipeline](#4-rest-pipeline)
5. [WebSocket streams](#5-websocket-streams)
6. [Order-update queue](#6-order-update-queue)
7. [KeyVault and crypto](#7-keyvault-and-crypto)
8. [Exchange directory](#8-exchange-directory)
9. [Clock synchronization](#9-clock-synchronization)
10. [Capabilities (public API)](#10-capabilities-public-api)
11. [Configuration](#11-configuration)
12. [Environment variables](#12-environment-variables)
13. [Database schema](#13-database-schema)
14. [UI behaviour](#14-ui-behaviour)
15. [Diagnostics and health](#15-diagnostics-and-health)
16. [Failure modes and recovery](#16-failure-modes-and-recovery)
17. [Limitations and warnings](#17-limitations-and-warnings)

---

## 1. Role in the system

Broker is the **single exchange gateway** for the whole T.B.O.T installation. Every other bot (collector, grid, execution) talks to an exchange **only** through Broker.

```
┌─────────────┐  get_ohlcv / get_latest_price / create_*  ┌─────────────┐
│ Collector   │ ─────────────────────────────────────────►│             │
├─────────────┤                                           │             │
│ Grid        │  get_markets / get_symbols                │  Broker     │──► Exchange
├─────────────┤ ─────────────────────────────────────────►│  (this      │    (REST + WS)
│ Execution   │  create_* / cancel_order / fetch_* /      │  module)    │
│             │  panic_close / get_order_updates /        │             │
│             │  register_key_owner                       │             │
└─────────────┘ ─────────────────────────────────────────►└─────────────┘
```

**What Broker owns:**
- ccxt / ccxt.pro clients, their aiohttp sessions, WebSocket connections.
- All API keys, their encryption, and their lock/unlock lifecycle.
- The REST rate-limit budget (a single `asyncio.PriorityQueue` across all consumers).
- The `fetch_*` deduplication cache (5 s TTL).
- Per-owner order-update streams (`OrderUpdateQueue[owner_bot_id]`).
- TBOT-clock injection into ccxt (`exchange.milliseconds()`).
- Public price cache (`get_latest_price` fanout).
- Raw trades buffer per `(exchange, market_type, symbol)` — 60 s ring
  deque with **both** clocks (`exchange_ts_ns`, `tbot_utc_ns`).
  Source for `get_trades`; no aggregation in broker.

**What Broker does NOT do:**
- Make trading decisions.
- Persist order plans (that is grid/execution).
- Arbitrate between multiple execution bots — it accepts multiple owners per key_id (with a warning) and fans out order updates per owner.
- Aggregate trades into candles — that is the collector's job (fast mode).

**Manifest quirk:** `__manifest__.py` declares `"depends": []`. At runtime Broker has no consumer dependency.

---

## 2. Architecture

### 2.1. Components

| Component | File | Purpose |
|---|---|---|
| `BrokerBot` | `models.py` | Lifecycle, capabilities, key CRUD, order updates, panic close |
| `BrokerService` | `lib/broker_service.py` | Orchestrates carousels, WS workers, REST queue, clock resync |
| `ConnectionCarousel` | `lib/broker_service.py` | Stable WS connection objects per `(ConnectionKey, key_id)` |
| `RequestCarousel` | `lib/broker_service.py` | Priority REST queue + client cache + dedup |
| `ConnectionKey` / `ConnectionInfo` / `RESTResponse` | `lib/broker_service.py` | Data classes |
| `KeyVault` | `lib/key_vault.py` | Encrypted key storage + in-memory decrypted cache |
| `crypto` | `lib/crypto.py` | Fernet + PBKDF2-SHA256 helpers |
| `install_exchange_clock` | `lib/clock.py` | Replaces `exchange.milliseconds` with TBOT UTC |
| `EXCHANGES`, `MARKETS`, `_PARAM_MAP`, `EXCHANGE_WORKAROUNDS` | `lib/exchange_directory.py` | Single source of truth for exchange metadata |
| `OrderUpdateQueue` | `lib/order_updates.py` | Per-owner bounded deques + event wakeups |
| `broker_form`, `render_broker_block`, `BrokerTypeMeta` | `components.py` | UI form, health panel, type metadata, callbacks |

### 2.2. Internal state of `BrokerBot`

```python
self._ui_password:    str | None               # set by UI "Unlock keys"; priority over env
self._vault:          KeyVault                 # broker_keys table + _decrypted cache
self._order_updates:  OrderUpdateQueue         # one deque per owner_bot_id
self._service:        BrokerService            # carousels, workers
```

### 2.3. Internal state of `BrokerService`

```python
self.carousel:         ConnectionCarousel      # WS-side cache (per key_id)
self.requests:         RequestCarousel         # REST-side cache + queue

self._ws_clients:       dict[(exchange, market_type), client]  # ws_data (price + trades share)
self._ws_order_clients: dict[key_id, client]                    # ws_orders
self._price_workers:    dict[(exchange, market_type, symbol), Task]
self._orders_workers:   dict[('orders', key_id), Task]
self._trades_workers:   dict[(exchange, market_type, symbol), Task]
self._price_cache:      dict[(exchange, market_type, symbol), {bid, ask, ts_ns}]
self._trades:           dict[(exchange, market_type, symbol), deque(maxlen=200_000)]
self._trades_seen:      dict[(exchange, market_type, symbol), set[(ts_ns, trade_id)]]
self._trades_last_tick_ns:       dict[(exchange, market_type, symbol), int]
self._first_trade_exchange_ns:   dict[(exchange, market_type, symbol), int]  # monotone session min
self._first_trade_tbot_ns:       dict[(exchange, market_type, symbol), int]  # monotone session min
self._worker_started_tbot_ns:    dict[(exchange, market_type, symbol), int]  # diagnostic
self._key_to_owner:     dict[key_id, owner_bot_id]

self._orders_last_activity_ns    # last real order event
self._orders_last_tick_ns        # last wakeup (event OR idle timeout)
self._clock_task                 # 10-minute resync loop
self._provider: str              # from config['default_provider'], default 'ccxt'
```

---

## 3. BrokerService — connection & request carousels

### 3.1. ConnectionCarousel (WS side)

```python
class ConnectionCarousel:
    def __init__(self, logger, time_provider): ...
    async def get_or_create(self, key: ConnectionKey, key_id, make_client) -> ConnectionInfo
    async def get(self, key, key_id) -> ConnectionInfo | None
    async def release(self, key, key_id) -> None  # pops and closes
    def all_infos(self) -> list[ConnectionInfo]
    async def close_all(self) -> None
```

- Keyed by `(ConnectionKey, key_id)`. `ConnectionKey` is a frozen dataclass `(exchange, market_type, kind, provider)`.
- **Concurrent creation is coalesced** via a per-key `asyncio.Future` — only one coroutine actually calls `make_client`; others await the future.
- **Reconnect mutates the client inside** the existing `ConnectionInfo`; subscribers (WS loops) preserve their reference to the info, not to the client. This is what allows `WS_FAILURES_BEFORE_RESET` to replace the client without losing the loop state.

`ConnectionKey.kind` values:

| kind | Purpose | Shared |
|---|---|---|
| `'rest'` | REST client cache key | No |
| `'ws_data'` | Price (`watch_trades` + `watch_*`) stream | **Shared between price and trades loops** |
| `'ws_orders'` | Private order stream (per key_id) | No |

### 3.2. RequestCarousel (REST side)

```python
class RequestCarousel:
    WORKER_COUNT = 3
    async def start(self) -> None
    async def stop(self) -> None
    async def submit(self, key, method, params, key_id=None,
                     deduplicate=True, timeout_sec=FETCH_TIMEOUT_SEC,
                     owner: str = "unknown") -> RESTResponse
    async def get_client(self, key, key_id) -> Any
```

- **3 worker tasks**, each pulls `(priority, seq, key, method, params, key_id, future, timeout_sec, dedup_key, owner)` from an `asyncio.PriorityQueue`.
- **Client cache** — independent from `ConnectionCarousel._conns`. Has its own `_clients` dict and `_client_lock` (separate from the WS carousel). Same coalescing pattern.
- **Owner attribution** — every `submit` reads `get_exchange_owner()` from the `_owner_ctx` contextvar in `core.exchange` (defaults to `"internal"`). The owner is stored in `_client_owners[(key, key_id)]` and appears in `get_status().rest_client_keys`.

### 3.3. Rate limiting

A single `asyncio.Lock` + `_last_mono` timestamp enforces `MIN_REQUEST_INTERVAL_SEC = 0.05` between successive worker dispatches. Across all 3 workers this yields **at most 20 dispatch ticks per second**, regardless of how many priority entries are queued. Each dispatched request then hits ccxt's own `enableRateLimit=True` throttle inside the client.

**Both limits apply.** Broker's floor protects against burst congestion; ccxt's per-endpoint limits protect against exchange-specific throttles.

---

## 4. REST pipeline

### 4.1. Priority classification

```python
priority = PRIORITY_FETCH                # 2
if method.startswith('cancel_'):         priority = PRIORITY_CANCEL   # 0
elif method.startswith('create_') or method == 'set_leverage':
                                          priority = PRIORITY_PLACE    # 1
```

Within the same priority, entries are FIFO by a monotonic `self._counter` (tie-break in the tuple).

### 4.2. Deduplication

**Only `fetch_*` methods are deduplicated.**

```python
deduplicate = method.startswith('fetch_')
```

Create/cancel/set_leverage are **never** deduped — their `clientOrderId` is already the idempotency key on the exchange side, and merging concurrent calls would be wrong (each cancel targets a specific order).

Dedup key = `(key, method, items, key_id)` where `items = sorted(params)`. TTL = `DEDUP_TTL_NS = 5 s`. Refcounted: a completing worker decrements the refcount; the entry is removed when refcount hits 0 or the TTL expires.

### 4.3. Timeouts

| Constant | Value | Applied to |
|---|---|---|
| `FETCH_TIMEOUT_SEC` | 15.0 | `fetch_*` capabilities |
| `PLACE_TIMEOUT_SEC` | 30.0 | `create_market_order`, `create_limit_order` |
| Caller override | any | `submit(..., timeout_sec=...)` — used by `cancel_order`, `panic_close` |

`submit` waits on the future with `timeout_sec + 5.0` on the caller side (giving the worker headroom for its own `wait_for`).

### 4.4. Client creation (REST)

`_make_rest_client`:

1. `ccxt.async_support` module.
2. `get_ccxt_market_type(exchange, market_type)` → `ccxt_type`.
3. `cfg = {'enableRateLimit': True, 'timeout': int(FETCH_TIMEOUT_SEC*1000), 'options': {'defaultType': ccxt_type, 'recvWindow': 10000}}`.
4. `create_aiohttp_session()` from `core.universal_resolver`; on failure — proceeds without a custom session.
5. If `key_id` — fetches credentials from the vault, injects `apiKey`/`secret`.
6. `ccxt_id = resolve_ccxt_exchange_id(exchange, market_type)`.
7. `client = getattr(ccxt_async, ccxt_id)(cfg)`.
8. `install_exchange_clock(client, logger, monotonic=config['use_monotonic_nonce'])`.
9. `client._tbot_session = session` — for later explicit close.
10. Best-effort `load_time_difference()` (5 s timeout, non-fatal).
11. `load_markets()` (up to `FETCH_TIMEOUT_SEC`).

**Note:** `close_tbot_client` explicitly closes the aiohttp session stored in `client._tbot_session` in addition to `client.close()`. ccxt does **not** close user-supplied sessions by itself; without this you would accumulate `Unclosed client session` warnings and socket leaks after each reconnect.

### 4.5. Error handling

- Errors inside the worker are captured and set on the future via `future.set_exception(e)`.
- Clock errors are detected but not auto-handled in REST — they surface to the caller (execution/grid) which decides whether to retry or pause.
- `cancel_order` in `BrokerBot._cap_cancel_order` **treats `-2011` / `Unknown order` / `OrderNotFound` as success** and returns `True` — the order is already gone; the caller will `fetch_order` for the terminal state.

---

## 5. WebSocket streams

All WS loops share the following failure-handling contract:

- Any exception increments `failures`.
- `failures >= WS_FAILURES_BEFORE_RESET (5)` → `carousel.release(key, key_id)` + remove from the cache dict (guarded by `is client` — see §5.1) + sleep `WS_RESET_SLEEP_SEC = 60.0`.
- Otherwise sleep `WS_RETRY_SLEEP_SEC = 1.0`.
- The task itself does **not** die — it re-enters the loop and recreates the client.
- **Clock errors** (`-1021`, `InvalidNonce`, `Timestamp for this request`, `outside of the recvWindow`, `-1022`, `Signature for this request`) — first occurrence → `ERROR` log; subsequent → `DEBUG`.

### 5.1. Price stream

One asyncio task per `(exchange, market_type, symbol)`, spawned lazily on the first `get_latest_price` call for that key.

- **Client**: `ccxt.pro` per `(exchange, market_type)`, cached in `self._ws_clients`, obtained through
  `ConnectionCarousel.get_or_create(ConnectionKey(..., 'ws_data', ...), None, self._make_ws_client)`.
  **Shared with the trades stream** — same client, same aiohttp session, two independent `watch_*` loops (`watch_trades` + the price method).
- **Method dispatch**: `get_price_stream_method(exchange, market_type)` from `exchange_directory`:
  - `'watch_bids_asks'` — default
  - `'watch_order_book'` — from `EXCHANGE_WORKAROUNDS` (e.g., Binance USDT-M perp)
  - `'watch_ticker'` — alternative
  - `'rest_poll'` — not handled by the WS loop; a `rest_poll` workaround is not implemented in the current code (MEXC swap lists it, but no REST-poll task is spawned — this is a **known gap**).
- **Timeout**: `WS_PRICE_TIMEOUT_SEC = 15.0`.
- **Cache**: `self._price_cache[(exchange, market_type, symbol)] = {'bid', 'ask', 'ts_ns'}`.
- **Shared client, shared fate on reset**: when either `_ws_price_loop` or `_ws_trades_loop` hits `WS_FAILURES_BEFORE_RESET`, it releases the shared `ws_data` connection. The peer loop observes the failure on its next `watch_*` call, increments its own `failures`, and immediately recreates a fresh client — the `_ws_clients.pop(ws_key, None)` is guarded by `is client` so it does not drop a newer connection created by the peer. Only the worker that triggered the reset pays the full 60 s sleep.

### 5.2. Orders stream

One asyncio task per `key_id`, spawned by `register_key_owner` (and only if not already running).

- **Client**: `ccxt.pro` per `key_id`, via `ConnectionCarousel` with `ConnectionKey(..., 'ws_orders', ...)`.
- **Method**: `watch_orders()` — all symbols on the account/key.
- **Timeout**: `WS_ORDERS_TIMEOUT_SEC = 300.0`. **On timeout it is not a failure** — the idle stream proves the socket is alive. It updates `_orders_last_tick_ns` and continues (`failures = 0`).
- **On update**:
  - `_orders_last_tick_ns = now`
  - `_orders_last_activity_ns = now`
  - For each order: `owner = self._key_to_owner.get(key_id)`; if set — `await order_updates.push(owner, cid, order)`. Push failures are logged at DEBUG and do **not** terminate the loop.
- **Timeout as liveness**: this is why `get_status()` uses
  `max_tick_age_ns = (WS_ORDERS_TIMEOUT_SEC + WS_RETRY_SLEEP_SEC * WS_FAILURES_BEFORE_RESET) * 1e9 = 305 s`
  as the stall threshold.

### 5.3. Trades stream

One asyncio task per `(exchange, market_type, symbol)`, spawned lazily on the first `get_trades` call for that key.

- **Client**: shares the `ws_data` ccxt.pro client with the price stream (see §5.1). Two independent `watch_*` calls multiplex on the same connection.
- **Method**: `client.watch_trades(symbol)`.
- **Storage**: a bounded `deque(maxlen=TRADES_DEQUE_MAXLEN=200_000)` per key. Each entry:
  ```python
  {
    'exchange_ts_ns': int,   # from the exchange feed
    'tbot_utc_ns':    int,   # TBOT UTC at receipt
    'price':          float, # > 0
    'amount':         float, # > 0
    'side':           str | None,
    'trade_id':       str,   # '' if exchange does not provide one
  }
  ```
- **Filter** at ingest: `px <= 0`, `amt <= 0`, `ex_ts_ns <= 0` — dropped. This eliminates the zero-trade artifact (`0.0 0.0 0.0 0.0 0.0` rows seen in earlier versions).
- **Dedup**: `(exchange_ts_ns, trade_id)` when `trade_id` is present. Needed because ccxt.pro adapters replay recent trades on reconnect or first subscribe — without this, the collector's aggregated candle `volume` doubles on every reconnect. If `trade_id` is empty, no dedup (rare; most exchanges provide IDs).
- **Eviction**: time-based on `tbot_utc_ns < now - TRADES_HISTORY_NS`. `TRADES_HISTORY_NS = 60 s`. `maxlen` is an **emergency cap** for extremely high-volume pairs; time eviction dominates in practice. Both clocks are stored **deliberately**: `exchange_ts_ns` drives candle windowing (market time), `tbot_utc_ns` drives eviction (monotone, immune to exchange clock drift).
- **Session bounds** (`_first_trade_exchange_ns`, `_first_trade_tbot_ns`): **monotone minima** — never reset, even across reconnect. They mark "the first trade this worker ever saw" and let consumers detect a partial-start boundary. `_worker_started_tbot_ns` is diagnostic only.
- **Idle timeout**: `asyncio.wait_for(..., timeout=WS_TRADES_TIMEOUT_SEC)` (15 s). A timeout is **not a failure** — it proves the socket is alive (mirrors the orders stream). `_trades_last_tick_ns` updates and `failures = 0`.
- **Failure handling**: on real exception `failures += 1`. At `WS_FAILURES_BEFORE_RESET` — release the shared `ws_data` client (see §5.1 "shared fate"), reset the diagnostic `_worker_started_tbot_ns`, sleep 60 s. The deque and `_first_trade_*` bounds are **not** cleared: session history survives reconnect.
- **Known limitation — replay boundary**: after reconnect, if the adapter's replay does not cover the entire gap, `_first_trade_exchange_ns` still reflects the earliest trade ever seen. Windows starting after that boundary are treated as complete even if the replay missed a few trades at the edge. Consumer (collector) tolerates this as an occasional brief gap — see the collector README's Rule G (gaps are legal, zero candles are not).

Constants (`broker_service.py`):

| Constant | Value | Meaning |
|---|---|---|
| `TRADES_DEQUE_MAXLEN` | `200_000` | Emergency cap per symbol |
| `TRADES_HISTORY_NS` | `60_000_000_000` | Time-based eviction (60 s) |
| `WS_TRADES_TIMEOUT_SEC` | `15.0` | Idle wait; not an error |

### 5.4. `register_key_owner` and `unregister_key_owner`

```python
def register_key_owner(self, key_id, owner_bot_id):
    existing = self._key_to_owner.get(key_id)
    if existing is not None and existing != owner_bot_id:
        self.logger.warning(
            f"register_key_owner: key {key_id[:8]}… already owned by bot "
            f"{existing}, overwriting with {owner_bot_id} "
            f"(recommendation: 1 key = 1 executor)")
    self._key_to_owner[key_id] = owner_bot_id
    # spawn _ws_orders_loop if not running
```

- **Multiple owners per key are allowed** (with a warning). The last caller becomes the active owner for that key_id; previous owners will stop receiving updates because `push` routes by `owner = self._key_to_owner.get(key_id)` (single owner per key).
- `unregister_key_owner(key_id, caller_id)` — **silently ignored** if `caller_id` is provided and does not match the current owner (non-owner trying to unregister is a no-op).

Note: `_ws_trades_loop` is **not** tied to `key_id` — see §17.14.

### 5.5. Clock resync loop

`_clock_resync_loop` — every **600 s**:

1. For each REST client in `self.requests._clients`: `await client.load_time_difference()` (5 s timeout, best-effort).
2. For each WS order client in `self._ws_order_clients`: same.

Rationale: even with `install_exchange_clock` and the initial `load_time_difference`, long sessions can accumulate drift (NTP jumps, sleep/wake). Without periodic resync, Binance `-1021` on signed requests recurs.

---

## 6. Order-update queue

`OrderUpdateQueue` in `lib/order_updates.py` — one bounded deque + one `asyncio.Event` per `owner_bot_id`.

| Constant | Value |
|---|---|
| `MAXLEN` | `10_000` |
| `MAX_BATCH` | `100` |
| `OVERFLOW_LOG_INTERVAL_NS` | `60 s` |

- **`push(owner, cid, update)`**: monotonic id per owner; on overflow — oldest evicted, warning throttled to once per 60 s per owner. Sets the owner's `asyncio.Event`.
- **`poll(owner, since_id, timeout_sec)`**: returns the first `MAX_BATCH` records with `id > since_id`. If empty — waits on the event up to `timeout_sec`, then re-checks. `since_id` is **exclusive**.
- **`cleanup(owner)`**: drops the deque, id counter, event, and overflow-timer for that owner.
- **`stats()`**: `{'owners': int, 'total_records': int}`.

The consumer (execution) calls `get_order_updates(owner_bot_id, since_id, timeout_sec=0.3)` in a loop, advancing `since_id` to the max received id.

---

## 7. KeyVault and crypto

### 7.1. Table

`broker_keys` (auto-created by `KeyVault._ensure_table`):

| Column | Type | Notes |
|---|---|---|
| `key_id` | TEXT PK | `uuid.uuid4()` |
| `exchange` | TEXT NOT NULL | |
| `market_type` | TEXT NOT NULL | |
| `label` | TEXT | Freeform user label |
| `mode` | TEXT NOT NULL | `plain` \| `encrypted` |
| `api_key` | TEXT | plaintext mode only |
| `api_secret` | TEXT | plaintext mode only |
| `api_key_enc` | TEXT | Fernet token (encrypted mode only) |
| `api_secret_enc` | TEXT | Fernet token |
| `salt_key` | TEXT | Base64 salt for the API key |
| `salt_secret` | TEXT | Base64 salt for the secret |
| `created_ns` | INTEGER | |
| `updated_ns` | INTEGER | |

Index: `idx_broker_keys_ex_mt` on `(exchange, market_type)`.

### 7.2. Crypto

`lib/crypto.py`:

```
PBKDF2HMAC(SHA256, length=32, salt=16 bytes, iterations=200_000)
    → base64.urlsafe_b64encode(...)
Fernet(key).encrypt(plaintext.encode()).decode()
```

- Per-key **two salts**: one for `api_key`, one for `api_secret`.
- `encrypt(plaintext, password)` → `(token, salt_b64)`.
- `decrypt(token, salt_b64, password)` → plaintext.
- `encrypt_with_salt(plaintext, password, salt_b64)` — reuse a stored salt.
- `new_salt()` — `base64.b64encode(os.urandom(16))`.

### 7.3. Password priority

Per docstring: **UI-unlocked > `TBOT_KEY_PASSWORD` env > `None`**.

Implementation:

```python
def _password_provider(self):
    if self._ui_password:
        return self._ui_password
    return os.environ.get('TBOT_KEY_PASSWORD')
```

- `_ui_password` is set by `_cap_unlock_keys` (the UI's "Unlock keys" button) and **persists in memory** until process restart.
- Every successfully decrypted key is cached in `_decrypted[key_id] = (ak, sk)`. Subsequent `get_credentials` calls hit the cache and **never re-decrypt**.
- Encrypted keys that were not unlocked by the last password remain locked (`KeyLockedError` on `get_credentials`). UI shows them as `locked`.

### 7.4. Lifecycle

| Operation | Effect |
|---|---|
| `add_key(mode='plain')` | Stores plaintext, **does not** add to `_decrypted` |
| `add_key(mode='encrypted')` | Requires password; encrypts; caches decrypted in `_decrypted` |
| `get_credentials(key_id)` | Cache hit → return; miss → decrypt (plain or encrypted), cache, return. Raises `KeyLockedError` on failure |
| `unlock(password)` | Iterates all `mode='encrypted'` keys; caches those it can decrypt; already-cached keys are returned in `unlocked` (not a failure); if nothing decrypted → `ValueError("Wrong password")` |
| `invalidate(key_id)` | Removes from `_decrypted` (does not touch DB) |
| `delete_key(key_id)` | Removes from DB and `_decrypted` |

`list_keys()` adds a computed `locked` field: `mode == 'encrypted' and key_id not in _decrypted`.

### 7.5. Recommendations

- **1 key = 1 execution bot.** Multiple executors sharing one key_id will contend for the same WS orders stream; the last registered owner wins.
- **Encrypted mode is strongly recommended** for any real usage. Plain mode stores keys in cleartext inside the bot's SQLite DB.
- **`TBOT_KEY_PASSWORD` in env is weaker than UI-unlock** for automation — an env var is readable by anything with process access. Use it only in a locked-down environment.

---

## 8. Exchange directory

`lib/exchange_directory.py` — **single source of truth** for exchange metadata. Rule: any exchange-specific `if` must live here, not in runtime components.

### 8.1. Static UI data

```python
EXCHANGES = [
    {'label': 'Binance', 'value': 'binance', 'default': True,
     'defaults': {'market': 'spot', 'symbol': 'BTC/USDT'}},
    {'label': 'KuCoin',  'value': 'kucoin'},
    {'label': 'MEXC',    'value': 'mexc'},
    {'label': 'OKX',     'value': 'okx'},
    {'label': 'Bybit',   'value': 'bybit'},
]
MARKETS = [{'label': 'Spot', 'value': 'spot'}, {'label': 'Futures', 'value': 'futures'}]
```

`get_exchange_options()` merges `ccxt.exchanges` with the static list, sorted by label, and adds `defaults`.

### 8.2. Client-order-id parameter map

`_PARAM_MAP[(exchange, operation)]` → `param_name | None`:

| Exchange | create | cancel | fetch |
|---|---|---|---|
| binance / binanceusdm / binancecoinm | `newClientOrderId` | `origClientOrderId` | `origClientOrderId` |
| bybit | `orderLinkId` | `orderLinkId` | `orderLinkId` |
| kucoin / kucoinfutures | `clientOid` | `clientOid` | `clientOid` |
| okx | `clOrdId` | `clOrdId` | `clOrdId` |
| mexc / mexc3 | `clientOrderId` | `clientOrderId` | `None` (fetch skipped) |
| gate / gateio | `text` | `text` | `text` |
| htx | `client-order-id` | `client-order-id` | `client-order-id` |

Unknown exchange / operation → `DEFAULT_CLIENT_ID_PARAM = 'clientOrderId'`.

`build_client_id_params(exchange, operation, cid)` returns `{}` if `cid` is falsy or the map says `None` for that operation.

### 8.3. ccxt id resolution

```python
resolve_ccxt_exchange_id('binance', 'futures')  → 'binanceusdm'
resolve_ccxt_exchange_id('kucoin',  'futures')  → 'kucoinfutures'
resolve_ccxt_exchange_id(other,     any)        → exchange.lower()
```

Rationale (from the file's own docstring): ccxt's `binance` class is a union of spot + USDT-M + COIN-M; `options.defaultType` does not restrict `load_markets()`. Instantiating `binanceusdm` explicitly restricts the market set to USDT-M, which is what the UI's `futures` value promises.

`get_ccxt_market_type(exchange, market_type)` translates the UI's `spot`/`futures` into ccxt's per-exchange type: `spot` → `'spot'`; `futures` → `'swap'` for binance/kucoin/mexc/okx; `'linear'` for bybit; `'swap'` as the default.

### 8.4. Workarounds

`EXCHANGE_WORKAROUNDS[(exchange, ccxt_market_type)]` — a small dict of per-exchange runtime overrides:

| Key | Meaning |
|---|---|
| `price_stream` | `'watch_bids_asks'` (default) / `'watch_order_book'` / `'watch_ticker'` / `'rest_poll'` |
| `price_poll_interval_sec` | Poll interval when `price_stream='rest_poll'` |
| `balance_type` | Value to put in `params={'type': balance_type}` for `fetch_balance`; `None` → omit entirely |

Current entries:

| Pair | Entry |
|---|---|
| `('mexc', 'swap')` | `price_stream='rest_poll'`, `price_poll_interval_sec=1.0`, `balance_type='swap'` |
| `('binance', 'swap')` | `price_stream='watch_order_book'`, `balance_type='future'` |
| `('bybit', 'linear')` | `balance_type='swap'` |
| `('okx', 'swap')` | `balance_type='swap'` |
| `('kucoin', 'swap')` | `balance_type=None` (defaultType='swap' handles it; explicit breaks kucoinfutures) |

`get_workaround(exchange, market_type, key, default)` looks the pair up by `(exchange, get_ccxt_market_type(...))` and returns the value or `default`.

**Known gap:** `price_stream='rest_poll'` is registered for MEXC swap, but no REST-poll price task is implemented in `BrokerService`. For MEXC swap the price cache stays empty; `get_latest_price` returns `{'bid': None, 'ask': None, 'stale': True, 'source': 'broker'}` indefinitely.

**Same applies to trades:** the trades stream uses `watch_trades` unconditionally — there is no `trades_stream` workaround key. For MEXC swap (or any exchange where `watch_trades` is broken), the trades deque stays empty and `get_trades` returns `{'trades': [], ...}`. If you need trades on such a pair, add a `trades_stream` workaround entry plus the corresponding REST-poll or alternative-WS path.

---

## 9. Clock synchronization

### 9.1. The problem

ccxt's default `exchange.milliseconds()` uses `time.time() * 1000` — the OS wall clock. When the OS clock drifts from the exchange's server clock by more than `recvWindow` (typically 5–10 s), every signed REST request fails with `-1021 InvalidNonce`.

### 9.2. The fix

`install_exchange_clock(exchange, logger, monotonic=False)` overrides `exchange.milliseconds`:

```python
def _tbot_milliseconds():
    # 1. Snapshot path (respects TTL and field consistency)
    if monotonic:
        return time_sync_service.get_utc_ns_monotonic() // 1_000_000
    snap = time_sync_service.get_clock_snapshot()
    if snap is not None:
        utc = time_sync_service.utc_from_snapshot(snap, time.monotonic_ns())
        if utc is not None:
            return utc // 1_000_000
    # 2. Raw model time (if snapshot is stale but service is synced)
    return time_sync_service.get_utc_ns() // 1_000_000
    # 3. Hard fallback: system time
    return time.time_ns() // 1_000_000
```

Also sets `exchange.options['timeDifference'] = 0` — **before** `load_time_difference()` runs, so that `nonce()` is meaningful even in the first requests.

### 9.3. Where it is applied

`install_exchange_clock` is called on every client created by `_make_rest_client` and `_make_ws_client`.

The `monotonic` flag comes from `config['use_monotonic_nonce']` (default `False`). When `True`, nonces are derived from `get_utc_ns_monotonic()` — guaranteed non-decreasing even when the model re-anchors. Trade-off: during a model step, nonces temporarily **lead** true UTC by the step magnitude (see `time_sync.md` §"Monotonic UTC").

### 9.4. Periodic resync

Every 10 minutes (`_clock_resync_loop`), `load_time_difference()` is called on every REST and WS-orders client. This absorbs residual drift and NTP jumps that happen over long sessions.

---

## 10. Capabilities (public API)

`BrokerBot.get_capabilities()` returns a large dict. Grouped by purpose:

### 10.1. Public market data

| Capability | Getter | Signature | Returns |
|---|---|---|---|
| `get_ohlcv` | `_cap_get_ohlcv` | `(exchange, market_type, symbol, timeframe, limit=500)` | `[{timestamp (ns), open, high, low, close, volume, timestamp_scale='exchange_ns'}, ...]` |
| `get_latest_price` | `_cap_get_latest_price` | `(exchange, market_type, symbol)` | `{'bid', 'ask', 'ts_ns', 'stale', 'source': 'broker'}` |
| `get_trades` | `_cap_get_trades` | `(exchange, market_type, symbol, from_ns=0, to_ns=0, time_scale='exchange')` | `{'trades': [...], 'first_trade_exchange_ts_ns', 'first_trade_tbot_utc_ns', 'oldest_exchange_ts_ns', 'oldest_tbot_utc_ns', 'worker_started_at_tbot_utc_ns'}` |
| `get_markets` | `_cap_get_markets` | `(exchange, market_type)` | `dict` from `client.markets` |
| `get_market` | `_cap_get_market` | `(exchange, market_type, symbol)` | `dict` for one symbol |
| `get_symbols` | `_cap_get_symbols` | `(exchange, market_type)` | `[symbol, ...]` filtered by ccxt type |
| `get_supported_market_types` | `_cap_get_supported_market_types` | `(exchange)` | `['spot', 'futures', ...]` |
| `get_exchange_options` | `_cap_get_exchange_options` | `()` | `[{label, value, defaults}, ...]` |
| `get_broker_status` | `_cap_get_broker_status` | `()` | health dict (see §15) |

**`get_trades` response schema:**

```python
{
  'trades': [
    {
      'exchange_ts_ns': int,   # > 0
      'tbot_utc_ns':    int,   # > 0
      'price':          float, # > 0
      'amount':         float, # > 0
      'side':           str | None,
      'trade_id':       str,   # '' if absent
    }, ...
  ],
  'first_trade_exchange_ts_ns':    int,  # 0 if none yet (session min)
  'first_trade_tbot_utc_ns':       int,
  'oldest_exchange_ts_ns':         int,  # 0 if deque empty
  'oldest_tbot_utc_ns':            int,
  'worker_started_at_tbot_utc_ns': int,  # diagnostic
}
```

- `from_ns` — **exclusive** lower bound. `to_ns = 0` → until now.
- `time_scale` ∈ `{'exchange', 'tbot'}` — selects which clock is used for the `[from_ns, to_ns)` filter **and** for the sort key. Each trade in the result still carries **both** clocks.
- Result is a **copy** — the consumer cannot mutate the internal deque.

### 10.2. Private trading

All private capabilities take `key_id` as the first argument.

| Capability | Signature | Notes |
|---|---|---|
| `create_market_order` | `(key_id, symbol, side, amount, position_id, signal_number, attempt_n, reduce_only=False, cid=None)` | `reduceOnly` param when set; `cid` translated via `build_client_id_params` |
| `create_limit_order` | `(key_id, symbol, side, amount, price, position_id, signal_number, attempt_n, reduce_only=False, cid=None)` | same |
| `cancel_order` | `(key_id, symbol, order_id, cid=None)` | `-2011`/`Unknown order`/`OrderNotFound` → `True` (already gone) |
| `fetch_order` | `(key_id, symbol, order_id, cid=None)` | raw ccxt dict |
| `fetch_open_orders` | `(key_id, symbol)` | |
| `fetch_positions` | `(key_id, symbols=None)` | |
| `fetch_balance` | `(key_id, currency=None)` | uses `balance_type` workaround |
| `set_leverage` | `(key_id, symbol, leverage)` | returns `True` |
| `panic_close` | `(key_id, symbol)` | composite: cancel-all + fetch-positions + market reduce-only close; returns `{closed_size, errors, orders_canceled, avg_fill_price}` |

**`_normalize_order`** returns only `{'id', 'cid', 'status', 'filled', 'average', 'fee', 'info'}`. It does **not** include `amount` or `price`. Consumers that need the raw response should use `fetch_order` and inspect the full dict.

### 10.3. Order updates

| Capability | Getter | Signature |
|---|---|---|
| `get_order_updates` | `_cap_get_order_updates` | `(owner_bot_id, since_id, timeout_sec=0.3)` → `[{id, cid, update}, ...]` |
| `cleanup_order_updates` | `_cap_cleanup_order_updates` | `(owner_bot_id)` |

### 10.4. Key management

| Capability | Setter? | Signature |
|---|---|---|
| `list_exchange_keys` | getter | `(exchange=None, market_type=None)` → `[{key_id, exchange, market_type, label, mode, created_ns, locked}, ...]` |
| `get_key_info` | getter | `(key_id)` → `{key_id, exchange, market_type, label, mode, created_ns, updated_ns}` |
| `add_key` | setter | `{exchange, market_type, label, api_key, api_secret, mode, password}` → `{'key_id'}` |
| `delete_key` | setter | `{key_id}` → `{'ok': True}` |
| `unlock_keys` | setter | `{password}` → `{unlocked, failed, total_encrypted}` |
| `invalidate_key_cache` | setter | `{key_id}` |
| `register_key_owner` | setter | `{key_id, owner_bot_id}` → `{'ok': True}` |
| `unregister_key_owner` | setter | `{key_id, owner_bot_id}` → `{'ok': True}` (only effective for the current owner) |

`unlock_keys` sets `self._ui_password = password` on success, so subsequent `get_credentials` calls use the new password without re-reading env.

---

## 11. Configuration

Broker config lives in `bot_settings` of `data/bot_<broker_bot_id>.db`. The reference form exposes only `default_provider`, but the following keys are read by the runtime:

| Key | Default | Read by | Notes |
|---|---|---|---|
| `default_provider` | `'ccxt'` | `BrokerService.__init__` | `'ccxt'` / `'tbotex'`; normalized in `prepare_new_config` / `process_edit_save` |
| `use_monotonic_nonce` | `False` | `_make_rest_client`, `_make_ws_client` | When True, `install_exchange_clock(..., monotonic=True)` |
| `data_db_path` | managed | `BaseBot` | Path to the broker's DB |
| `bot_type` | `'broker'` | system | Immutable |

**Note on `default_provider`:** the form shows a disabled single-option dropdown (`ccxt` only) with a help text explaining that native providers are reserved for a future release. The runtime accepts `'tbotex'` in config, but the current `BrokerService` always instantiates **ccxt/ccxt.pro** clients.

**Immutable fields (form):** `default_provider` in practice (single option); `bot_type` (rejected with `ValueError` in `process_edit_save`).

---

## 12. Environment variables

| Variable | Value | Effect |
|---|---|---|
| `TBOT_KEY_PASSWORD` | string | Fallback password for `KeyVault` decryption when `_ui_password` is not set |
| `TBOT_WS_DIAG` | `1` | (consumed by other modules; broker's WS loops do not implement a separate diag block in the current code) |
| `TBOT_WS_VERBOSE` | `1` | (same caveat as above) |

Broker itself reads only `TBOT_KEY_PASSWORD` directly. Changing env vars requires a process restart.

---

## 13. Database schema

`data/bot_<broker_bot_id>.db`:

### 13.1. `broker_keys`

Created by `KeyVault._ensure_table()` — see §7.1.

### 13.2. Standard BaseBot tables

`BaseBot.__init__` instantiates `DBSQLite3` and forces creation of `bot_settings` via `self.env['bot.settings']`. No other tables are created by broker.

---

## 14. UI behaviour

### 14.1. Form (`broker_form`)

```
┌─ Default provider for new keys ─────────────────────────────┐
│ [ccxt ▾] (disabled, ccxt only)                              │
│ Broker currently supports ccxt only. Native provider        │
│ support (tbotex) is reserved for a future release.          │
├─ Exchange keys ─────────────────────────────────────────────┤
│ [list rendered by update_keys_list]                         │
│   • label — exchange/market_type [unlocked|locked|plain]    │
│     [Delete]                                                │
├─ Add new key ───────────────────────────────────────────────┤
│ Exchange, Market type, Label, API Key, API Secret,          │
│ Storage mode (Plain / Encrypted), Password                  │
│ [Add key]                                                   │
├─ Unlock ────────────────────────────────────────────────────┤
│ Password to unlock encrypted keys                           │
│ [Unlock keys]                                               │
│ Status: ...                                                 │
└─────────────────────────────────────────────────────────────┘
```

- **API Key/Secret/Password** are `type="password"` fields; the CSS rule `#bots-container input[type="password"] { padding-right: 16px }` reserves space for the show/hide icon.
- **Storage mode** defaults to `encrypted`.
- **Password** field is only meaningful for encrypted mode; the runtime does not enforce this client-side, but `_cap_add_key` → `KeyVault.add_key(mode='encrypted', password=None)` raises `ValueError`.

### 14.2. Bot card (`render_broker_block`)

```
┌─ #N Broker Bot [status color] ─────────────────────────────┐
│ State:    running=healthy · healthy=healthy                 │
│ Connections: N WS · M REST · K keys                          │
│ REST clients:  exchange/market_type(private) — owner_ctx     │
│ Order book: W order book worker · S symbol · last Xs · WS …  │
│ Trades:     T trades worker · S symbol · last Xs · WS …      │
│ Orders:     O order worker · K owner · E with events · WS …  │
│ [Edit]                                                       │
└──────────────────────────────────────────────────────────────┘
```

The health panel is rendered by `_render_health_panel_from_status` and refreshed every 5 s via `global-interval` (through `update_broker_health`).

**Labeling note:** the "Order book" row is backed by the `price_*` fields in `get_status()` — kept for backward compatibility with older state dumps. It sources `get_latest_price`. The "Trades" row is backed by `trades_*`. "Orders" is the private per-key stream that feeds `get_order_updates`.

### 14.3. Health states

`_state_text` mapping for the `*_state` field:

| State | Meaning |
|---|---|
| `off` | No workers (no consumers have asked for this stream yet) |
| `connecting` | Workers spawned, first data not yet available |
| `connected` | Client exists, no tick yet (orders only) |
| `idle` | WS alive, no events since last wakeup (orders only — timeout path) |
| `active` | Fresh data / recent event |
| `stalled` | Tick age exceeds threshold |

`_render_health_panel_from_status` also renders green "healthy" / red "down" badges for `running` and `healthy` in the `State` row.

### 14.4. Key list

Rendered by `update_keys_list` (every 5 s and on any trigger):

- `plain` mode → state `plain`, color gray.
- `encrypted` mode, `locked=True` → state `locked`, color red.
- `encrypted` mode, `locked=False` → state `unlocked`, color green.

Each entry has a **Delete** button (`broker-del-key-btn`); the delete callback calls `_cap_delete_key` and prints `Deleted: <key_id[:8]>` (or `Error: ...`).

### 14.5. Form status

A single `Div` (`broker-form-status`) receives messages from:

- **Add key** → `OK: key added (abc12345)` or `Error: ...`.
- **Unlock keys** → `All N encrypted keys unlocked.` / `Unlocked M/N. F still locked — enter their passwords separately.` / `No encrypted keys to unlock.` / `Password required` / `Error: ...`.
- **Delete key** → `Deleted: abc12345` or `Error: ...`.

`unlock_keys` returns `ValueError("Wrong password")` from `KeyVault.unlock` when nothing decrypts; the UI shows `Error: Wrong password`.

---

## 15. Diagnostics and health

### 15.1. `get_status()` return contract

```python
{
    'running':              bool,
    'ws_connections':       int,       # len(carousel.all_infos())
    'rest_clients':         int,       # len(requests._clients)
    'rest_client_keys':     [str],     # "exchange/market_type[(private)] — owner"
    'price_workers':        int,
    'ws_price_healthy':     bool | None,
    'price_state':          'off' | 'connecting' | 'active' | 'stalled',
    'price_symbols':        int,       # len(_price_cache)
    'last_price_tick_ns':   int,
    'last_price_age_ns':    int,
    'orders_workers':       int,       # len(_orders_workers)
    'orders_owners':        int,       # len(_key_to_owner)
    'orders_with_events':   int,       # order_updates.stats()['owners']
    'orders_state':         'off' | 'connecting' | 'connected' | 'idle' | 'active' | 'stalled',
    'ws_orders_healthy':    bool | None,
    'trades_workers':       int,       # len(_trades_workers)
    'trades_symbols':       int,       # len(_trades)
    'trades_state':         'off' | 'connecting' | 'active' | 'stalled',
    'ws_trades_healthy':    bool | None,
    'last_trade_tick_ns':   int,
    'last_trade_age_ns':    int,
}
```

Plus, from `_cap_get_broker_status`:

- `keys_count` — `len(await self._vault.list_keys())`.
- `healthy` — equal to `running` (**note:** this is a **self-report**; it does not probe exchange connectivity).

### 15.2. Health thresholds

| Field | Threshold |
|---|---|
| `price_state = 'stalled'` | `now - last_price_ns > MAX_PRICE_AGE_IN_WS` (30 s) |
| `orders_state = 'stalled'` | `now - _orders_last_tick_ns > max_tick_age_ns` (305 s) |
| `orders_state = 'idle'` | `_orders_last_tick_ns > 0`, `_orders_last_activity_ns == 0` |
| `trades_state = 'stalled'` | `now - last_trade_tick_ns > (WS_TRADES_TIMEOUT_SEC + WS_RETRY_SLEEP_SEC × WS_FAILURES_BEFORE_RESET) × 1e9` (= 20 s) |

### 15.3. Owner attribution

`core/exchange.py` defines a `contextvars.ContextVar('exchange_owner')`. Before calling `broker.get(...)`, the caller's coroutine may set this contextvar. `RequestCarousel.submit` reads it and stores the owner per `(key, key_id)`.

The health panel exposes it as `rest_client_keys`: e.g., `"binance/spot(private) — ui:executor_broker_keys"` or `"binance/futures — internal"`. This is useful to detect **which consumer is holding a REST client** and at what rate.

### 15.4. Logging

- Logger name: `broker_<bot_id>` (module `'app'`) from `BrokerBot`; `BrokerService` logs to the same logger via `broker_bot.logger`.
- Log file: `logs/app_YYYYMMDD.log`.

**Relevant log lines to watch:**

| Pattern | Meaning |
|---|---|
| `BrokerService started` / `stopped` | Service lifecycle |
| `ws_price failed (N): ...` | WS price error streak |
| `ws_orders <key_id> failed (N): ...` | WS orders error streak |
| `ws_trades <exchange>/<mt>/<symbol> failed (N): ...` | WS trades error streak |
| `clock error from exchange — TBOT time drifted beyond recvWindow` | Signed requests failing until `time_sync` recovers |
| `register_key_owner: key abc12345… already owned by bot X, overwriting with Y` | Two owners contending for one key |
| `OrderUpdateQueue[N] overflow at 10000` | Consumer too slow; oldest records evicted |

---

## 16. Failure modes and recovery

### 16.1. WS price stream death

- Any exception increments `failures`.
- `failures >= WS_FAILURES_BEFORE_RESET (5)` → `carousel.release(key, None)` + remove from `_ws_clients` (guarded by `is client`) + sleep 60 s.
- The task itself does **not** die — it re-enters the loop and re-creates the client.
- Consumers see `price_state='stalled'` in `get_broker_status` after 30 s of silence.

### 16.2. WS orders stream death

Same as price, with a 300 s idle timeout that is **not** a failure (it proves liveness). On real failure — release + 60 s sleep. `_orders_last_tick_ns` grows; `orders_state='stalled'` after 305 s.

### 16.2a. WS trades stream death

Same pattern as §16.1 price. `WS_TRADES_TIMEOUT_SEC = 15 s` is **not** a failure — it is an idle-tick. `_trades_last_tick_ns` updates and `failures` resets to 0. On real failure streak (`≥ 5`), the shared `ws_data` client is released (see §5.1 "shared fate") — the price stream will recreate it on its next iteration; the trades deque and session bounds (`_first_trade_*`) are **preserved**.

Consumers see `trades_state='stalled'` after ~20 s of silence (see §15.2).

### 16.3. REST client pool

- Workers retry indefinitely (they are coroutines, not processes).
- A `future.set_exception` in the worker is captured and propagated to the caller; the worker moves on to the next queue item.
- `close_tbot_client` closes both the ccxt client and its aiohttp session — no leak on reconnect.

### 16.4. ccxt clock drift

- `-1021` on signed requests → checked by `_is_clock_error` in WS loops → `ERROR` log on first occurrence; `DEBUG` thereafter.
- `install_exchange_clock` + 10-minute `load_time_difference` resync keep the model accurate.
- If `time_sync_service` itself has drifted beyond `recvWindow`, **no** amount of client-side resync will help — the caller must stop the bot, verify `time_sync` telemetry, and restart.

### 16.5. Key locked during trade

If a `create_order`/`fetch_*` is called for a locked encrypted key, `get_credentials` raises `KeyLockedError`. The capability call fails; the caller (execution) will classify it and pause the bot. Broker itself does not retry on lock errors — unlock and resume manually.

### 16.6. Password change without restart

`unlock_keys` sets `_ui_password` in memory and immediately decodes every encrypted key it can. Keys that fail to decrypt with the new password **stay locked** — the UI shows `Unlocked M/N. F still locked`. There is no per-key password model — every encrypted key is expected to share the same password.

### 16.7. Broker restart during a session

- `BotManager` treats broker as `_protected_bot` and will auto-start it on load. It also **auto-restarts** broker when consumers call `restart_bot` / `force_restart_bot` (rate-limited).
- All client caches (`_ws_clients`, `_ws_order_clients`, `requests._clients`, `_price_cache`, `_trades`) are cleared on `stop()`.
- `OrderUpdateQueue` is **per broker process** — `since_id` in execution resets to 0 after broker restart (execution detects this via `get_order_updates` raising or `_ensure_broker` returning False).

### 16.8. Order-update overflow

If a consumer stops polling for long enough that `MAXLEN=10000` overflows, oldest records are evicted. The consumer will silently miss updates for older orders. The **reconciler in execution** is the fallback — it directly `fetch_order`s any row that has not reached a terminal state, so nothing is permanently lost, but there is a window of `since_id` gaps.

### 16.9. Exchange workarounds drift

When a workaround is no longer needed (ccxt fixed the issue, or the exchange changed), remove the entry from `EXCHANGE_WORKAROUNDS`. Do not add `if exchange == 'mexc'` checks outside this file — the pattern is designed to keep all such exceptions in one place.

---

## 17. Limitations and warnings

### 17.1. Single broker per installation

`BotManager` protects broker from being stopped or removed via the API. Creating a second broker is rejected by `add_bot` (`ValueError: Broker bot #N already exists; only one broker is supported`). This is a hard design choice, not a soft recommendation.

### 17.2. One key — one execution bot (recommended)

`register_key_owner` allows multiple owners but only routes order updates to the **last** registered owner. The warning in the log means the previous owner is now receiving nothing. Any workflow with two executors sharing a key must implement its own fan-out — broker does not.

### 17.3. `_normalize_order` drops `amount` and `price`

Consumers that need those fields (e.g., execution's auto-correct of `amount_placed` from REST) will find them missing. Use `fetch_order` on the returned id if exact numbers are required. This is a **known cosmetic gap** in the current code.

### 17.4. MEXC swap price stream

`EXCHANGE_WORKAROUNDS` declares `price_stream='rest_poll'` for MEXC swap, but the current `BrokerService` has **no REST-poll implementation**. For MEXC swap the price cache stays empty. Do not trade MEXC swap on this version without adding a poll loop.

### 17.5. `get_broker_status.healthy` is self-report

`healthy` is set to `running` — it does **not** probe the exchange. If the WS clients are stalled and the REST clients are failing, `running=True` and `healthy=True`. Consumers that need true health should combine this with `price_state` / `trades_state` / `orders_state` and a live `get_latest_price` probe.

### 17.6. `default_provider='tbotex'` is a no-op

Broker itself always instantiates ccxt / ccxt.pro. The `tbotex` value is normalized and stored, but has **no effect on broker's own client factories**. It is reserved for the future release where `BrokerService` selects a native provider for the underlying transport.

### 17.7. Plain storage mode

`mode='plain'` stores API keys in cleartext in `broker_keys.api_key` / `api_secret`. Anyone with read access to `data/bot_<broker_id>.db` (backups, syslog, forensic copies) sees the keys. Only use this for short-lived testnet accounts.

### 17.8. `TBOT_KEY_PASSWORD` weakness

Environment variables are readable by anything with process access (same user, container neighbors, `/proc/<pid>/environ` on Linux). Prefer UI unlock for any real usage; treat `TBOT_KEY_PASSWORD` as a convenience for isolated, single-user deployments.

### 17.9. Rate-limit budget is global

`MIN_REQUEST_INTERVAL_SEC = 0.05` is enforced **across all consumers**. If one execution bot floods the queue with fetches, other bots' cancels and places still respect the 50 ms floor but they will queue behind earlier priority entries. In practice `cancel` and `place` jump ahead of `fetch` (priority 0/1 vs 2), so trading remains responsive — but the total dispatch rate to a given exchange is capped at ~20/s regardless of how many consumers exist.

### 17.10. ccxt.pro is a hard dependency

`BrokerService._make_ws_client` imports `ccxt.pro` unconditionally. If `ccxt` was installed without `.pro` (older builds), broker fails at `start()`.

### 17.11. Manifest quirk

`__manifest__.py` declares `"depends": []`. Broker has no consumer dependency.

### 17.12. Key vault is not thread-safe across processes

`KeyVault._lock` is an `asyncio.Lock` scoped to one event loop. Do not attempt to access the same broker DB from a second process (e.g., a CLI tool) concurrently — use a read-only connection with `PRAGMA busy_timeout` and short transactions.

### 17.13. Trades deque is bounded

`TRADES_DEQUE_MAXLEN = 200_000` per symbol (emergency cap) plus a 60-second time window. On very high-volume pairs (`BTC/USDT:USDT` at peak), time eviction dominates but `oldest_exchange_ts_ns` can advance faster than 60 s in wall-clock terms. Consumers that request older trades get an empty window. For sub-minute aggregators (collector fast-mode ≥ 1 s TF), this is never an issue because their lookback is `≤ 60 s`. Do not build a consumer that expects > 60 s of raw trade history.

### 17.14. Trades stream is not stopped by `unregister_key_owner`

`_ws_trades_loop` is spawned on the first `get_trades` call for a `(exchange, market_type, symbol)` and lives until broker `stop()`. Unlike the orders stream (keyed by `key_id`, torn down by `unregister_key_owner`), the trades stream is **public** — it has no owner and is not tied to any key. If all consumers stop polling, the worker keeps running and keeps the deque warm for the next consumer (no latency on cold start). If you want auto-stop on no-consumers, add ref-counting around `get_trades` — out of scope of the current code.

### 17.15. Replay boundary at reconnect

After a `watch_trades` reconnect, ccxt.pro may replay recent trades. Dedup via `(exchange_ts_ns, trade_id)` prevents double-counting, but if the replay window does not cover the entire WS gap, `first_trade_exchange_ns` still marks the first trade ever seen — a window crossing that boundary may appear "complete" to a consumer even if a handful of trades at the very start are missing. In practice this is ≤ 1 s of loss per reconnect; consumers must tolerate occasional brief gaps (this is the collector's Rule G: gaps are legal).

### 17.16. Shared `ws_data` client — coupled reset

`_ws_price_loop` and `_ws_trades_loop` share the same ccxt.pro client (and its aiohttp session). A reset in one worker (`≥ 5` consecutive failures) releases the shared connection; the peer observes it on its next `watch_*` call and recreates the client immediately. Cost: 1–2 poll cycles of degraded price/trades for the peer. Benefit: no duplicate WS connections per `(exchange, market_type)`. See §5.1.

---

## Appendix. Module structure

```
modules/broker/
├── __init__.py        # imports models and components
├── __manifest__.py    # depends=[] (broker is infrastructure)
├── models.py          # BrokerBot, capabilities
├── components.py      # UI: form, health panel, type metadata, callbacks
└── lib/
    ├── broker_service.py   # ConnectionCarousel, RequestCarousel, WS price/trades/orders loops
    ├── exchange_directory.py  # EXCHANGES, MARKETS, _PARAM_MAP, EXCHANGE_WORKAROUNDS
    ├── clock.py            # install_exchange_clock
    ├── key_vault.py        # KeyVault + broker_keys table
    ├── crypto.py           # Fernet + PBKDF2
    └── order_updates.py    # OrderUpdateQueue
```

**Runtime dependencies:**

```
core/                          (registry, base_bot, database, exchange contextvar,
                                time_sync, universal_resolver, logger)
ccxt (async_support)           (REST client)
ccxt.pro                       (WS clients)
cryptography                   (Fernet, PBKDF2-SHA256)
aiohttp                        (via core.universal_resolver)
```

---

## ⚠️ Disclaimer (repeat)

**This is a demonstration / educational version, not a finished product.**

**Trading cryptocurrencies involves significant risk and may result in the loss of your entire invested capital.** This module is provided "as is", without warranty of any kind, for educational and research purposes only. The author assumes no responsibility or liability for any financial loss, key leak, or other damage incurred through the use of this code. **Test on testnet and with minimal capital first. Use at your own risk.**