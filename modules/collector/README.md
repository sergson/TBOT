# Collector Bot – Technical Manual (ORM Edition)

## Overview

**Collector Bot** is a module for fetching and storing OHLCV (candlestick) data from cryptocurrency exchanges. It is part of the **T.B.O.T** framework and can be used as a standalone data source or as a foundation for other trading bots.

The bot fully utilizes the new `core.database` ORM layer and contains no direct SQL queries.

## How the Collector Works

The bot logic is located in `modules/collector/models.py` (class `CollectorBot`, inheriting from `BaseBot`).

### Core Loop

1. **Initialization**
   - Loads its configuration from the **local** database `data/bot_<id>.db`, table `bot_settings`, via `core.database.get_bot_config(bot_id)`.
     `get_bot_config()` reads all key/value pairs from the local `bot_settings` table (each value JSON-decoded) and additionally enriches the result with `bot_type` (and optionally `status`) taken from the global `config.db` (`bots` table).
   - Creates the ORM environment `self.env = DBSQLite3(config['data_db_path'])`, which automatically manages tables.
   - Dynamically obtains the candle model manager via `self.env.get_model_manager('collector.candle', table_name)`, where `table_name = safe_table_name(symbol)` (see `modules/collector/lib/utils.py`).

2. **Data Collection**
   - **Initial historical load**: if the table contains fewer than `candles_limit` records, the bot fetches `candles_limit` candles from the exchange.
   - **Periodic updates**: every `timeframe_to_seconds(timeframe)` seconds, the bot fetches the latest 5 candles and inserts only new ones (based on `timestamp`).
   - **Limit enforcement**: after each insert, old records are deleted if the total exceeds `candles_limit * 1.1`.

3. **Concurrency**
   - Runs as an asyncio task inside a dedicated event loop thread (`bot_manager.loop`).
   - All exchange requests are made asynchronously via CCXT's async support (`ccxt.async_support`).

4. **Error Handling**
   - Exceptions are logged but do not stop the bot; the loop continues after sleeping for the required interval.

### Timeframe Mapping

The function `timeframe_to_seconds(tf)` converts strings like `'1m'`, `'5m'`, `'1h'`, `'1d'` into seconds and determines the polling interval.

## Database Structure

### Global Configuration Database (`config.db`)

Tables `bots` and `settings` are described in the main T.B.O.T README. For the collector, the row in `bots` always has `type = 'collector'`. This file holds only bot metadata (id, type, name, status, position, created_at) and application-wide settings — **not** the collector's own configuration.

### Per-Bot Database (`data/bot_<id>.db`)

- **`bot_settings`** – key/value storage for the bot's configuration. Written by `core.database.add_bot()` / `update_bot_config()`, read back by `get_bot_config()`. The collector does **not** add runtime state keys of its own here.
- **Candle tables** – one table per trading pair, created dynamically. The table name is derived from the pair via `safe_table_name(symbol)` (in practice `/` and `-` are replaced, plus any other unsafe characters sanitized).

#### Table `bot_settings`

| Key             | Description                                                         |
|-----------------|---------------------------------------------------------------------|
| `exchange`      | Exchange name (`binance`, `kucoin`, `mexc`, `okx`, `bybit`, …)      |
| `market_type`   | `spot` or `futures`                                                 |
| `symbol`        | Trading pair, e.g., `BTC/USDT`                                      |
| `timeframe`     | `1m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `1d`                    |
| `candles_limit` | Number of candles to keep                                           |
| `data_db_path`  | Path to the SQLite file (same as `data/bot_<id>.db`)                |
| `bot_type`      | `"collector"` — injected by `get_bot_config()` from the global DB   |

`update_bot_config(bot_id, config)` updates or adds only the provided keys; it **does not** delete unrelated keys already present in `bot_settings`.

#### Candle Table (Model `Candle`)

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
```

- **`timestamp`** – Unix timestamp in **nanoseconds** (int64). Uniqueness is enforced by the primary key.
- All price and volume values are `REAL`.
- The table is created automatically by the ORM on first access (via `get_model_manager`).

## Reading Collected Data

### Via Inter-Bot Exchange (Recommended)

CollectorBot implements `get_capabilities()`. The exact capability name is internal; consumers rely on the **keywords**. The grid bot, for example, calls:

```python
await self.setup_exchange(collector_id, {"candles": ["candles", "ohlcv"]})
```

So the collector must expose a capability whose keywords include `"candles"` and/or `"ohlcv"`. A typical declaration looks like:

```python
def get_capabilities(self):
    return {
        "ohlcv_data": {
            "keywords": ["candles", "ohlcv", "quotes", "market_data"],
            "getter": self._get_ohlcv_data,
            "setter": None,
        },
        "symbol": {
            "keywords": ["symbol", "pair", "ticker"],
            "getter": self._get_symbol,
            "setter": None,
        }
    }
```

Consumers use `ExchangeHandle.get("candles", limit=...)` — matching is done by keywords, not by the capability name.

### Direct Reading via ORM (Optional)

```python
from core.database import DBSQLite3, get_bot_config
from modules.collector.lib.utils import safe_table_name

bot_id = 1
config = get_bot_config(bot_id)
env = DBSQLite3(config['data_db_path'])
table_name = safe_table_name(config['symbol'])
candle_manager = env.get_model_manager('collector.candle', table_name)

# Get the last 100 candles
records = candle_manager.search([], order='timestamp DESC', limit=100)
data = records.read()  # list of dictionaries
```

Note: use `safe_table_name()`, not a naive `symbol.replace('/', '_')` — the helper handles `-` and other unsafe characters as well.

## Exchange Directory & Dynamic Options

The collector's add/edit form does not hard-code a fixed list of exchanges. It builds options dynamically:

```python
from modules.collector.lib.exchange_directory import (
    EXCHANGES, MARKETS, get_exchange_options, get_default_exchange_value
)

exchange_options = get_exchange_options()        # ccxt.exchanges merged with EXCHANGES
default_exchange = get_default_exchange_value()  # EXCHANGES item with default=True
```

- `EXCHANGES` — static per-exchange settings (label, defaults for market/symbol, optional `markets` list).
- `MARKETS` — static fallback list of market types: `spot`, `futures`.
- `get_exchange_options()` — merges `ccxt.exchanges` with static entries; custom entries keep their label / `defaults` / `markets`; other exchanges get an auto-generated label and empty defaults.
- Default exchange = `EXCHANGES` entry flagged with `default: True` (currently Binance).

### Market type / symbol discovery

`market_type` and `symbol` dropdowns are populated by async callbacks in `components.py` that use `AsyncExchangeFetcher`:

- `update_market_types(exchange_id)` — calls `fetcher.get_supported_market_types()` and maps ccxt types to `spot` / `futures`. Default value is `spot` if available, otherwise the first one.
- `update_symbols(exchange_id, market_type)` — calls `fetcher.get_symbols()` for the selected market type.

### Exchange-specific `futures` mapping

`AsyncExchangeFetcher` maps the unified `'futures'` to an exchange-specific ccxt type:

| Exchange          | ccxt type   |
|-------------------|-------------|
| binance, kucoin   | `future`    |
| mexc, okx         | `swap`      |
| bybit             | `linear`    |
| any other         | `future`    |

### Editing restrictions

- The `symbol` field is **disabled** in edit mode (`disabled=current_bot_id is not None`) — the pair cannot be changed after creation.

## Managing Collector Bots Programmatically

All functions are described in the main README. Additional notes:

- `update_bot_config(bot_id, config)` updates or adds the provided keys without deleting unrelated ones — internal state of other modules remains untouched.
- When `symbol` changes, the old candle table is dropped in `on_config_updated()` via `self.env.drop_table(old_table_name)`, and the new one is created automatically on first write.

## Extending CollectorBot via Registry Inheritance

The class registry allows extending `CollectorBot` without modifying the source files.

### Example: Adding a Method via `_inherit`

```python
from core import auto_reg

@auto_reg
class CollectorExtension:
    _inherit = "collector.bot"

    def get_last_price(self):
        """Returns the last close price using ORM."""
        manager = self._get_candle_manager()
        records = manager.search([], order='timestamp DESC', limit=1)
        return records[0].close if records else 0.0
```

All methods defined in the extension will be available to collector instances after loading the module.

## UI Behaviour & Bot Naming

- Type in UI: `"Data Collector"` (declared in `CollectorTypeMeta.display_name`).
- Default name: `"collector bot"` (assigned by the generic save-bot callback as `f"{bot_type} bot"`).
- `data_db_path` is assigned automatically by `process_edit_save` if missing.
- The bot card uses `html.Details` / `html.Summary` for collapsing.
- The card header has `id={'type': 'collector-bot-header', 'index': bot_id}` and is updated by a callback reacting to `global-interval` (5 s) — colour reflects status (`running` → green, `stopped` → red).
- The chart (candles + volume) is refreshed every 5 s **only when** the bot status is `running`.

## Troubleshooting & Logging

### Log Files

The logger groups files by **module_type**, not by logger name. All collector-related loggers (module logger `collector_module`, per-bot loggers such as `collector_<id>`, and the fetcher logger `async_fetcher`) share a single file:

- `logs/collector_YYYYMMDD.log`

There is **no** separate per-bot log file in the current logger design.

### Common Issues

| Problem | Cause | Solution |
|---------|-------|----------|
| No data / table not created | Bot not started or fetcher initialization error | Check status and logs |
| "database is locked" | Simultaneous access from different threads | ORM uses RLock and WAL; avoid external connections |
| Timestamp in seconds instead of nanoseconds | Old data from a previous version | Delete the bot database and restart |
| Duplicate timestamps | Incorrect conversion | Ensure CCXT returns ms, and the bot converts to ns |
| Bot does not start after reboot | Only `running` status is auto-started | Set status manually |
| Empty symbol list in form | Exchange unreachable, or `market_type` / `exchange` not yet chosen | Check logs, wait for callback to complete |

## Summary of Key Code Snippets

| Action | Code |
|--------|------|
| Get candle manager | `manager = self._get_candle_manager()` |
| Read recent candles | `records = manager.search([], order='timestamp DESC', limit=100)` |
| Get symbol | `await self._get_symbol()` or `self.config['symbol']` |
| List dynamic exchanges | `get_exchange_options()` |
| Resolve default exchange | `get_default_exchange_value()` |
| Fetch market types for exchange | `await fetcher.get_supported_market_types()` |
| Fetch symbols | `await fetcher.get_symbols()` |
| Create and start | `bot_id = add_bot('collector', name, config); bot_manager.add_bot(bot_id); bot_manager.start_bot(bot_id)` |
| Stop | `bot_manager.stop_bot(bot_id)` |
| Delete | `bot_manager.remove_bot(bot_id); delete_bot(bot_id)` |
| Update config | `update_bot_config(bot_id, config); bot_manager.bots[bot_id].config_dirty = True` |
| Extend class | `@auto_reg class Ext: _inherit = "collector.bot"` |

---

For details, refer to the source code in `modules/collector/models.py`, `components.py`, `lib/fetcher.py`, `lib/exchange_directory.py`, and core `core/database.py`.