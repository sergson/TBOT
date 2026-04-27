# Collector Bot – Technical Manual

This document describes the **Collector Bot** module – a data collector that fetches and stores OHLCV (candlestick) data from cryptocurrency exchanges.  
It is part of the **T.B.O.T** framework and can be used as a standalone data source or as a foundation for other trading bots.

---

## Table of Contents

1. [How the Collector Works](#how-the-collector-works)
2. [Database Structure](#database-structure)
   - [Configuration Database (`config.db`)](#configuration-database-configdb)
   - [Market Data Database](#market-data-database)
3. [Reading Collected Data](#reading-collected-data)
4. [Inter‑Bot Data Exchange (Recommended)](#inter-bot-data-exchange-recommended)
   - [Exposing Capabilities](#exposing-capabilities)
   - [Consuming Collector Data via ExchangeHandle](#consuming-collector-data-via-exchangehandle)
   - [Data Flow Overview](#data-flow-overview)
5. [Managing Collector Bots Programmatically](#managing-collector-bots-programmatically)
   - [Adding a New Collector Bot](#adding-a-new-collector-bot)
   - [Starting / Stopping a Bot](#starting--stopping-a-bot)
   - [Deleting a Bot and Cleaning Up](#deleting-a-bot-and-cleaning-up)
   - [Clearing Collected Data](#clearing-collected-data)
   - [Modifying Bot Parameters (e.g., timeframe)](#modifying-bot-parameters)
6. [Extending CollectorBot via Registry Inheritance](#extending-collectorbot-via-registry-inheritance)
   - [Using `bot_registry.get_model`](#using-bot_registryget_model)
   - [Adding New Methods Through `_inherit`](#adding-new-methods-through-_inherit)
   - [Dynamic Extension (Monkey-Patching) Without Creating a Module](#dynamic-extension-monkey-patching-without-creating-a-module)
7. [Using Collector Data Inside Other Bots (Direct SQLite)](#using-collector-data-inside-other-bots-direct-sqlite)
8. [UI Behaviour & Bot Naming](#ui-behaviour--bot-naming)
9. [Troubleshooting & Logging](#troubleshooting--logging)

---

## How the Collector Works

The collector bot is defined in `modules/collector/models.py` as `CollectorBot`. It inherits from `BaseBot` and implements the required `start()` and `stop()` methods.

### Core Loop

1. **Initialisation**  
   - Reads its configuration from the database (exchange, market type, symbol, timeframe, candles limit, database path).  
   - Creates an `AsyncExchangeFetcher` instance for the specified exchange and market type.  
   - Ensures the target table exists in the market data database (`_init_db()`).

2. **Data Collection**  
   - **Initial historical load**: if the table contains fewer than `candles_limit` records, the bot fetches `candles_limit` candles from the exchange.  
   - **Periodic updates**: every `timeframe_to_seconds(timeframe)` seconds, the bot fetches the latest 5 candles and inserts only new ones (based on timestamp).  
   - **Limit enforcement**: after each insert, the bot deletes rows older than the most recent `candles_limit` candles.

3. **Concurrency**  
   - The bot runs as an asyncio task inside a dedicated event loop thread.  
   - All exchange requests are made asynchronously via CCXT’s async support.

4. **Error Handling**  
   - Exceptions are logged but do not stop the bot; the loop continues after sleeping for the required interval.

### Timeframe Mapping

The function `timeframe_to_seconds(tf)` converts strings like `1m`, `5m`, `1h`, `1d` into seconds.  
This value determines the sleep interval between fetch cycles.

---

## Database Structure

### Configuration Database (`config.db`)

The bot’s settings are stored in two places:

#### Table `bots` (global bot registry)

| Column        | Type      | Description                                                                 |
|---------------|-----------|-----------------------------------------------------------------------------|
| `id`          | INTEGER PK| Unique bot identifier (used everywhere)                                     |
| `type`        | TEXT      | Always `'collector'` for this bot type                                     |
| `name`        | TEXT      | Display name (default: `"{exchange} {symbol}"`, e.g. `"binance BTC/USDT"`)  |
| `status`      | TEXT      | `'running'` or `'stopped'`                                                  |
| `position`    | INTEGER   | Sorting order in the UI                                                     |
| `created_at`  | TIMESTAMP | Creation time                                                               |
| `config_data` | TEXT      | JSON string that mirrors the `config_collector_type` fields (redundant)     |

#### Table `config_collector_type`

Created automatically from `CONFIG_SCHEMA` defined in `components.py`.

| Column             | Type    | Description                                                             |
|--------------------|---------|-------------------------------------------------------------------------|
| `bot_id`           | INTEGER | FK → `bots.id` ON DELETE CASCADE                                        |
| `exchange`         | TEXT    | Exchange name (`binance`, `kucoin`, `mexc`, `okx`, `bybit`)             |
| `market_type`      | TEXT    | `spot` or `futures`                                                     |
| `symbol`           | TEXT    | Trading pair, e.g. `BTC/USDT`                                           |
| `timeframe`        | TEXT    | `1m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `1d`                        |
| `candles_limit`    | INTEGER | Number of candles to keep in the market database                        |
| `data_db_path`     | TEXT    | Path to the SQLite file that holds the OHLCV data, e.g. `data/bot_42.db`|

> **Important:** The `data_db_path` is automatically set when the bot is created via the UI. When adding a bot programmatically, you must provide a valid path.

### Market Data Database

Each collector bot uses a **separate SQLite database file** (specified by `data_db_path`). Inside that file, a **table is created per symbol** (slashes replaced by underscores).  
Example: symbol `BTC/USDT` → table name `BTC_USDT`.

#### Table Schema

```sql
CREATE TABLE IF NOT EXISTS BTC_USDT (
    timestamp INTEGER PRIMARY KEY,  -- Unix timestamp in UTC seconds
    open      REAL,
    high      REAL,
    low       REAL,
    close     REAL,
    volume    REAL
);
```

- **`timestamp`**: Unix time (seconds since 1970-01-01 00:00:00 UTC). The bot ensures uniqueness via `INSERT OR IGNORE`.
- All price and volume values are **floats** as returned by CCXT.
- The bot maintains at most `candles_limit` rows, automatically deleting the oldest entries.

---

## Reading Collected Data

Any other bot or script can read the collected OHLCV data directly from the market database using standard SQLite queries.  
**However**, for a cleaner, loosely‑coupled approach that does not require knowledge of database paths or table names, prefer the **Inter‑Bot Data Exchange** mechanism described in the next section.

### Example: Read all candles (using Pandas) – direct access

```python
import sqlite3
import pandas as pd
from core.database import get_bot_config

def read_all_candles(bot_id: int) -> pd.DataFrame:
    config = get_bot_config(bot_id)
    if not config or config.get('bot_type') != 'collector':
        raise ValueError(f"Bot {bot_id} is not a collector")

    db_path = config['data_db_path']
    symbol = config['symbol']
    table_name = symbol.replace('/', '_').replace('-', '_')

    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query(f"""
            SELECT timestamp, open, high, low, close, volume
            FROM {table_name}
            ORDER BY timestamp ASC
        """, conn)

    if not df.empty:
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='s', utc=True)
    return df
```

### Example: Get latest candle (raw SQLite) – direct access

```python
import sqlite3
from core.database import get_bot_config

def get_last_candle(bot_id: int):
    config = get_bot_config(bot_id)
    if not config or config.get('bot_type') != 'collector':
        return None

    db_path = config['data_db_path']
    symbol = config['symbol']
    table_name = symbol.replace('/', '_')

    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(f"""
            SELECT timestamp, open, high, low, close, volume
            FROM {table_name}
            ORDER BY timestamp DESC LIMIT 1
        """)
        row = cur.fetchone()
        if row:
            return {
                'timestamp': row[0],
                'open': row[1],
                'high': row[2],
                'low': row[3],
                'close': row[4],
                'volume': row[5]
            }
    return None
```

> **Note:** The bot writes data asynchronously. Always handle the case where the table may be empty or the bot is still starting.

---

## Inter‑Bot Data Exchange (Recommended)

The T.B.O.T framework now includes a built‑in mechanism for bots to share data **without hard‑coded database paths or table names**. Every bot can declare what data it provides, and other bots can request that data through a central `BotManager` using a simple keyword‑based mapping. The collector bot already implements this interface.

### Exposing Capabilities

The `CollectorBot` class defines `get_capabilities()`, which returns a dictionary describing what data it can offer:

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

- **`keywords`** – a list of strings that other bots can use to request this particular dataset.
- **`getter`** – an **asynchronous** callable (a bound method) that returns the data. It may accept extra arguments – for example, `_get_ohlcv_data(limit=500)` limits the number of candles returned.
- **`setter`** – `None` because the collector does not allow external writing.

The actual getter methods are:

```python
async def _get_ohlcv_data(self, limit=500):
    # Reads the last `limit` candles from the SQLite database
    # Returns a list of dicts: [{"timestamp": ..., "open": ..., ...}, ...]
    ...

async def _get_symbol(self):
    return self.config['symbol']
```

These methods are asynchronous even though the database access is synchronous – they use `loop.run_in_executor()` to avoid blocking the event loop.

### Consuming Collector Data via ExchangeHandle

Any other bot that has a reference to the `BotManager` (available as `self.manager` if the bot passes `manager` to `BaseBot`) can securely access the collector’s data.

**Step 1 – Obtain an `ExchangeHandle`**  
Inside the consumer bot’s `start()` method (or later), call `self.setup_exchange()` with the target collector’s ID and a mapping from **your local names** to **the collector’s keywords**:

```python
async def start(self):
    # My local names → collector's keywords
    mapping = {
        "prices": ["candles", "ohlcv"],
        "ticker": ["symbol", "pair"]
    }
    await self.setup_exchange(target_bot_id=5, mapping=mapping)
    # ... other startup logic
```

The handle is cached in `self.dynamics[5]`.

**Step 2 – Fetch data**  
Use the handle to call `get()` with your local name:

```python
handle = self.get_exchange(5)   # or directly self.dynamics[5]
if handle:
    # Get the last 100 candles
    candles = await handle.get("prices", limit=100)
    # Get the trading pair
    symbol = await handle.get("ticker")
```

Extra keyword arguments passed to `handle.get()` (like `limit=100`) are forwarded directly to the getter method – here, to `_get_ohlcv_data(limit=100)`.

The consumer bot **never touches SQLite, table names, or file paths**. If the collector is later moved to a different storage backend, the consumer remains unchanged.

### Data Flow Overview

1. Collector registers its capabilities (keywords + async methods).
2. Consumer asks the `BotManager` for an exchange with the collector, specifying a keyword map.
3. `BotManager` matches the keywords against the collector’s capabilities and creates an `ExchangeHandle` that holds references to the real getter methods.
4. The consumer uses the handle to read data – the call is proxied to the collector’s async methods.
5. When the collector is removed, all handles to it are automatically invalidated.

This design keeps bots decoupled and makes the system easily extensible without modifying existing code.

---

## Managing Collector Bots Programmatically

All management functions rely on the core modules: `core.database` and `core.bot_manager`.

### Adding a New Collector Bot

The correct sequence is:  
1. Create the bot entry in the database (using `add_bot` and inserting into `config_collector_type`).  
2. Call `bot_manager.add_bot(bot_id)` to register and start the bot (if it should run immediately).

```python
import sqlite3
import json
from core.database import add_bot, DB_CONFIG
from core.bot_manager import bot_manager  # assume this is the global instance

def create_and_start_collector(exchange: str, market_type: str, symbol: str,
                               timeframe: str, candles_limit: int) -> int:
    # 1. Generate database path (ensure data/ exists)
    import os
    os.makedirs('data', exist_ok=True)

    # 2. Insert basic bot info (status will be 'stopped' by default)
    bot_name = f"{exchange} {symbol}"
    bot_id = add_bot('collector', bot_name)

    # 3. Build config
    data_db_path = f"data/bot_{bot_id}.db"
    config = {
        'exchange': exchange,
        'market_type': market_type,
        'symbol': symbol,
        'timeframe': timeframe,
        'candles_limit': candles_limit,
        'data_db_path': data_db_path
    }

    # 4. Update bots.config_data JSON
    with sqlite3.connect(DB_CONFIG) as conn:
        conn.execute('UPDATE bots SET config_data = ? WHERE id = ?',
                     (json.dumps(config), bot_id))

    # 5. Insert into type-specific config table
    with sqlite3.connect(DB_CONFIG) as conn:
        conn.execute('''
            INSERT INTO config_collector_type
            (bot_id, exchange, market_type, symbol, timeframe, candles_limit, data_db_path)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (bot_id, exchange, market_type, symbol, timeframe, candles_limit, data_db_path))
        conn.commit()

    # 6. (Optionally) create empty database file
    open(data_db_path, 'a').close()

    # 7. Register the bot with BotManager (this will not start it unless status='running')
    bot_manager.add_bot(bot_id)

    # 8. Start the bot immediately if desired
    bot_manager.start_bot(bot_id)

    return bot_id
```

> **Important:** `bot_manager.add_bot(bot_id)` does **not** start the bot automatically. It only creates the instance. The bot will start only if its status in the DB is `'running'` or you explicitly call `bot_manager.start_bot(bot_id)`.

### Starting / Stopping a Bot

Use the `BotManager` instance (the same one used by the UI).

```python
from core.bot_manager import bot_manager

# Start a bot (asynchronous, non-blocking)
bot_manager.start_bot(bot_id)

# Stop a bot
bot_manager.stop_bot(bot_id)
```

You can also update the status in the database and rely on `load_bots()` on next startup:

```python
from core.database import update_bot_status

update_bot_status(bot_id, 'running')   # or 'stopped'
```

### Deleting a Bot and Cleaning Up

```python
from core.bot_manager import bot_manager
from core.database import delete_bot

# 1. Stop and remove from manager (this also schedules deletion of the .db file)
bot_manager.remove_bot(bot_id)

# 2. Delete configuration from database
delete_bot(bot_id)               # cascades to config_collector_type
```

The `BotManager.remove_bot()` method also removes the market database file after a 3‑second delay (to allow any pending reads to finish). **All other bots that had an `ExchangeHandle` to this collector will have that handle automatically removed.**

### Clearing Collected Data

To erase all collected candles **without deleting the bot**:

1. Stop the bot.
2. Delete the market database file (or truncate the table).
3. Restart the bot – it will re‑fetch the initial `candles_limit` candles.

```python
import os
from core.bot_manager import bot_manager
from core.database import get_bot_config

def clear_collector_data(bot_id: int):
    # 1. Stop the bot if running
    bot_manager.stop_bot(bot_id)

    # 2. Get the database path
    config = get_bot_config(bot_id)
    if not config or config.get('bot_type') != 'collector':
        return
    db_path = config['data_db_path']

    # 3. Delete the file (bot will recreate on next start)
    if os.path.exists(db_path):
        os.remove(db_path)

    # 4. Start the bot again
    bot_manager.start_bot(bot_id)
```

> **Caution:** If you delete the file while the bot is running, the bot will crash. Always stop the bot first.

### Modifying Bot Parameters (e.g., timeframe)

Changing the **symbol** is not recommended – it is simpler to create a new collector bot. However, changing the **timeframe** or **candles_limit** can be done without data loss.

**Example: Change `timeframe` from `1m` to `5m`**

```python
import sqlite3
import json
from core.database import DB_CONFIG, get_bot_config
from core.bot_manager import bot_manager

bot_id = 42
new_timeframe = "5m"

# 1. Stop the bot
bot_manager.stop_bot(bot_id)

# 2. Update the database
with sqlite3.connect(DB_CONFIG) as conn:
    # Update type-specific table
    conn.execute('''
        UPDATE config_collector_type
        SET timeframe = ? WHERE bot_id = ?
    ''', (new_timeframe, bot_id))

    # Also update the JSON config_data
    config = get_bot_config(bot_id)  # old config
    config['timeframe'] = new_timeframe
    conn.execute('UPDATE bots SET config_data = ? WHERE id = ?',
                 (json.dumps(config), bot_id))
    conn.commit()

# 3. (Optional) If you want to keep existing candles, the bot will continue appending.
#    However, the new timeframe will change the fetch interval. No table change is required.

# 4. Restart the bot
bot_manager.start_bot(bot_id)
```

> **Note:** Changing `candles_limit` works the same way – just update the column and restart. The bot will automatically enforce the new limit on the next write cycle.

---

## Extending CollectorBot via Registry Inheritance

The T.B.O.T registry (`core/registry.py`) allows you to **add new methods or override existing ones** without modifying the original `CollectorBot` class. This is done by creating a new class with `_inherit = "collector.bot"`.

### Using `bot_registry.get_model`

First, you can retrieve the final bot class (after all extensions have been applied) and instantiate it manually:

```python
from core.registry import bot_registry

# Get the final CollectorBot class (with all extensions)
CollectorClass = bot_registry.get_model("collector.bot")
if CollectorClass:
    # Create an instance for a given bot_id (normally done by BotManager)
    bot_instance = CollectorClass(bot_id)
    # Now you can call its methods (e.g., start, stop, or custom methods)
```

### Adding New Methods Through `_inherit`

Suppose you want to add a method `get_last_price()` to every collector bot. Create a new module (e.g., `modules/collector_extension/`) with the following code:

```python
# modules/collector_extension/models.py
from core import auto_reg
import sqlite3

@auto_reg
class CollectorExtension:
    _inherit = "collector.bot"   # extends the existing collector.bot

    def get_last_price(self) -> float:
        """Return the last close price from the market database."""
        config = self.config   # self.config is already loaded by CollectorBot.__init__
        db_path = config['data_db_path']
        symbol = config['symbol']
        table_name = symbol.replace('/', '_')
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(f"SELECT close FROM {table_name} ORDER BY timestamp DESC LIMIT 1")
            row = cur.fetchone()
            return row[0] if row else 0.0
```

After this module is loaded (by `load_modules`), all existing and future collector bots will have the `get_last_price()` method available.

### Overriding Existing Methods

You can also override `start()`, `stop()`, or `_run()` to add custom behaviour:

```python
@auto_reg
class LoggingCollector:
    _inherit = "collector.bot"

    async def start(self):
        print(f"[LOG] Collector {self.bot_id} is starting")
        await super().start()   # calls the original start() method
        print(f"[LOG] Collector {self.bot_id} started")
```

> **Important:** Always call `super().method()` to preserve the original functionality.
> **Capabilities & extensions:** If you override `get_capabilities()`, remember to call `super().get_capabilities()` if you want to keep the original entries and just add new ones.

### Dynamic Extension (Monkey-Patching) Without Creating a Module

In addition to the declarative module‑based inheritance (`_inherit`), the T.B.O.T registry allows **dynamic runtime extension** of existing bot classes. This is useful when you want to add methods to a collector bot “on the fly” from another module or even from an interactive script, without creating a separate module or restarting the application.

#### How It Works

The `bot_registry` stores the **final merged class** for each model name (e.g., `"collector.bot"`). You can retrieve that class, add new methods or attributes directly to it, and all future instances (and optionally existing ones) will gain those methods.

#### Example: Adding `get_avg_price()` Dynamically

```python
from core.registry import bot_registry

CollectorClass = bot_registry.get_model("collector.bot")

def get_avg_price(self, lookback: int = 10) -> float:
    import sqlite3
    config = self.config
    table = config['symbol'].replace('/', '_')
    with sqlite3.connect(config['data_db_path']) as conn:
        cur = conn.execute(f"""
            SELECT close FROM {table}
            ORDER BY timestamp DESC LIMIT ?
        """, (lookback,))
        rows = cur.fetchall()
        if not rows:
            return 0.0
        return sum(r[0] for r in rows) / len(rows)

CollectorClass.get_avg_price = get_avg_price
```

After this code runs, **all collector bot instances** will have the `get_avg_price` method. Because Python looks up methods on the class, existing instances see it immediately.

#### Dynamic Override of Existing Methods

You can also override an existing method at runtime:

```python
original_start = CollectorClass.start

async def patched_start(self):
    print("[DYNAMIC] Before original start")
    await original_start(self)
    print("[DYNAMIC] After original start")

CollectorClass.start = patched_start
```

This will affect all collector bots the next time `start()` is called.

#### Important Considerations

1. **Thread safety** – Apply dynamic patches when bots are idle or stopped.
2. **Persistence** – Dynamic changes are lost on restart. For permanent extensions, prefer the `_inherit` approach.
3. **Capabilities** – You can also dynamically patch `get_capabilities()` to expose new data via the exchange system, but ensure the getters you add are async and properly bound.

---

## Using Collector Data Inside Other Bots (Direct SQLite)

While the recommended approach is the `ExchangeHandle` mechanism, you can still access the SQLite database directly if needed. This section describes that legacy method.

### Example: Strategy bot that uses the collector’s data (direct SQLite)

```python
# Inside another bot's _run() method
import asyncio
import sqlite3
from core.database import get_bot_config

class MyStrategyBot(BaseBot):
    async def _run(self):
        collector_id = 1
        while self.running:
            config = get_bot_config(collector_id)
            if config and config.get('bot_type') == 'collector':
                db_path = config['data_db_path']
                table = config['symbol'].replace('/', '_')
                with sqlite3.connect(db_path) as conn:
                    cur = conn.execute(f"SELECT close FROM {table} ORDER BY timestamp DESC LIMIT 1")
                    row = cur.fetchone()
                    if row:
                        self.logger.info(f"Latest price: {row[0]}")
            await asyncio.sleep(60)
```

### Accessing the collector bot instance directly

If your bot has a reference to the `bot_manager`, you can access the collector instance and call any extended methods:

```python
collector_instance = bot_manager.bots.get(collector_id)
if collector_instance and hasattr(collector_instance, 'get_last_price'):
    price = collector_instance.get_last_price()
```

---

## UI Behaviour & Bot Naming

When you add a collector bot through the web dashboard:

1. The **type selector** shows “Data Collector” (from `CollectorTypeMeta.display_name`).
2. After filling the form and clicking **Save**, the bot’s name is automatically set to `{exchange} {symbol}` (e.g., `binance BTC/USDT`).
3. The `data_db_path` is generated as `data/bot_{bot_id}.db`.
4. The bot appears in the main container with:
   - A **candlestick chart** (built by `build_figure()`).
   - **Start/Stop** and **Delete** buttons.
5. The chart remembers its zoom/pan state via `relayout-store`.

### Where to find the list of all bots (including collectors)

```python
from core.database import get_all_bots

all_bots = get_all_bots()
for bot in all_bots:
    print(bot['id'], bot['type'], bot['name'], bot['status'])
```

---

## Troubleshooting & Logging

### Log files

All collector‑related logs are written to:

- `logs/collector_YYYYMMDD.log` – for the generic collector module.
- `logs/collector_<bot_id>_YYYYMMDD.log` – per‑bot logs (the logger name is `collector_{bot_id}`).

### Common issues

| Problem                                  | Likely cause                                                                 | Solution                                                                 |
|------------------------------------------|------------------------------------------------------------------------------|--------------------------------------------------------------------------|
| Table not created / no data              | Bot not started or exchange connection failed                               | Check bot status in DB; look for errors in the log file.                |
| “database is locked”                     | Another process has the market database open (e.g., a reader)               | Ensure you close SQLite connections promptly. Use `with` blocks.        |
| Duplicate timestamps                     | Rare – `INSERT OR IGNORE` prevents duplicates, but if primary key violated, check timezone handling. | Verify that timestamps from CCXT are in UTC. The bot converts ms to seconds. |
| Bot does not auto‑start after reboot     | Only bots with `status='running'` are loaded by `BotManager.load_bots()`.    | Ensure you set `status='running'` before restarting `app.py`.            |
| Custom extension methods not found       | The module containing the `_inherit` class was not loaded.                  | Check that your module is inside `modules/` and has `__init__.py`. Also verify that `load_modules` runs. |
| `ExchangeHandle.get()` raises KeyError   | The local name used in `get()` does not match any name given during `setup_exchange()`, or the target bot has no matching capability. | Double‑check the keyword mapping and that the target bot is a collector with `get_capabilities()` implemented. |
| Getter not returning data as expected    | The getter may be asynchronous, but the caller might be using it incorrectly. | Always `await` the `handle.get()` call. Check the collector’s logs for errors in `_get_ohlcv_data`. |

### Forcing a bot to re‑fetch historical data

Stop the bot, delete the market database file (or drop the table), then start it again.

```python
clear_collector_data(bot_id)  # as defined above
```

---

## Summary of Key Code Snippets

| Action                                   | Code / Function                                                                 |
|------------------------------------------|---------------------------------------------------------------------------------|
| Read all candles (direct SQLite)         | Direct SQLite query (see example)                                               |
| Read last candle (direct SQLite)         | SQLite query with `ORDER BY timestamp DESC LIMIT 1`                             |
| **Expose data via capabilities**         | Implement `get_capabilities()` returning keywords & async getters               |
| **Consume data via ExchangeHandle**      | `await self.setup_exchange(target_id, mapping)`, then `await handle.get(name)`  |
| Create and start collector bot           | `create_and_start_collector(...)` (custom function) + `bot_manager.start_bot()` |
| Start/stop a bot                         | `bot_manager.start_bot(bot_id)` / `bot_manager.stop_bot(bot_id)`                |
| Delete a bot                             | `bot_manager.remove_bot(bot_id)` + `delete_bot(bot_id)`                         |
| Clear all collected data                 | `clear_collector_data(bot_id)` (as defined)                                     |
| Change timeframe                         | Stop → update DB → restart                                                      |
| Get collector bot class                  | `bot_registry.get_model("collector.bot")`                                       |
| Extend collector with new methods (static) | Create a class with `_inherit = "collector.bot"` and `@auto_reg`                |
| Extend collector with new methods (dynamic) | Retrieve class via `bot_registry` and attach methods at runtime                 |

---

For any questions or further details, refer to the source code of `modules/collector/` and the core modules. The collector bot is designed to be simple and reliable – a perfect data provider for your trading strategies.