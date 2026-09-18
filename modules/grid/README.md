# Grid Bot – Technical Manual (ORM Edition)

## Overview

**Grid Bot** is a configurable averaging bot that builds a grid of price levels, generates signals for averaging entries, and manages position closure based on configurable PNL thresholds.
It is part of the **T.B.O.T** framework and can operate **autonomously** (with built-in execution emulation) or together with a separate **executor bot**.

The bot is fully migrated to the new ORM layer (`core.database`). No direct SQL queries remain; all database operations are performed through declarative models and the `self.env` environment.

## How the Grid Bot Works

The Grid Bot is defined in `modules/grid/models.py` as `GridBot`. It inherits from `BaseBot` (`_inherit = "base.bot"`) and implements the required `start()` and `stop()` methods, along with comprehensive averaging logic.

### Core Loop

1. **Initialisation**
   - Loads its configuration from the **local** database `data/bot_<id>.db`, table `bot_settings`, via `core.database.get_bot_config(bot_id)`. `get_bot_config()` reads all key/value pairs from the local `bot_settings` table (each value JSON-decoded) and adds `bot_type` (and optionally `status`) from the global `config.db`.
   - Creates the ORM environment `self.env = DBSQLite3(db_path)`, which manages the bot's own tables (`levels_signals`, `deals_history`, `price_history`, `bot_settings`).
   - Reads/creates `current_position_id` from `bot_settings` via the ORM (`self.env['bot.settings']`) — this is the **same** `bot_settings` table that stores the configuration, but the key is written directly by `GridBot`, not through the add-bot form.
   - Connects to the specified collector bot to receive real-time price data via the inter-bot exchange mechanism (`setup_exchange` / `get_exchange`).
   - Cleans up orphaned positions (`_ensure_clean_levels_table`): orphan records whose `position_id` is already present in `deals_history` are purged; if exactly one unknown orphan exists, it is adopted as the current position; if several exist, they are moved to history.
   - If no active position exists, determines the initial market direction (based on the last two prices, or via an analyst bot when available — `_get_initial_direction`) and builds a fresh grid of levels using `_rebuild_levels`.

2. **Main Cycle (`_run`)**
   Every `poll_interval_sec` seconds the bot:
   - Updates price history from the collector via `self._collector_handle.get("candles", limit=limit)`.
   - Checks all grid levels against the current price and extrapolation to decide whether to generate an averaging signal.
   - Handles timeouts for unexecuted signals (only when `execution_timeout_sec > 0`), applying `recalc_strategy`.
   - Evaluates smart-averaging reversal conditions and smart-execution follow-ups.
   - If `self.config_dirty` is set, calls `on_config_updated()` and recalculates pending levels.
   - If a position was closed in the previous iteration, automatically opens a new one (`_need_new_position` flag).

3. **Signal Generation**
   When the price approaches an averaging level, the bot either:
   - **Immediately** creates a signal if the price has already crossed the level.
   - **Predictively** creates a signal if a linear extrapolation (`linear_extrapolate_price`) indicates the price will hit the level within `execution_reserve_sec` seconds.
   The signal is recorded in `levels_signals` with `signal_flag=1`.
   In **emulation mode** (`execution_timeout_sec == 0`), the signal is instantly marked as executed (`real_flag=1`) and the grid is recalculated for subsequent levels (`_apply_execution_and_recalc`).

4. **Position Closure**
   - **Liquidation**: triggered when the current PNL ≤ `-liquidation_pnl`. Order type from `liquidation_order_type`.
   - **Take-Profit**: triggered when PNL ≥ `close_pnl` (optionally gated by an analyst signal when `use_analyst_close=1` and `analyst_bot_id` is set — `_get_analyst_signal`, currently a stub returning `None`).
   When a closure signal is generated, the bot either waits for an executor (if `execution_timeout_sec > 0`) or immediately closes the position, copies all records to `deals_history` (`moved_to_history_at` is stamped with `self.get_time_ns()`), resets `max_averaging_count` to `max_averaging_count_initial`, and starts a new position (new UUID).
   A manual close is available via `request_close_position()` (called from UI).

5. **Concurrency**
   - Runs as an asyncio task inside the dedicated event loop thread (`bot_manager.loop`).
   - All database operations are done through the ORM, which uses `threading.RLock` and WAL mode, ensuring thread safety.

## Database Structure

### Local Bot Database

Each grid bot creates its own SQLite database file at `data/bot_{bot_id}.db`. It contains the following tables.

#### `levels_signals` – Active Position Grid

| Column                      | Type       | Description                                                                                     |
|-----------------------------|------------|-------------------------------------------------------------------------------------------------|
| `id`                        | INTEGER PK | Auto-increment ID                                                                              |
| `position_id`               | TEXT       | Unique identifier for the current position (changes after each close)                          |
| `signal_number`             | INTEGER    | `0` = entry, `1..max_averaging_count` = averaging levels, `-1` = liquidation line, `-2` = close signal |
| `current_price`             | REAL       | Market price at the moment the record was created/updated                                      |
| `current_timestamp`         | INTEGER    | Unix UTC timestamp in **nanoseconds** of `current_price`                                        |
| `signal_volume_rates`       | JSON       | List of volume parts for this level (e.g. `[1.0, 2.0, 4.0]`)                                    |
| `signal_direction`          | SELECTION  | `'buy'` / `'sell'` / `'close'` / `'liquidation'`                                                |
| `signal_vol_rate`           | REAL       | Volume of **this level only** (in relative parts)                                              |
| `signal_order_type`         | SELECTION  | `'limit'` or `'market'`                                                                        |
| `signal_price`              | REAL       | Calculated price of the level                                                                  |
| `signal_liquidation`        | BOOLEAN    | `True` if this row is a liquidation signal                                                     |
| `signal_close`              | BOOLEAN    | `True` if this row is a close signal                                                           |
| `signal_position_volume`    | REAL       | Total accumulated volume of the position **after** this level is executed                     |
| `avg_entry_price`           | REAL       | (Signal) average entry price after this level (signal estimate)                                |
| `position_cost`             | REAL       | Total cost of the position after this level                                                    |
| `signal_pnl`                | REAL       | Estimated PNL% (with leverage) after this level                                                |
| `signal_flag`               | BOOLEAN    | `False` = inactive level, `True` = signal generated (waiting for execution)                     |
| `signal_timestamp`          | INTEGER    | Unix timestamp (ns) when the signal was created                                                 |
| `signal_smart`              | BOOLEAN    | `True` if this is a smart-averaging reversal signal                                             |
| `signal_smart_reversed`     | INTEGER    | Marker of the parent level for smart reversal (or `-1` / `-2` state flags)                      |
| `real_timestamp`            | INTEGER    | Time (ns) of actual execution (filled by executor or emulation)                                 |
| `real_vol_rate`             | REAL       | Actually executed volume                                                                       |
| `real_price`                | REAL       | Actual execution price                                                                         |
| `real_pnl`                  | REAL       | Actually realised PNL%                                                                        |
| `real_position_volume`      | REAL       | Actual total position volume after execution                                                   |
| `real_flag`                 | BOOLEAN    | `False` = not executed, `True` = executed (real data available)                                 |
| `real_avg_entry_price`      | REAL       | Actual average entry price after execution                                                     |
| `created_at`                | CHAR       | Row creation time (default `"CURRENT_TIMESTAMP"` literal string; not auto-updated)              |

#### `deals_history` – Closed Positions

Mirrors `levels_signals` with two changes:
- PK is `history_id` (INTEGER, auto-increment), not `id`.
- Adds `moved_to_history_at` (INTEGER, ns) — stamp when the row was copied from `levels_signals`.

Records are copied here during `_close_position()`; the `id` and `created_at` columns are stripped.

#### `price_history` – Market Prices

| Column      | Type       | Description                          |
|-------------|------------|--------------------------------------|
| `timestamp` | INTEGER PK | Unix UTC timestamp (nanoseconds)    |
| `open`      | REAL       |                                      |
| `high`      | REAL       |                                      |
| `low`       | REAL       |                                      |
| `close`     | REAL       |                                      |
| `volume`    | REAL       |                                      |

Filled from the collector bot's candles via exchange. Row count is trimmed when it exceeds `limit * 1.1`.

#### `bot_settings` – Configuration AND Runtime State

This is the **same** `bot_settings` table used by `core.database.add_bot()` / `update_bot_config()`. It stores both:

- **Configuration parameters** of the bot (written by the add-bot / edit-bot form via `update_bot_config()`, read back by `get_bot_config()`).
- **Runtime state keys** written directly by `GridBot` through the ORM (`self.env['bot.settings']`).

Runtime-only keys used by `GridBot`:

| Key                    | Meaning                                        | Written by                      |
|------------------------|------------------------------------------------|----------------------------------|
| `current_position_id`  | UUID of the current active position            | `_get_current_position_id` / `_set_position_id` |
| `max_averaging_count`  | Current (runtime) cap on averaging levels      | `start()` / `_close_position`   |

Because `update_bot_config()` merges rather than replaces, these runtime keys survive configuration updates. Conversely, `GridBot.__init__` reads `max_averaging_count` via the ORM model `bot.settings`, not via `get_bot_config()`, because it may have been changed at runtime.

### Global Configuration Database (`config.db`)

The `bots` table holds metadata (id, type, name, status, position, created_at) for all bots. The `settings` table holds application-wide key/value settings. Neither stores the bot's own configuration — that lives in the local `bot_settings` table described above.

## Configuration Parameters

All parameters can be set via the web UI or programmatically. Default values are shown for a new bot (as defined in `form_component()`).

| Parameter                    | Type    | Default          | Description                                                                                                   |
|------------------------------|---------|------------------|---------------------------------------------------------------------------------------------------------------|
| `collector_bot_id`           | INTEGER | *required*       | ID of the collector bot that provides OHLCV data                                                              |
| `collector_table`            | TEXT    | `None` (auto)    | Specific table name in the collector's DB (optional)                                                          |
| `analyst_bot_id`             | INTEGER | `None`           | ID of an analyst bot for entry/exit signals (optional)                                                        |
| `use_analyst_close`          | INTEGER | `1`              | Whether to wait for an analyst signal before closing (1 = Yes)                                                |
| `leverage`                   | INTEGER | `200`            | Leverage used in PNL calculations                                                                             |
| `averaging_strategy`         | TEXT    | `"sum_prev"`     | Volume progression: `"equal"`, `"sum_prev"`, `"double"`, `"triple"`                                          |
| `averaging_threshold_pnl`    | REAL    | `20`             | PNL% drop from current average that triggers the next averaging level                                        |
| `max_averaging_count`        | INTEGER | `7`              | Maximum number of averaging levels                                                                           |
| `smart_averaging_count`      | INTEGER | `2`              | Number of recent levels to monitor for price reversal (smart averaging). Capped to `max_averaging_count`.     |
| `breakeven_pnl`              | REAL    | `4.0`            | PNL% at which the break-even line is drawn                                                                   |
| `close_pnl`                  | REAL    | `50.0`           | Target PNL% for closing the whole position                                                                   |
| `liquidation_pnl`            | REAL    | `90`             | PNL% at which the position is forcibly closed (liquidation)                                                  |
| `liquidation_order_type`     | TEXT    | `"market"`       | Order type for liquidation: `"market"` or `"limit"`                                                          |
| `poll_interval_sec`          | INTEGER | `30`             | Seconds between main loop iterations                                                                          |
| `execution_timeout_sec`      | INTEGER | `0`              | Max seconds to wait for an executor to fill a signal; `0` = instant emulation                                |
| `recalc_strategy`            | TEXT    | `"recalc_grid"`  | Action on signal timeout: `"recalc_grid"`, `"market_order"`, `"none"`                                        |
| `execution_reserve_sec`      | INTEGER | `30`             | Seconds to look ahead via linear extrapolation for predictive signals                                        |

### Derived / runtime-only keys

| Key                          | Written by                                       | Notes                                                                                                  |
|------------------------------|--------------------------------------------------|--------------------------------------------------------------------------------------------------------|
| `execution_reserve_ratio`    | UI form only                                     | Fraction of `execution_timeout_sec`; consumed by `prepare_config_for_save()` which converts it into `execution_reserve_sec` and drops the key. Never persisted. |
| `max_averaging_count_initial`| `prepare_new_config` / `process_edit_save`       | Snapshot of the user-configured `max_averaging_count`. Used to reset the runtime value after each position close. |
| `data_db_path`               | Core (`add_bot`)                                 | Set to `data/bot_{bot_id}.db` on first save; enforced by `process_edit_save`.                            |

There is **no fixed cap** on the number of closed positions kept in `deals_history` in the current code.

## Inter-Bot Data Exchange

### Receiving Price Data from a Collector

The grid bot obtains market prices exclusively through the **ExchangeHandle** mechanism provided by `BotManager`.

    # Inside GridBot.start()
    await self.setup_exchange(collector_id, {"candles": ["candles", "ohlcv"]})
    self._collector_handle = self.get_exchange(collector_id)

Later, in `_update_price_history`:

    candles = await self._collector_handle.get("candles", limit=limit)

Note: the capability name is **`"candles"`** (not `"ohlcv_data"`). The collector must expose a capability whose advertised keywords include `"candles"` / `"ohlcv"`. This completely decouples the grid bot from the collector's database paths and table names.

### Exposing Signals to an Executor (Future)

The grid bot already exposes its capabilities for consumption by external bots (declared in `get_capabilities()`):

- `"signal"` – get pending signals (`signal_flag=True, real_flag=False`) via `_get_pending_signals`.
- `"record_execution"` – set execution results via `_set_execution_result` (fills `real_*` fields and sets `real_flag=True`).
- `"get_position_id"` – returns the current position UUID (`_get_position_id`).
- `"get_signals_by_position"` – returns all signals of a specific position (active + history, `_get_signals_by_position`).
- `"timeout"` – returns `execution_timeout_sec` (`_get_timeout`).

This allows a dedicated executor bot to monitor the grid bot, execute orders on an exchange, and report back, making the system fully automated when `execution_timeout_sec > 0`.

## Managing Grid Bots Programmatically

All management functions rely on the core modules: `core.database` and `core.bot_manager`.

### Adding a New Grid Bot

    from core.database import add_bot
    from core.bot_manager import bot_manager

    def create_and_start_grid(collector_id: int, **kwargs) -> int:
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

        bot_id = add_bot('grid', 'grid bot', config)
        bot_manager.add_bot(bot_id)
        bot_manager.start_bot(bot_id)

        return bot_id

### Starting / Stopping a Bot

    from core.bot_manager import bot_manager

    bot_manager.start_bot(bot_id)
    bot_manager.stop_bot(bot_id)

Status is reflected in the database and UI.

### Deleting a Bot and Cleaning Up

    import os
    from core.bot_manager import bot_manager
    from core.database import delete_bot

    def delete_grid_bot(bot_id):
        bot_manager.remove_bot(bot_id)
        delete_bot(bot_id)
        db_path = f"data/bot_{bot_id}.db"
        if os.path.exists(db_path):
            os.remove(db_path)

The local DB file is also cleaned automatically by `cleanup_orphan_databases()` on next startup.

### Modifying Bot Parameters on the Fly

Parameters can be changed while the bot is running:

    from core.database import get_bot_config, update_bot_config
    from core.bot_manager import bot_manager

    config = get_bot_config(bot_id)
    config['close_pnl'] = 60.0
    config['averaging_threshold_pnl'] = 15

    update_bot_config(bot_id, config)

    if bot_id in bot_manager.bots:
        bot_manager.bots[bot_id].config_dirty = True

The bot's main loop will call `on_config_updated()` and recalculate future levels via `_update_levels()`.

## UI Behaviour & Visualization

The web dashboard renders each grid bot with a rich interactive view.

### Main Chart

- **Price line** – black line from `price_history`.
- **Levels**:
  - **Liquidation line** – red solid.
  - **Break-even line** – green dashed.
  - **Close PNL line** – green solid.
  - **Averaging levels** – blue dashed (non-executed only; grouped under one legend entry).
- **Markers**:
  - **Entry** – green/red triangle (buy/sell).
  - **Active signals** – light green/red triangles.
  - **Executed levels** – dark green/red triangles.
  - **Close/Liquidation signals** – blue X / black star.
  - **Real executions** – dark variants of the same shapes.

All lines and markers update in real time (via `global-interval`).

### History Chart & Deals Table

Below the main chart, a separate **History Chart** displays closed deals from `deals_history` using a black price line (`colors.BLACK`) and markers for signals / real executions:
- Signals: `MEDIUM_GREEN` triangles-up, `MEDIUM_RED` triangles-down, `MEDIUM_BLUE` x (close), `MEDIUM_GRAY` star (liquidation).
- Real executions: `DARK_GREEN` / `DARK_RED` triangles, `DARK_BLUE` x (real close), `BLACK` star (real liquidation).

A **Deals History** table shows one position at a time. It includes navigation buttons (**Prev** / **Next**) to browse through all closed positions stored in `deals_history`, ordered by `max(moved_to_history_at)` per `position_id`, descending. The table lists signal and real data: times, prices, volumes, PNL, and cumulative position volume. Rows with `signal_number = -1` or `-2` are tinted green/red depending on the sign of `real_pnl`.

### Inline Editing & Buttons

- **Edit** button opens an inline form with all current parameters – save applies changes immediately without stopping the bot.
- **Close Position** button manually triggers a position closure at the current market price via `request_close_position()` (dispatched onto `bot_manager.loop` with `asyncio.run_coroutine_threadsafe`).
- **Start/Stop** and **Delete** buttons control the bot's lifecycle.

The chart remembers its zoom/pan state across updates (`relayout-store`).

## Extending GridBot via Registry Inheritance

Like the collector, the grid bot can be extended using the `_inherit` mechanism.

    from core import auto_reg

    @auto_reg
    class MyGridExtension:
        _inherit = "grid.bot"

        def my_custom_method(self):
            # access self.config, self.env, etc.
            pass

Extensions are automatically merged into the final `GridBot` class (which itself is declared as `_inherit = "base.bot"`). You can also override `start()`, `stop()`, or `_run()` – always call `super()` to preserve core logic.

## Troubleshooting & Logging

### Log Files

All grid-related logs use the module type `"analytics"`, so they are written to:

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
| Database is locked                        | Multiple connections writing simultaneously; WAL mode not set  | ORM uses RLock and WAL; avoid external connections                                          |
| New position not opening after close      | `_need_new_position` flag not handled                          | Check `_run` loop for the flag and that `_rebuild_levels` is called                        |
| Real execution markers missing            | `real_flag` not set or `real_price` is NULL                   | Verify that signals are actually being executed (emulation sets these fields)              |
| Orphan position silently dropped          | Multiple `position_id`s not present in `deals_history`         | See `_ensure_clean_levels_table` — orphans are moved to history and cleared                |

For deeper debugging, increase the log level for `analytics` to `DEBUG` in the settings panel.

## Summary of Key Code Snippets

| Action                                      | Code / Function                                                              |
|---------------------------------------------|------------------------------------------------------------------------------|
| Create and start a grid bot                 | `add_bot('grid', 'grid bot', config)` + `bot_manager.add_bot()` + `start_bot()` |
| Get pending signals (for executor)          | `await bot_instance._get_pending_signals()` (via capabilities)               |
| Report execution result                     | `await bot_instance._set_execution_result(data)`                             |
| Trigger manual close                        | `await bot_instance.request_close_position()`                                |
| Apply configuration change while running    | `bot_instance.config_dirty = True` or `await bot_instance.on_config_updated()` |
| Read closed deals from history              | Query `deals_history` table using ORM                                        |
| Extend with custom methods                  | Create a class with `_inherit = "grid.bot"` and use `@auto_reg`              |

---

For any questions or further details, refer to the source code of `modules/grid/` and the core modules. The grid bot is designed for flexibility – it can operate as a standalone automated strategy or as a signal generator for an external execution system.