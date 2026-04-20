# T.B.O.T — Trading Bot Open Toolkit

## Architecture

The project is built on a modular architecture with a central registry of bot types.  
The core (`app.py`, `database.py`, `bot_manager.py`) contains no specific logic for any particular bot — all functionality is delegated to pluggable modules.

### Key Principles

- **Bot Registry** (`bot_registry.py`) — stores metadata for all bot types (add form, DB schema, bot class, UI render function).
- **Universal Core** — works with any registered type without requiring changes when new types are added.
- **Bot Modules** — each type (collector, analyst, trader) is implemented in a separate file and registers itself with the registry.

---

## Project Structure

```
tbots/
├── app.py # Dash entry point (universal core)
├── config.db # Settings database (SQLite)
├── data/ # Market data database folder
│ └── bot_*.db # Data files for individual bots
├── modules/
│ ├── init.py # Import all bot modules
│ ├── base_bot.py # Abstract base class for all bots
│ ├── bot_registry.py # Bot type registry
│ ├── database.py # Universal database functions
│ ├── bot_manager.py # Bot lifecycle management
│ ├── collector_module.py # Collector bot module (registers type 'collector')
│ ├── collector_bot.py # Collector logic implementation
│ ├── fetcher.py # Asynchronous data fetching from exchanges (ccxt)
│ ├── logger.py # Configurable logging
│ └── universal_resolver.py # Cross-platform DNS resolver
├── assets/ # CSS, images (optional)
└── requirements.txt
```
---

## Settings Database (`config.db`)

### Table `bots` (General Information)

| Field         | Type        | Description                                      |
|---------------|-------------|--------------------------------------------------|
| `id`          | INTEGER PK  | Unique bot identifier                            |
| `type`        | TEXT        | Bot type (`'collector'`, `'analyst'`, …)         |
| `name`        | TEXT        | Display name                                     |
| `status`      | TEXT        | `'running'` / `'stopped'`                        |
| `position`    | INTEGER     | Sorting order in UI                              |
| `created_at`  | TIMESTAMP   | Creation timestamp                               |
| `config_data` | TEXT        | **JSON** with arbitrary configuration fields     |

### Type Configuration Tables (`config_<type>`)

For each registered type, a separate table is created, for example:

~~~sql
CREATE TABLE config_collector (
    bot_id INTEGER PRIMARY KEY,
    exchange TEXT NOT NULL,
    market_type TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    candles_limit INTEGER NOT NULL,
    data_db_path TEXT NOT NULL,
    FOREIGN KEY(bot_id) REFERENCES bots(id) ON DELETE CASCADE
);
~~~

The table schema is defined in the bot module during registration (`CONFIG_SCHEMA`).

### Table `settings`

| Field   | Type    | Description                    |
|---------|---------|--------------------------------|
| `key`   | TEXT PK | Setting key                    |
| `value` | TEXT    | Value (often in JSON format)   |

---

## How to Add a New Bot Type

1. **Create a module**, e.g., `modules/trading_module.py`.

2. **Define a bot class** inheriting from `BaseBot`:

   ~~~python
   from modules.base_bot import BaseBot

   class TradingBot(BaseBot):
       async def start(self): ...
       async def stop(self): ...
   ~~~

3. **Define the configuration schema** (fields for the `config_trading` table):

   ~~~python
   CONFIG_SCHEMA = {
       'exchange': 'TEXT NOT NULL',
       'api_key': 'TEXT',
       'secret': 'TEXT',
       ...
   }
   ~~~

4. **Create a form component function** (Dash component):

   ~~~python
   def trading_form():
       return html.Div([ ... ])
   ~~~

5. **Create a render function** for the UI block:

   ~~~python
   def render_trading_block(bot_id: int, config: dict, relayout_store: dict):
       return html.Div(...)
   ~~~

6. **Register the type** with the registry:

   ~~~python
   from modules.bot_registry import bot_registry

   bot_registry.register(
       type_id='trading',
       display_name='Trading Bot',
       form_component=trading_form,
       config_schema=CONFIG_SCHEMA,
       bot_class=TradingBot,
       render_block=render_trading_block
   )
   ~~~

7. **Import the module** in `modules/__init__.py`:

   ~~~python
   from . import trading_module
   ~~~

Done! The core will automatically pick up the new type: an option will appear in the dropdown, your form will be shown upon creation, the `config_trading` table will be created in the database, and the manager will be able to start your bots.

---

## Core Components

### `app.py`
- Universal Dash application.
- Dynamically builds UI based on registered types.
- Callbacks for adding, deleting, starting/stopping bots.
- Stores `relayout` state for graphs.

### `database.py`
- Database initialization: creates common tables and configuration tables for all types.
- Functions: `add_bot`, `get_all_bots`, `get_bot_config`, `update_bot_status`, `delete_bot`.
- Settings handling: `get_setting` / `save_setting`.

### `bot_manager.py`
- Loads bots with status `'running'` from the database at startup.
- Creates bot instances via the registry.
- Manages their lifecycle (`start` / `stop` / `remove`).
- Thread-safe interaction with `asyncio`.

### `collector_module.py`
- Example of a collector bot module.
- Registers the `'collector'` type.
- Provides a configuration form (exchange, pair, timeframe, candle limit).
- Contains the `build_figure` function for graph rendering.

### `collector_bot.py`
- Inherits from `BaseBot`.
- Periodically fetches OHLCV data via `AsyncExchangeFetcher` and saves it to SQLite.
- Supports initial historical data loading and pruning old candles.

### `fetcher.py`
- Asynchronous client based on `ccxt` with support for `spot` / `futures`.
- `fetch_ohlcv` method returns a `pandas.DataFrame`.
- Uses `universal_resolver` for cross-platform DNS resolution.

### `logger.py`
- Singleton `PerformanceLogger` with configurable log levels for different modules.
- Loading / saving settings from/to the database.

---

## Installation and Running

### Using Conda (recommended)

~~~bash
conda create -n tbot python=3.10
conda activate tbot
pip install -r requirements.txt
~~~

### Running

~~~bash
python app.py
~~~

The application will be available at `http://127.0.0.1:8050`.

### Configuration

- In the interface (⚙️ button) you can enable debug mode and change logging levels.
- To add a new bot, click **➕**, select the type, and fill out the form.

---

## Requirements

The `requirements.txt` file should include:
dash
plotly
pandas
ccxt
aiohttp
sqlalchemy # optional, if ORM is needed
---

## Extending Functionality

Thanks to the modular architecture, you can easily add:

- New bot types (analytics, trading, notifications).
- New data sources (exchanges, APIs).
- New visualizations (indicators, volumes, order books).
- Data export to other formats.

Simply create a new module and register it with the registry — the core remains unchanged.

---

## Disclaimer

**Risk Warning:** Trading cryptocurrencies and other digital assets involves significant risk and may result in the loss of your invested capital. This software is provided for educational and research purposes only. The author assumes no responsibility for any financial losses or damages incurred through the use of this software. Use at your own risk.

---

## Author

Created and maintained by [sergson](https://github.com/sergson)

---

## License

GNU General Public License v3.0
