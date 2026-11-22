# Grid Bot — Technical Manual

> ## ⚠️ DEMONSTRATION / EDUCATIONAL VERSION
>
> **This is a demonstration and educational version, not a finished product.**
>
> - The module is provided to illustrate the T.B.O.T architecture, ORM patterns, and UI conventions — not as a production-ready component.
> - Behavior, configs, and file layout may change without notice.
> - You are responsible for your own security review, testing, and compliance if you build on this code.

> ## 💰 RISK DISCLAIMER
>
> **Trading cryptocurrencies involves significant risk and may result in the loss of your entire invested capital.**
>
> - This software is provided **for educational and research purposes only**, "as is", without warranty of any kind.
> - The author assumes **no responsibility or liability** for any financial loss, missed profit, or other damage incurred through the use of this software.
> - **Never trade with funds you cannot afford to lose.** Test on demo/testnet environments with minimal capital first.

---

## Overview

**Grid Bot** — a configurable averaging bot that builds a price grid, generates averaging signals, and manages position closure according to configurable PnL thresholds.

Part of the **T.B.O.T** framework. It can run **standalone** (with built-in fill emulation) or together with a separate **Execution** bot.

Fully ported to the `core.database` ORM layer. No raw SQL in the bot logic; all DB operations go through declarative models and the `self.env` environment.

**Key features:**
- Feedback isolation per position (`position_id` + `signal_timestamp`).
- Sanity check of position accounting when transferring to history.
- Dynamic effective-volume computation with a 1 s cache.
- UI indicator for `expected vs current position volume` mismatch.
- Single-bot (emulated execution) and two-bot (real execution) modes.
- Consistent UI styling (`ui_compact`, `info_panel`, `compact_table`).
- Optional analyst signal gating before close.
- Entry-timeout protection: rebuild streak + auto-pause.

---

## Role in the system

```
┌──────────────┐    candles (OHLCV)     ┌──────────────┐
│  Collector   │ ──────────────────────►│  Grid Bot    │
│              │   capability "candles" │ (this module)│
└──────────────┘                        └──────┬───────┘
                                               │
                      signal (0..N, -1, -2, -3)│
                                               ▼
                                        ┌──────────────┐
                                        │  Execution   │
                                        │  Bot         │
                                        └──────┬───────┘
                                               │
                                        record_execution
                                               │
                                               ▼
                                         (back to Grid)
```

- **Consumer** of collector's candle data (capability `ohlcv_data`, keyword `"candles"`).
- **Provider** of execution signals (capability `signal`) and **consumer** of execution feedback (capability `record_execution`).
- **Optional** consumer of an analyst bot's `signal` (capability keywords `"close"`, `"direction"`). Currently `_get_analyst_signal()` is a **stub returning `None`** — the `use_analyst_close=1` branch will wait forever; do not enable it until the analyst capability is implemented.

**Dependency in `__manifest__.py`:** `"depends": ["collector"]`.

---

## How the Grid Bot Works

`GridBot` is defined in `modules/grid/models.py`, inherits from `BaseBot`, and implements `start()` / `stop()` plus the entire averaging logic.

### Two operating modes

Controlled by `execution_timeout_sec`:

| Mode | `execution_timeout_sec` | Behavior |
|---|---|---|
| **Single-bot** | `0` | Grid emulates execution locally (`_emulate_execution_for_signal`). No external execution bot needed. |
| **Two-bot** | `> 0` | Grid creates plan rows (`signal_flag=1`) and waits for an Execution bot to send feedback via `record_execution`. Grid enforces signal timeouts and recalc strategies. |

In both modes the plan lives in `levels_signals`; the difference is whether `real_flag=1` rows appear immediately (emulation) or via feedback.

### Core loop (`_run`)

Iterates every `poll_interval_sec`:

1. **Config-dirty check** — if `self.config_dirty` is set, calls `on_config_updated()` (which recalculates levels).
2. **Collector health check** — `_get_cached_collector_status()` (2 s TTL). If the collector is not `running`/`paused` — skips the loop iteration (with a throttled warning once per 5 min). If `unknown` — proceeds with caution.
3. **`_update_price_history(limit=2000)`** — incremental sync from the collector (see below).
4. **`_check_grid_levels()`** — signal generation (entry / averaging / close / liquidation / timeouts).
5. **If `self.running` became `False`** (parked by entry-cannot-execute) — breaks out.
6. **`_check_smart_averaging()`** — smart reverse signals.
7. **`_check_smart_executions()`** — post-fill processing for smart signals.
8. **Force-reset timeout check** — if `_force_reset_pending` and deadline passed, resets locally with an error log.
9. **`_need_new_position` handling** — if set:
   - If any `real_flag=1` rows exist → `_close_position()` (move to history, new position_id).
   - Then `_rebuild_levels(direction, current_price, current_ts)`.

### Price history sync (`_update_price_history`)

Incremental with a persisted anchor:

- First call after start/restart:
  - Reads `MAX(timestamp)` from `price_history` → `self._last_processed_ts`.
  - Fetches `limit` candles (2000 by default).
  - Writes all candles with `timestamp >= _last_processed_ts` (refreshes the forming candle).
  - Prunes to `limit` rows when `count > limit * 1.1`.
  - Sets `_initial_price_load_done = True`.
- Subsequent calls:
  - Fetches only **5** candles.
  - Uses `>=` (not `>`) — the current forming candle may have an unchanged open-timestamp but updated OHLCV.
  - Refreshes existing rows, inserts new ones.
  - Prunes if needed.

`self._price_history` keeps the last two candles in memory for linear extrapolation (predictive signals).

### Signal generation (`_check_grid_levels`)

Uses `current_price = self._price_history[-1][1]` (last known close).

1. **Entry check** — if there is no entry (`signal_number=0`), sets `_need_new_position = True`.
2. **Last executed** — `_get_last_executed()` returns the latest row with `signal_number >= 0, real_flag=1, real_position_volume > 0`. If none:
   - If entry exists but is not real and `age > execution_timeout_sec` — triggers `_handle_entry_timeout_rebuild` (streak + pause).
   - Otherwise waits for the executor.
3. **Effective avg/vol** via `_get_effective_avg_vol(last_exec)` (with 1 s cache).
4. **PnL** = `sign * (current_price / effective_avg - 1) * lev * 100`.
5. **Liquidation**: if `pnl <= -liquidation_pnl` and no pending `-1` — creates a `-1` signal (order type from `liquidation_order_type`, default `market`). In single-bot mode — closes immediately.
6. **Close**: `pnl >= breakeven_pnl` (optionally gated by analyst), `pnl >= close_pnl` — creates `-2` (order type `limit`). Guard: if any `-2` row is already `real_flag=1`, skip (grid has already been reset by `_apply_feedback_to_record`).
7. **Averaging levels** — for each unexecuted level with `signal_number >= 1`, `real_flag=0`, `signal_flag=0`, `signal_retry < MAX_SIGNAL_RETRIES`:
   - **Immediate cross** — `current_price` already crossed the level → `_create_signal` with `signal_timestamp=current_ts`.
   - **Predictive** — linear extrapolation of the last two candles over `execution_reserve_sec`. If the level will be reached within `reserve_sec` — creates the signal with `signal_timestamp = now + time_to_cross`.
8. **Timeout of pending signals** — if `execution_timeout_sec > 0`:
   - `signal_retry >= MAX_SIGNAL_RETRIES` → skip (never recreate).
   - Negative signal (`-1`, `-2`) — refreshed at current price (`recalc_strategy='recalc_grid'`) or switched to `market` with a fresh timestamp (`recalc_strategy='market_order'`).
   - Entry `0` timeout — resets position and rebuilds levels (`_handle_entry_timeout_rebuild`) or switches to market.
   - Averaging `1..N` timeout — `_recalc_levels_from(n)` or switches to market.

### Signal creation (`_create_signal`)

Creates or updates a plan row. Returns `bool`:

- `True` — row created/updated.
- `False` — refused. Callers log a debug message and return. For negative signals that have already been executed, `_need_new_position = True`.

Refusal reasons:

- `price is None`.
- `signal_retry >= MAX_SIGNAL_RETRIES` (retry exhausted).
- Row already has `real_flag=1` for `signal_number < 0` (close/liq already executed — grid will reset).

`signal_timestamp` is preserved for pending rows (`signal_flag=1, real_flag=0`), incremented `signal_retry`, and updated for new rows.

### Entry-timeout streak (`_handle_entry_timeout_rebuild`)

- Increments `self._reset_no_fill_streak`.
- If `_reset_no_fill_streak > MAX_ENTRY_RESET_STREAK` (default 5) — parks the grid:
  - `config['paused_reason'] = 'entry_cannot_execute'`, `paused_since_ns`.
  - `update_bot_status('paused')`, `self.running = False`.
  - Returns `False`.
- Else: recalculates direction from the freshest two candles, calls `_close_position()`, `_rebuild_levels(direction, price, candle_ts)`.
- `_reset_no_fill_streak` is reset to 0 on manual `start()`.

### Feedback handling (`_set_execution_result`)

The order of checks matters:

1. **Scale check** — `real_timestamp_scale` must be `'tbot_utc_ns'`; otherwise the feedback is dropped with an error log.
2. **Position-id filter** — if `data['position_id']` doesn't match `self._position_id`, WARNING + ignore.
3. **Panic `-3` branch** — only applied if `_force_reset_pending` is set; otherwise treated as a late feedback and ignored.
4. **Strict search** — by `(position_id, signal_number, signal_timestamp)`.
5. **Already-real handling**:
   - Entry/averaging (`signal_number >= 0`) — ignored (idempotent).
   - Close/liq — processed as long as remaining volume > `plan_vol * close_dust_fraction`. 0-fill retry (state `canceled`/`rejected`/... with `real_volume=0`) — **state is not modified** (protects the `any_close-guard`).
6. **Loose search** — by `(position_id, signal_number, real_flag=0)`. If found — adopts the executor's timestamp, WARNING about the mismatch.
7. **History fallback** — row already in `deals_history`; apply feedback there.
8. **Zero-fill branches**:
   - `signal_number < 0` with `real_volume=0`:
     - Smart reverse → mark as `real_flag=1, real_vol_rate=0` (no position reset).
     - Terminal state (`canceled`, `rejected`, `expired`, `not_found`, `not_sent`) → `signal_flag=0, signal_retry+1` (position kept).
     - State `filled` → reset (if the record belongs to the current position).
     - Unknown/None → safe default: keep position, increment retry.
   - `signal_number >= 0` with `real_volume=0`:
     - Terminal reject reasons (`stale_signal`, `superseded`, `recovery_no_ref`) → set `signal_retry=MAX_SIGNAL_RETRIES`, no retry. Entry (0) → local reset; averaging on a live position → `force_reset_position()` (two-bot) or `_close_position()` (single-bot).
     - Retryable → increment; on reaching `MAX_SIGNAL_RETRIES` — give up (entry → reset).
9. **Normal path** — `_apply_feedback_to_record`.

### Effective avg/vol (`_get_effective_avg_vol`)

- `avg` — `row.real_avg_entry_price` (fallback `row.avg_entry_price`).
- `vol` — computed dynamically: `Σ real_vol_rate` for `signal_number >= 0` minus `Σ real_vol_rate` for `signal_number < 0`, clamped at 0.
- **Dust-collapse:** if `vol < total_in * close_dust_fraction` — treated as flat (protects against accumulated rounding artifact in legacy data).
- Cached in `self._vol_cache[self._position_id] = (now_ns, vol)` with 1 s TTL. Invalidated on every fill and on `_close_position`.

### PnL split semantics (`_apply_feedback_to_record`)

- `signal_number < 0` (close/liq/panic) → `real_pnl` computed from the **old** `old_avg` (realized PnL of the closed part).
- `signal_number >= 0` (entry/averaging) → `real_pnl` computed from the **new** `new_avg` (current PnL of the position).

`_apply_trade_to_position` — on decrease the entry avg is **not** changed (this is the cost basis of the remaining part).

### Sanity on close (`_close_position`)

Before transferring rows to history, each entry/averaging row with `real_flag=1` is sanity-checked:

- **Primary** — `real_avg_entry_price` deviates from plan-`avg_entry_price` by > 50% → anomaly.
- **Fallback** (plan-avg missing) — `real_avg` is more than 10× away from the current price → anomaly.
- On anomaly: `_recompute_weighted_avg(records)` is called. If valid (`cur*0.1 <= weighted_avg <= cur*10` for positive `cur`) — written into `real_avg_entry_price`.
- If the recompute is not valid — the row is **not** transferred, stays in `levels_signals`, and its `position_id` is appended to `bot_settings.skipped_position_ids` (JSON array) for manual review.
- `_close_in_progress` guard prevents re-entrant calls.

`_ensure_clean_levels_table` is called on start: any `position_id` different from the current one is moved to history. If exactly one orphan remains — a new `position_id` is generated.

### Smart averaging

- `_check_smart_averaging` — for each of the last `smart_averaging_count` executed rows (`signal_number >= max_count - smart_count + 1`), if the price moved back by `averaging_threshold_pnl / 2` in the reverse direction, a smart reverse signal is created with `signal_number <= min(existing negative) - 1` but `>= PANIC_SIGNAL_NUMBER + 1` (collision check).
- `_check_smart_executions` — when a smart signal fills with `real_vol_rate > 0`, the parent is marked `signal_smart_reversed=-2`, `max_averaging_count` is incremented (up to the hard cap), and `_update_levels()` is called.
- `_reset_lost_smart_parents` / `_maybe_reset_lost_smart_parent` — parents whose smart children were all cancelled or failed are reset to `signal_smart_reversed=0`.

### Capabilities provided

```python
def get_capabilities(self):
    return {
        "signal":            # pending signals: signal_flag=1, real_flag=0
            {"keywords": ["grid_signal", "signal", "order_signal"], ...},
        "record_execution":  # executor feedback
            {"keywords": ["grid_fill", "execution", "fill_order"], ...},
        "get_position_id":   # current position_id
            {"keywords": ["position_id"], ...},
        "get_signals_by_position":  # active + history rows for a position
            {"keywords": ["position_signals"], ...},
        "timeout":           # execution_timeout_sec
            {"keywords": ["execution_timeout"], ...},
        "execution_reserve_sec":  # look-ahead in seconds
            {"keywords": ["execution_reserve", "signal_lookahead"], ...},
        "request_close_position":  # setter: external close request
            {"keywords": ["request_close_position", "grid_close", "reset_position"], ...},
        "constants":         # panic_signal_number, smart_reverse_base, close_market_fraction
            {"keywords": ["signal_constants", "panic_constants"], ...},
    }
```

`_get_constants()` currently returns:

```python
{
    "panic_signal_number": PANIC_SIGNAL_NUMBER,   # -999_999
    "smart_reverse_base":  SMART_REVERSE_BASE,    # -100
    "close_market_fraction": 0.15,
}
```

`close_market_fraction` is a separate knob from `close_dust_fraction`:

| Range of remaining | Behavior |
|---|---|
| `≤ close_dust_fraction × plan_vol` (grid, default 1%) | Treated as 0 — position reset |
| `≤ close_market_fraction × plan_vol` (execution, 15%) | Market-closed by execution |
| `> close_market_fraction × plan_vol` | `force-complete` feedback; residual absorbed at the next signal 0 |

### Public methods for external callers

- **`request_close_position()`** — if no real execution exists (`_get_last_executed()` returns `None`) or the price is unavailable — resets the plan locally. Otherwise creates a `-2` close signal and, in single-bot mode, closes immediately.
- **`force_reset_position()`** — panic reset.
  - **Single-bot mode** (`execution_timeout_sec == 0`) — immediate local reset.
  - **Two-bot mode** — creates `-3` panic signal, sets `_force_reset_pending=True` and `_force_reset_deadline_ns = now + FORCE_RESET_TIMEOUT_NS (30 s)`. When feedback arrives — resets. When the deadline passes — resets locally as-is with an error log.

---

## Database Structure

### Per-bot database (`data/bot_<id>.db`)

#### `levels_signals` — active position grid

| Column                   | Type       | Notes |
|--------------------------|------------|-------|
| `id`                     | INTEGER PK | Auto-increment |
| `position_id`            | TEXT       | UUID of the current position, indexed |
| `signal_number`          | INTEGER    | `0` entry, `1..N` averaging, `-1` liquidation, `-2` close, `-3` panic (execution-side), `< SMART_REVERSE_BASE` smart reverse |
| `current_price`          | REAL       | Market price at signal creation / update |
| `current_timestamp`      | INTEGER    | Exchange candle timestamp |
| `tbot_utc_stamp_ns`      | INTEGER    | TBOT UTC of the row write, indexed |
| `signal_volume_rates`    | JSON       | Progressive volume list, e.g. `[1.0, 2.0, 4.0]` |
| `signal_direction`       | SELECT     | `buy` / `sell` / `close` / `liquidation` |
| `signal_vol_rate`        | REAL       | Relative volume of this level |
| `signal_order_type`      | SELECT     | `limit` / `market` |
| `signal_price`           | REAL       | Calculated level price |
| `signal_liquidation`     | BOOL       | `1` — liquidation row |
| `signal_close`           | BOOL       | `1` — close row |
| `signal_position_volume` | REAL       | Cumulative volume **after** this level |
| `avg_entry_price`        | REAL       | Planned average entry price after this level |
| `position_cost`          | REAL       | Planned total position cost after this level |
| `signal_pnl`             | REAL       | Planned PnL% (leveraged) after this level |
| `signal_flag`            | BOOL       | `1` — signal emitted / pending execution |
| `signal_timestamp`       | INTEGER    | TBOT UTC of the semantic moment (may be in the future) |
| `signal_retry`           | INTEGER    | Retry counter (0..`MAX_SIGNAL_RETRIES`) |
| `signal_smart`           | BOOL       | `1` — smart reverse signal |
| `signal_smart_reversed`  | INTEGER    | Smart bookkeeping: `-1` (parent waiting), `-2` (processed) |
| `real_timestamp`         | INTEGER    | TBOT UTC of actual execution |
| `real_vol_rate`          | REAL       | Actual executed volume |
| `real_price`             | REAL       | Actual fill price |
| `real_pnl`               | REAL       | Realized/current PnL% (split semantics) |
| `real_position_volume`   | REAL       | Actual cumulative volume after fill |
| `real_flag`              | BOOL       | `1` — executed |
| `real_avg_entry_price`   | REAL       | Actual average entry price after fill |
| `created_at`             | TEXT       | String default `"CURRENT_TIMESTAMP"` (does not auto-update) |

#### `deals_history` — closed positions

Same fields as `levels_signals` plus:

- `history_id` — INTEGER PK (auto-increment).
- `moved_to_history_at` — INTEGER (TBOT UTC of transfer).

Rows are moved here by `_close_position`. Rows flagged as anomalous (sanity) stay in `levels_signals` and their `position_id` goes to `bot_settings.skipped_position_ids`.

**No automatic retention** is applied to `deals_history` in the current code — it grows with every closed position.

#### `price_history` — market prices

| Column              | Type       | Notes |
|---------------------|------------|-------|
| `timestamp`         | INTEGER PK | Exchange candle open-time (ns) |
| `open`              | REAL       | |
| `high`              | REAL       | |
| `low`               | REAL       | |
| `close`             | REAL       | |
| `volume`            | REAL       | |
| `tbot_utc_stamp_ns` | INTEGER    | TBOT UTC of write, indexed |

Filled by `_update_price_history` from the collector.

#### `bot_settings` — configuration and internal state

Key-value store (values JSON-encoded).

| Key                          | Value |
|------------------------------|-------|
| `current_position_id`        | UUID of the currently active position |
| `collector_bot_id`           | Linked collector bot |
| `analyst_bot_id`             | Optional analyst bot |
| `use_analyst_close`          | `1` / `0` |
| `leverage`                   | Leverage for PnL |
| `close_pnl`                  | Target PnL% |
| `exchange_provider`          | `'ccxt'` (default) / `'tbotex'` |
| `max_averaging_count`        | Current max averaging count |
| `max_averaging_count_initial`| Initial value (used to reset on close) |
| `skipped_position_ids`       | JSON array of positions whose average could not be recovered by sanity |
| `paused_reason`              | e.g. `'entry_cannot_execute'` (set by the entry-timeout streak) |
| `paused_since_ns`            | TBOT UTC of pause |
| `data_db_path`               | Path to the local DB |

All other parameters (see the Configuration section) live in the same table.

### Global `config.db`

Standard `bots` row (`type='grid'`). Nothing type-specific.

---

## Configuration

Defaults below are what the **UI form** (`form_component`) shows for a new bot. Programmatic `add_bot()` with missing keys uses `prepare_config_for_save` defaults, which differ slightly for `averaging_threshold_pnl` (20.0 vs 70 in the form) and `max_averaging_count` (no explicit default → `_truncate_max_count` falls back to `1`).

| Parameter                     | Type    | UI default     | Description |
|-------------------------------|---------|----------------|-------------|
| `collector_bot_id`            | INTEGER | *required*     | OHLCV source |
| `collector_table`             | TEXT    | `None` (auto)  | Optional table-name override in the collector DB |
| `analyst_bot_id`              | INTEGER | `None`         | Optional entry/exit gate (currently a stub) |
| `use_analyst_close`           | INTEGER | `0`            | Wait for the analyst before close |
| `exchange_provider`           | TEXT    | `"ccxt"`       | `"ccxt"` or `"tbotex"` |
| `leverage`                    | INTEGER | `10`           | PnL multiplier |
| `averaging_strategy`          | TEXT    | `"sum_prev"`   | `"equal"`, `"sum_prev"`, `"double"`, `"triple"` |
| `averaging_threshold_pnl`     | REAL    | `70` (form)    | PnL%-drawdown from current avg that arms the next averaging level |
| `max_averaging_count`         | INTEGER | `3` (form)     | Number of averaging levels |
| `smart_averaging_count`       | INTEGER | `2`            | Number of tail levels monitored for smart reverse |
| `breakeven_pnl`               | REAL    | `4.0`          | PnL% at which the stop-loss stops moving into loss |
| `close_pnl`                   | REAL    | `50.0`         | Target PnL% for a full close |
| `liquidation_pnl`             | REAL    | `90`           | PnL% for a forced liquidation |
| `liquidation_order_type`      | TEXT    | `"market"`     | `"market"` or `"limit"` |
| `close_dust_fraction`         | REAL    | `0.02`         | Precision-dust threshold (fraction of `plan_vol`); clamped to `[0.001, 0.1]` |
| `poll_interval_sec`           | INTEGER | `30`           | Main-loop period |
| `execution_timeout_sec`       | INTEGER | `0`            | Max wait for the executor; `0` → single-bot emulation |
| `recalc_strategy`             | TEXT    | `"recalc_grid"`| On timeout: `"recalc_grid"`, `"market_order"`, `"none"` |
| `execution_reserve_sec`       | INTEGER | `derived`      | Look-ahead in seconds; derived from `execution_reserve_ratio` in the form |
| `execution_reserve_ratio`     | REAL    | `1/3` (form)   | UI-only: `execution_reserve_sec = execution_timeout_sec * ratio` |
| `history_days`                | INTEGER | `7`            | History-chart window, `[1, 365]` |
| `history_positions_limit`     | INTEGER | `100`          | Max positions shown in the deals table, `[10, 1000]` |
| `max_averaging_count_initial` | INTEGER | `= max_averaging_count` | Reset value on close (internal) |

### Normalization (`prepare_config_for_save`)

- `use_analyst_close` — list → `int` (`1` if `1` in list, else `0`).
- Numeric fields coerced to `int` when not `None`: `leverage`, `max_averaging_count`, `smart_averaging_count`, `poll_interval_sec`, `execution_timeout_sec`, `execution_reserve_sec`, `history_days`, `history_positions_limit`.
- `smart_averaging_count ≤ max_averaging_count`.
- Float fields with defaults: `averaging_threshold_pnl=20.0`, `breakeven_pnl=4.0`, `close_pnl=50.0`, `liquidation_pnl=90.0`.
- `history_days` clamped to `[1, 365]`, default `7`.
- `history_positions_limit` clamped to `[10, 1000]`, default `100`.
- `execution_reserve_ratio` → `execution_reserve_sec = int(execution_timeout_sec * ratio)`; removed from the saved config.
- `exchange_provider` normalized to `'ccxt'` | `'tbotex'`.
- `close_dust_fraction` clamped to `[0.001, 0.1]`.
- `bot_type` is dropped.

### Hard caps (`_truncate_max_count`)

- `MAX_AVERAGING_HARD_CAP = 500` — `max_averaging_count_initial` and `smart_averaging_count` are clamped to this.
- Negative `smart_averaging_count` → `0`.
- Called in `__init__` and in `on_config_updated`.

### Immutable fields

After creation, the form disables: `collector_bot_id`, `analyst_bot_id`.

`symbol`, `exchange`, `market_type` are **not** grid fields — they belong to the collector's config. Grid only follows `collector_bot_id`.

### Runtime config update

`on_config_updated()`:

1. Reads the new config.
2. If `exchange_provider` changed — re-resolves `_provider_factory`.
3. Calls `_truncate_max_count()`.
4. Falls back `max_averaging_count` to `max_averaging_count_initial` if missing.
5. Persists back to `bot_settings`.
6. Calls `_update_levels()` to recalculate pending averaging rows.

---

## Inter-Bot Data Exchange

### Consuming candles (Grid → Collector)

```python
await self.setup_exchange(collector_id, {"candles": ["candles", "ohlcv"]})
self._collector_handle = self.get_exchange(collector_id)

candles = await self._collector_handle.get("candles", limit=limit)
```

Collector returns candles in DESC order; grid relies on `candles[0]` being the freshest.

### Emitting signals (Grid → Execution)

The executor calls `Grid._get_pending_signals()` (via capability `signal`). Rows returned satisfy `signal_flag=1 AND real_flag=0`; each row is augmented with:

- `signal_timestamp_scale = 'tbot_utc_ns'`
- `current_timestamp_scale = 'exchange_ns'`

### Receiving feedback (Execution → Grid)

Executor calls `Grid._set_execution_result(data)` with:

```python
{
    'position_id': str,
    'signal_number': int,
    'signal_timestamp': int,
    'signal_timestamp_scale': 'tbot_utc_ns',
    'real_price': float,
    'real_volume': float,
    'real_timestamp': int,
    'real_timestamp_scale': 'tbot_utc_ns',
    'exchange_state': str,      # 'filled' | 'partial' | 'canceled' | 'rejected' | 'expired' | 'not_found' | 'not_sent'
    'reject_reason': str,       # optional
    'is_partial': bool,         # optional
    'signal_smart': bool,       # optional
    'signal_retry': int,        # optional
}
```

Handling described in "Feedback handling".

### Constants for the executor

`_get_constants()` — see "Capabilities provided". The executor caches them in `bot_settings['signal_constants']`.

### Grid constants (hardcoded)

| Constant | Value | Where used |
|---|---|---|
| `PANIC_SIGNAL_NUMBER` | `-999_999` | `force_reset_position`, feedback handling |
| `SMART_REVERSE_BASE` | `-100` | Smart signal numbering ceiling |
| `MAX_SIGNAL_RETRIES` | `5` | Retry limit on zero-fill |
| `MAX_ENTRY_RESET_STREAK` | `5` | Entry-timeout pause threshold |
| `CLOSE_DUST_FRACTION_DEFAULT` | `0.01` | Fallback when config lacks `close_dust_fraction` |
| `FORCE_RESET_TIMEOUT_NS` | `30 s` | Executor-confirm timeout for panic reset |

---

## UI Behaviour

### Create / edit form (`form_component`)

Groups of fields (all `inline=True`):

- **Source:** Collector (dropdown), Collector Table Name (auto-populated), Analyst (dropdown).
- **Use analyst signal for closing:** `RadioItems`.
- **Grid:**
  - `leverage` (1..500, default 10)
  - `averaging_strategy` (default `sum_prev`)
  - `averaging_threshold_pnl` (1..95, default 70)
  - `max_averaging_count` (1..500, default 3)
  - `smart_averaging_count` (0..500, default 2)
  - `breakeven_pnl` (default 4.0)
  - `close_pnl` (default 50.0)
  - `liquidation_pnl` (default 90)
  - `liquidation_order_type` (default `market`)
  - `close_dust_fraction` (Small 0.5% / Default 1% / Medium 2% / Large 5%)
- **Loop:**
  - `poll_interval_sec` (10..3600, default 30)
  - `execution_timeout_sec` (0..3600, default 0)
  - `recalc_strategy` (`none` / `recalc_grid` / `market_order`)
  - `execution_reserve_ratio` (1/10, 1/7, 1/3, 1/2)
- **History:**
  - `history_days` (1..365, default 7)
  - `history_positions_limit` (10..1000, default 100)

**Form callbacks:**

- `update_collector_tables(collector_id)` — populates the `collector_table` dropdown from the collector DB (`env.list_tables()`, filtered to exclude `bot_*`).

### Bot card (`render_grid_block`)

```
┌─ #N Grid Bot ← Collector Bot #M [status color] ──────────────┐
│                                                                │
│ Collector: M (table: auto)                                    │
│ Analyst:   — | Use for close: False                           │
│ Grid:      Leverage 10 | Strategy sum_prev | ...              │
│ PNL:       Break-even 4.0% | Close 50.0% | Liq 90%            │
│ Loop:      Poll 30s | Timeout 0s | Recalc recalc_grid | ...   │
│ [⚠ Position volume mismatch: expected=..., current=...]      │
│                                                                │
│ [Stop] [Close Position] [Force Reset] [Edit] [Delete]         │
│ ──────────────────────                                         │
│ [Main chart]                                                   │
│ [History chart]                                                │
│ [◀ Prev] Position 1 of N [Next ▶]                             │
│ [Deals table for the selected position]                       │
└────────────────────────────────────────────────────────────────┘
```

- **Info panel** — `graphics.info_panel([...])` with the labels above. `_render_position_consistency(bot_id)` returns `None` when in sync and a red badge otherwise.
- **Header (`grid-bot-header`)** — style refreshed by `global-interval` based on `status`.
- **Main chart (`grid-graph`)** — rebuilt every 5 s if `status=='running'`; shows price line, liq/close/breakeven/averaging levels, signal markers, and real-execution markers.
- **History chart (`history-graph`)** — rebuilt every 5 s if running; shows price line from `price_history` (window `history_days`) plus signal and real-execution markers from `deals_history`.
- **Deals table (`deals-container`)** — single callback `manage_deals` handles the table content, Prev/Next, and the position indicator. Data source is `deals_history`, ordered by `history_id DESC`. `history_positions_limit` limits the number of positions in the nav list.
- **Buttons:**
  - **Stop / Start** — standard `status-btn` in `app.py`.
  - **Close Position (`grid-close-btn`)** — `request_close_position()`, 5 s timeout.
  - **Force Reset (`grid-force-reset-btn`)** — `force_reset_position()`, 10 s timeout. **In single-bot mode this is a local-only reset** (no exchange order is emitted).
  - **Edit** — inline form.
  - **Delete** — standard `delete` handler.

### Chart markers

| Marker | Style |
|---|---|
| Price high / low | Black line |
| Liquidation level | Red solid |
| Break-even | Green dashed |
| Close PNL | Green solid |
| Averaging level | Blue dashed (unexecuted only) |
| Entry | Green/red triangle (buy/sell) |
| Buy signal | Medium green triangle |
| Sell signal | Medium red triangle |
| Close signal | Blue `x` |
| Liquidation signal | Black star |
| Real buy / real sell | Dark green / dark red triangles |
| Real close / real liquidation | Dark blue `x` / black star |

Relayout (pan/zoom) is preserved via `dcc.Store('relayout-store')`.

---

## Extending Grid Bot via Registry Inheritance

```python
from core import auto_reg

@auto_reg
class MyGridExtension:
    _inherit = "grid.bot"

    def my_custom_method(self):
        # self.config, self.env, self._position_id are available
        pass
```

Extensions are merged into the final `GridBot` class. Overriding `start()`, `stop()`, `_run()` is allowed — call `super()` to preserve base behavior.

---

## Troubleshooting

### Log files

Grid logs use module `"analytics"`:

- `logs/analytics_YYYYMMDD.log`

Logger names:

- `grid_<bot_id>` — GridBot logic.
- `grid_bot_ui` — UI / visualization.

### Common issues

| Problem | Likely cause | Solution |
|---|---|---|
| Chart shows no levels / entry | Invalid `position_id` or no active position | Check `bot_settings.current_position_id` |
| Stop/Start buttons do not reflect status | Callback not refreshed by `bots-trigger` | Verify `Input('bots-trigger', 'data')` |
| Levels shift a lot after timeout | Stale `_recalc_levels_from` using old average | Ensure `real_avg_entry_price` / `effective_avg` is used from the latest executed level |
| `database is locked` | Multiple connections | ORM uses RLock + WAL; avoid external connections |
| New position not opening after close | `_need_new_position` flag not processed | Check `_run` and `_rebuild_levels` call |
| Real-execution markers missing | `real_flag` not set or `real_price` NULL | Verify signals are actually being executed (emulation fills these fields) |
| ⚠ `Position volume mismatch` in info panel | `expected_vol ≠ current_vol` | Usually a race between fill and DB read. If it persists — inspect `levels_signals` and `_vol_cache` |
| ⚠ `Sanity: N rows kept for position ... manual review required` | Sanity detected anomalous `real_avg`, recompute not possible | Review `skipped_position_ids` in `bot_settings` manually |
| `_create_signal: entry/averaging sig=N already real — plan update ignored` | Attempt to overwrite plan fields of an already executed level | Normal behavior. If it repeats — inspect grid logic |
| `Panic/Close/Liquidation already executed for pos=... Refusing to recreate` | Attempt to re-emit a closing signal | Position is parked for reset (`_need_new_position=True`) |
| Grid enters `paused` with `entry_cannot_execute` | Entry could not be filled after `MAX_ENTRY_RESET_STREAK` rebuilds | Check executor / broker / exchange orders, then manually Start |
| Force Reset does nothing | Single-bot mode with no active exchange position | Expected — reset is local only in single-bot |
| `_get_analyst_signal()` blocks closing | Analyst capability is a stub returning `None` | Disable `use_analyst_close` until the analyst bot is implemented |

### Diagnostic commands

Check position consistency manually:

```python
from core.database import DBSQLite3
env = DBSQLite3(f"data/bot_{bot_id}.db")

pos_rec = env['bot.settings'].search([('key', '=', 'current_position_id')], limit=1)
pos_id = pos_rec[0].value.strip("'\"")

rows = env['levels.signals'].search([('position_id', '=', pos_id), ('real_flag', '=', True)])
total_in  = sum(float(r.real_vol_rate or 0) for r in rows if (r.signal_number or 0) >= 0)
total_out = sum(float(r.real_vol_rate or 0) for r in rows if (r.signal_number or 0) < 0)
print('current_vol =', max(0.0, total_in - total_out))

skipped = env['bot.settings'].search([('key', '=', 'skipped_position_ids')], limit=1)
if skipped:
    import json
    print('skipped:', json.loads(skipped[0].value or '[]'))
env.close()
```

Deep diagnostics: set `analytics` to `DEBUG` in the settings panel.

---

## Summary of Key Code Snippets

| Action | Code |
|---|---|
| Create and start | `add_bot('grid', 'grid bot', config)` → `bot_manager.add_bot()` → `start_bot()` |
| Get pending signals (executor side) | `await grid._get_pending_signals()` (via capability `signal`) |
| Report execution result | `await grid._set_execution_result(data)` (with `position_id` + `signal_timestamp`) |
| Trigger manual close | `await grid.request_close_position()` |
| Trigger panic reset | `await grid.force_reset_position()` |
| Apply config change at runtime | `grid.config_dirty = True` or `await grid.on_config_updated()` |
| Read closed deals | ORM query on `deals.history` |
| Extend with custom methods | `@auto_reg class X: _inherit = "grid.bot"` |
| Read skipped positions | `env['bot.settings']` → `skipped_position_ids` (JSON) |

---

## Appendix. Module structure

```
modules/grid/
├── __init__.py        # from . import models, components
├── __manifest__.py    # depends=["collector"]
├── models.py          # GridBot, LevelsSignals, DealsHistory, PriceHistory
└── components.py      # UI: form, charts, deals table, callbacks
```

Runtime dependencies:

```
modules/collector/            (candles capability)
modules/execution/            (optional consumer: signal, record_execution)
modules/broker/lib/exchange_directory.py  (EXCHANGES, MARKETS, options)
core/                         (registry, base_bot, database, exchange, graphics, styles, colors, logger)
```

---

## ⚠️ Disclaimer (repeat)

**This is a demonstration / educational version, not a finished product.**

**Trading cryptocurrencies involves significant risk and may result in the loss of your entire invested capital.** This module is provided "as is", without warranty of any kind, for educational and research purposes only. The author assumes no responsibility or liability for any financial loss incurred through the use of this code. **Test on testnet and with minimal capital first. Use at your own risk.**

---
