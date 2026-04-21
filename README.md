# T.B.O.T — Trading Bot Open Toolkit

## Architecture

The project is built on a modular, plugin-based architecture with a central model registry.  
The core (`core/`) provides foundational services (database, logging, bot lifecycle), while all business logic resides in independent modules inside the `modules/` folder. The core has no hardcoded knowledge of any specific bot type — everything is discovered and assembled at runtime.

### Key Principles

- **Model Registry** (`core/registry.py`) — a singleton that holds all bot models and their metadata. Classes are registered via a decorator and can be extended by other modules through inheritance. The registry supports proper `super()` chaining: extensions can call the original method using `super().method()` and the call will traverse the entire inheritance chain.
- **Dynamic Module Loading** — at startup, the system scans the `modules/` directory and imports all valid packages. Registration happens automatically as modules are loaded.
- **Extension by Inheritance** — modules can add or override behaviour of existing bot classes by defining a class with an `_inherit` attribute pointing to the base model name. The registry merges these classes while preserving the original model name, allowing `super()` to work seamlessly.
- **Separation of Concerns** — each module contains its own bot logic (`models.py`), UI components (`components.py`), and optional internal libraries.

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
│   ├── loader.py              # Dynamic module discovery
│   ├── bot_manager.py         # Bot lifecycle management
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

        from . import models
        from . import components

3. **Define the bot class** in `models.py` using the `@auto_reg` decorator:

        from core import auto_reg, BaseBot

        @auto_reg
        class TraderBot(BaseBot):
            _name = "trader.bot"
            _inherit = "base.bot"

            def __init__(self, bot_id):
                super().__init__(bot_id)
                # custom initialisation

            async def start(self):
                # custom start logic

            async def stop(self):
                # custom stop logic

   The `_name` uniquely identifies this model. `_inherit` tells the registry that this class extends `base.bot`.

4. **Define the type metadata** in `components.py`:

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

   - `_name` must end with `.type` – this is how the UI discovers available bot types.
   - `bot_model` points to the bot class name defined above.

5. **Optionally add a manifest** `__manifest__.py` (for future dependency resolution):

        {
            'name': 'Trader',
            'version': '1.0',
            'depends': [],
            'author': 'Your Name',
        }

That's it! The loader will find the module at startup, the registry will build the final bot class (with all extensions applied), and the UI will automatically show "Trading Bot" in the type dropdown.

---

## Core Components

### `core/registry.py`
- Singleton registry storing all model classes.
- `@auto_reg` decorator registers a class. Classes with `_name` define a new model; classes with `_inherit` extend an existing one.
- Merges extensions by creating a new class that inherits from the extension and the current model, preserving the original `_name`. This ensures that `super()` calls in extensions correctly dispatch to the next class in the method resolution order (MRO).

### `core/base_bot.py`
- Abstract base class `BaseBot` with `_name = "base.bot"`.
- Defines the minimum interface (`start()`, `stop()`) and common attributes (`bot_id`, `running`, `task`).

### `core/loader.py`
- Scans the `modules/` folder for subdirectories containing `__init__.py`.
- Imports each package, triggering the registration of all decorated classes inside.

### `core/bot_manager.py`
- Manages running bot instances.
- Loads bots marked as `'running'` from the database on startup.
- Uses the registry to instantiate the correct bot class for a given type.
- Handles start/stop/remove operations with asyncio integration.

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

    # modules/collector_logger/models.py
    from core import auto_reg

    @auto_reg
    class CollectorLoggerExtension:
        _inherit = "collector.bot"

        async def start(self):
            print(f"[LOG] Starting collector {self.bot_id}")
            await super().start()  # Calls the original collector.bot start() method
            print(f"[LOG] Collector {self.bot_id} started")

Because the registry merges this class with the original `collector.bot`, the final class will have the combined behaviour. The `super()` call works correctly, walking up the inheritance chain, so the original bot logic is preserved and extended.

**Important:** When writing an extension class, always call `super().__init__(*args, **kwargs)` in the constructor (if you override it) and use `super().method()` in overridden methods to maintain the chain.

---

## Installation and Running

### Using Conda (recommended)

    conda create -n tbot python=3.10
    conda activate tbot
    pip install -r requirements.txt

### Running

    python app.py

The application will be available at `http://127.0.0.1:8050`.

### Configuration

- Click the ⚙️ button to toggle debug mode or change logging levels for different components.
- Click ➕ to add a new bot — choose the type from the dropdown and fill in the generated form.

---

## Requirements

The `requirements.txt` file should include:

    dash
    plotly
    pandas
    ccxt
    aiohttp

---

## Disclaimer

**Risk Warning:** Trading cryptocurrencies and other digital assets involves significant risk and may result in the loss of your invested capital. This software is provided for educational and research purposes only. The author assumes no responsibility for any financial losses or damages incurred through the use of this software. Use at your own risk.

---

## Author

Created and maintained by [sergson](https://github.com/sergson)

---

## License

GNU General Public License v3.0