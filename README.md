# T.B.O.T — Trading Bot Open Toolkit

## Architecture

The project is built on a modular, plugin-based architecture with a central model registry.  
The core (`core/`) provides foundational services (ORM database access, logging, bot lifecycle), while all business logic resides in independent modules inside the `modules/` folder. The core has no hardcoded knowledge of any specific bot type — everything is discovered and assembled at runtime.

### Key Principles

- **Model Registry** (`core/registry.py`) — a singleton that holds all model classes and their metadata. Classes are registered via the `@auto_reg` decorator. Extensions through `_inherit` are supported: child classes add or modify existing behavior, and `super()` continues to work through the entire inheritance chain.
- **Dynamic Module Loading** — at startup, the system scans the `modules/` directory and imports all valid packages. Registration occurs automatically.
- **Extension by Inheritance** — modules can add or override behavior of existing bot classes by specifying `_inherit` and using `@auto_reg`. The final class combines the original and extension, preserving the original name.
- **Separation of Concerns** — each module contains its bot logic (`models.py`), UI components (`components.py`), and optional internal libraries (`lib/`).
- **Dynamic Inter-Bot Communication** — bots can declare their data capabilities and access data from other bots through an exchange mechanism without direct imports or tight coupling.

## Project Structure

    tbot/
    ├── app.py                     # Dash entry point (universal UI core)
    ├── config.db                  # Global settings database (SQLite)
    ├── data/                      # Bot databases
    │   └── bot_*.db               # Individual bot databases (config + runtime data)
    ├── core/                      # Foundation layer
    │   ├── __init__.py
    │   ├── registry.py            # Model registry and registration decorator
    │   ├── base_bot.py            # Abstract base class for all bots
    │   ├── exchange.py            # ExchangeHandle for inter-bot data exchange
    │   ├── loader.py              # Dynamic module discovery
    │   ├── bot_manager.py         # Bot lifecycle management
    │   ├── database.py            # ORM layer, declarative models, DB utilities
    │   ├── logger.py              # Configurable logger with moods
    │   ├── colors.py              # Color palette (reference)
    │   ├── styles.py              # Standard styles, fonts, margins
    │   └── graphics.py            # Atomic factories for Plotly graphic elements
    ├── modules/                   # Plug-in modules
    │   ├── collector/             # Data collector bot module
    │   │   ├── __init__.py
    │   │   ├── models.py          # CollectorBot class (inherits BaseBot)
    │   │   ├── components.py      # UI form, renderer, type metadata
    │   │   └── lib/               # Helper modules
    │   │       ├── fetcher.py
    │   │       └── universal_resolver.py
    │   └── grid/                  # Grid bot module
    │       ├── __init__.py
    │       ├── models.py          # GridBot logic
    │       ├── components.py      # UI, charts, deals table
    │       └── lib/               # Optional internal helpers
    ├── assets/                    # CSS and other static files
    │   └── custom.css             # Styles for custom UI elements
    ├── logs/                      # Log files
    └── requirements.txt

## ORM Layer and Declarative Models

The core includes a simple ORM:
- **Declarative models** — tables are described as classes with field attributes (`Field` and its subclasses: `Char`, `Text`, `Integer`, `Boolean`, `Float`, `Selection`, `Json`).
- **Automatic registry** — `ModelMeta` metaclass registers models in `MODEL_REGISTRY`.
- **Extension via `_inherit`** — a child class adds fields to an existing model without creating a new table.
- **`self.env` environment** — each bot receives a `DBSQLite3` instance via `self.env`, giving access to model managers by string key: `self.env['levels.signals']`.
- **ORM methods** — `search`, `search_count`, `browse`, `read`, `create`, `write`, `unlink`, `filtered`, `mapped`, `sorted`.
- **Domains** — filtering using Polish notation (`&`, `|`, `!`), supporting operators `=`, `!=`, `>`, `<`, `>=`, `<=`, `in`, `not in`, `like`, `ilike`, `between`.
- **Transactions and locking** — WAL mode, `threading.RLock`, `transaction()` context manager.

### Example model definition (inside a bot module)

```python
from core.database import Model, Integer, Float, Char, Boolean, Json

class LevelsSignals(Model):
    _name = 'levels.signals'
    _table = 'levels_signals'

    id = Integer(primary_key=True, autoincrement=True, required=True)
    position_id = Char(required=True, index=True)
    signal_number = Integer(required=True)
    signal_price = Float()
    signal_flag = Boolean(default=False)
    signal_volume_rates = Json()
    # ... other fields
```

For dynamic tables (e.g., collector candles) the flag `_dynamic = True` and the method `env.get_model_manager()` are used.

## Style and Graphics References

- **`core/colors.py`** — centralized color palette used across all modules. Changing a constant updates the color throughout the application.
- **`core/styles.py`** — standard values for fonts, margins, borders, and ready-made style dictionaries (e.g., `STYLE_BOTCARD`, `bot_card_header_style()`).
- **`core/graphics.py`** — atomic Plotly factories: lines, horizontal lines, markers, candles, volumes, `apply_layout`. They simplify chart building and ensure uniformity.

## Settings Database (`config.db`)

### Table `bots` (General Information)

| Field       | Type       | Description                         |
|-------------|------------|-------------------------------------|
| `id`        | INTEGER PK | Unique bot identifier               |
| `type`      | TEXT       | Bot type (`collector`, `grid`)     |
| `name`      | TEXT       | Display name                        |
| `status`    | TEXT       | `'running'` / `'stopped'`           |
| `position`  | INTEGER    | Sorting order in UI                 |
| `created_at`| TIMESTAMP  | Creation timestamp                  |

Each bot's configuration is **not** stored in a separate table. Instead, it is kept as JSON key-value pairs in the bot's local database (`data/bot_<id>.db`) inside the `bot_settings` table. This isolates each bot's data and simplifies removal.

### Table `settings`

| Field   | Type   | Description                          |
|---------|--------|--------------------------------------|
| `key`   | TEXT PK| Setting key                          |
| `value` | TEXT   | Value (often in JSON format)        |

### Per-Bot Database (`data/bot_<id>.db`)

- `bot_settings` — key-value storage for configuration and internal state.
- Additional tables created by the bot's logic (e.g., collector creates a candle table, Grid bot creates `levels_signals`, `price_history`, `deals_history`, etc.).

The path to this database is stored in the config under the key `data_db_path`.

## How to Add a New Bot Type

1. **Create a module folder** under `modules/`, e.g., `modules/trader/`.

2. **Add an `__init__.py`** that imports the module's components:

       from . import models
       from . import components

3. **Define the bot class** in `models.py` using the `@auto_reg` decorator:

       from core import auto_reg, BaseBot

       @auto_reg
       class TraderBot(BaseBot):
           _name = "trader.bot"
           _inherit = "base.bot"

           def __init__(self, bot_id, manager=None):
               super().__init__(bot_id, manager)
               # custom initialization

           async def start(self):
               # start logic

           async def stop(self):
               # stop logic

   - `_name` must end with `.bot`.
   - `_inherit` points to the base class (usually `"base.bot"`).

   If your bot **consumes** data from others, always pass `manager` to the parent constructor. If it **provides** data, implement `get_capabilities()`.

4. **Define the type metadata** in `components.py`:

       from core import auto_reg
       from dash import dcc, html

       def trader_form(current_bot_id=None):
           return html.Div([ ... ])

       def render_trader_block(bot_id, config, relayout_store):
           return html.Div(...)

       @auto_reg
       class TraderTypeMeta:
           _name = "trader.type"
           display_name = "Trading Bot"
           form_component = staticmethod(trader_form)
           bot_model = "trader.bot"
           render_block = staticmethod(render_trader_block)

   - `_name` must end with `.type`.
   - Additional methods: `prepare_new_config`, `process_edit_save`, `register_callbacks`.

5. **That’s it!** The loader will find the module, the registry will build the final class, and the UI will show the new type in the dropdown.

## Inter-Bot Data Exchange

### Concept

Bots can share data without direct access to each other's databases. Each bot declares its capabilities through `get_capabilities()`. A consumer describes what data it needs using keywords. The core dynamically links them via `ExchangeHandle`.

### 1. Providing Data (Provider)

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

Getters and setters must be asynchronous.

### 2. Consuming Data (Consumer)

```python
async def start(self):
    mapping = {
        "prices": ["candles", "ohlcv"],
        "ticker": ["symbol", "pair"]
    }
    await self.setup_exchange(target_bot_id=5, mapping=mapping)

# Later:
handle = self.get_exchange(5)
candles = await handle.get("prices", limit=100)
symbol = await handle.get("ticker")
```

### 3. Writing Data (Optional)

If the provider has a setter:

```python
await handle.set("local_name", new_data)
```

### 4. Lifecycle and Cleanup

When a bot is stopped or removed, `BotManager` automatically removes references to it from other bots' `dynamics`.

## Core Components

### `core/registry.py`
- Singleton model registry.
- `@auto_reg` registers classes; `_name` creates a new model, `_inherit` extends an existing one.
- When extending, a new class is created that preserves the original name.

### `core/base_bot.py`
- Abstract class `BaseBot` with `_name = "base.bot"`.
- Attributes: `bot_id`, `running`, `task`, `manager`, `dynamics`, `env` (ORM environment).
- Abstract methods: `start()`, `stop()`, `get_capabilities()`.
- `setup_exchange()` and `get_exchange()` for inter-bot communication.
- `_cleanup_dynamics()`.

### `core/exchange.py`
- `ExchangeHandle` stores references to getters/setters and allows receiving/sending data.

### `core/bot_manager.py`
- Manages running bots and their interaction.
- `request_exchange()` creates or returns an `ExchangeHandle` based on keyword mapping.
- When removing a bot, all references are cleared.

### `core/loader.py`
- Scans `modules/` and imports packages, triggering class registration.

### `core/database.py`
- Initializes `config.db`, provides functions: `add_bot`, `get_all_bots`, `get_bot_config`, `update_bot_status`, `delete_bot`, `save_setting`, `get_setting`, `cleanup_orphan_databases`.
- Implements the ORM layer: fields, metaclass, model managers, `DBSQLite3` environment, domains, transactions.
- `_save_config_to_local_db()` updates only the given keys without deleting others (preserves `current_position_id`, etc.).

### `core/logger.py`
- `PerformanceLogger` — singleton with support for module log levels (`app`, `collector`, `fetcher`, `database`, `analytics`).
- Allows loading and saving settings to the database.
- Contains a mood queue and `get_pending_mood()` method for UI indication.
- `get_recent_logs()` returns the last lines from log files.

### `core/colors.py` / `core/styles.py` / `core/graphics.py`
- Centralized references for colors, styles, and graphic element factories.

### `app.py`
- Universal Dash UI.
- Sticky panel with buttons ➕ (add bot), ⚙️ (settings), 📋 (logs).
- Supports header moods via Store and callbacks.
- Log panel updates every 1 sec if open.
- Dynamically discovers bot types and renders their cards.
- Bot cards use `html.Details`/`html.Summary` for collapsing; header color reflects status (green — running, red — stopped).

## Extending Existing Bots

To extend an existing bot, create a module with a class decorated with `@auto_reg` and `_inherit = "bot_name.bot"`:

```python
@auto_reg
class CollectorLoggerExtension:
    _inherit = "collector.bot"

    async def start(self):
        print(f"[LOG] Starting collector {self.bot_id}")
        await super().start()
        print(f"[LOG] Collector {self.bot_id} started")
```

The registry will merge the classes, preserving the original name. `super()` works correctly.

## Installation and Running

### Using Conda (recommended)

    conda create -n tbot python=3.10
    conda activate tbot
    pip install -r requirements.txt

### Running

    python app.py

The application will be available at `http://127.0.0.1:8050`.

## Configuration

- The ⚙️ button opens the settings panel: debug mode, logging levels for different modules.
- The ➕ button adds a new bot; choose the type from the dropdown and fill out the form.
- The 📋 button shows recent log entries.
- The T.B.O.T header collapses/expands on click; when collapsed, it shrinks and compresses.
- Bot cards collapse by clicking the header; header color indicates status.

## Requirements

    dash
    plotly
    pandas
    ccxt
    aiohttp

## Disclaimer

**Risk Warning:** Trading cryptocurrencies and other digital assets involves significant risk and may result in the loss of your invested capital. This software is provided for educational and research purposes only. The author assumes no responsibility for any financial losses or damages incurred through the use of this software. Use at your own risk.

## Author

Created and maintained by [sergson](https://github.com/sergson)

## License

GNU General Public License v3.0