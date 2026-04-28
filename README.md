# T.B.O.T — Trading Bot Open Toolkit

## Architecture

The project is built on a modular, plugin-based architecture with a central model registry.  
The core (`core/`) provides foundational services (database, logging, bot lifecycle), while all business logic resides in independent modules inside the `modules/` folder. The core has no hardcoded knowledge of any specific bot type — everything is discovered and assembled at runtime.

### Key Principles

- **Model Registry** (`core/registry.py`) — a singleton that holds all bot models and their metadata. Classes are registered via a decorator and can be extended by other modules through inheritance. The registry supports proper `super()` chaining: extensions can call the original method using `super().method()` and the call will traverse the entire inheritance chain.
- **Dynamic Module Loading** — at startup, the system scans the `modules/` directory and imports all valid packages. Registration happens automatically as modules are loaded.
- **Extension by Inheritance** — modules can add or override behaviour of existing bot classes by defining a class with an `_inherit` attribute pointing to the base model name. The registry merges these classes while preserving the original model name, allowing `super()` to work seamlessly.
- **Separation of Concerns** — each module contains its own bot logic (`models.py`), UI components (`components.py`), and optional internal libraries.
- **Dynamic Inter‑Bot Communication** — bots can expose their data capabilities and other bots can access them through a central exchange mechanism, without direct coupling.

---

## Project Structure

```
tbot/
├── app.py                     # Dash entry point (universal UI core)
├── config.db                  # Settings database (SQLite)
├── data/                      # Market data databases
│   └── bot_*.db
├── core/                      # Foundation layer
│   ├── __init__.py
│   ├── registry.py            # Model registry and registration decorator
│   ├── base_bot.py            # Abstract base class for all bots
│   ├── exchange.py            # ExchangeHandle for inter-bot data access
│   ├── loader.py              # Dynamic module discovery
│   ├── bot_manager.py         # Bot lifecycle management & exchange orchestration
│   ├── database.py            # Common DB functions (bots, settings)
│   └── logger.py              # Configurable logging
├── modules/                   # Plug-in modules
│   └── collector/             # Example collector bot module
│       ├── __init__.py
│       ├── __manifest__.py    # Module metadata (optional)
│       ├── models.py          # Bot class definition (inherits BaseBot)
│       ├── components.py      # UI form, renderer, and type metadata
│       └── lib/               # Module-specific utilities
│           ├── fetcher.py
│           └── universal_resolver.py
├── logs/                      # Log files
└── requirements.txt
```

---

## Settings Database (`config.db`)

### Table `bots` (General Information)

| Field         | Type        | Description                                      |
|---------------|-------------|--------------------------------------------------|
| `id`          | INTEGER PK  | Unique bot identifier                            |
| `type`        | TEXT        | Bot type identifier (e.g., `'collector'`)        |
| `name`        | TEXT        | Display name                                     |
| `status`      | TEXT        | `'running'` / `'stopped'`                        |
| `position`    | INTEGER     | Sorting order in UI                              |
| `created_at`  | TIMESTAMP   | Creation timestamp                               |
| `config_data` | TEXT        | **JSON** with arbitrary configuration fields     |

### Type Configuration Tables (`config_<type>_type`)

For each registered bot type, a separate table is created based on the schema defined in its metadata class. Example for the `collector` type:

```sql
CREATE TABLE config_collector_type (
    bot_id INTEGER PRIMARY KEY,
    exchange TEXT NOT NULL,
    market_type TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    candles_limit INTEGER NOT NULL,
    data_db_path TEXT NOT NULL,
    FOREIGN KEY(bot_id) REFERENCES bots(id) ON DELETE CASCADE
);
```

The table name and columns are derived from the `config_schema` attribute of the type metadata class.

### Table `settings`

| Field   | Type    | Description                    |
|---------|---------|--------------------------------|
| `key`   | TEXT PK | Setting key                    |
| `value` | TEXT    | Value (often in JSON format)   |

---

## How to Add a New Bot Type

1. **Create a module folder** under `modules/`, e.g., `modules/trader/`.
2. **Add an `__init__.py`** that imports the module's components:

    ```python
    from . import models
    from . import components
    ```

3. **Define the bot class** in `models.py` using the `@auto_reg` decorator:

    ```python
    from core import auto_reg, BaseBot

    @auto_reg
    class TraderBot(BaseBot):
        _name = "trader.bot"
        _inherit = "base.bot"

        def __init__(self, bot_id, manager=None):
            super().__init__(bot_id, manager)
            # custom initialisation

        async def start(self):
            # custom start logic

        async def stop(self):
            # custom stop logic
    ```

   The `_name` uniquely identifies this model. `_inherit` tells the registry that this class extends `base.bot`.

   **For bots that will consume data from others:** always accept and pass `manager` to the parent constructor (as shown above). **For bots that provide data:** implement `get_capabilities()` (see [Inter‑Bot Data Exchange](#inter-bot-data-exchange)).

4. **Define the type metadata** in `components.py`:

    ```python
    from core import auto_reg
    from dash import dcc, html

    CONFIG_SCHEMA = {
        'exchange': 'TEXT NOT NULL',
        'api_key': 'TEXT',
        ...
    }

    def trader_form():
        return html.Div([ ... ])

    def render_trader_block(bot_id, config, relayout_store):
        return html.Div(...)

    @auto_reg
    class TraderTypeMeta:
        _name = "trader.type"
        display_name = "Trading Bot"
        form_component = staticmethod(trader_form)
        config_schema = CONFIG_SCHEMA
        bot_model = "trader.bot"
        render_block = staticmethod(render_trader_block)
    ```

   - `_name` must end with `.type` – this is how the UI discovers available bot types.
   - `bot_model` points to the bot class name defined above.

5. **Optionally add a manifest** `__manifest__.py` (for future dependency resolution):

    ```json
    {
        "name": "Trader",
        "version": "1.0",
        "depends": [],
        "author": "Your Name"
    }
    ```

That's it! The loader will find the module at startup, the registry will build the final bot class (with all extensions applied), and the UI will automatically show "Trading Bot" in the type dropdown.

---

## Inter‑Bot Data Exchange

### Concept

Bots can share data without any hardcoded imports or direct database access. Each bot declares **what** data it can provide (and optionally receive) by implementing `get_capabilities()`. A consumer bot describes **which** data it needs using a simple keyword mapping. The core framework then dynamically binds the two, handing the consumer an `ExchangeHandle` that works like a local data access object.

### 1. Exposing Data (Provider Bot)

Implement `get_capabilities()` in your bot class. Return a dictionary where each key is an internal name and the value contains:

- `keywords` – a list of strings that other bots can use to request this data (e.g., `"candles"`, `"market_data"`).
- `getter` – an **async callable** (usually a bound method) that returns the data. It may accept arguments (e.g., `limit`).
- `setter` – (optional) an async callable that receives data and stores it. Set to `None` if the data is read‑only.

**Example from `CollectorBot`:**

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

The corresponding getter methods are standard async functions that access the bot’s private database or configuration:

```python
async def _get_ohlcv_data(self, limit=500):
    # reads from SQLite and returns a list of dicts
    ...

async def _get_symbol(self):
    return self.config['symbol']
```

> **Note:** Getters and setters must be **asynchronous**, even if they do not perform I/O internally (you can just return a value directly). This keeps the interface uniform and non‑blocking.

### 2. Consuming Data (Consumer Bot)

A bot that needs data from another bot obtains an `ExchangeHandle` during its startup or at any later point.

First, ensure your bot’s `__init__` accepts and passes the `manager` argument to `BaseBot`:

```python
class AnalyticsBot(BaseBot):
    def __init__(self, bot_id, manager=None):
        super().__init__(bot_id, manager)
        ...
```

Then, request the exchange (typically inside `start()`):

```python
async def start(self):
    # Map my local names to the target’s keywords
    mapping = {
        "prices": ["candles", "ohlcv"],   # any keyword that matches the target's capability
        "ticker": ["symbol", "pair"]
    }
    # setup_exchange caches the handle in self.dynamics[target_id]
    await self.setup_exchange(target_bot_id=5, mapping=mapping)
    self.running = True
    # ... remaining start logic
```

Later, in your processing loop, use the handle to fetch data:

```python
handle = self.get_exchange(5)
if handle:
    candles = await handle.get("prices", limit=100)  # extra args are forwarded to the getter
    symbol = await handle.get("ticker")
    # ... perform analysis ...
```

You can also pass arguments to `get()` if the provider’s getter accepts them. The `ExchangeHandle` will transparently call the provider’s method with those arguments.

### 3. Writing Data (Optional)

If the target bot declares a `setter` for a capability, you can write data back:

```python
await handle.set("local_name", new_data)
```

The framework does not impose any structure on the data being written – it is up to the provider’s setter to validate and store it appropriately.

### 4. Lifecycle & Cleanup

- When a bot is stopped or removed, the `BotManager` automatically removes all `ExchangeHandle` references to that bot from other bots’ `dynamics`. You do not need to manually clean them.
- In your own `stop()` method, always call `super().stop()` (or `self._cleanup_dynamics()`) to clear your own outgoing handles. This is not strictly required but considered good practice.

---

## Core Components

### `core/registry.py`
- Singleton registry storing all model classes.
- `@auto_reg` decorator registers a class. Classes with `_name` define a new model; classes with `_inherit` extend an existing one.
- Merges extensions by creating a new class that inherits from the extension and the current model, preserving the original `_name`. This ensures that `super()` calls in extensions correctly dispatch to the next class in the method resolution order (MRO).

### `core/base_bot.py`
- Abstract base class `BaseBot` with `_name = "base.bot"`.
- Defines the minimum interface (`start()`, `stop()`) and common attributes:
  - `bot_id` – unique bot identifier.
  - `running` – boolean status flag.
  - `task` – asyncio task reference.
  - `manager` – reference to the `BotManager` (set automatically when the bot is created). Use this to request data exchanges with other bots.
  - `dynamics` – dictionary that stores `ExchangeHandle` objects (and any other dynamic runtime objects) keyed by target bot ID. Do not modify directly; use `setup_exchange()` / `get_exchange()`.
- Abstract method `get_capabilities()` – must be implemented by any bot that wishes to expose data to other bots. Returns a dictionary describing available data sets with keywords, getter and optional setter callables.
- Convenience methods:
  - `async setup_exchange(target_bot_id, mapping)` – asks the `BotManager` to create (or retrieve) an `ExchangeHandle` for the given target bot and the specified keyword mapping. The handle is stored in `self.dynamics`.
  - `get_exchange(target_bot_id)` – returns a previously obtained `ExchangeHandle` or `None`.
  - `_cleanup_dynamics()` – clears all dynamic references; called automatically when the bot is stopped (if you call `super().stop()`).

### `core/exchange.py`
- `ExchangeHandle` – a generic mediator object that holds references to getter and setter functions of a specific target bot.
- Created by `BotManager.request_exchange()` and stored in the requester’s `dynamics`.
- Usage by a consumer bot:
  ```python
  handle = self.get_exchange(target_bot_id)
  data = await handle.get("local_name", optional_args...)
  # if a setter was provided:
  await handle.set("local_name", new_value)
  ```
- The handle completely hides the data location and access details; the consumer only works with friendly local names defined during the exchange setup.

### `core/bot_manager.py`
- Manages running bot instances and inter‑bot communication.
- `request_exchange(requester_id, target_id, mapping) -> ExchangeHandle`:
  - Retrieves capabilities from the target bot.
  - Matches each local name (keys of `mapping`) to a target capability by checking keywords.
  - Creates an `ExchangeHandle` populated with the matched getters (and setters).
  - Caches the handle in `requester.dynamics[target_id]`.
  - Subsequent calls for the same pair return the already cached handle.
- When a bot is removed, all other bots’ `dynamics` entries for that bot are automatically cleared.

### `core/loader.py`
- Scans the `modules/` folder for subdirectories containing `__init__.py`.
- Imports each package, triggering the registration of all decorated classes inside.

### `core/database.py`
- Initialises `config.db` and creates tables for bots, settings, and type‑specific configuration (using schemas from registered type metadata).
- Provides functions: `add_bot`, `get_all_bots`, `get_bot_config`, `update_bot_status`, `delete_bot`, `save_setting`, `get_setting`.

### `core/logger.py`
- Singleton `PerformanceLogger` with per‑module log levels.
- Persists settings in the database.

### `app.py`
- Universal Dash UI.
- Discovers available bot types from the registry (classes with `_name` ending in `.type`).
- Dynamically renders add forms and bot blocks using the functions provided by each type's metadata.
- Manages global callbacks for adding, deleting, starting/stopping bots, and updating graphs.

---

## Extending Existing Bots

To modify or add functionality to an existing bot (e.g., adding logging to all collectors), create a new module and define an extension class:

```python
# modules/collector_logger/models.py
from core import auto_reg

@auto_reg
class CollectorLoggerExtension:
    _inherit = "collector.bot"

    async def start(self):
        print(f"[LOG] Starting collector {self.bot_id}")
        await super().start()  # Calls the original collector.bot start() method
        print(f"[LOG] Collector {self.bot_id} started")
```

Because the registry merges this class with the original `collector.bot`, the final class will have the combined behaviour. The `super()` call works correctly, walking up the inheritance chain, so the original bot logic is preserved and extended.

**Important:** When writing an extension class, always call `super().__init__(*args, **kwargs)` in the constructor (if you override it) and use `super().method()` in overridden methods to maintain the chain.

> **Note on capabilities in extensions:** If you extend a provider bot, your extension can override `get_capabilities()` to add, modify, or remove entries. Remember to call `super().get_capabilities()` if you want to preserve the original capabilities and just extend them.

---

## Installation and Running

### Using Conda (recommended)

```bash
conda create -n tbot python=3.10
conda activate tbot
pip install -r requirements.txt
```

### Running

```bash
python app.py
```

The application will be available at `http://127.0.0.1:8050`.

### Configuration

- Click the ⚙️ button to toggle debug mode or change logging levels for different components.
- Click ➕ to add a new bot — choose the type from the dropdown and fill in the generated form.

---

## Requirements

The `requirements.txt` file should include:

```
dash
plotly
pandas
ccxt
aiohttp
```

---

## Disclaimer

**Risk Warning:** Trading cryptocurrencies and other digital assets involves significant risk and may result in the loss of your invested capital. This software is provided for educational and research purposes only. The author assumes no responsibility for any financial losses or damages incurred through the use of this software. Use at your own risk.

---

## Author

Created and maintained by [sergson](https://github.com/sergson)

---

## License

GNU General Public License v3.0