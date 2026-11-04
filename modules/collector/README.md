# Collector Bot — Technical Manual

> ## ⚠️ DEMONSTRATION / EDUCATIONAL VERSION
>
> **This is a demonstration and educational version, not a finished product.**
>
> - The module is provided to illustrate the T.B.O.T architecture, ORM patterns, and UI conventions — not as a production-ready component.
> - Behavior, configs, and file layout may change without notice.
> - You are responsible for your own security review, testing, and compliance if you build on this code.

> ## 💰 RISK DISCLAIMER
>
> **Trading cryptocurrencies involves significant risk and may result in the loss of your entire invested capital.**
>
> - This software is provided **for educational and research purposes only**, "as is", without warranty of any kind.
> - The author assumes **no responsibility or liability** for any financial loss, missed profit, or other damage incurred through the use of this software.
> - **Never trade with funds you cannot afford to lose.** Test on demo/testnet environments with minimal capital first.

---

## Overview

**Collector Bot** — a module for fetching and storing OHLCV data (candles) from cryptocurrency exchanges. It is part of the **T.B.O.T** framework. It can be used as a standalone data source or as a price feed for the Grid Bot.

The bot **does not talk to an exchange directly**. All exchange operations are performed through the **Broker** bot (`modules/broker/`) via the capabilities mechanism. Collector requests candles from Broker, stores them in its own database, and serves them to other bots via `ExchangeHandle`.

Fully ported to the `core.database` ORM layer — there are no raw SQL queries in the bot logic.

**Key features:**
- Dynamic per-symbol table creation (`collector.candle`, `_dynamic=True`).
- Historical bootstrap on first start + incremental updates.
- Automatic rotation of old candles (10% over-trigger).
- Symbol change on the fly → old table dropped, new one created on next write.
- Consistent UI styling (`ui_compact`, `info_panel`, `candlestick_trace`).
- Extension via `_inherit` without touching the source files.

---

## Role in the system

```
┌──────────────┐   get_ohlcv / get_symbols       ┌──────────┐
│ Collector    │ ───────────────────────────────►│  Broker  │
│ (this module)│                                 │          │
└──────┬───────┘   capabilities (requests)      └────┬─────┘
       │                                             │
       │ ohlcv_data / symbol                         │ REST + WS
       │ (provides)                                  ▼
       ▼                                       ┌──────────┐
┌──────────────┐                               │ Exchange │
│  Grid Bot    │                               │ (ccxt)   │
│  (consumer)  │                               └──────────┘
└──────────────┘
```

- **Broker** — the sole owner of ccxt/ccxt.pro clients, rate-limits, and API keys. Collector does not create its own REST clients.
- **Collector** — caches candles in a local SQLite database and exposes them to other bots via the `ohlcv_data` capability.
- **Grid** — a consumer of candles: `await self._collector_handle.get("candles", limit=N)`.

**Dependency in `__manifest__.py`** — `"depends": []`. This is a historical entry: the runtime dependency on Broker is not declared in the manifest; instead it is resolved at start via a capability lookup (`_find_broker_id`).

---

## How the Collector Works

The bot's logic lives in `modules/collector/models.py` (the `CollectorBot` class, inheriting from `BaseBot`).

### Core Loop

1. **Initialization (`__init__` + `start`)**
   - Loads the config from the local DB `data/bot_<id>.db` (`bot_settings`) via `get_bot_config()`.
   - Creates the ORM environment `self.env = DBSQLite3(db_path)` (inside `BaseBot.__init__`).
   - Calls `self._resolve_exchange_provider()` — resolves `_provider_factory` if `exchange_provider='tbotex'` and the pair is registered. **Note:** in the current code Collector does not use `_provider_factory` directly — exchange access goes through Broker. This call is kept for compatibility and for the case where a `CollectorBot` extension uses the native provider directly.
   - Finds Broker via `self.manager._broker_id` (or by `type='broker'` in `get_all_bots()`). If Broker is absent — the bot does not start.
   - Sets up a capability mapping to Broker: `get_ohlcv`, `get_symbols`, `get_markets`, `get_supported_market_types`, `get_broker_status`.
   - Caches `self._candle_table_name = safe_table_name(symbol)`.
   - Spawns the `_run()` asyncio task.

2. **Health probing (`_ensure_broker`)**
   - Checks Broker status via `manager.get_bot_status()`.
   - If Broker is not `'running'` — calls `manager.restart_bot(self.bot_id, broker_id)` (rate-limited: not more often than once every 10 seconds).
   - Probe: `await broker_handle.get("get_broker_status")` with a 5 s timeout. On timeout — `force_restart_bot`.
   - If Broker is not healthy — the loop waits 5 seconds and retries.

3. **Data Collection (`_run`)**
   - **Initial historical load:** if the table has fewer rows than `candles_limit` — fetch `candles_limit` candles. The `initial_load_done` flag is reset in `on_config_updated()`.
   - **Incremental updates:** after the initial load the bot fetches the **latest 5 candles** and either updates existing rows (by timestamp) or inserts new ones.
   - **Fetch interval:**
     ```python
     fetch_interval_sec = timeframe_to_seconds(timeframe)
     fetch_interval = max(5, min(fetch_interval_sec // 4, 60))
     ```
     That is, polling happens **once per quarter of the timeframe**, but no more often than every 5 s and no less often than every 60 s. This lets the UI show the currently forming candle at a reasonable frequency.
   - **Retention / prune:** after an insert, checks `count > candles_limit * 1.1`. If so — deletes the oldest rows until `count <= candles_limit`.

4. **Concurrency**
   - Runs as an asyncio task on the shared event loop (`BotManager`).
   - The ORM uses `threading.RLock` and WAL mode; DB locks are impossible for one-bot-one-DB.

5. **Error handling**
   - Exceptions in `_run` are logged and do not stop the bot; the loop resumes after a `fetch_interval` pause.
   - Exceptions in `_ensure_broker` — throttled warning, loop resumes after a 5-second pause.
   - Exceptions in `_upsert_candles` — logged with `exc_info=True`; the next iteration retries.

### `timeframe_to_seconds(tf)`

Supported suffixes: `m` (minutes), `h` (hours), `d` (days). Example: `'1m'` → 60, `'4h'` → 14400, `'1d'` → 86400. Unknown suffix — `ValueError`.

### `on_config_updated()` (runtime reconfiguration)

Called from `_run` when `self.config_dirty == True` (set after a successful edit-save via `save_editing` in `app.py`):

1. Saves `old_symbol` and `old_table`.
2. Re-reads the config from the DB.
3. Re-resolves `_provider_factory`.
4. Resets `initial_load_done = False` — the next cycle does a full reload.
5. If `safe_table_name(new_symbol) != old_table` — calls `self.env.drop_table(old_table)`.
6. Updates `self._candle_table_name`.

---

## Database Structure

### Global Configuration Database (`config.db`)

The `bots` and `settings` tables are described in the main T.B.O.T README. For a collector bot, `type` is always `'collector'`.

### Per-Bot Database (`data/bot_<id>.db`)

#### `bot_settings` — key-value config

| Key            | Description                                                                    |
|----------------|--------------------------------------------------------------------------------|
| `exchange`     | Exchange name (`binance`, `kucoin`, `mexc`, `okx`, `bybit`, ...)              |
| `market_type`  | `spot` or `futures`                                                            |
| `symbol`       | Trading pair, e.g. `BTC/USDT`                                                  |
| `timeframe`    | `1m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `1d`                               |
| `candles_limit`| Number of candles to retain (100 / 500 / 1000 / 2000)                          |
| `data_db_path` | Path to the SQLite file (equal to `data/bot_<id>.db`)                          |
| `bot_type`     | `"collector"` (added by the system)                                            |

**Note:** the `exchange_provider` field is **not created** and **not normalized** by `prepare_new_config` in collector. It may be set manually and `_resolve_exchange_provider()` will read it, but it has no effect on the current data path — candles are always fetched via Broker.

#### Candle table (Model `Candle`, dynamic per symbol)

```python
class Candle(Model):
    _name = 'collector.candle'
    _table = None          # dynamic table name
    _dynamic = True        # do not create table automatically

    timestamp = Integer(primary_key=True, required=True)
    open = Float()
    high = Float()
    low = Float()
    close = Float()
    volume = Float()
    tbot_utc_stamp_ns = Integer(index=True)
```

- **Table name** — `safe_table_name(symbol)`: `re.sub(r'[^a-zA-Z0-9_]', '_', symbol)`. Example: `BTC/USDT` → `BTC_USDT`. `BTC/USDT:USDT` → `BTC_USDT_USDT`.
- **`timestamp`** — Unix open time of the candle in **nanoseconds** (int64). PK.
- **`tbot_utc_stamp_ns`** — TBOT UTC of the row-write moment (indexed). Used by the UI's "last write age" indicator.
- **All prices/volumes are `REAL`.**
- The table is created automatically by the ORM on first access via `env.get_model_manager('collector.candle', table_name)`.

#### Multiple symbols

Collector works with only one pair at a time (`self.config['symbol']`). Changing the symbol via `on_config_updated()` **drops the old table**. If you need data for multiple pairs — create multiple Collector bots.

---

## Inter-Bot Data Exchange

### Consuming data (Collector → Broker)

```python
await self.setup_exchange(broker_id, {
    "get_ohlcv": ["ohlcv", "candles"],
    "get_symbols": ["symbols"],
    "get_markets": ["markets"],
    "get_supported_market_types": ["market_types"],
    "get_broker_status": ["broker_status", "status", "health"],
})
self._broker_handle = self.get_exchange(broker_id)
```

The main request — `get_ohlcv`:

```python
candles = await self._broker_handle.get(
    "get_ohlcv",
    exchange=self.config['exchange'],
    market_type=self.config['market_type'],
    symbol=symbol,
    timeframe=timeframe,
    limit=fetch_limit,
)
```

Broker returns a list of dicts:

```python
{
  'timestamp': int,          # exchange time, ns
  'open': float, 'high': float, 'low': float, 'close': float,
  'volume': float,
  'timestamp_scale': 'exchange_ns',
}
```

### Providing data (Collector → Grid / others)

`CollectorBot.get_capabilities()`:

```python
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
```

**`_get_ohlcv_data(limit=500)`** — returns the latest `limit` candles in **DESC order** (newest first). Each row is extended with scale-marker fields:

```python
{
  'timestamp': int,
  'open': float, 'high': float, 'low': float, 'close': float, 'volume': float,
  'tbot_utc_stamp_ns': int,
  'timestamp_scale': 'exchange_ns',
  'tbot_utc_stamp_ns_scale': 'tbot_utc_ns',
}
```

This eliminates unit ambiguity for the consumer. The Grid bot uses `timestamp_scale='exchange_ns'` for exchange alignment and `tbot_utc_stamp_ns` for internal bookkeeping.

**`_get_symbol()`** — current trading pair (string).

### Direct ORM access (not recommended, but possible)

```python
from core.database import DBSQLite3, get_bot_config
from modules.collector.lib.utils import safe_table_name

bot_id = 1
config = get_bot_config(bot_id)
env = DBSQLite3(config['data_db_path'])
table_name = safe_table_name(config['symbol'])
candle_manager = env.get_model_manager('collector.candle', table_name)

records = candle_manager.search([], order='timestamp DESC', limit=100)
data = records.read()
env.close()
```

Use this for diagnostics only. The canonical path is the `ohlcv_data` capability.

---

## Configuration

### Main parameters

| Parameter       | Type    | Default          | Description                                                  |
|-----------------|---------|------------------|--------------------------------------------------------------|
| `exchange`      | TEXT    | `binance`        | ccxt id of the exchange. On creation — `binance` if unset.   |
| `market_type`   | TEXT    | `spot`           | `spot` \| `futures`.                                         |
| `symbol`        | TEXT    | `BTC/USDT`       | Trading pair. Cannot be changed in edit mode (the form locks the field). |
| `timeframe`     | TEXT    | `1m`             | `1m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `1d`.            |
| `candles_limit` | INTEGER | `100`            | 100 / 500 / 1000 / 2000. Retention in table rows.            |

### Normalization (`collector_prepare_new_config`)

On creation (`prepare_new_config`) and on edit (`process_edit_save`) values are normalized:

- `symbol` — if empty, `'BTC/USDT'`.
- `candles_limit` — coerced to the nearest of `(100, 500, 1000, 2000)`.
- `timeframe` — if not in the valid list, `'1m'`.
- `market_type` — if not `'spot'`/`'futures'`, `'spot'`.
- `exchange` — if empty, `'binance'`.
- `exchange_provider` — **not normalized** in collector (unlike grid/execution).

### Immutable fields

Cannot be changed after creation:

- `symbol` (dropdown disabled in the edit form).
- `data_db_path` (managed by the system).

`exchange` and `market_type` can be changed via the edit form (but on exchange change the old table persists until the next `on_config_updated()`).

### Retention

- **Candle table:** `candles_limit` (via the prune logic with 10% over-trigger).
- **`bot_settings`:** not cleaned up.
- **Logs:** `logs/collector_YYYYMMDD.log` — standard retention mechanism from `core/logger.py`.

---

## UI Behaviour

### Create / edit form (`collector_form`)

Fields:

| Field           | Type       | Notes                                                       |
|-----------------|------------|-------------------------------------------------------------|
| Exchange        | Dropdown   | `get_exchange_options()` — ccxt + static EXCHANGES          |
| Market type     | Dropdown   | Updated by a callback on exchange change (via Broker)       |
| Symbol          | Dropdown   | Updated by a callback on exchange/market_type change (via Broker). Disabled in edit. |
| Timeframe       | Dropdown   | Fixed list                                                  |
| Candles limit   | Dropdown   | Fixed list                                                  |

**Callbacks:**

- `update_market_types(exchange_id)` — calls `broker.get_supported_market_types(exchange)` and populates the list of available types. If Broker is unavailable — falls back to the static `MARKETS`.
- `update_symbols(exchange_id, market_type)` — calls `broker.get_symbols(exchange, market_type)` and populates the list of pairs. If Broker is unavailable — empty list.

Both callbacks use `_run_broker_cap` — the same `run_coroutine_threadsafe` mechanism used in `bot_manager`, with `owner_ctx` for REST-client attribution.

### Bot card (`render_collector_block`)

```
┌─ #N Collector Bot ← binance BTC/USDT (spot) [status color] ────┐
│                                                                 │
│ Exchange: binance BTC/USDT (spot)                              │
│ Feed:     timeframe 1m | retention 1000 candles | last write: fresh (12s) │
│                                                                 │
│ [Stop]  [Edit]  [Delete]                                       │
│ ───────────────────────                                         │
│ [Candlestick chart + volume]                                   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

- **Header (`collector-bot-header`)** — style refreshed every 5 s by `global-interval` based on `status`: `running` → green, `stopped` → red.
- **Info panel (`collector-info-panel`)** — `graphics.info_panel` with Exchange/Feed fields. Refreshed every 5 s.
  - **`_format_candles_age(bot_id)`** — reads `MAX(tbot_utc_stamp_ns)` from the candle table and compares with TBOT UTC:
    - `< 20s` → `"fresh (Xs)"`
    - `< 90s` → `"Xs ago"`
    - `≥ 90s` → `"⚠ stale (Xs)"`
    - No data / error → `"no data"` / `"—"`
- **Chart (`collector-graph`)** — `make_subplots(2, 1)` with candlestick (row 1) and volume (row 2). Refreshed every 5 s **only if** `bot['status'] == 'running'`.
- **Buttons** — Start/Stop (standard `status-btn` from `app.py`), Edit, Delete.

### Relayout persistence

The chart's pan/zoom state is stored in `dcc.Store('relayout-store')` and reapplied on the next build via `graphics.normalize_dash_relayout(...)`.

---

## Extending CollectorBot via Registry Inheritance

The class registry lets you extend `CollectorBot` without touching the source:

```python
from core import auto_reg

@auto_reg
class CollectorLastPriceExtension:
    _inherit = "collector.bot"

    def get_last_price(self):
        manager = self._get_candle_manager()
        records = manager.search([], order='timestamp DESC', limit=1)
        return records[0].close if records else 0.0
```

All extension methods become available on CollectorBot instances after the module loads.

**If the extension works with the native provider directly:** `self._provider_factory` is available after `__init__` provided that `exchange_provider='tbotex'` and the `(exchange, market_type)` pair is registered in `core.tbotex`. Otherwise `_provider_factory is None`.

---

## Troubleshooting

### Log files

- `logs/collector_YYYYMMDD.log` — module-wide log.

Logger name: `collector_<bot_id>` (module_type `'collector'`).

### Common issues

| Problem | Likely cause | Solution |
|---|---|---|
| No data / table not created | Bot not started, or Broker unavailable | Check `status` in `bots` and `broker.bot` in `BotManager.bots` |
| `No broker bot configured — collector cannot start` | Broker missing or not created | Broker is auto-created on the first `app.py` run; check `load_bots()` |
| `Broker health probe timeout` | Broker is stuck / event loop overloaded | Check Broker logs; on repeat — `force_restart_bot` |
| `database is locked` | Concurrent access | The ORM uses RLock + WAL; avoid external connections |
| Timestamps in seconds instead of nanoseconds | Legacy data | Drop the bot's DB and restart |
| Duplicate timestamps | Wrong conversion | Ensure Broker returns `timestamp_scale='exchange_ns'` |
| Bot not starting after reboot | Auto-start only for `'running'` | Set the status manually |
| Collector cannot find the pair after a symbol change | Old table dropped, new one created on the first `_upsert_candles` | Wait for the first `_run` cycle |
| `last write: ⚠ stale` | Collector is not updating candles | Check `status` in `bots`, `fetch_interval`, and Broker status |
| `Symbol not set` in `_get_candle_manager` | Config with an empty `symbol` | Fix the config via the UI or `update_bot_config` |

### Diagnostic commands

Check config and table existence:

```python
from core.database import DBSQLite3, get_bot_config
from modules.collector.lib.utils import safe_table_name

cfg = get_bot_config(bot_id)
env = DBSQLite3(cfg['data_db_path'])
table_name = safe_table_name(cfg['symbol'])

tables = env.list_tables()
print('candle table present:', table_name in tables)

if table_name in tables:
    mgr = env.get_model_manager('collector.candle', table_name)
    print('rows:', mgr.search_count([]))
    last = mgr.search([], order='timestamp DESC', limit=3)
    for r in last:
        print(r.timestamp, r.close, r.tbot_utc_stamp_ns)
env.close()
```

Deep diagnostics: set `collector` to `DEBUG` in the settings panel.

---

## Summary of Key Code Snippets

| Action | Code |
|---|---|
| Create and start | `bot_id = add_bot('collector', name, config); bot_manager.add_bot(bot_id); bot_manager.start_bot(bot_id)` |
| Get candle manager | `manager = self._get_candle_manager()` |
| Read recent candles | `records = manager.search([], order='timestamp DESC', limit=100)` |
| Read via capability | `await self._collector_handle.get("candles", limit=N)` |
| Get symbol via capability | `await self._collector_handle.get("symbol")` |
| Stop | `bot_manager.stop_bot(bot_id)` |
| Delete | `bot_manager.remove_bot(bot_id); delete_bot(bot_id)` |
| Update config | `update_bot_config(bot_id, config); bot_manager.bots[bot_id].config_dirty = True` |
| Extend class | `@auto_reg class Ext: _inherit = "collector.bot"` |
| Check provider availability | `from core.tbotex import has_provider; has_provider('binance', 'spot')` |

---

## Appendix. Module structure

```
modules/collector/
├── __init__.py        # from . import models, components
├── __manifest__.py    # name, version, depends=[], bot_model, type_model
├── models.py          # CollectorBot, Candle (dynamic model)
├── components.py      # UI: form, candlestick chart, callbacks, CollectorTypeMeta
└── lib/
    └── utils.py       # safe_table_name
```

**Runtime dependencies:**

```
modules/broker/lib/exchange_directory.py  (EXCHANGES, MARKETS, get_exchange_options, get_default_exchange_value)
modules/broker/                            (capabilities: get_ohlcv, get_symbols, get_markets,
                                            get_supported_market_types, get_broker_status)
core/                                      (registry, base_bot, database, exchange, graphics, styles, colors, logger)
```

---

## ⚠️ Disclaimer (repeat)

**This is a demonstration / educational version, not a finished product.**

**Trading cryptocurrencies involves significant risk and may result in the loss of your entire invested capital.** This module is provided "as is", without warranty of any kind, for educational and research purposes only. The author assumes no responsibility or liability for any financial loss incurred through the use of this code. **Test on testnet and with minimal capital first. Use at your own risk.**

---
