T.B.O.T — Trading Bot Open Toolkit

> ## ⚠️ DEMONSTRATION / EDUCATIONAL VERSION
>
> **This is NOT a finished product. This is a demonstration and educational version of the T.B.O.T framework.**
>
> - The codebase is provided to illustrate architecture, patterns, and ideas — not as a ready-to-deploy trading system.
> - Do not treat any part of this repository as production-ready. APIs, configs, and behavior may change without notice.
> - If you intend to build on this code, you are responsible for your own security review, testing, and compliance.

> ## 💰 RISK DISCLAIMER — TRADING CRYPTOCURRENCIES
>
> **Trading cryptocurrencies and digital assets involves significant risk and may result in the loss of your entire invested capital.**
>
> - Digital-asset markets are volatile, may be illiquid, and are not protected by bank deposit insurance or equivalent investor safeguards.
> - Leverage (futures) magnifies both gains and losses and may result in forced liquidation.
> - APIs, exchange rules, and rate limits can change at any time; a bot that works today may fail tomorrow.
> - This software is provided **for educational and research purposes only**, "as is", without warranty of any kind.
> - The author and contributors assume **no responsibility or liability** for any financial loss, missed profit, sanction, or other damage incurred through the use of this software.
> - **Never trade with funds you cannot afford to lose.** Test on demo/testnet environments and with minimal capital first.

---

## Architecture

T.B.O.T (Trading Bot Open Toolkit) is a modular plugin-based framework with a central model registry.

The **core** (`core/`) provides foundational services — ORM-level database access, logging, bot lifecycle, precise time synchronization, and inter-bot data exchange. All business logic lives in independent modules inside `modules/`. The core knows nothing about specific bot types; everything is discovered and assembled at runtime.

### Key Principles

- **Model Registry** (`core/registry.py`) — singleton holding all registered classes and their metadata. Registration via `@auto_reg`. Extension via `_inherit`: child classes add or modify behavior, `super()` continues to work through the inheritance chain.
- **Dynamic Module Loading** — at startup the system scans `modules/` and imports all valid packages. Registration happens automatically via side effects.
- **Extension by Inheritance** — modules add or override behavior of existing bots via `_inherit` and `@auto_reg`. The final class merges the original and extension, preserving the original name.
- **Separation of Concerns** — each module contains bot logic (`models.py`), UI components (`components.py`), and optional internal libraries (`lib/`).
- **Dynamic Inter-Bot Communication** — bots declare their data capabilities and get data from other bots via the exchange mechanism, without direct imports or hard coupling.
- **Opt-in Native Providers** — for specific `(exchange, market_type)` pairs you can register a native provider (a replacement for ccxt) without changing the calling code.
- **Broker as Infrastructure** — all exchange operations pass through a single **Broker** bot. Consumers (collector, grid, execution) never talk to an exchange directly; they call broker capabilities.
- **Broker is a feed; Collector is an aggregator** — Broker exposes raw market data (candles from REST, latest price, raw trades with both `exchange_ts_ns` and `tbot_utc_ns`). Broker does **not** aggregate trades into candles. That responsibility belongs to the Collector (and only the Collector). Grid and Execution consume aggregated series or scalar prices, never raw trade streams.
- **Sparse historical data is legal (Rule G)** — the Collector never fabricates zero rows, never fills gaps with the previous `close`, never extrapolates. A gap in `ohlcv_data` means "no trades/no data over that period", not "price was 0". Consumers must handle sparse series. Zero-candles corrupt downstream PnL/level computations and are forbidden across the whole stack.

## Project Structure

```
tbot/
├── app.py                     # Dash entry point (universal UI core)
├── config.db                  # Global settings database (SQLite)
├── data/                      # Per-bot databases
│   └── bot_<id>.db            # Individual bot databases (config + runtime data)
├── core/                      # Foundation layer
│   ├── __init__.py
│   ├── registry.py            # Model registry and registration decorator
│   ├── base_bot.py            # Abstract base class for all bots
│   ├── exchange.py            # ExchangeHandle for inter-bot data exchange
│   ├── loader.py              # Dynamic module discovery
│   ├── bot_manager.py         # Bot lifecycle management
│   ├── database.py            # ORM layer, declarative models, DB utilities
│   ├── logger.py              # Configurable logger with moods
│   ├── time_sync.py           # Adaptive NTP-based precise time service
│   ├── universal_resolver.py  # Cross-platform aiohttp DNS resolver (used by broker)
│   ├── tbotex/                # Native exchange providers (opt-in)
│   │   ├── __init__.py
│   │   ├── protocol.py        # ExchangeClientProtocol (Protocol)
│   │   ├── registry.py        # (exchange, market_type) → factory
│   │   ├── loader.py          # Dynamic provider discovery
│   │   ├── ui_helpers.py      # UI dropdown for exchange_provider
│   │   └── providers/         # Catalog for native-provider implementations
│   ├── colors.py              # Color palette (reference)
│   ├── styles.py              # Standard + compact UI styles
│   └── graphics.py            # Plotly factories + info-panel / compact-table
├── modules/                   # Plug-in modules
│   ├── broker/                # Exchange-layer bot (infrastructure)
│   │   ├── __init__.py
│   │   ├── __manifest__.py
│   │   ├── models.py          # BrokerBot (inherits BaseBot)
│   │   ├── components.py      # UI: keys form, health panel, type metadata
│   │   └── lib/
│   │       ├── broker_service.py   # connection carousel, REST+WS workers (price / trades / orders)
│   │       ├── exchange_directory.py # per-exchange UI + workarounds + cid params
│   │       ├── clock.py            # ccxt/ccxt.pro TBOT-clock override
│   │       ├── key_vault.py        # encrypted key storage
│   │       ├── crypto.py           # Fernet/PBKDF2 helpers
│   │       └── order_updates.py    # per-owner WS order-update queue
│   ├── collector/             # OHLCV collector bot
│   │   ├── __init__.py
│   │   ├── __manifest__.py
│   │   ├── models.py          # CollectorBot + dynamic Candle model
│   │   ├── components.py      # UI: form, candlestick chart, type metadata
│   │   └── lib/
│   │       └── utils.py       # safe_table_name
│   ├── grid/                  # Grid-averaging bot
│   │   ├── __init__.py
│   │   ├── __manifest__.py
│   │   ├── models.py          # GridBot + LevelsSignals / DealsHistory / PriceHistory
│   │   └── components.py      # UI: form, main & history charts, deals table
│   └── execution/             # Execution bot (signal → order)
│       ├── __init__.py
│       ├── __manifest__.py
│       ├── models.py          # ExecutionBot + ExecutionOrder / PriceTick / ExecutionStats
│       ├── components.py      # UI: form, orders chart, pause badge, attention list
│       └── lib/
│           ├── order_manager.py    # thin proxy to broker (no client of its own)
│           ├── signal_manager.py   # per-signal orchestration (phases, reorder, finalize)
│           └── universal_resolver.py  # cross-platform aiohttp DNS resolver (shared copy)
├── assets/
│   └── custom.css
├── logs/
└── requirements.txt
```

> **Note on paths** — a few historical READMEs referenced `core/exchange_directory.py` and `core/universal_resolver.py`. In the current codebase the canonical location for the exchange directory is `modules/broker/lib/exchange_directory.py`; the aiohttp DNS resolver lives (or is imported as) `core.universal_resolver` — a copy also exists under `modules/execution/lib/universal_resolver.py` for standalone use.

## Modules

The framework ships with four reference modules. Their detailed technical manuals are maintained separately (see each module's README), this section is only an overview.

### Broker (`modules/broker/`)

The exchange-infrastructure bot. Exactly **one** broker per T.B.O.T installation; it is auto-created on first start and is protected from deletion or stopping via `BotManager._protected_bots`.

- **Key vault** — encrypted API-key storage (Fernet / PBKDF2-SHA256, per-key salt). Storage modes: `plain` and `encrypted`. `TBOT_KEY_PASSWORD` (env) or the UI password unlocks the vault.
- **Connection carousel + RequestCarousel** — long-lived ccxt / ccxt.pro clients per `(exchange, market_type, kind)`, plus a priority REST queue (`cancel > place > fetch`), per-exchange rate-limits, and `fetch_*`-only dedup (5 s TTL).
- **WS price stream** — one loop per `(exchange, market_type, symbol)` with per-exchange workarounds (`price_stream: watch_bids_asks | watch_order_book | watch_ticker | rest_poll`). Serves `get_latest_price`.
- **WS trades stream** — one loop per `(exchange, market_type, symbol)`, **sharing the same ccxt.pro client with the price stream** (unified `ConnectionKey.kind='ws_data'`). Maintains a bounded `deque` of raw trades with **both clocks** (`exchange_ts_ns` from the exchange feed, `tbot_utc_ns` from TBOT on receipt), a 60-second time-based eviction (monotone, immune to exchange clock drift), and a `(exchange_ts_ns, trade_id)` dedup guard against ccxt.pro replay-on-reconnect. Serves `get_trades`. **No aggregation in broker** — raw trades only; candles are the Collector's job.
- **WS orders stream** — one loop per `key_id`; pushes updates into `OrderUpdateQueue[owner_bot_id]`, consumed by execution bots via `get_order_updates`.
- **Exchange directory** — single source of truth: UI dropdown data, `build_client_id_params(exchange, op, cid)`, `get_ccxt_market_type`, `resolve_ccxt_exchange_id`, per-exchange `balance_type`/`price_stream` workarounds.
- **Capabilities** — `get_ohlcv`, `get_latest_price`, `get_trades`, `get_markets/symbols`, `create_*_order`, `cancel_order`, `fetch_*`, `set_leverage`, `panic_close`, `get_order_updates`, `register_key_owner`, plus key-CRUD and health.
- **Health panel** exposes three independent stream states: **Order book** (price feed, backed by `price_*` fields in `get_status()` for backward compat), **Trades**, **Orders** — each with its own worker count, symbol count, last-tick age and WS state.

### Collector (`modules/collector/`)

OHLCV collector. Consumes broker's `get_ohlcv` capability and stores candles in per-symbol dynamic tables (`collector.candle`, `_dynamic = True`) inside its own DB.

- Capabilities: `ohlcv_data` (serves candles with explicit `timestamp_scale='exchange_ns'`), `symbol`.
- **Rule G** is enforced structurally: only rows actually returned by `get_ohlcv` are written. No zero rows, no gap-fill, no extrapolation. `ohlcv_data` can legitimately return a sparse series.
- Historical bootstrap on first run; incremental tail fetches afterwards; pruning to `candles_limit` (with 10% over-trigger).
- On symbol change the old dynamic table is dropped; the new one is created on the next write.
- **TF-change handling**: changing `timeframe` via the form stops the bot (manual restart required). On restart the collector decides the table disposition using persisted `_last_run_mode` / `_last_run_tf` in `bot_settings`:
  - cross-mode (fast ↔ slow, or slow intra-mode) → drop table + full REST reload;
  - fast → fast coarser → aggregate existing rows;
  - fast → fast finer → drop (fine data unrecoverable, PK collision otherwise).
  In the **current REST-only version** every change is effectively drop + full reload.
- **Planned (not implemented)**: a *fast mode* that consumes broker's `get_trades`, aggregates raw trades into sub-minute TF buckets locally, and writes via REPLACE (idempotent). Grid/Execution interfaces will not change; the `ohlcv_data` capability keeps the same shape. The broker side (`get_trades`, dedup, both clocks) is already in place; only the collector aggregator loop is pending. Until then, the form only exposes slow timeframes (`1m`..`1d`) and any stale sub-minute TF in config is coerced to `1m` at start.
- UI: candlestick + volume chart, compact info panel with "last write age" indicator.

### Grid (`modules/grid/`)

Grid-averaging bot. Builds a price grid, emits signals, processes fills, manages close/liquidation/smart-averaging.

- Modes: **single-bot** (`execution_timeout_sec=0` — grid emulates fills locally) and **two-bot** (grid sends signals to an Execution bot and waits for feedback).
- Per-position `position_id`, levels in `levels.signals`, closed positions in `deals.history`, price ticks in `price.history`.
- Emits signal types `0` (entry), `1..N` (averaging), `-1` (liquidation), `-2` (close), `-3` (panic — handled by execution).
- Capabilities for execution: `signal`, `record_execution`, `get_position_id`, `timeout`, `execution_reserve_sec`, `request_close_position`, `constants` (panic number, close-market fraction, etc.).
- Sanity checks on history transfer, `skipped_position_ids` for manual review, `close_dust_fraction` for precision-remainder handling.
- **Consumes `ohlcv_data` as sparse** — Rule G applies. Missing candles in the historical window are treated as "no data", not as zero-price ticks.
- UI: live grid chart + history chart + deals table with Prev/Next navigation, "Close Position" and "Force Reset" buttons.

### Execution (`modules/execution/`)

Trade-execution bot. Receives signals from a Grid bot, executes through the Broker, and sends feedback.

- **`SignalManager`** orchestrates each signal across phases: `phase1` (place at signal price), `phase2` (cancel-and-replace on price moves with cancel-confirm invariant), `phase3` (grace) — deadline derived from grid-supplied `source_timeout`.
- **`OrderManager`** is a thin proxy to broker capabilities only — no own ccxt client, no own queue.
- **`OrderUpdateQueue`**-consumer: execution polls `get_order_updates(owner_bot_id, since_id)` and routes by `clientOrderId`.
- **Breaker / PAUSED**: `auth`, `funds`, `invalid_order`, `network` counters → pause with cooldown and probe-based unpause. `-3` (panic) always executes.
- **Recovery & reconciler**: `open`/`partial`/`not_sent` recovered on start; orphaned rows reconciled on a 5 s loop; feedback retried with 24 h cutoff.
- **Precision-dust handling**: `close_dust_fraction` (in grid constants) vs `close_market_fraction` — small remainders collapsed by grid, medium remainders market-closed by execution, large remainders force-completed so grid can advance.
- Uses `get_latest_price` (price feed) for the trading anchor; **does not** consume `get_trades` (raw stream is not its concern).
- UI: orders chart (per-semantics shapes/colors), pause badge, account card, attention list with "Close" button.

### Module dependencies

`__manifest__.py` files declare:

| Module | `depends` |
|---|---|
| broker | `[]` (infrastructure — no consumer dependency) |
| collector | `["broker"]` |
| grid | `["collector"]` |
| execution | `["grid"]` (also uses broker at runtime) |

The manifests are advisory; the effective runtime dependency graph is:

```
broker  ←─ collector  ←─ grid  ←─ execution
   ↑                                    │
   └────────────────────────────────────┘
                (execution ↔ broker)
```

## ORM Layer and Declarative Models

The core includes a lightweight ORM:

- **Declarative models** — tables described as classes with field attributes (`Field` and subclasses: `Char`, `Text`, `Integer`, `Boolean`, `Float`, `Selection`, `Json`).
- **Automatic registry** — metaclass `ModelMeta` registers models in `MODEL_REGISTRY`.
- **Extension via `_inherit`** — child class adds fields to an existing model without creating a new table.
- **`self.env` environment** — each bot gets a `DBSQLite3` instance via `self.env`; model managers are accessed by string key: `self.env['levels.signals']`.
- **ORM methods** — `search`, `search_count`, `browse`, `read`, `create`, `write`, `unlink`, `filtered`, `mapped`, `sorted`.
- **Domains** — filtering in Polish notation (`&`, `|`, `!`), operators `=`, `!=`, `>`, `<`, `>=`, `<=`, `in`, `not in`, `like`, `ilike`, `between`.
- **Transactions and locking** — WAL mode, `threading.RLock`, `transaction()` context manager with nested savepoints.
- **Dynamic tables** — for tables whose name is only known at runtime (per-symbol candles, etc.), set `_dynamic = True` and use `env.get_model_manager('model.name', table_name)`.

### Model example

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
```

## Broker as the single exchange gateway

All exchange access in T.B.O.T goes through Broker. A consumer bot never instantiates a ccxt client; it declares a capability mapping and calls it via `ExchangeHandle`:

```python
await self.setup_exchange(broker_id, {
    "get_latest_price": ["latest_price", "current_price", "price"],
    "create_market_order": ["create_market_order"],
    "fetch_balance": ["fetch_balance"],
})
broker = self.get_exchange(broker_id)

price = await broker.get("get_latest_price",
                          exchange="binance", market_type="futures",
                          symbol="BTC/USDT")
```

Exchange names, ccxt market-type translation, client-order-id parameters, and per-exchange quirks all live in `modules/broker/lib/exchange_directory.py`. Rule: **never write `if exchange == 'mexc' and market_type == 'futures'` outside that file** — add an entry to `EXCHANGE_WORKAROUNDS` instead.

### Broker feed vs aggregation

Broker deliberately exposes **raw** market data at three levels of granularity:

| Capability | Data | Timestamps |
|---|---|---|
| `get_ohlcv` | Exchange-aggregated candles (REST) | `exchange_ns` (candle open time) |
| `get_latest_price` | Best bid/ask snapshot from WS | `ts_ns` (WS tick) |
| `get_trades` | Raw trades from WS | **Both** `exchange_ts_ns` and `tbot_utc_ns` |

Any component that needs **aggregated** data from raw trades (e.g. sub-minute candles) must do the aggregation itself. The Collector is the only reference consumer that does this (planned fast mode). Broker does not know about timeframe windows, does not know which consumer is asking, and does not cache anything but raw data.

The dual-clock contract is important for anyone writing a raw-trade consumer:
- `exchange_ts_ns` — market clock; drives TF-window bucketing.
- `tbot_utc_ns` — receipt clock; drives deque eviction (robust to exchange clock drift) and lets you measure WS lag.

## Time Synchronization Service

`core/time_sync.py` — adaptive NTP service. Thread-safe singleton, started once in `app.py` before any bot logic. Gives lock-free UTC to every module.

- **Architecture** — colony of autonomous filter instances. Each instance filters NTP samples against its own threshold and history, picks a favorite server, and evolves (reproduction, death, inheritance). A consensus layer aggregates their outputs into one correction with robust statistics and a post-gate.
- **PLL clock model** — holds `anchor_offset` and `rate` (drift). Published as an immutable `ClockSnapshot` with a dynamic TTL and a declared accuracy.
- **Public API** — `start()`, `stop()`, `wait_for_first_sync()`, `get_utc_ns()`, `get_utc_ns_with_precision()`, `get_utc_ns_monotonic()`, `get_clock_snapshot()`, `get_sync_telemetry()`.
- **Self-healing** — watchdog restarts dead sync threads, handles sleep/resume, colony-bias integrator compensates per-server constant bias.
- **Keep-awake** — on Windows disables Modern Standby and Power Throttling; on Linux verifies that systemd sleep targets are masked.
- **Full technical reference**: see `time_sync.md` (physics, constants, telemetry contract, Chrony comparison).

## Style and Graphics References

- **`core/colors.py`** — centralized palette. Changing a constant updates the color across the whole app.
- **`core/styles.py`** — standard font sizes, margins, borders, plus ready-made style dicts:
  - **Base:** `STYLE_BOTCARD`, `STYLE_BOTCARD_BODY`, `bot_card_header_style(status)`, `MARGIN_BETWEEN_BLOCKS`.
  - **Compact UI:** `FONT_SIZE_COMPACT`, `LABEL_STYLE`, `INPUT_STYLE` (only for `dcc.Input`), `DROPDOWN_STYLE` (no fixed height), `FIELD_ROW_STYLE`, **`STYLE_FORM`** (formerly referenced as `FORM_STYLE` in older notes), `HELP_STYLE`, `INFO_PANEL_STYLE`.
  - **Factories:** `input_field(...)`, `dropdown_field(...)` — compact form fields.
- **`core/graphics.py`** — atomic Plotly factories: `line_trace`, `hline`, `marker`, `candlestick_trace`, `volume_bar_trace`, `apply_layout(..., show_title, show_axis_titles)`. Plus UI helpers:
  - `info_panel(rows)` — compact `label: value` list (accepts both scalars and Dash Components).
  - `compact_table(data, columns, ...)` — compact `dash_table.DataTable` wrapper with `style_data_conditional`.
  - `db_fill_badge(stats)` — DB-fill indicator (orders / prices / stats — used by the execution card).
  - `normalize_dash_relayout(relayout)` — converts Dash flat relayout dict into Plotly nested form.

## Settings Database (`config.db`)

### Table `bots` (General Information)

| Field        | Type       | Description                                       |
|--------------|------------|---------------------------------------------------|
| `id`         | INTEGER PK | Unique bot identifier                             |
| `type`       | TEXT       | Bot type (`broker`, `collector`, `grid`, `execution`) |
| `name`       | TEXT       | Display name                                      |
| `status`     | TEXT       | `'running'` / `'stopped'` / `'paused'`            |
| `position`   | INTEGER    | Sorting order in the UI                           |
| `created_at` | TIMESTAMP  | Creation timestamp                                |

Per-bot configuration is not stored in a separate table; it lives as JSON key-value pairs in the bot's own database (`data/bot_<id>.db`, table `bot_settings`). This isolates data and simplifies deletion.

### Table `settings`

| Field   | Type    | Description       |
|---------|---------|-------------------|
| `key`   | TEXT PK | Setting key       |
| `value` | TEXT    | Value (often JSON)|

### Per-Bot Database (`data/bot_<id>.db`)

- `bot_settings` — key-value config + internal state.
- Additional tables created by each bot's own logic:
  - collector — per-symbol candles (`collector.candle`, dynamic tables);
  - grid — `levels_signals`, `deals_history`, `price_history`;
  - execution — `execution_orders`, `execution_prices`, `execution_stats`;
  - broker — `broker_keys` (created on demand).

Path to the DB is in the config under `data_db_path`.

**Internal state keys** (not user-facing, managed by the bot itself):

| Bot | Key | Purpose |
|---|---|---|
| collector | `_last_run_mode` | Last mode the bot actually ran (`'slow'`; `'fast'` reserved for planned fast mode). Used on restart to decide drop/aggregate. |
| collector | `_last_run_tf` | Last timeframe the bot actually ran with. Used on restart to decide drop/aggregate. |
| grid | `current_position_id` | Active position UUID. |
| grid | `max_averaging_count` | Current (possibly incremented by smart-averaging) cap. |
| execution | `execution_paused` | Breaker state. |
| execution | `balance_snapshot` | Latest account snapshot for the UI. |
| execution | `latency_stats` | Aggregated latency telemetry. |

## How to Add a New Bot Type

1. **Create a module folder** in `modules/`, e.g. `modules/trader/`.

2. **Add `__init__.py`** importing models and components:

       from . import models
       from . import components

3. **Define the bot class** in `models.py` via `@auto_reg`:

       from core import auto_reg, BaseBot
       from core.database import get_bot_config
       from core.logger import perf_logger

       @auto_reg
       class TraderBot(BaseBot):
           _name = "trader.bot"
           _inherit = "base.bot"

           def __init__(self, bot_id, manager=None):
               super().__init__(bot_id, manager)
               self.config = get_bot_config(bot_id)
               self.logger = perf_logger.get_logger(f"trader_{bot_id}", "app")
               self._resolve_exchange_provider()   # if it uses an exchange

           async def start(self): ...
           async def stop(self): ...
           def get_capabilities(self): return {}

   - `_name` must end with `.bot`.
   - `_inherit` is usually `"base.bot"`.
   - If the bot **consumes** data — pass `manager` to the parent.
   - If it **provides** data — implement `get_capabilities()`.
   - If it works with an exchange — call `_resolve_exchange_provider()` after `config`/`logger` and normalize `exchange_provider` in the config.

4. **Define type metadata** in `components.py`:

       from core import auto_reg
       from dash import dcc, html

       def trader_form(current_bot_id=None): ...
       def render_trader_block(bot_id, config, relayout_store): ...

       @auto_reg
       class TraderTypeMeta:
           _name = "trader.type"
           display_name = "Trading Bot"
           form_component = staticmethod(trader_form)
           bot_model = "trader.bot"
           render_block = staticmethod(render_trader_block)

   - `_name` ends with `.type`.
   - Optional hooks: `prepare_new_config`, `process_edit_save`, `register_callbacks`.

5. **Done.** The loader finds the module, the registry assembles the final class, the UI shows the type in the dropdown.

Real examples in this repository: `broker/broker.type`, `collector/collector.type`, `grid/grid.type`, `execution/execution.type`.

## Inter-Bot Data Exchange

Bots exchange data without direct access to each other's DB. The provider declares capabilities via `get_capabilities()`; the consumer describes the data it needs by keywords. The core wires them through `ExchangeHandle`.

### 1. Providing Data (Provider)

```python
def get_capabilities(self):
    return {
        "signal": {
            "keywords": ["grid_signal", "signal", "order_signal"],
            "getter": self._get_pending_signals,
            "setter": None,
        },
        "record_execution": {
            "keywords": ["grid_fill", "execution", "fill_order"],
            "getter": None,
            "setter": self._set_execution_result,
        },
    }
```

Getters/setters **must be async**.

### 2. Consuming Data (Consumer)

```python
async def start(self):
    await self.setup_exchange(source_bot_id, {
        "signal": ["grid_signal", "signal", "order_signal"],
        "timeout": ["execution_timeout"],
        "constants": ["signal_constants", "panic_constants"],
    })
    self.source_handle = self.get_exchange(source_bot_id)

    signals = await self.source_handle.get("signal")
    constants = await self.source_handle.get("constants")
```

### 3. Writing Data (Optional)

```python
await self.feedback_handle.set("record_execution", payload)
```

### 4. Lifecycle

On bot stop/removal, `BotManager` clears references to it from the `dynamics` of other bots automatically.

### 5. Real capability graph in the reference modules

| Consumer | Provider | Capability (keyword group) |
|---|---|---|
| collector | broker | `get_ohlcv` (`candles`, `ohlcv`), `get_symbols`, `get_markets`, `get_supported_market_types`, `get_broker_status` |
| collector (planned) | broker | `get_trades` (`trades`, `raw_trades`) — raw trade buffer for future fast mode; not consumed by any reference module yet |
| grid | collector | `candles` (`ohlcv`) |
| grid | analyst (optional) | `signal` (`close`, `direction`) |
| execution | grid | `signal`, `timeout`, `execution_reserve_sec`, `request_close_position`, `constants` |
| execution | broker | `get_latest_price`, `get_broker_status`, `get_market`, `get_markets`, `create_market_order`, `create_limit_order`, `cancel_order`, `fetch_order`, `fetch_open_orders`, `fetch_positions`, `fetch_balance`, `set_leverage`, `panic_close`, `get_order_updates`, `cleanup_order_updates`, `register_key_owner`, `unregister_key_owner` |
| execution → grid (feedback) | grid | `record_execution` (`grid_fill`, `execution`, `fill_order`) |

## Core Components

### `core/registry.py`
Singleton registry. `@auto_reg` registers classes; `_name` creates a new one, `_inherit` extends an existing one. The final class keeps the original name.

### `core/base_bot.py`
- `BaseBot` with `_name = "base.bot"`.
- Attributes: `bot_id`, `running`, `task`, `manager`, `dynamics`, `env`, `tbotex_provider_name`, `_provider_factory`.
- Abstract methods: `start()`, `stop()`, `get_capabilities()`.
- `setup_exchange()`, `get_exchange()` — inter-bot communication.
- `_resolve_exchange_provider()` — pick a native provider from config; falls back to ccxt with a warning.

### `core/exchange.py`
`ExchangeHandle` — holds getter/setter references, receives and provides data. Carries `owner_label` for diagnostics and propagates via `contextvars` so broker can attribute REST-client usage.

### `core/bot_manager.py`
- Owns running bots and their interaction.
- `_protected_bots` (broker) cannot be stopped or removed via the API.
- `restart_bot` / `force_restart_bot` are only allowed from trusted callers (`TRUSTED_RESTART_TYPES = ('collector.bot', 'execution.bot')`) and rate-limited (10 s per target).
- `request_exchange()` — creates/returns `ExchangeHandle` by keyword mapping.
- On bot removal — clears all references.

### `core/loader.py`
Scans `modules/` and imports packages, triggering class registration via side effects.

### `core/database.py`
- Initializes `config.db`, functions: `add_bot`, `get_all_bots`, `get_bot_config`, `update_bot_status`, `delete_bot`, `save_setting`, `get_setting`, `cleanup_orphan_databases`, `cleanup_orphan_bots`.
- ORM: fields, metaclass, model managers, `DBSQLite3` environment, domains, transactions.
- `_save_config_to_local_db()` — updates only the given keys (preserves `current_position_id` and other internal state).
- Migration of `feedback_state` in `execution_orders` (`abandoned` → `delivery_failed`) — on first start, versioned via `PRAGMA user_version`.
- `timesync_config.db` — persistent drift history for the time service (separate DB, own lock).

### `core/logger.py`
- `PerformanceLogger` — singleton with per-module levels (`app`, `time`, `collector`, `fetcher`, `database`, `analytics`, `execution`).
- Asynchronous logging via `QueueHandler` + `QueueListener` + `Watchdog`, with **drop-oldest** on queue-full (never blocks producers).
- Mood queue and `get_pending_mood()` for UI indication.
- `get_recent_logs()` — tail-read of log files, session-aware (drops previous-session entries).
- `set_clock()` — replaceable time source, typically bound to `time_sync_service.get_utc_ns`.

### `core/time_sync.py`
- `TimeSyncService` — singleton NTP service.
- Filter colony + consensus, PLL clock model, colony-bias integrator.
- Lock-free `get_utc_ns()`, `get_utc_ns_monotonic()`, `get_clock_snapshot()`, `get_sync_telemetry()`.
- Watchdog, keep-awake, graceful start/stop.

### `core/tbotex/`
- Registry and loader for native providers.
- `ExchangeClientProtocol` — narrow interface.
- `ui_helpers` — dropdown for `exchange_provider` in forms.

### `core/colors.py` / `core/styles.py` / `core/graphics.py`
- Centralized palette, style dicts (including compact UI), Plotly factories and UI helpers.

### `app.py`
- Universal Dash UI.
- Sticky panel: ➕ (add bot), ⚙️ (settings), 📋 (logs), ⏱ (time telemetry).
- Header moods via Store + callbacks.
- Logs panel updates each second when open; time panel short → medium → full.
- Discovers bot types dynamically and renders their cards.
- Loads native providers (`load_tbotex_providers()`) after `load_modules("modules")`.
- Runs the `feedback_state` migration **before** `BotManager.load_bots()`.

## Config Conventions

Each bot stores its config in `bot_settings` inside its local DB. Recommended fields when working with an exchange:

| Key                 | Description                                                        |
|---------------------|--------------------------------------------------------------------|
| `exchange`          | ccxt id (`binance`, `bybit`, `kucoin`, `mexc`, `okx`, ...)         |
| `market_type`       | `spot` / `futures`                                                 |
| `exchange_provider` | `ccxt` (default) / `tbotex` — normalized on save                   |
| `data_db_path`      | `data/bot_<id>.db` (set by `add_bot`)                              |
| `bot_type`          | Set by the system (`broker`/`collector`/`grid`/`execution`)        |

Broker-specific:

| Key                | Description                                                       |
|--------------------|-------------------------------------------------------------------|
| `default_provider` | `ccxt` (native providers reserved for a future release)          |
| `use_monotonic_nonce` | Use `get_utc_ns_monotonic()` for ccxt nonces                  |

Collector-specific:

| Key                | Description                                                       |
|--------------------|-------------------------------------------------------------------|
| `collector_table`  | Optional override of the per-symbol dynamic table name           |
| `timeframe`        | `1m` / `5m` / `15m` / `30m` / `1h` / `2h` / `4h` / `1d` (slow mode, current) |
| `candles_limit`    | Retention of the per-symbol table                                 |
| `_last_run_mode`   | Internal. Set on every successful `start()`. Drives table disposition on restart. |
| `_last_run_tf`     | Internal. Set on every successful `start()`. Drives table disposition on restart. |

**Planned for the fast mode** (not active in the current version): sub-minute timeframes (`1s`, `5s`, `15s`, `30s`) served from broker's `get_trades`; the same `bot_settings` keys `_last_run_mode` / `_last_run_tf` will govern fast↔slow and intra-fast table transitions.

Grid-specific:

| Key                        | Description                                                       |
|----------------------------|-------------------------------------------------------------------|
| `collector_bot_id`         | source of candles                                                 |
| `analyst_bot_id`           | optional entry/exit gate                                          |
| `leverage`                 | used in PnL computations                                          |
| `averaging_strategy`       | `equal` / `sum_prev` / `double` / `triple`                        |
| `max_averaging_count_initial` | reset value after a position close                             |
| `skipped_position_ids`     | JSON array of positions that need manual review                   |

Execution-specific:

| Key                     | Description                                                          |
|-------------------------|----------------------------------------------------------------------|
| `source_bot_id`         | grid bot providing signals                                           |
| `feedback_bot_id`       | optional feedback target (defaults to source)                        |
| `broker_key_id`         | key stored in Broker's vault (required)                              |
| `unit_volume` / `unit_volume_mode` | base or quote sizing per signal unit                        |
| `poll_divisor`          | signal polls per source TTL (`poll_interval = source_timeout / divisor`) |
| `stale_price_sec`       | freshness threshold for best-price feed                              |
| `breaker_*`             | thresholds for `invalid_order`, `insufficient_funds`, `network`      |

Immutable after creation: `exchange`, `market_type`, `symbol` (grid/execution), `source_bot_id` (execution).

## Environment Variables

| Variable                     | Values        | Effect                                                                 |
|------------------------------|---------------|------------------------------------------------------------------------|
| `TIME_SYNC_KEEP_AWAKE`       | `0` / `1`     | Disable/enable keep-awake (Windows/Modern Standby, Linux mask check). |
| `TIME_SYNC_VALIDATE_ORIGIN`  | `0` / `1`     | Disable origin echo check in NTP packets.                              |
| `TBOT_WS_DIAG`               | `1`           | WebSocket diagnostics (iter logs, listenKey, REST heartbeat, versions). |
| `TBOT_WS_VERBOSE`            | `1`           | ccxt `verbose=True` + per-tick order logs.                             |
| `TBOT_KEY_PASSWORD`          | string        | Password used by Broker's `KeyVault` to unlock encrypted API keys.     |
| `TBOT_LOG_QUEUE_SIZE`        | int           | Log-queue size per module (default `10000`).                           |
| `TBOT_LOG_DROP_ALERT`        | int           | Drop-alert threshold per watchdog tick (default `100`).                |
| `TBOT_DB_STRICT_BATCH`       | `0` / `1`     | Fail hard on suspicious `executemany`/`lastrowid` in the ORM.          |

Changing env vars requires a process restart.

## Installation and Running

### Using Conda (recommended)

    conda create -n tbot python=3.10
    conda activate tbot
    pip install -r requirements.txt

`requirements.txt` currently lists:

```
dash[async]>=2.0.0
plotly>=5.0.0
pandas>=1.3.0
ccxt>=4.0.0
aiohttp>=3.8.0
ntplib>=0.4.0
pytest>=9.1.1
```

**Missing optional but required dependencies:**

- `cryptography` — needed by `modules/broker/lib/crypto.py` (Fernet/PBKDF2).
- `ccxt` ships `.pro` in recent versions; if your build doesn't, install `ccxt` explicitly and ensure `import ccxt.pro` works (used by `modules/broker/lib/broker_service.py`).

If you use encrypted keys, add:

    pip install cryptography

### Running

    python app.py

The application is available at `http://127.0.0.1:8050`.

## Configuration

- ⚙️ — settings panel: debug mode, log levels per module, log retention.
- ➕ — add a bot; pick a type from the dropdown and fill the form.
- 📋 — recent log records.
- ⏱ — time-sync telemetry (short → medium → full → close).
- Header T.B.O.T collapses/expands on click.
- Bot cards collapse; header color reflects status (`running` — green, `paused` — yellow, `stopped` — red).

## Extending Existing Bots

Create a module with a class under `@auto_reg` and `_inherit = "bot_name.bot"`:

```python
@auto_reg
class CollectorLoggerExtension:
    _inherit = "collector.bot"

    async def start(self):
        print(f"[LOG] Starting collector {self.bot_id}")
        await super().start()
        print(f"[LOG] Collector {self.bot_id} started")
```

The registry merges classes and preserves the original name. `super()` continues to work correctly.

## Requirements

```
dash[async]
plotly
pandas
ccxt
aiohttp
cryptography
```

- `ntplib` is listed in `requirements.txt` for historical reasons; the NTP client in `core/time_sync.py` is implemented with raw `socket` and `struct` and does not import `ntplib`.
- `ccxt.pro` is used by `modules/broker/lib/broker_service.py` and ships as part of `ccxt` in recent releases.

## ⚠️ Disclaimer (repeat)

**This is a demonstration / educational version, not a finished product.**

**Trading cryptocurrencies and other digital assets involves significant risk and may result in the loss of your entire invested capital.** This software is provided "as is", without warranty of any kind, for educational and research purposes only. The author assumes no responsibility or liability for any financial losses or damages incurred through the use of this software. **Test on demo/testnet environments and with minimal capital first. Use at your own risk.**

---

## Support the Project ☕

If this project has saved you time or helped in your work, you can support its development using cryptocurrency:

| Network / Token | Wallet Address |
| :--- | :--- |
| **💚 USDT (TRC-20)** | TCnh86qJvsmRdkKajafcFqzbvY2Eiqw4UE |
| **🔷 EVM (ETH / BSC / Polygon)** | 0x650C779ABf16e2D697957E73f53F781cdBA49Ecc |
| **₿ BTC** | bc1qwekqfc0epnmfkm4n4sfd3s6d4ku69pw5xphtg0 |

*Please double-check the network before sending funds.*

## Author

Created and maintained by [sergson](https://github.com/sergson)

## License

GNU General Public License v3.0