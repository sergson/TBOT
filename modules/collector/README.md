# Collector Bot – Technical Manual (ORM Edition)

## Overview

**Collector Bot** is a module for fetching and storing OHLCV (candlestick) data from cryptocurrency exchanges. It is part of the **T.B.O.T** framework and can be used as a standalone data source or as a foundation for other trading bots.

The bot fully utilizes the new `core.database` ORM layer and contains no direct SQL queries.

## How the Collector Works

The bot logic is located in `modules/collector/models.py` (class `CollectorBot`, inheriting from `BaseBot`).

### Core Loop

1. **Initialization**
   - Loads its configuration from the local database `data/bot_<id>.db` (table `bot_settings`) via `get_bot_config()`.
   - Creates the ORM environment `self.env = DBSQLite3(db_path)`, which automatically manages tables.
   - Dynamically obtains the candle model manager via `self.env.get_model_manager('collector.candle', table_name)`.

2. **Data Collection**
   - **Initial historical load**: if the table contains fewer than `candles_limit` records, the bot fetches `candles_limit` candles from the exchange.
   - **Periodic updates**: every `timeframe_to_seconds(timeframe)` seconds, the bot fetches the latest 5 candles and inserts only new ones (based on timestamp).
   - **Limit enforcement**: after each insert, old records are deleted if the total exceeds `candles_limit * 1.1`.

3. **Concurrency**
   - Runs as an asyncio task inside a dedicated event loop thread.
   - All exchange requests are made asynchronously via CCXT’s async support.

4. **Error Handling**
   - Exceptions are logged but do not stop the bot; the loop continues after sleeping for the required interval.

### Timeframe Mapping

The function `timeframe_to_seconds(tf)` converts strings like `'1m'`, `'5m'`, `'1h'`, `'1d'` into seconds and determines the polling interval.

## Database Structure

### Global Configuration Database (`config.db`)

Tables `bots` and `settings` are described in the main T.B.O.T README. For the collector, the bot type is always `'collector'`.

### Per‑Bot Database (`data/bot_<id>.db`)

- **`bot_settings`** – key-value storage for the bot’s configuration.
- **Candle table** – created dynamically for each trading pair. The table name equals the symbol with `/` and `-` replaced by `_` (e.g., `BTC_USDT`). Managed by the `Candle` ORM model.

#### Table `bot_settings`

| Key            | Description                                                         |
|----------------|---------------------------------------------------------------------|
| `exchange`     | Exchange name (`binance`, `kucoin`, `mexc`, `okx`, `bybit`)        |
| `market_type`  | `spot` or `futures`                                                 |
| `symbol`       | Trading pair, e.g., `BTC/USDT`                                      |
| `timeframe`    | `1m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `1d`                    |
| `candles_limit`| Number of candles to keep                                           |
| `data_db_path` | Path to SQLite file (same as `data/bot_<id>.db`)                    |
| `bot_type`     | `"collector"` (added by the system)                                 |

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
- The table is created automatically by the ORM on first access.

## Reading Collected Data

### Via Inter-Bot Exchange (Recommended)

CollectorBot implements `get_capabilities()`:

```python
def get_capabilities(self):
    return {
        "ohlcv_data": {
            "keywords": ["candles", "ohlcv", "quotes", "market_data", "stonks"],
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

Consumers use `ExchangeHandle` to retrieve data (see main README).

### Direct Reading via ORM (Optional)

```python
from core.database import DBSQLite3, get_bot_config

bot_id = 1
config = get_bot_config(bot_id)
env = DBSQLite3(config['data_db_path'])
table_name = config['symbol'].replace('/', '_')
candle_manager = env.get_model_manager('collector.candle', table_name)

# Get the last 100 candles
records = candle_manager.search([], order='timestamp DESC', limit=100)
data = records.read()  # list of dictionaries
```

## Inter-Bot Data Exchange

Described in the main README. CollectorBot provides:
- **`ohlcv_data`** – last `limit` candles (method `_get_ohlcv_data(limit=500)`).
- **`symbol`** – current trading pair.

Getters are asynchronous, but internally they use the ORM, which is already thread-safe.

## Managing Collector Bots Programmatically

All functions are described in the main README. Additional notes:

- `update_bot_config(bot_id, config)` now **does not delete** existing keys; it only updates or adds the provided ones. Internal states (e.g., `current_position_id`) remain untouched.
- When `symbol` changes, the old table is dropped in `on_config_updated()` via `self.env.drop_table(old_table_name)`, and the new one is created automatically.

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

- Type in UI: "Data Collector".
- Default name: `"collector bot"`.
- `data_db_path` is assigned automatically.
- The bot card uses `html.Details`/`html.Summary` for collapsing.
- The card header has `id={'type': 'collector-bot-header', 'index': bot_id}` and is updated by a callback (every 5 seconds) depending on status (`running` → green, `stopped` → red).
- The chart (candles + volume) updates every 5 seconds if the bot is running.

## Troubleshooting & Logging

### Log Files

- `logs/collector_YYYYMMDD.log` – general module log.
- `logs/collector_<bot_id>_YYYYMMDD.log` – per-bot log.

### Common Issues

| Problem | Cause | Solution |
|---------|-------|----------|
| No data / table not created | Bot not started or fetcher initialization error | Check status and logs |
| "database is locked" | Simultaneous access from different threads | ORM uses RLock and WAL; avoid external locks |
| Timestamp in seconds instead of nanoseconds | Old data | Delete bot database and restart |
| Duplicate timestamps | Incorrect conversion | Ensure CCXT returns ms, and bot converts to ns |
| Bot does not start after reboot | Only `'running'` status is auto-started | Set status manually |

## Summary of Key Code Snippets

| Action | Code |
|--------|------|
| Get candle manager | `manager = self._get_candle_manager()` |
| Read recent candles | `records = manager.search([], order='timestamp DESC', limit=100)` |
| Get symbol | `await self._get_symbol()` or `self.config['symbol']` |
| Create and start | `bot_id = add_bot('collector', name, config); bot_manager.add_bot(bot_id); bot_manager.start_bot(bot_id)` |
| Stop | `bot_manager.stop_bot(bot_id)` |
| Delete | `bot_manager.remove_bot(bot_id); delete_bot(bot_id)` |
| Update config | `update_bot_config(bot_id, config); bot_manager.bots[bot_id].config_dirty = True` |
| Extend class | `@auto_reg class Ext: _inherit = "collector.bot"` |

---

For details, refer to the source code in `modules/collector/models.py`, `components.py`, and core `core/database.py`.