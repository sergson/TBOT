# Grid Bot – Technical Manual

This document describes the **Grid Bot** module – a configurable averaging bot that builds a grid of price levels, generates signals for averaging entries, and manages position closure based on configurable PNL thresholds.  
It is part of the **T.B.O.T** framework and can operate **autonomously** (with built‑in execution emulation) or together with a separate **executor bot**.

---

## Table of Contents

1. [How the Grid Bot Works](#how-the-grid-bot-works)
2. [Database Structure](#database-structure)
   - [Local Bot Database](#local-bot-database)
   - [Global Configuration Database (`config.db`)](#global-configuration-database-configdb)
3. [Configuration Parameters](#configuration-parameters)
4. [Inter‑Bot Data Exchange](#inter-bot-data-exchange)
   - [Receiving Price Data from a Collector](#receiving-price-data-from-a-collector)
   - [Exposing Signals to an Executor (Future)](#exposing-signals-to-an-executor-future)
5. [Managing Grid Bots Programmatically](#managing-grid-bots-programmatically)
   - [Adding a New Grid Bot](#adding-a-new-grid-bot)
   - [Starting / Stopping a Bot](#starting--stopping-a-bot)
   - [Deleting a Bot and Cleaning Up](#deleting-a-bot-and-cleaning-up)
   - [Modifying Bot Parameters on the Fly](#modifying-bot-parameters-on-the-fly)
6. [UI Behaviour & Visualization](#ui-behaviour--visualization)
   - [Main Chart](#main-chart)
   - [History Chart & Deals Table](#history-chart--deals-table)
   - [Inline Editing & Buttons](#inline-editing--buttons)
7. [Extending GridBot via Registry Inheritance](#extending-gridbot-via-registry-inheritance)
8. [Troubleshooting & Logging](#troubleshooting--logging)

---

## How the Grid Bot Works

The Grid Bot is defined in `modules/grid/models.py` as `GridBot`. It inherits from `BaseBot` and implements the required `start()` and `stop()` methods, along with a comprehensive averaging logic.

### Core Loop

1. **Initialisation**  
   - Reads its configuration from its own SQLite database (`data/bot_<id>.db`), specifically from the `bot_settings` table.  
   - Connects to the specified collector bot to receive real‑time price data via the inter‑bot exchange mechanism.  
   - Creates or opens the local SQLite database for storing levels, signals, price history, and internal state.  
   - If no active position exists, determines the initial market direction (based on the last two prices or an external analyst bot) and builds a fresh grid of levels using `_rebuild_levels`.

2. **Main Cycle (`_run`)**  
   Every `poll_interval_sec` seconds the bot:  
   - Updates the price history from the collector.  
   - Checks all grid levels against the current price and extrapolation to decide whether to generate an averaging signal.  
   - Handles timeouts for unexecuted signals (only when `execution_timeout_sec > 0`).  
   - Evaluates smart averaging reversal conditions.  
   - If a position was closed in the previous iteration, automatically opens a new one.

3. **Signal Generation**  
   When the price approaches an averaging level, the bot either:  
   - **Immediately** creates a signal if the price has already crossed the level.  
   - **Predictively** creates a signal if a linear extrapolation indicates the price will hit the level within `execution_reserve_sec` seconds.  
   The signal is recorded in the database with `signal_flag=1`.  
   In **emulation mode** (`execution_timeout_sec == 0`), the signal is instantly marked as executed (`real_flag=1`) and the grid is recalculated for subsequent levels.

4. **Position Closure**  
   - **Liquidation**: triggered when the current PNL ≤ `-liquidation_pnl`.  
   - **Take‑Profit**: triggered when PNL ≥ `close_pnl` (optionally after confirmation from an analyst bot).  
   When a closure signal is generated, the bot either waits for an executor (if `execution_timeout_sec > 0`) or immediately closes the position, copies all records to the `deals_history` table, and starts a new position.

5. **Concurrency**  
   - Runs as an asyncio task inside the main event loop thread.  
   - All database operations are synchronous but called via `run_in_executor` when invoked by external bots (e.g., through capabilities).  
   - Configuration changes from the web UI are applied asynchronously and trigger a safe recalculation of future levels without losing history.

---

## Database Structure

### Local Bot Database

Each grid bot creates its own SQLite database file at `data/bot_{bot_id}.db`. It contains the following tables:

#### `levels_signals` – Active Position Grid

| Column                      | Type       | Description                                                                                     |
|-----------------------------|------------|-------------------------------------------------------------------------------------------------|
| `id`                        | INTEGER PK | Auto‑increment ID                                                                              |
| `position_id`               | TEXT       | Unique identifier for the current position (changes after each close)                         |
| `signal_number`             | INTEGER    | `0` = entry, `1..max_averaging_count` = averaging levels, `-1` = liquidation line, `-2` = close signal |
| `current_price`             | REAL       | Market price at the moment the record was created/updated                                      |
| `current_timestamp`         | REAL       | Unix UTC timestamp of `current_price`                                                          |
| `signal_volume_rates`       | TEXT       | JSON list of volume parts for this level (e.g. `[1.0, 2.0, 4.0]`)                             |
| `signal_direction`          | TEXT       | `'buy'` / `'sell'` / `'close'` / `'liquidation'`                                                |
| `signal_vol_rate`           | REAL       | Volume of **this level only** (in relative parts)                                              |
| `signal_order_type`         | TEXT       | `'limit'` or `'market'`                                                                        |
| `signal_price`              | REAL       | Calculated price of the level                                                                  |
| `signal_liquidation`        | INTEGER    | Flag: `1` if this row is a liquidation signal, otherwise `0`                                   |
| `signal_close`              | INTEGER    | Flag: `1` if this row is a close signal, otherwise `0`                                         |
| `signal_position_volume`    | REAL       | Total accumulated volume of the position **after** this level is executed                     |
| `avg_entry_price`           | REAL       | (Signal) average entry price after this level (signal estimate)                                |
| `position_cost`             | REAL       | Total cost of the position after this level                                                    |
| `signal_pnl`                | REAL       | Estimated PNL% (with leverage) after this level                                                |
| `signal_flag`               | INTEGER    | `0` = inactive level, `1` = signal generated (waiting for execution)                           |
| `signal_timestamp`          | REAL       | Unix timestamp when the signal was created                                                     |
| `signal_smart`              | INTEGER    | `1` if this is a smart averaging reversal signal                                               |
| `signal_smart_reversed`     | INTEGER    | `1` if the smart signal was reversed                                                           |
| `real_timestamp`            | REAL       | Time of actual execution (filled by executor or emulation)                                     |
| `real_vol_rate`             | REAL       | Actually executed volume                                                                       |
| `real_price`                | REAL       | Actual execution price                                                                         |
| `real_pnl`                  | REAL       | Actually realised PNL%                                                                        |
| `real_position_volume`      | REAL       | Actual total position volume after execution                                                   |
| `real_flag`                 | INTEGER    | `0` = not executed, `1` = executed (real data available)                                       |
| `real_avg_entry_price`      | REAL       | Actual average entry price after execution                                                     |
| `created_at`                | TIMESTAMP  | Row creation time                                                                              |

#### `deals_history` – Closed Positions

Same structure as `levels_signals` plus `history_id` (PK) and `moved_to_history_at`.  
Records from `levels_signals` are moved here when a position closes. The number of stored positions is currently limited by a hardcoded value (`max_deals = 10000`), not by a user‑configurable parameter.

#### `price_history` – Market Prices

| Column      | Type   | Description                           |
|-------------|--------|---------------------------------------|
| `timestamp` | REAL PK| Unix UTC timestamp                  |
| `open`      | REAL   |                                      |
| `high`      | REAL   |                                      |
| `low`       | REAL   |                                      |
| `close`     | REAL   |                                      |
| `volume`    | REAL   |                                      |

Filled from the collector bot’s candles.

#### `bot_settings` – Configuration and Internal State

This table stores **both the bot’s configuration parameters** and **internal runtime state** as key‑value pairs. Values are JSON‑encoded when necessary.

Important keys include:

| Key                    | Value                                   |
|------------------------|-----------------------------------------|
| `current_position_id`  | UUID of the current active position     |
| `collector_bot_id`     | ID of the linked collector bot          |
| `leverage`             | Leverage used in PNL calculations       |
| `close_pnl`            | Target close PNL%                       |
| `data_db_path`         | Path to this local database file        |

All other configuration parameters are stored in the same table.

### Global Configuration Database (`config.db`)

This file contains only general information about all bots and global settings.

#### Table `bots`

| Column        | Type       | Description                                             |
|---------------|------------|---------------------------------------------------------|
| `id`          | INTEGER PK | Unique bot identifier                                  |
| `type`        | TEXT       | Always `'grid'`                                        |
| `name`        | TEXT       | Display name (default: `"grid bot"`)                   |
| `status`      | TEXT       | `'running'` or `'stopped'`                             |
| `position`    | INTEGER    | Sorting order in the UI                                |
| `created_at`  | TIMESTAMP  | Creation time                                          |

There is **no** `config_data` column and **no** separate `config_grid_type` table. All configuration is stored in the bot’s own local database (`bot_settings` table).

---

## Configuration Parameters

All parameters can be set via the web UI or programmatically. Default values are shown for a new bot.

| Parameter                  | Type    | Default          | Description                                                                                                   |
|----------------------------|---------|------------------|---------------------------------------------------------------------------------------------------------------|
| `collector_bot_id`         | INTEGER | *required*       | ID of the collector bot that provides OHLCV data                                                              |
| `collector_table`          | TEXT    | `None` (auto)    | Specific table name in the collector’s DB (optional)                                                          |
| `analyst_bot_id`           | INTEGER | `None`           | ID of an analyst bot for entry/exit signals (optional)                                                        |
| `use_analyst_close`        | INTEGER | `1`              | Whether to wait for an analyst signal before closing (1 = Yes)                                                |
| `leverage`                 | INTEGER | `200`            | Leverage used in PNL calculations                                                                             |
| `averaging_strategy`       | TEXT    | `"sum_prev"`     | Volume progression: `"equal"`, `"sum_prev"`, `"double"`, `"triple"`                                          |
| `averaging_threshold_pnl`  | REAL    | `20`             | PNL% drop from current average that triggers the next averaging level                                        |
| `max_averaging_count`      | INTEGER | `7`              | Maximum number of averaging levels                                                                           |
| `smart_averaging_count`    | INTEGER | `2`              | Number of recent levels to monitor for price reversal (smart averaging)                                      |
| `breakeven_pnl`            | REAL    | `4.0`            | PNL% at which stop‑loss stops moving into loss                                                               |
| `close_pnl`                | REAL    | `50.0`           | Target PNL% for closing the whole position                                                                   |
| `liquidation_pnl`          | REAL    | `90`             | PNL% at which the position is forcibly closed (liquidation)                                                  |
| `liquidation_order_type`   | TEXT    | `"market"`       | Order type for liquidation: `"market"` or `"limit"`                                                          |
| `poll_interval_sec`        | INTEGER | `30`             | Seconds between main loop iterations                                                                          |
| `execution_timeout_sec`    | INTEGER | `0`              | Max seconds to wait for an executor to fill a signal; `0` = instant emulation                                |
| `recalc_strategy`          | TEXT    | `"recalc_grid"`  | Action on signal timeout: `"recalc_grid"`, `"market_order"`, `"none"`                                        |
| `execution_reserve_sec`    | INTEGER | `30`             | Seconds to look ahead via linear extrapolation for predictive signals                                        |

The old `deals_display_count` parameter has been removed. The number of closed positions kept in history is currently fixed at 10000.

---

## Inter‑Bot Data Exchange

### Receiving Price Data from a Collector

The grid bot obtains market prices exclusively through the **ExchangeHandle** mechanism provided by `BotManager`.

    # Inside GridBot.start()
    await self.setup_exchange(collector_id, {"candles": ["candles", "ohlcv"]})
    self._collector_handle = self.get_exchange(collector_id)

Later, in `_update_price_history`:

    candles = await self._collector_handle.get("candles", limit=limit)

The collector must implement the `"ohlcv_data"` capability with keywords `["candles", "ohlcv"]` (the default collector does).  
This completely decouples the grid bot from the collector’s database paths and table names.

### Exposing Signals to an Executor (Future)

The grid bot already exposes its capabilities for consumption by external bots:

- `"signal"` – get pending signals (`signal_flag=1, real_flag=0`) via `_get_pending_signals`.
- `"record_execution"` – set execution results via `_set_execution_result` (fills `real_*` fields and sets `real_flag=1`).
- `"get_position_id"` – returns the current position UUID.
- `"get_signals_by_position"` – returns all signals of a specific position (active or historical).

This allows a dedicated executor bot to monitor the grid bot, execute orders on an exchange, and report back, making the system fully automated when `execution_timeout_sec > 0`.

---

## Managing Grid Bots Programmatically

All management functions rely on the core modules: `core.database` and `core.bot_manager`.

### Adding a New Grid Bot

    import os
    from core.database import add_bot
    from core.bot_manager import bot_manager

    def create_and_start_grid(collector_id: int, **kwargs) -> int:
        # Default configuration
        config = {
            "collector_bot_id": collector_id,
            "collector_table": None,
            "analyst_bot_id": None,
            "use_analyst_close": 1,
            "leverage": 200,
            "averaging_strategy": "sum_prev",
            "averaging_threshold_pnl": 20,
            "max_averaging_count": 7,
            "smart_averaging_count": 2,
            "breakeven_pnl": 4.0,
            "close_pnl": 50.0,
            "liquidation_pnl": 90,
            "liquidation_order_type": "market",
            "poll_interval_sec": 30,
            "execution_timeout_sec": 0,
            "recalc_strategy": "recalc_grid",
            "execution_reserve_sec": 30
        }
        config.update(kwargs)

        # add_bot creates the record in config.db and saves config to data/bot_<id>.db
        bot_id = add_bot('grid', 'grid bot', config)

        # Create the bot instance (does not start it)
        bot_manager.add_bot(bot_id)

        # Optionally start immediately
        bot_manager.start_bot(bot_id)

        return bot_id

### Starting / Stopping a Bot

    from core.bot_manager import bot_manager

    # Start (asynchronous, non-blocking)
    bot_manager.start_bot(bot_id)

    # Stop
    bot_manager.stop_bot(bot_id)

Status is reflected in the database and the UI.

### Deleting a Bot and Cleaning Up

    import os
    from core.bot_manager import bot_manager
    from core.database import delete_bot

    def delete_grid_bot(bot_id):
        # 1. Stop and remove from manager
        bot_manager.remove_bot(bot_id)

        # 2. Remove from config.db
        delete_bot(bot_id)

        # 3. The local database file (data/bot_<id>.db) is NOT deleted automatically.
        #    It will be cleaned up at the next application startup by cleanup_orphan_databases().
        #    To delete it immediately:
        db_path = f"data/bot_{bot_id}.db"
        if os.path.exists(db_path):
            os.remove(db_path)

### Modifying Bot Parameters on the Fly

Parameters can be changed while the bot is running:

    from core.database import get_bot_config, update_bot_config
    from core.bot_manager import bot_manager

    bot_id = 42

    # 1. Load current config
    config = get_bot_config(bot_id)
    if not config:
        raise ValueError("Config not found")

    # 2. Modify desired fields
    config['close_pnl'] = 60.0
    config['averaging_threshold_pnl'] = 15

    # 3. Save to the bot's local DB
    update_bot_config(bot_id, config)

    # 4. Notify the running bot (if it exists)
    if bot_id in bot_manager.bots:
        bot_manager.bots[bot_id].config_dirty = True
        # The bot's main loop will call on_config_updated() and recalculate future levels.

---

## UI Behaviour & Visualization

The web dashboard renders each grid bot with a rich interactive view.

### Main Chart

- **Price line** – black line from `price_history`.
- **Levels**:
  - **Liquidation line** – red solid.
  - **Break‑even line** – green dashed.
  - **Close PNL line** – green solid.
  - **Averaging levels** – blue dashed (non‑executed only).
- **Markers**:
  - **Entry** – green/red triangle (buy/sell).
  - **Active signals** – light green/red triangles.
  - **Executed levels** – dark green/red triangles.
  - **Close/Liquidation signals** – blue X / black star.
  - **Real executions** – dark variants of the same shapes.

All lines and markers update in real time (via the `global-interval` timer).

### History Chart & Deals Table

Below the main chart, a separate **History Chart** displays closed deals from `deals_history` using a grey price line and markers for signals/real executions.  

A **Deals History** table shows one position at a time. It includes navigation buttons (**Prev** / **Next**) to browse through all closed positions stored in `deals_history`. The table lists signal and real data: times, prices, volumes, PNL, and cumulative position volume.

### Inline Editing & Buttons

- **Edit** button opens an inline form with all current parameters – save applies changes immediately without stopping the bot.
- **Close Position** button manually triggers a position closure at the current market price.
- **Start/Stop** and **Delete** buttons control the bot’s lifecycle.

The chart remembers its zoom/pan state across updates (`relayout-store`).

---

## Extending GridBot via Registry Inheritance

Like the collector, the grid bot can be extended using the `_inherit` mechanism.

    from core import auto_reg

    @auto_reg
    class MyGridExtension:
        _inherit = "grid.bot"

        def my_custom_method(self):
            # access self.config, self._db_path, etc.
            pass

Extensions are automatically merged into the final `GridBot` class. You can also override `start()`, `stop()`, or `_run()` – always call `super()` to preserve core logic.  
Dynamic runtime patching is also possible via `bot_registry.get_model("grid.bot")`.

---

## Troubleshooting & Logging

### Log Files

All grid‑related logs use the module type `"analytics"`, so they are written to:

- `logs/analytics_YYYYMMDD.log`

The actual logger names are:

- `grid_<bot_id>` – for bot logic (`GridBot` class).
- `grid_bot_ui` – for UI/visualization components.

### Common Issues

| Problem                                   | Likely cause                                                  | Solution                                                                                   |
|-------------------------------------------|----------------------------------------------------------------|--------------------------------------------------------------------------------------------|
| Graph shows no levels or entry point      | Incorrect `position_id` lookup or no active position           | Ensure the bot is running and has a valid position. Check `bot_settings.current_position_id` |
| Buttons (Stop/Start) do not reflect state | Callback not updating on `bots-trigger`                       | Verify the callback has `Input('bots-trigger', 'data')`                                    |
| Levels shift dramatically after timeout   | Outdated `_recalc_levels_from` using old average               | Ensure the method uses `real_avg_entry_price` or `effective_avg` from the last executed level |
| Database is locked                        | Multiple connections writing simultaneously; WAL mode not set  | Enable WAL (`PRAGMA journal_mode=WAL`). Close connections promptly.                         |
| New position not opening after close      | `need_new_position` flag not handled                           | Check `_run` loop for the flag and that `_rebuild_levels` is called.                       |
| Real execution markers missing            | `real_flag` not set or `real_price` is NULL                   | Verify that signals are actually being executed (emulation sets these fields).              |

For deeper debugging, increase the log level for `analytics` to `DEBUG` in the settings panel.

---

## Summary of Key Code Snippets

| Action                                      | Code / Function                                                              |
|---------------------------------------------|------------------------------------------------------------------------------|
| Create and start a grid bot                 | `add_bot('grid', 'grid bot', config)` + `bot_manager.add_bot()` + `start_bot()` |
| Get pending signals (for executor)          | `await bot_instance._get_pending_signals()` (via capabilities)               |
| Report execution result                     | `await bot_instance._set_execution_result(data)`                             |
| Trigger manual close                        | `await bot_instance.request_close_position()`                                |
| Apply configuration change while running    | `bot_instance.config_dirty = True` or `await bot_instance.on_config_updated()` |
| Read closed deals from history              | Query `deals_history` table in the bot’s local DB                            |
| Extend with custom methods                  | Create a class with `_inherit = "grid.bot"` and use `@auto_reg`              |

---

For any questions or further details, refer to the source code of `modules/grid/` and the core modules. The grid bot is designed for flexibility – it can operate as a standalone automated strategy or as a signal generator for an external execution system.