# Collector Bot – Technical Manual

This document describes the **Collector Bot** module – a data collector that fetches and stores OHLCV (candlestick) data from cryptocurrency exchanges.  
It is part of the **T.B.O.T** framework and can be used as a standalone data source or as a foundation for other trading bots.

---

## Table of Contents

1. [How the Collector Works](#how-the-collector-works)
2. [Database Structure](#database-structure)
   - [Global Configuration Database (`config.db`)](#global-configuration-database-configdb)
   - [Per‑Bot Database (`data/bot_<id>.db`)](#per-bot-database-databot_iddb)
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
   - Reads its configuration from its own SQLite database (`data/bot_<id>.db`), specifically from the `bot_settings` table.  
   - Creates an `AsyncExchangeFetcher` instance for the specified exchange and market type.  
   - Ensures the target table exists in the same database (`_init_db()`).

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

### Global Configuration Database (`config.db`)

This file contains general information about all bots and global settings.

#### Table `bots`

| Column        | Type      | Description                                                         |
|---------------|-----------|---------------------------------------------------------------------|
| `id`          | INTEGER PK| Unique bot identifier (used everywhere)                             |
| `type`        | TEXT      | Always `'collector'` for this bot type                              |
| `name`        | TEXT      | Display name (default: `"collector bot"`)                           |
| `status`      | TEXT      | `'running'` or `'stopped'`                                          |
| `position`    | INTEGER   | Sorting order in the UI                                             |
| `created_at`  | TIMESTAMP | Creation time                                                       |

#### Table `settings`

Stores global application settings as key‑value pairs. Not directly relevant to individual bots.

> **Note:** There is **no** separate type‑specific configuration table (like `config_collector_type`). All configuration for each bot is stored in its own database file (see below).

### Per‑Bot Database (`data/bot_<id>.db`)

Each bot instance has its own SQLite file. This file contains both the bot’s configuration and, for collectors, the market data.

#### Table `bot_settings`

Stores the bot’s configuration as key‑value pairs. Values are JSON‑encoded when necessary.

| Key               | Description                                                                 |
|-------------------|-----------------------------------------------------------------------------|
| `exchange`        | Exchange name (`binance`, `kucoin`, `mexc`, `okx`, `bybit`)                 |
| `market_type`     | `spot` or `futures`                                                         |
| `symbol`          | Trading pair, e.g. `BTC/USDT`                                               |
| `timeframe`       | `1m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `1d`                            |
| `candles_limit`   | Number of candles to keep in the market database                            |
| `data_db_path`    | Path to the SQLite file that holds the OHLCV data (same file)               |
| `bot_type`        | Always `"collector"` (added by the system)                                  |

#### Market Data Table

A table is created per symbol (slashes replaced by underscores).  
Example: symbol `BTC/USDT` → table name `BTC_USDT`.

    CREATE TABLE IF NOT EXISTS BTC_USDT (
        timestamp INTEGER PRIMARY KEY,  -- Unix timestamp in UTC seconds
        open      REAL,
        high      REAL,
        low       REAL,
        close     REAL,
        volume    REAL
    );

- **`timestamp`**: Unix time (seconds since 1970-01-01 00:00:00 UTC). The bot ensures uniqueness via `INSERT OR IGNORE`.
- All price and volume values are **floats** as returned by CCXT.
- The bot maintains at most `candles_limit` rows, automatically deleting the oldest entries.

---

## Reading Collected Data

Any other bot or script can read the collected OHLCV data directly from the market database using standard SQLite queries.  
**However**, for a cleaner, loosely‑coupled approach that does not require knowledge of database paths or table names, prefer the **Inter‑Bot Data Exchange** mechanism described in the next section.

### Example: Read all candles (using Pandas) – direct access

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

### Example: Get latest candle (raw SQLite) – direct access

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

> **Note:** The bot writes data asynchronously. Always handle the case where the table may be empty or the bot is still starting.

---

## Inter‑Bot Data Exchange (Recommended)

The T.B.O.T framework includes a built‑in mechanism for bots to share data **without hard‑coded database paths or table names**. Every bot can declare what data it provides, and other bots can request that data through a central `BotManager` using a simple keyword‑based mapping. The collector bot already implements this interface.

### Exposing Capabilities

The `CollectorBot` class defines `get_capabilities()`, which returns a dictionary describing what data it can offer:

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

- **`keywords`** – a list of strings that other bots can use to request this particular dataset.
- **`getter`** – an **asynchronous** callable (a bound method) that returns the data. It may accept extra arguments – for example, `_get_ohlcv_data(limit=500)` limits the number of candles returned.
- **`setter`** – `None` because the collector does not allow external writing.

The actual getter methods are:

    async def _get_ohlcv_data(self, limit=500):
        # Reads the last `limit` candles from the SQLite database
        # Returns a list of dicts: [{"timestamp": ..., "open": ..., ...}, ...]
        ...

    async def _get_symbol(self):
        return self.config['symbol']

These methods are asynchronous even though the database access is synchronous – they use `loop.run_in_executor()` to avoid blocking the event loop.

### Consuming Collector Data via ExchangeHandle

Any other bot that has a reference to the `BotManager` (available as `self.manager` if the bot passes `manager` to `BaseBot`) can securely access the collector’s data.

**Step 1 – Obtain an `ExchangeHandle`**  
Inside the consumer bot’s `start()` method (or later), call `self.setup_exchange()` with the target collector’s ID and a mapping from **your local names** to **the collector’s keywords**:

    async def start(self):
        # My local names → collector's keywords
        mapping = {
            "prices": ["candles", "ohlcv"],
            "ticker": ["symbol", "pair"]
        }
        await self.setup_exchange(target_bot_id=5, mapping=mapping)
        # ... other startup logic

The handle is cached in `self.dynamics[5]`.

**Step 2 – Fetch data**  
Use the handle to call `get()` with your local name:

    handle = self.get_exchange(5)   # or directly self.dynamics[5]
    if handle:
        # Get the last 100 candles
        candles = await handle.get("prices", limit=100)
        # Get the trading pair
        symbol = await handle.get("ticker")

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

The correct sequence is simple because `add_bot()` already creates the bot record and saves the configuration.

    from core.database import add_bot
    from core.bot_manager import bot_manager  # global instance used by the app

    def create_and_start_collector(exchange, market_type, symbol, timeframe, candles_limit):
        config = {
            'exchange': exchange,
            'market_type': market_type,
            'symbol': symbol,
            'timeframe': timeframe,
            'candles_limit': candles_limit,
            # data_db_path will be added automatically by add_bot()
        }
        # Creates bot in config.db and saves config in data/bot_<id>.db
        bot_id = add_bot('collector', f"{exchange} {symbol}", config)

        # Create the bot instance in BotManager (does not start it)
        bot_manager.add_bot(bot_id)

        # Optionally start immediately
        bot_manager.start_bot(bot_id)

        return bot_id

> **Important:** `bot_manager.add_bot(bot_id)` creates the instance but does **not** start it. The bot will start only if you call `bot_manager.start_bot(bot_id)` or if its status is `'running'` and the application restarts (via `load_bots()`).

### Starting / Stopping a Bot

Use the `BotManager` instance (the same one used by the UI).

    from core.bot_manager import bot_manager

    # Start a bot (asynchronous, non-blocking)
    bot_manager.start_bot(bot_id)

    # Stop a bot
    bot_manager.stop_bot(bot_id)

You can also update the status in the database and rely on `load_bots()` on next startup:

    from core.database import update_bot_status
    update_bot_status(bot_id, 'running')   # or 'stopped'

### Deleting a Bot and Cleaning Up

    from core.bot_manager import bot_manager
    from core.database import delete_bot
    import os

    def delete_collector_bot(bot_id):
        # 1. Stop and remove from manager
        bot_manager.remove_bot(bot_id)

        # 2. Delete configuration from config.db
        delete_bot(bot_id)

        # 3. The market database file is NOT deleted automatically by remove_bot.
        #    It will be cleaned up at next application startup by cleanup_orphan_databases().
        #    If you need to delete it now, you can do so manually:
        db_path = f"data/bot_{bot_id}.db"
        if os.path.exists(db_path):
            os.remove(db_path)

> **Note:** `BotManager.remove_bot()` also removes all `ExchangeHandle` references to this bot from other bots’ `dynamics` automatically.

### Clearing Collected Data

To erase all collected candles **without deleting the bot**:

1. Stop the bot.
2. Delete the market database file (or drop the table).
3. Restart the bot – it will re‑fetch the initial `candles_limit` candles.

    import os
    from core.bot_manager import bot_manager
    from core.database import get_bot_config

    def clear_collector_data(bot_id):
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

> **Caution:** If you delete the file while the bot is running, the bot will crash. Always stop the bot first.

### Modifying Bot Parameters

Changing the **symbol** is not recommended – it is simpler to create a new collector bot. However, changing the **timeframe** or **candles_limit** can be done without data loss.

**Example: Change `timeframe` from `1m` to `5m`**

    from core.database import get_bot_config, update_bot_config
    from core.bot_manager import bot_manager

    bot_id = 42
    new_timeframe = "5m"

    # 1. Stop the bot
    bot_manager.stop_bot(bot_id)

    # 2. Load current config
    config = get_bot_config(bot_id)
    if not config:
        raise ValueError("Config not found")

    # 3. Modify the desired field
    config['timeframe'] = new_timeframe

    # 4. Save the updated config to the bot's local DB
    update_bot_config(bot_id, config)

    # 5. If the bot instance still exists (it was stopped but not removed), mark it dirty
    if bot_id in bot_manager.bots:
        bot_manager.bots[bot_id].config_dirty = True

    # 6. Restart the bot
    bot_manager.start_bot(bot_id)

> **Note:** The `update_bot_config` function completely replaces the configuration in `data/bot_<id>.db`. If the bot is running, setting `config_dirty` will cause the bot’s main loop to call `on_config_updated()` and reload its settings.

---

## Extending CollectorBot via Registry Inheritance

The T.B.O.T registry (`core/registry.py`) allows you to **add new methods or override existing ones** without modifying the original `CollectorBot` class. This is done by creating a new class with `_inherit = "collector.bot"`.

### Using `bot_registry.get_model`

First, you can retrieve the final bot class (after all extensions have been applied) and instantiate it manually:

    from core.registry import bot_registry

    # Get the final CollectorBot class (with all extensions)
    CollectorClass = bot_registry.get_model("collector.bot")
    if CollectorClass:
        # Create an instance for a given bot_id (normally done by BotManager)
        bot_instance = CollectorClass(bot_id)
        # Now you can call its methods (e.g., start, stop, or custom methods)

### Adding New Methods Through `_inherit`

Suppose you want to add a method `get_last_price()` to every collector bot. Create a new module (e.g., `modules/collector_extension/`) with the following code:

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

After this module is loaded (by `load_modules`), all existing and future collector bots will have the `get_last_price()` method available.

### Overriding Existing Methods

You can also override `start()`, `stop()`, or `_run()` to add custom behaviour:

    @auto_reg
    class LoggingCollector:
        _inherit = "collector.bot"

        async def start(self):
            print(f"[LOG] Collector {self.bot_id} is starting")
            await super().start()   # calls the original start() method
            print(f"[LOG] Collector {self.bot_id} started")

> **Important:** Always call `super().method()` to preserve the original functionality.  
> **Capabilities & extensions:** If you override `get_capabilities()`, remember to call `super().get_capabilities()` if you want to keep the original entries and just add new ones.

### Dynamic Extension (Monkey-Patching) Without Creating a Module

In addition to the declarative module‑based inheritance (`_inherit`), the T.B.O.T registry allows **dynamic runtime extension** of existing bot classes. This is useful when you want to add methods to a collector bot “on the fly” from another module or even from an interactive script, without creating a separate module or restarting the application.

#### How It Works

The `bot_registry` stores the **final merged class** for each model name (e.g., `"collector.bot"`). You can retrieve that class, add new methods or attributes directly to it, and all future instances (and optionally existing ones) will gain those methods.

#### Example: Adding `get_avg_price()` Dynamically

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

After this code runs, **all collector bot instances** will have the `get_avg_price` method. Because Python looks up methods on the class, existing instances see it immediately.

#### Dynamic Override of Existing Methods

You can also override an existing method at runtime:

    original_start = CollectorClass.start

    async def patched_start(self):
        print("[DYNAMIC] Before original start")
        await original_start(self)
        print("[DYNAMIC] After original start")

    CollectorClass.start = patched_start

This will affect all collector bots the next time `start()` is called.

#### Important Considerations

1. **Thread safety** – Apply dynamic patches when bots are idle or stopped.
2. **Persistence** – Dynamic changes are lost on restart. For permanent extensions, prefer the `_inherit` approach.
3. **Capabilities** – You can also dynamically patch `get_capabilities()` to expose new data via the exchange system, but ensure the getters you add are async and properly bound.

---

## Using Collector Data Inside Other Bots (Direct SQLite)

While the recommended approach is the `ExchangeHandle` mechanism, you can still access the SQLite database directly if needed. This section describes that legacy method.

### Example: Strategy bot that uses the collector’s data (direct SQLite)

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

### Accessing the collector bot instance directly

If your bot has a reference to the `bot_manager`, you can access the collector instance and call any extended methods:

    collector_instance = bot_manager.bots.get(collector_id)
    if collector_instance and hasattr(collector_instance, 'get_last_price'):
        price = collector_instance.get_last_price()

---

## UI Behaviour & Bot Naming

When you add a collector bot through the web dashboard:

1. The **type selector** shows “Data Collector” (from `CollectorTypeMeta.display_name`).
2. After filling the form and clicking **Save**, the bot’s name is set to the default value `"collector bot"`. It is **not** automatically derived from the exchange/symbol.
3. The `data_db_path` is automatically generated as `data/bot_<id>.db`.
4. The bot appears in the main container with:
   - A **candlestick chart** (built by `build_figure()`).
   - **Start/Stop** and **Delete** buttons.
5. The chart remembers its zoom/pan state via `relayout-store`.

To change the bot’s name, you must update the `bots` table directly (or add a custom UI feature). Example:

    import sqlite3
    conn = sqlite3.connect('config.db')
    conn.execute("UPDATE bots SET name = ? WHERE id = ?", ("My Collector", bot_id))
    conn.commit()
    conn.close()

### Where to find the list of all bots (including collectors)

    from core.database import get_all_bots

    all_bots = get_all_bots()
    for bot in all_bots:
        print(bot['id'], bot['type'], bot['name'], bot['status'])

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
| Deleting bot does not remove data file   | `BotManager.remove_bot()` does not delete the `.db` file.                    | The file will be removed at next startup by `cleanup_orphan_databases()`. To delete immediately, use `os.remove()` after stopping the bot. |

### Forcing a bot to re‑fetch historical data

Stop the bot, delete the market database file (or drop the table), then start it again.

    clear_collector_data(bot_id)  # as defined above

---

## Summary of Key Code Snippets

| Action                                   | Code / Function                                                                 |
|------------------------------------------|---------------------------------------------------------------------------------|
| Read all candles (direct SQLite)         | Direct SQLite query (see example)                                               |
| Read last candle (direct SQLite)         | SQLite query with `ORDER BY timestamp DESC LIMIT 1`                             |
| **Expose data via capabilities**         | Implement `get_capabilities()` returning keywords & async getters               |
| **Consume data via ExchangeHandle**      | `await self.setup_exchange(target_id, mapping)`, then `await handle.get(name)`  |
| Create and start collector bot           | `add_bot('collector', name, config)` + `bot_manager.add_bot()` + `start_bot()` |
| Start/stop a bot                         | `bot_manager.start_bot(bot_id)` / `bot_manager.stop_bot(bot_id)`                |
| Delete a bot                             | `bot_manager.remove_bot(bot_id)` + `delete_bot(bot_id)` + manual file removal   |
| Clear all collected data                 | `clear_collector_data(bot_id)` (as defined)                                     |
| Change timeframe                         | Stop → `get_bot_config` → modify → `update_bot_config` → restart                |
| Get collector bot class                  | `bot_registry.get_model("collector.bot")`                                       |
| Extend collector with new methods (static) | Create a class with `_inherit = "collector.bot"` and `@auto_reg`                |
| Extend collector with new methods (dynamic) | Retrieve class via `bot_registry` and attach methods at runtime                 |

---

For any questions or further details, refer to the source code of `modules/collector/` and the core modules. The collector bot is designed to be simple and reliable – a perfect data provider for your trading strategies.