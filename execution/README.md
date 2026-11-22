# Execution Bot — Technical Manual

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
> - Futures trading with leverage magnifies both gains and losses and may lead to forced liquidation.
> - This software is provided **for educational and research purposes only**, "as is", without warranty of any kind.
> - The author assumes **no responsibility or liability** for any financial loss, missed profit, or other damage incurred through the use of this software.
> - **Never trade with funds you cannot afford to lose.** Test on demo/testnet environments with minimal capital first.

---

## Contents

1. [Role in the system](#1-role-in-the-system)
2. [Architecture](#2-architecture)
3. [Signal lifecycle](#3-signal-lifecycle)
4. [Phase model](#4-phase-model)
5. [SignalManager](#5-signalmanager)
6. [OrderManager (shim)](#6-ordermanager-shim)
7. [Broker interaction: price + order updates](#7-broker-interaction-price--order-updates)
8. [Feedback to grid](#8-feedback-to-grid)
9. [Breaker / PAUSED](#9-breaker--paused)
10. [Recovery and Reconciler](#10-recovery-and-reconciler)
11. [Panic close](#11-panic-close)
12. [Position and unit_volume](#12-position-and-unit_volume)
13. [Configuration](#13-configuration)
14. [Environment variables](#14-environment-variables)
15. [Database schema](#15-database-schema)
16. [Reject reasons reference](#16-reject-reasons-reference)
17. [Diagnostics and telemetry](#17-diagnostics-and-telemetry)
18. [Failure modes and recovery](#18-failure-modes-and-recovery)
19. [Limitations and warnings](#19-limitations-and-warnings)

---

## 1. Role in the system

Execution Bot is the **execution layer** between a Grid bot and an exchange:

```
┌──────────────────────── signal (0, 1..N, -1, -2, -999999)────────────────────────┐
│  Grid Bot  │ ──────────────────────────────────────────────────▶ │  Execution Bot │
│  (source)  │                                  │                │
│            │ ◀──────────────── record_execution ──────────────── │                │
└────────────────────────┘        (feedback)                └────────────────┘
                                                        │
                                                        │ capabilities:
                                                        │ get_latest_price,
                                                        │ create_*, cancel_order,
                                                        │ fetch_*, panic_close,
                                                        │ get_order_updates,
                                                        │ register_key_owner
                                                        ▼
                                                 ┌─────────────────┐
                                                 │    Broker       │
                                                 │  (ccxt/WS)      │
                                                 └────────┬────────┘
                                                        │
                                                        ▼
                                                  ┌─────────────┐
                                                  │ Exchange    │
                                                  └─────────────┘
```

**What the bot does:**
- Consumes signals from the source grid bot: entry (`0`), averaging (`1..N`), liquidation (`-1`), close (`-2`), panic (`-999999`), smart reverse (`< -100`).
- Places, cancels, and re-places orders according to a phase model (`phase1` / `phase2` / `phase3`).
- Tracks execution state via broker's **order-update queue** and REST reconciliation.
- Sends feedback to the grid (`record_execution`) with the actual fill and volume.
- Defends itself with a breaker (`auth` / `funds` / `invalid_order` / `network`), recovery after restart, and a reconciler for orphaned rows.

**What the bot does NOT do:**
- Make trading decisions (that is the grid's job).
- Talk to an exchange directly — **there is no ccxt client inside this module**. All exchange access goes through broker capabilities.
- Store API keys — keys live in the broker's `KeyVault`. Execution only holds a `broker_key_id` reference.
- Manage multiple positions simultaneously — one execution bot = one position of one grid.

---

## 2. Architecture

### 2.1. Components

| Component | File | Purpose |
|---|---|---|
| `ExecutionBot` | `models.py` | Owns all components, main loop, recovery, reconciler, breaker, background tasks |
| `SignalManager` | `lib/signal_manager.py` | Handles one signal: attempts, reorder, watchdog, remainder handling, finalize |
| `OrderManager` | `lib/order_manager.py` | **Thin proxy** to broker capabilities — no client, no queue, no workers |
| `make_client_order_id` | `lib/order_manager.py` | Deterministic `client_order_id` (`{bot_id}-{pos[:8]}-{sig}-{att}`, ≤ 36 chars) |

**Not present in this module (unlike some historical notes):**

- No `lib/exchange_ws.py` — WebSocket loops live in `modules/broker/lib/broker_service.py`. Execution consumes broker's order-update queue and price cache via capabilities.
- No `lib/fetcher.py` — all REST calls are broker capabilities.
- No `lib/crypto.py` — the key vault and encryption live in `modules/broker/lib/`.

### 2.2. Internal state of `ExecutionBot`

```python
self.source_handle:       ExchangeHandle | None   # to grid (signals)
self.broker_handle:       ExchangeHandle | None   # to broker (everything else)
self.feedback_handle:     ExchangeHandle | None   # to grid (feedback)

self.order_manager:       OrderManager | None     # shim
self.order_id_index:      dict[str, SignalManager]  # WS routing by cid
self.active_managers:     dict[(pos_id, sig_num), SignalManager]
self.signal_tasks:        dict[(pos_id, sig_num), asyncio.Task]
self.blocked_positions:   set[pos_id]             # after stale entry

self.price_buffer:        deque(maxlen=10000)     # for price_history persistence

self.paused / paused_reason / paused_since_ns / paused_check_at_ns
self._breaker_counters   = {auth, funds, invalid_order, network}
self._session_invalid_order_count                 # repause tracking
self._feedback_tasks:     set[asyncio.Task]       # fire-and-forget
self._balance_snapshot_counter                    # throttle for balance snapshot
self.ui_event_counter                             # UI refresh signal
self._panic_signal_number                         # cached from grid constants
```

### 2.3. Background tasks

| Task | Interval | Purpose |
|---|---|---|
| `_run` | `poll_interval` (`source_timeout / poll_divisor`) | Poll grid signals, spawn managers |
| `_poll_broker_price` | `broker_poll_interval_ms / 1000` | Fetch best bid/ask via broker |
| `_poll_order_updates` | continuous (blocking poll 0.3 s) | Consume broker's `OrderUpdateQueue` |
| `_flush_price_buffer_loop` | `poll_interval` | Persist price ticks to `execution_prices` |
| `_feedback_retry_loop` | 5 s | Retry pending feedback |
| `_latency_aggregate_loop` | 300 s | Aggregate latency percentiles |
| `_order_reconciler_loop` | 5 s | Reconcile orphaned rows |
| `_paused_watchdog_loop` | 5 s | Probe unpause conditions |
| `_balance_snapshot_loop` | 30 s | Persist `balance_snapshot` for UI |

---

## 3. Signal lifecycle

### 3.1. Signal payload (from grid)

`_poll_source` calls `source_handle.get("signal")` which returns a list of:

```python
{
    'position_id': str,                 # UUID
    'signal_number': int,               # 0=entry, 1..N=avg, -1=liq, -2=close,
                                        # -999999=panic, <SMART_REVERSE_BASE=smart reverse
    'signal_direction': 'buy'|'sell'|'close'|'liquidation',
    'signal_order_type': 'limit'|'market',
    'signal_price': float,
    'signal_vol_rate': float,           # relative volume (1.0, 2.0, 4.0, ...)
    'signal_timestamp': int,
    'signal_timestamp_scale': 'tbot_utc_ns',
    'signal_smart': bool,               # optional
    'signal_retry': int,                # optional
}
```

### 3.2. Filter chain in `_poll_source`

Applied in order:

| # | Condition | Action |
|---|---|---|
| 1 | `position_id is None or sig_num is None` | skip |
| 2 | `paused` and `sig_num != panic` | skip (throttled log once per 60 s) |
| 3 | `deadline_ns <= now_ns` | stale branch (see 3.3) |
| 4 | `pos in blocked_positions` and `sig_num != panic` and `sig_num != 0` | skip |
| 5 | `sig_num == 0` and `pos in blocked_positions` | unblock, continue |
| 6 | `(pos, sig_num) in active_managers` | skip (dedup — grid repeats signals) |
| 7 | `_decide_on_signal(...)` returns `'skip'` or `'noop'` | skip |
| 8 | `_decide_on_signal(...)` returns `'rebuild'` | `_rebuild_pair(old_row, new_ts)`; on failure — skip |
| 9 | `unit_volume_mode='quote'` and no reference price | skip + warning |
| 10 | create `SignalManager(plan)` and task | proceed |

### 3.3. Deadline and stale handling

```python
safety = max(self.source_timeout * PHASE4_RATIO, PHASE4_MIN_SEC)  # 5%, min 1 s
deadline_ns = sig_ts + (self.source_timeout - safety) * 1e9
```

If `now_ns >= deadline_ns`:

1. **Idempotency** — if a stale row for `(pos, sig, signal_timestamp=sig_ts)` already exists → skip.
2. **Existing row for this ts**:
   - **Terminal** (`filled`/`canceled`/`rejected`/`expired`/`not_found`) → fire-and-forget feedback if not yet sent → `continue`.
   - **Non-terminal** (`open`/`partial`/`not_sent`) → force-finalize:
     - `open`/`partial` — `cancel_order` + write `canceled`/`stale_signal` + feedback.
     - `not_sent` — write `reject_reason='stale_signal'` + feedback.
   - `continue`.
3. **No existing row**:
   - Non-urgent (`sig_num >= 0`): create a stub row with `exchange_state='not_sent'`, `reject_reason='stale_signal'`, `feedback_state='pending'`, `amount_*=0`. Fire-and-forget feedback.
     - If `sig_num == 0` — add to `blocked_positions` and call source's `request_close_position(reason='stale_entry_signal')`.
   - Urgent (`sig_num < 0`): sets `signal_order_type = 'market'` and falls through to normal processing.

### 3.4. `_decide_on_signal(pos, sig_num, sig_ts, sig_retry, last_row)`

| Condition | Decision |
|---|---|
| `last_row is None` | `'new'` |
| `sig_ts == r_ts` and `sig_retry > r_retry` and state in `('rejected','expired','canceled','not_found','not_sent')` | `'new'` (retry) |
| `sig_ts == r_ts` and `sig_retry > r_retry` and state other | `'skip'` |
| `sig_ts < r_ts` | `'skip'` |
| `sig_ts == r_ts` and state in `('open','partial','filled')` | `'skip'` |
| `sig_ts == r_ts` and state in `('rejected','expired','canceled','not_found','not_sent')` | `'noop'` |
| `sig_ts > r_ts` and state in `('open','partial','not_sent')` | `'rebuild'` |
| `sig_ts > r_ts` and state terminal | `'new'` |

### 3.5. Volume computation (`unit_volume`)

```python
unit_volume = config['unit_volume']       # default 1.0, effective (see §12.4)
mode        = config['unit_volume_mode']  # 'quote' | 'base'
rate        = signal_vol_rate

if mode == 'quote':
    ref_price = signal_price or best_price_from_buffer
    raw = rate * unit_volume / ref_price
else:  # 'base'
    raw = rate * unit_volume
```

This is the **pre-scaling** volume. The effective volume (`raw_eff`, `placed`) is produced in `SignalManager._compute_auto_scale` (see §5.3 and §12.4).

---

## 4. Phase model

Windows are anchored at `signal_timestamp` (TBOT UTC) and end at `deadline_ns`.

```
t = signal_timestamp                                    deadline_ns
│                                                              │
├──── phase1 ────┤────────────── phase2 ──────────────┤── phase3 ──┤
│                │                                │             │
│  set signal    │  reorder every N ms            │  grace      │
│  price only    │  by best price                 │  + finalize │
│                │                                │             │
```

### 4.1. Boundary computation (`_compute_phases`)

```python
total_ns = max(deadline_ns - sig_ts, 0)

# phase3 — 10% of total, clamped [2 s, 3 s]; if it overwhelms — 15% of total
phase3_ns = clamp(total_ns * 0.10, 2e9, 3e9)
if phase3_ns >= total_ns:
    phase3_ns = max(total_ns * 0.15, 1)
phase2_end_ns = deadline_ns - phase3_ns

# phase1 — execution_reserve_sec (from grid), capped at 70% of phase2
phase1_end_ns = sig_ts + execution_reserve_sec * 1e9
max_phase1_ns = (phase2_end_ns - sig_ts) * 0.7
if phase1_end_ns > sig_ts + max_phase1_ns:
    phase1_end_ns = sig_ts + max_phase1_ns
if phase1_end_ns <= sig_ts:
    phase1_end_ns = sig_ts + total_ns * 0.2

# phase2 reorder interval — phase2 duration / poll_divisor, floor 50 ms
divisor = max(int(self.bot.config.get('poll_divisor', 10) or 10), 1)
phase2_interval_ns = max(phase2_dur_ns // divisor, 50e6)
```

### 4.2. Price in each phase (`_determine_price`)

| Phase | Logic |
|---|---|
| **phase1** | For buy: `min(signal_price, best)`. For sell: `max(signal_price, best)`. If `best` is stale/`None`, falls back to `signal_price` (order is still placed) |
| **phase2** | `best_price` (fallback `signal_price`) |
| **phase3** | The limit is already waiting for `phase2_end_ns`; on timeout `_finalize` cancels and reports |
| **close/liq (`< 0`)** | `best_price` (fallback `signal_price`) — ignores phases |

### 4.3. Reorder (phase2 only)

`_reorder_loop`:
- Waits for `price_changed` event or `phase2_interval_ns` timeout.
- If `filled` — exit.
- Calls `_place_or_replace_order`:
  - If `active_order` exists and `active_order.price == current_best` — no-op.
  - Otherwise — cancel old, `_await_terminal_state`, create new.

---

## 5. SignalManager

### 5.1. Lifecycle

```
                      ┌────── run() ──────┐
                      │                     │
   ┌──────────────────────────────────────┤──────────────────────────────────┐  │
   │  is_panic? → _prepare_panic_plan    │  │
   │  sig==0?  → _reconcile_entry_position│  │
   │  _new_attempt()                     │  │
   │  _reconcile_skip_fill? → synthetic  │  │
   │  not is_panic? → _apply_preflight   │  │
   │  is_panic → _execute_panic_close    │  │
   │  market   → _execute_market         │  │
   │  limit    → _execute_limit          │  │
   └───────────────────────────────────┘  │
                      │                     │
                      ▼                     │
              finally: _finalize() ◀───────┘

                      ┌──── resume_from_row() ────┐
                      │                       │
   ┌──────────────────────────────────────┤────────────────────────────────────┐  │
   │  is_panic? → finalize (no re-execution)│  │
   │  market   → _reconcile_market_order   │  │
   │  limit    → _execute_limit            │  │
   └───────────────────────────────────┘  │
                      │                       │
                      ▼                       │
              finally: _finalize() ◀─────────┘
```

### 5.2. `_new_attempt(amount_placed=None)`

1. Marks previous attempts with `feedback_state in ('none','pending')` and `amount_filled == 0` as `superseded` (with `feedback_last_error='superseded_by_reorder'`).
2. Computes `attempt_n = max_prev + 1`.
3. Clears `order_id_index` for cids of terminal attempts with the same `(pos, sig)`.
4. Creates `execution.order` with `exchange_state='not_sent'`, `feedback_state='none'`.
5. Generates `client_order_id = make_client_order_id(bot_id, pos, sig, attempt_n)`.
6. Registers in `cid_to_row` and in `bot.order_id_index`.

`amount_requested` is taken from `plan['amount_requested']`. On the **first** attempt this is the pre-scaling `raw` (see §3.5). After `_apply_preflight`, `plan['amount_requested']` is overwritten with `raw_eff` (and the row is rewritten), so any subsequent `_new_attempt` on the reorder path (`_place_or_replace_order`, `_maybe_market_close_remainder`) already receives the effective value. This guarantees the feedback ratio `amount_filled / amount_requested ∈ [0, 1]` (see §8.1).

### 5.3. Pre-flight (`_apply_preflight`)

The pre-flight stage computes the effective volume and verifies funds.

**1. `_compute_auto_scale(rate, unit_volume, unit_volume_mode, max_scale)` → `(placed, raw_eff, uv_eff) | None`**

Contract:
- `unit_volume` is the cost of one quantum (quote-currency when `unit_volume_mode == 'quote'`, base-currency otherwise).
- On entry (non-reduce-only) `uv_eff` is raised until one quantum covers **both** exchange minimums (`min_cost` and `min_amount`). The bumped value is **persisted** into `bot_settings.unit_volume`, so the UI shows the effective number.
- On `reduce_only` the value is never bumped; if `placed` is below the exchange minimum the call returns `None` (`scale_impossible`), and the grid receives a zero-fill.
- `max_scale` guards against runaway: final `uv_eff` (after the precision bump) must not exceed `unit_volume × max_scale`.

Algorithm:

1. `ref_price = plan['price'] or best_price`. In `quote` mode, `ref_price <= 0` → `None` (cannot convert quote-per-quantum to base amount without a price). In `base` mode `ref_price` is not required.
2. If `unit_volume_auto_scale = False` — return `(raw, raw, unit_volume)` untouched (no floor, no bump, no persist). This is the user's explicit choice; conformance with exchange minimums becomes the user's responsibility.
3. Fetch `precision.amount`, `limits.amount.min`, `limits.cost.min` via broker `get_market`. Failure → `None`.
4. **`reduce_only` path**:
   - `raw = rate × unit_volume / ref_price` (quote) or `rate × unit_volume` (base);
   - `placed = floor_step(raw, precision)`;
   - reject (`None`) if `placed <= 0` or `placed < min_amount`.
5. **Entry path — bump `uv_eff`**:
   - quote: `uv_eff = max(uv, min_cost, min_amount × ref_price)`;
   - base:  `uv_eff = max(uv, min_amount, min_cost / ref_price)`.
6. `raw_eff = rate × uv_eff / ref_price` (quote) or `rate × uv_eff` (base); `placed = floor_step(raw_eff, precision)`.
7. **Precision bump.** After floor, `placed` may still be below `min_amount` or `placed × ref_price` below `min_cost`. If so:
   - `target_placed = max(placed, min_amount, min_cost / ref_price)`;
   - `n_steps = ceil(target_placed / precision)` — **integer**;
   - **bounded-retry loop (3 iterations)**: `uv_eff = n_steps × precision × ref_price / rate` (quote) or `n_steps × precision / rate` (base); recompute `raw_eff, placed = _calc(uv_eff)`; exit when both `placed >= min_amount` and `placed × ref_price >= min_cost`; else `n_steps += 1` (integer step — no float drift). The retry absorbs ULP loss in the `uv_eff → raw_eff → placed` round-trip for awkward ratios like `rate × ref_price × precision`.
8. **`max_scale` on the FINAL `uv_eff`** (after the precision bump): `uv_eff > unit_volume × max_scale` → `None` (`scale_impossible`). The bumped value is not persisted.
9. **Sanity**: `placed <= 0`, `placed < min_amount`, `placed × ref_price < min_cost` → `None`.
10. **Persist**: if `abs(uv_eff − unit_volume) > 1e-12` — write `uv_eff` into `bot_settings.unit_volume` and update `self.bot.config['unit_volume']` in memory. The in-memory update is **critical**: `_check_balance` (step 2 below) reads the buffer from the effective value, not the original input.

On `None` → write `exchange_state='not_sent'`, `reject_reason='scale_impossible'`, `amount_placed=0`; send feedback; cleanup.

**2. `_check_balance(placed)`** — see §12.5.
- Insufficient funds → reject with `insufficient_funds` + feedback + cleanup + `_register_breaker_failure('funds')`. `_cleanup()` runs **before** `_register_breaker_failure` — otherwise `_enter_paused` would finalize the manager and overwrite the reject reason.
- **Fail-open** on `fetch_balance` exception: returns True (the actual error will surface in `create_order`).

**3. Write `amount_requested = raw_eff`** into `plan['amount_requested']` **and** into `current_order.amount_requested`. This is the invariant that keeps the feedback ratio in `[0, 1]` on a full fill (see §8.1).

### 5.4. `_execute_market()`

1. `order_manager.create_market_order(side, amount, pos, sig, attempt, reduce_only)`.
2. Write `exchange_order_id`, `exchange_state='open'`, and the actual `amount` from REST into the row.
3. If REST returned `status='closed'` — `process_order_update` immediately.
4. Otherwise wait `filled_event` until `deadline_ns`; on timeout — `_reconcile_market_order`.
5. Error handling:
   - `-2022`/`ReduceOnly rejected` with `reduce_only=True` → `position_already_flat`, feedback, no breaker.
   - Otherwise `classify_exception` → reject_reason + breaker.

### 5.5. `_execute_limit()`

1. Launches `_reorder_loop` and `_order_watchdog_loop`.
2. Waits `filled_event` until `phase2_end_ns`.
3. Timeout → logs "phase 2 expired, entering phase 3".
4. Finally — cancels both tasks.

### 5.6. `_finalize()`

Order:

1. If `active_order` exists and state is non-terminal:
   - `cancel_order` + `_await_terminal_state` (3 s, or 1 s if `_stopping`).
   - If not confirmed → `feedback_state='pending'`, `feedback_last_error='cancel_unconfirmed'`, cleanup, exit.
2. If state is terminal:
   - **NEW (Patch C+):** for `reduce_only` and `not is_panic`:
     - **Zero-fill canceled/expired** (`amount_filled == 0`):
       `_probe_net_position()` → if flat → `_force_complete_feedback()` + cleanup.
       Otherwise leave as zero-fill canceled so grid can retry.
     - **Partial close** — `state in ('canceled','expired')` with `amount_filled > 0`,
       **or** `state == 'filled'` with `amount_filled < amount_requested × (1−REL_TOL)`
       (Binance caps reduce-only to the real position size but reports
       `executedQty == amount_placed`):
       → `_maybe_market_close_remainder()`; on success — cleanup.
   - Panic rows (`is_panic=True`) **do not** re-enter `_maybe_market_close_remainder` —
     `_execute_panic_close` already sent its own feedback.
   - If feedback not yet sent — send it.
   - Cleanup, exit.

3. Otherwise — total across all attempts:
   - `total >= placed × (1 - REL_TOL)` → `'filled'`.
   - `total > 0` and below → `'partial'`; for `reduce_only` **and not is_panic** —
     `_maybe_market_close_remainder()`.
   - `total == 0` → `'canceled'` + `reject_reason='timeout'`.
4. `_weighted_avg_fill_price` — weighted avg of the fill across all attempts.
5. `_send_feedback_for_row` (await).
6. `_cleanup`.

### 5.7. `_cleanup()`

Idempotent (guard `_cleaned`):

- Sets `filled_event`, `order_opened`, `order_canceled` — unblocks waiters.
- `unregister_all()` — removes cids from `bot.order_id_index`.
- `current_order = None`.
- Removes `(pos, sig)` from `bot.active_managers` and `bot.signal_tasks`.

### 5.8. Reorder — `_place_or_replace_order`

1. If `active_order` with same price — no-op.
2. Cancel old → `_await_terminal_state`. If not confirmed — reorder stops.
3. If cancel confirmed a fill — reorder not needed, `filled=True`.
4. Otherwise: determine new price, `amount = plan.amount_placed - total_filled`.
5. **B6:** reuse `current_order` only if `exchange_state='not_sent'` **and** `exchange_order_id is None`. Otherwise — new attempt.
6. `create_limit_order` → immediately write `exchange_state='open'` (prevents the next reorder from reusing the row).
7. If REST returned a terminal status — `process_order_update` immediately.

### 5.9. Order watchdog (`_order_watchdog_loop`)

- Every `reorder_interval / 2` (min 0.05 s).
- If `silent_ns < 2 × reorder_interval` — skip.
- Otherwise `fetch_order` to verify state.
- After 3 consecutive failures — exit (hand control back to `_finalize`).

### 5.10. Terminal confirm — `_await_terminal_state(order_id, cid, timeout_sec)`

```
event-based:
  - wait _terminal_by_cid[cid].set() up to CANCEL_CONFIRM_POLL_SEC (0.2 s)
  - on set: check row.exchange_state in TERMINAL_STATES → return True

fallback:
  - every CANCEL_CONFIRM_POLL_SEC call fetch_order
  - if status → process_order_update → check → return True

deadline: CANCEL_CONFIRM_TIMEOUT_SEC (3 s); 1 s when _stopping
```

### 5.11. Order-update ingestion (`process_order_update`)

Dispatches by `status`:

| Status | Effect |
|---|---|
| `closed` | `filled` or `partial` via `_classify_fill_state`; on `amount_filled > 0` and not paused — reset `_session_invalid_order_count` (`_on_order_success`); if the plan total passes the threshold — `filled=True` |
| `canceled` | Write `canceled`, signal terminal event |
| `open` | Write `open`, set `order_opened`, record WS latency |
| `rejected` | Write `rejected` + `reject_reason='order_rejected'`, signal terminal event |
| `expired` | Write `expired`, signal terminal event |
| `partially_filled` | Write `partial`/`filled` via `_classify_fill_state` |

Terminal-state guard: if `row.id != current_order.id` and `row.exchange_state in TERMINAL_STATES` — ignore (protects against reorder race).

### 5.12. Market-close remainder (Patch C+) — `_maybe_market_close_remainder`

Only for `reduce_only=True` and **not `is_panic`** (panic already sent its
own feedback — never re-enter).

`remaining` is derived from the **actual exchange position**, not from
`_total_filled()`. The probe is `_probe_net_position()`:

- `fetch_positions([symbol])` → signed net (`info.positionAmt`, fallback
  `contracts` + `side`);
- returns `None` on exception, `None` on `positions is None`, `None` on
  hedge mode / multiple non-zero positions (ambiguous);
- caller falls back to `_total_filled()` only when `None`.

Let `placed = plan.amount_placed`, `threshold = placed × close_market_fraction`
(default `0.15`, configurable via grid constants).

Sign protection: for `side='sell'` the exchange position must be ≥ 0
(long); for `side='buy'` — ≤ 0 (short). If the sign disagrees with the
close side — `_force_complete_feedback()` without placing an order.

| Case | Behavior |
|---|---|
| `abs(net) <= placed × 1e-9` | Already flat → `_force_complete_feedback()` |
| `abs(net) > threshold` | Too large for market → `_force_complete_feedback()` (grid absorbs at next `signal 0`) |
| `remaining <= threshold` | Up to `MAX_MARKET_CLOSE_ATTEMPTS = 3` market-close attempts, each with a new attempt row, `reduce_only=True`, wait `CLOSE_MARKET_WAIT_SEC = 10 s` |
| `-2022` on reduce-only | `position_already_flat` (success) |
| Terminal after attempt, **re-probe flat** | `_send_feedback_for_row(row)` → success |
| Terminal after attempt, **re-probe not flat** | Feedback only if `amount_filled > 0`; retry |
| Terminal after attempt, **probe returned `None`** | Fall back to `_total_filled() >= placed × (1−REL_TOL)` |
| 3 failed attempts | `_force_complete_feedback()` (`reject_reason='forced_complete_after_failure'`) |

The `_force_complete_feedback` writes a synthetic "filled" on the **current**
plan row (`amount_filled = amount_requested`) so grid can reset; the
actual exchange remainder is absorbed at the next `signal 0` via
`_reconcile_entry_position`.

**Key invariant:** after each market-close attempt the code re-probes the
exchange. `_total_filled()` is only a book-keeping fallback when the
probe is unavailable — it can over-report after a capped reduce-only fill.

### 5.13. `_reconcile_entry_position` (signal 0)

Fetches `fetch_positions([symbol])` and computes:

```
target_signed = entry_sign * signal_amount
delta         = target_signed - current_signed
```

If `delta ≈ 0` — `plan['_reconcile_skip_fill'] = True`, `run()` will emit a synthetic filled row instead of placing an order (prevents a zero-amount order).

Otherwise the plan's `side`, `amount_placed`, `amount_requested` are rewritten to `(|delta|, sign(delta))` so that after the fill the exchange position matches the plan target.

Fail-safe: if `fetch_positions` raises — return without modification (the order is placed as originally planned).

### 5.14. `_probe_net_position` — exchange position probe

Used by `_maybe_market_close_remainder` (both at entry and after each
attempt) and by the zero-fill flat branch in `_finalize`.

```python
async def _probe_net_position(self) -> float | None:
    positions = await broker_handle.get("fetch_positions", ...)
    if positions is None: return None
    net, nonzero = 0.0, 0
    for p in positions:
        s = self._signed_position(p)
        if abs(s) > 1e-12: nonzero += 1
        net += s
    if nonzero > 1: return None   # hedge mode / ambiguous
    return net
```
Returns:
float — signed net position (0.0 == flat);
None — probe failed, positions is None, or hedge mode
(nonzero > 1). Caller must fall back to _total_filled().

### 5.15. Weighted average fill price

`_weighted_avg_fill_price` — computes `Σ(amount_filled_i × avg_fill_price_i) / Σ(amount_filled_i)` across all attempts for the same `(pos, sig)`. Used in `_finalize` so a reorder following a partial fill does not lose the average price.

---

## 6. OrderManager (shim)

`modules/execution/lib/order_manager.py` is a **thin proxy** to broker capabilities. It has no client, no queue, no rate-limit of its own.

### 6.1. Proxied operations

All methods forward directly to `broker_handle.get(<capability>, ...)`:

| Method | Broker capability | Notes |
|---|---|---|
| `create_market_order(side, amount, pos, sig, attempt, reduce_only)` | `create_market_order` | builds `cid` and passes it |
| `create_limit_order(side, amount, price, pos, sig, attempt, reduce_only)` | `create_limit_order` | same |
| `cancel_order(order_id, cid)` | `cancel_order` | |
| `fetch_order(order_id, cid)` | `fetch_order` | |
| `fetch_open_orders()` | `fetch_open_orders` | |
| `fetch_positions(symbols=None)` | `fetch_positions` | defaults to `[self.symbol]` |
| `fetch_balance(currency=None)` | `fetch_balance` | |
| `set_leverage(leverage)` | `set_leverage` | |

### 6.2. Non-operations

- `wait_rate_limit()` — no-op (rate-limit lives in broker).
- `close()` — no-op (broker owns the client).
- `.exchange` / `.markets` properties — `raise RuntimeError` (use broker's `get_market`/`get_markets`).
- `_ensure_exchange()` — `raise RuntimeError`.

### 6.3. Client order id

```python
f"{bot_id}-{position_id[:8]}-{signal_number}-{attempt_n}"[:36]
```

36 chars is the Binance limit. The `bot_id` prefix prevents collisions between multiple executors sharing one broker.

`build_client_id_params(exchange, op, cid)` (from broker's `exchange_directory.py`) translates the unified cid into the per-exchange `params` key (`newClientOrderId` / `orderLinkId` / `clientOid` / `clOrdId` / `clientOrderId` / `text` / `client-order-id`).

---

## 7. Broker interaction: price + order updates

### 7.1. Price feed — `_poll_broker_price`

The price stream lives in broker (`watch_bids_asks` / `watch_order_book` / `watch_ticker` / `rest_poll` per `EXCHANGE_WORKAROUNDS`). Execution polls the broker cache:

```python
price = await self.broker_handle.get(
    "get_latest_price",
    exchange=self.config['exchange'],
    market_type=self.config['market_type'],
    symbol=self.config['symbol'])
# → {'bid': float, 'ask': float, 'ts_ns': int, 'stale': bool, 'source': 'broker'}
```

Strict Q10 validation before use:

- `bid` and `ask` both not `None`.
- `price['stale'] is False`.

On success: append to `price_buffer`, call `mgr.update_price(bid, ask, ts_ns)` on all `active_managers`.

Sleep = `config['broker_poll_interval_ms'] / 1000`, floor 0.05 s.

### 7.2. Order updates — `_poll_order_updates`

Broker pushes updates into a per-owner queue (`OrderUpdateQueue`). Execution polls it:

```python
updates = await self.broker_handle.get(
    "get_order_updates",
    owner_bot_id=self.bot_id,
    since_id=since_id,
    timeout_sec=0.3)
```

Each update is `{'id': int, 'cid': str, 'update': dict}`. Routed to `SignalManager.process_order_update` via `order_id_index[cid]`.

**Registration with broker**: `_poll_order_updates` calls `register_key_owner(key_id, owner_bot_id)` on first healthy poll. On broker restart (detected via `_ensure_broker` returning False, or an exception during `get_order_updates`), the `registered` flag resets and `since_id` restarts from 0 — order ids are broker-session-scoped.

`unregister_key_owner` is called from `stop()`.

### 7.3. Broker health — `_ensure_broker`

Same pattern as collector:

- Check `manager.get_bot_status(self._broker_id)`.
- If not `'running'` — `manager.restart_bot(self.bot_id, self._broker_id)` (rate-limited: once per 10 s).
- Probe `get_broker_status` with 5 s timeout. On timeout — `force_restart_bot`.
- On any failure — sleep 1–2 s and retry.

---

## 8. Feedback to grid

### 8.1. Payload

Built by `_build_feedback_payload(row)`:

```python
{
    'position_id': row.position_id,
    'signal_number': row.signal_number,
    'signal_timestamp': row.signal_timestamp,
    'signal_timestamp_scale': row.signal_timestamp_scale or 'tbot_utc_ns',
    'real_price': row.avg_fill_price or row.price_requested or 0.0,
    'real_volume': row.signal_vol_rate * (row.amount_filled / row.amount_requested),
    'real_timestamp': self.get_time_ns(),
    'real_timestamp_scale': 'tbot_utc_ns',
    'exchange_state': row.exchange_state,
    'reject_reason': row.reject_reason,
    'is_partial': row.exchange_state == 'partial',
    'signal_smart': bool(row.signal_smart),
    'signal_retry': int(row.signal_retry or 0),
}
```

`real_volume` — the filled volume expressed in signal-rate units (`signal_vol_rate` scale).

**Invariant.** `amount_requested` is the **effective** volume `raw_eff` (see §5.3), so `amount_filled / amount_requested ∈ [0, 1]` for all states, including a full fill with an auto-scale bump. A full fill yields `real_volume = signal_vol_rate` — no "quantum inflation" in `real_position_volume` on the grid side.

### 8.1a. Async override — `_build_feedback_payload_async(row)`

Before sending, `_send_feedback_for_row` calls
`_build_feedback_payload_async(row)`, which wraps
`_build_feedback_payload` and applies a **reduce-only flat-override**:

1. `if not row.reduce_only or row.exchange_state != 'filled'` → return as-is.
2. `fetch_positions([symbol])` via `order_manager`.
   - Exception or `positions is None` → return as-is (safe).
   - `nonzero > 1` (hedge mode) → return as-is (ambiguous).
   - `abs(net) < 1e-9` (**exchange flat**) → override:
     `payload['real_volume'] = float(row.signal_vol_rate or 0.0)`.

Why: Exchange may report a reduce-only order as fully `filled` even when
the real position was smaller than `amount_placed`. The formula
`real_volume = signal_vol_rate * amount_filled / amount_requested`
would under-report the close, and grid would wait forever for a
remainder that no longer exists.

This is a **second safety net**: even if `_finalize` skipped
`_maybe_market_close_remainder` (e.g. `_stopping`), the feedback still
reaches grid with the correct `real_volume`.

### 8.2. `feedback_state` lifecycle

```
none → pending → sent
             ├── delivery_failed   (age > 24 h)
             └── superseded        (replaced by a new attempt)
```

| State | Meaning |
|---|---|
| `none` | Not yet attempted |
| `pending` | Attempt failed; will be retried |
| `sent` | Successfully delivered |
| `delivery_failed` | Not delivered within 24 h |
| `abandoned` | Legacy; migrated to `delivery_failed` on startup |
| `superseded` | Attempt was replaced by reorder/rebuild |

### 8.3. `_send_feedback_for_row(row)`

- Payload built via `await self._build_feedback_payload_async(row)` (see §8.1a).
- Up to 3 attempts, backoff `0.1, 0.2, 0.4 s`.
- On success → `feedback_state='sent'`, `feedback_attempts += 1`, `_bump_ui_event()`.
- On failure → `feedback_state='pending'`, `feedback_last_error=str(e)[:255]`.

### 8.4. Fire-and-forget

`_send_feedback_fire_and_forget(row, name_prefix="fb_ff")` — used on the hot path in `_poll_source` (stale signals) and in `_feedback_retry_loop`:

- Creates an `asyncio.Task`, tracked in `self._feedback_tasks`.
- On `RuntimeError` (no loop) → `logger.warning` (row stays `pending`).
- Drained in `stop()` — cancel + gather.

### 8.5. Retry loop — `_feedback_retry_loop`

Every `FEEDBACK_RETRY_INTERVAL_SEC = 5 s`, calls `_flush_pending_feedback_once(fire_and_forget=True)`:

- Filters: `feedback_attempts < 20` (max), `created_ns < now - 5 s`, and either `feedback_state='pending'` or `feedback_state='none'` with terminal `exchange_state`.
- If `now - created_ns > 24 h` → `delivery_failed` (with explanatory `feedback_last_error`).
- Otherwise — send (fire-and-forget).

`_exit_paused` and `start()` call `_flush_pending_feedback_once(fire_and_forget=False)` — synchronous, result is needed before continuing.

---

## 9. Breaker / PAUSED

### 9.1. Thresholds

| Reason | Default | Config key |
|---|---|---|
| `auth` | **1** (hardcoded, not configurable) | — |
| `funds` | 1 | `breaker_insufficient_funds` |
| `invalid_order` | 3 | `breaker_invalid_order` |
| `network` | 5 | `breaker_network` |

Classification via `classify_breaker_reason(exc)` (in `lib/signal_manager.py`):

- `auth` — `AuthenticationError` / `PermissionDenied` / message contains `auth`, `signature`, `api key`, `permission`, `invalid key`.
- `funds` — `insufficient` / `balance` in message.
- `invalid_order` — `InvalidOrder` / `BadRequest` / `invalid order`.
- `network` — `asyncio.TimeoutError`, `ConnectionError`, `NetworkError`, `RequestTimeout`, `ExchangeNotAvailable`, `DDoSProtection`, `RateLimitExceeded`.
- `''` — not a breaker category.

### 9.2. Entering PAUSED — `_enter_paused(reason)`

1. `self.paused = True`, `paused_reason`, `paused_since_ns`, `paused_check_at_ns = now + 60 s`.
2. If `reason='invalid_order'` — increments `_session_invalid_order_count`; at `REPAUSE_THRESHOLD = 3` transitions to `invalid_order_persistent`:
   - `paused_check_at_ns = 0` (never auto-unpause).
   - Logs an ERROR.
3. Persists `execution_paused = {reason, since_ns, check_at_ns, attempt: 0, repause_count}`.
4. `update_bot_status('paused')`.
5. Finalizes all `active_managers` (5 s timeout; `_stopping` flag temporarily raised).

### 9.3. Unpause probe — `_can_unpause`

| `paused_reason` | Probe |
|---|---|
| `collector_unavailable` | `get_broker_status` on broker |
| `invalid_order` | Immediate True |
| `invalid_order_persistent` | False (never auto-unpause) |
| `auth`, `funds`, `network` | `order_manager.fetch_balance()` — success → True |
| default | True |

### 9.4. Watchdog — `_paused_watchdog_loop`

Every 5 s:

- If not paused → skip.
- If `invalid_order_persistent` → skip.
- If `now < paused_check_at_ns` → skip.
- `_can_unpause`:
  - True → `_exit_paused()`.
  - False → extend `paused_check_at_ns` by 60 s, persist, log.

### 9.5. `_exit_paused`

- Clears paused state in memory, resets breaker counters.
- Persists `execution_paused = None`.
- `update_bot_status('running')`.
- Synchronous feedback flush + `_reconcile_orders()`.

### 9.6. `_on_order_success`

Called from `process_order_update` when `amount_filled > 0` and `not paused`: resets `_session_invalid_order_count = 0`. Guard: a late fill during pause does **not** reset the counter (prevents premature clearing of `invalid_order_persistent`).

### 9.7. Behaviour while paused

- `_poll_source` skips all signals except `-999999` (panic).
- Skip logs are throttled to once per 60 s.
- `-999999` (panic close) **always executes**.
- Recovery on startup is skipped when paused.

---

## 10. Recovery and Reconciler

### 10.1. `_recover_in_flight()` (called on `start` if not paused)

1. Search:
   - `exchange_state in ('open','partial','not_sent')` AND
   - `feedback_state not in ('sent','abandoned','delivery_failed','superseded')`.
2. Group by `(position_id, signal_number)`, take the latest attempt; limit `RECOVERY_MAX_PAIRS = 20`.
3. **Concurrent with `Semaphore(5)`**, per group:
   - `open`/`partial` → `_recover_pair`:
     - `fetch_order`.
     - If `open`/`partially_filled` — hydrate `mgr.active_order` and `mgr.active_client_order_id`, spawn `resume_from_row()`.
     - If terminal — write state, feedback, exit.
     - If order_id is missing — write `reject_reason='recovery_no_ref'` (reconciler will pick it up).
   - `not_sent`:
     - If `deadline_ns` passed → `reject_reason='stale_recovery'`, feedback.
     - Otherwise → `SignalManager.from_row` + `mgr.run()`.

### 10.2. Reconciler — `_order_reconciler_loop` (every 5 s)

Runs `_reconcile_orders`, which handles three categories:

1. **Orphaned open/partial** — `exchange_state in ('open','partial')`, not in `active_managers`, and either `feedback_state in ('none','pending')` or `reject_reason='recovery_no_ref'`. Each → `_reconcile_row`.
2. **`recovery_no_ref` rows** — same `_reconcile_row`.
3. **Filled without feedback** — `exchange_state='filled'` and `feedback_state in ('none','pending')`; skip if `feedback_attempts >= FEEDBACK_MAX_ATTEMPTS`.

### 10.3. `_reconcile_row(row)`

- If no `exchange_order_id`:
  - If feedback already `sent` → exit.
  - Otherwise → `exchange_state='not_found'`, `reject_reason='recovery_no_ref'`, feedback.
- Otherwise `fetch_order`:
  - `closed` → `filled`/`partial` + feedback.
  - `canceled` → state + feedback.
  - `rejected` → state + `reject_reason='order_rejected'` + feedback.
  - `expired` → state + feedback.

### 10.4. Rebuild — `_rebuild_pair(old_row, new_sig_ts)`

Triggered when `sig_ts > last_row.signal_timestamp` and the old order is still alive.

1. If no `exchange_order_id` — write `reject_reason='superseded'` + feedback, return True.
2. Otherwise `cancel_order`.
3. `_await_row_terminal(old_row, timeout_sec=2.0)` — poll `fetch_order` every 0.2 s.
4. If old order filled/partial — abort (return False).
5. If state non-terminal — force `canceled`.
6. Write `reject_reason='superseded'`, `feedback_last_error='superseded_by_rebuild'`, feedback.

---

## 11. Panic close

### 11.1. Initiation

Two paths:

- User clicks **Force Reset** in the grid UI → grid emits `signal_number=-999999`.
- Grid has a stale entry (`signal 0` timeout) and calls `request_close_position` on its own — **in grid's single-bot mode**, this is a local reset (no signal).

### 11.2. `_prepare_panic_plan`

1. `fetch_positions([symbol])`.
2. If exception → `_panic_prepare_failed=True`, `amount=0`.
3. Otherwise finds the first non-zero position:
   - `close_side = 'sell' if contracts > 0 else 'buy'`.
   - `amount_requested = amount_placed = |contracts|`.

### 11.3. `_execute_panic_close`

Calls broker's `panic_close` capability (which does cancel-all + market-close in one composite call on the exchange side):

```python
result = await self.bot.broker_handle.get(
    "panic_close",
    key_id=..., symbol=...)
# → {'closed_size': float, 'errors': list, 'orders_canceled': int, 'avg_fill_price': float | None}
```

Dispatch:

| Condition | Result |
|---|---|
| `_panic_prepare_failed` | reject `panic_prepare_failed` |
| `amount_placed == 0` | reject `position_already_flat` |
| `closed_size > 0` and no errors | filled |
| no errors but `closed_size == 0` | filled + `reject_reason='position_already_flat'` |
| errors | rejected + `reject_reason='panic_close_failed'`, `reject_reason_raw='; '.join(errors)` |

### 11.4. Semantics for the grid

Grid waits up to `FORCE_RESET_TIMEOUT_NS = 30 s` for feedback. On receipt —
resets locally. On timeout — resets locally as-is, logs an error.

**Patch C+ invariant:** panic rows (`plan.is_panic=True`) are excluded
from `_maybe_market_close_remainder` by an explicit guard. `_execute_panic_close`
already sends its own feedback — re-entering market-close for a panic
row would produce duplicate feedback.

---

## 12. Position and unit_volume

### 12.1. `position_id` and signals

- One "position" = one grid cycle = `position_id` (UUID).
- All signals for one position share the same `position_id`.
- Dedup by `(position_id, signal_number)`.

### 12.2. `signal_vol_rate`

Relative volume: `1.0` for entry, then depends on the strategy (`sum_prev`: 2, 4, 8, ...; `double`: 2, 4, 8, ...; `triple`: 1, 3, 9, ...).

### 12.3. `unit_volume`

| `unit_volume_mode` | Pre-scaling formula |
|---|---|
| `quote` | `raw = rate × unit_volume / ref_price` |
| `base` | `raw = rate × unit_volume` |

`ref_price = signal_price` or, if absent, the freshest `price_buffer` entry.

This formula produces the **pre-scaling** volume. The **effective** volume (`raw_eff`, `placed`) is produced by `_compute_auto_scale` (see §12.4). When `auto_scale=True`, `unit_volume` in the config may be larger than the user's original input (see the "ratchet" note in §12.4).

### 12.4. Auto-scale — `unit_volume` = effective, persisted

**Contract.** The config always stores the **effective** `unit_volume`. The user edits the field in the form, but with `auto_scale=True` the bot raises it on the first entry signal to cover exchange minimums, persists the bumped value, and the UI then shows the effective number.

**Bump rule** (entry, `reduce_only=False`):
- quote: `uv_eff = max(uv, min_cost, min_amount × ref_price)`
- base:  `uv_eff = max(uv, min_amount, min_cost / ref_price)`

Then a **precision bump** may be required if, after `floor_step`, `placed` is still below `min_amount` or `placed × ref_price` is below `min_cost`. The bump computes `target_placed = max(placed, min_amount, min_cost / ref_price)`, `n_steps = ceil(target_placed / precision)` (integer), and enters a bounded 3-iteration retry: `uv_eff = n_steps × precision × ref_price / rate` (quote) or `n_steps × precision / rate` (base), recompute `placed`, exit when both minimums are satisfied, else `n_steps += 1`. The integer `n_steps` avoids float drift; the 3-iteration retry absorbs ULP loss in the `uv_eff → raw_eff → placed` round-trip for awkward ratios (e.g. `0.7 × 3.0 × 0.0001`).

**Guard rails.**

- **`max_scale`** (`0` = unlimited) is checked on the **final** `uv_eff` (after the precision bump): `uv_eff > unit_volume × max_scale` → reject `scale_impossible`, **not persisted**.
- **`reduce_only`** is never bumped and never persisted. If `placed < min_amount` → `scale_impossible`, the grid receives a zero-fill.
- **`unit_volume_auto_scale = False`** disables bump and persist entirely; the order is submitted as-is, and conformance with exchange minimums becomes the user's responsibility. In `quote` mode with no available `ref_price` the order is rejected `scale_impossible` (the base amount cannot be computed without a price).

**Ratchet.** `unit_volume` only moves **upward** with exchange minimum changes. It is **not** automatically reduced — reduction is a manual edit in the UI. This is deliberate: automatic downward adjustment could slide the value below real strategy needs on a temporary `min_cost` drop (e.g. after switching to another symbol) and stop covering the strategy's actual volume. To lower the value, edit it manually; the bot will re-bump on the next entry signal if the new value is below the exchange minimum.

### 12.5. Balance check — `_check_balance`

Currency used for spending:

| `market_type` | Side | `spend_cur` |
|---|---|---|
| `futures` | any | `quote` |
| `spot` | `buy` | `quote` |
| `spot` | `sell` | `base` |

Buffer (always in quote, converted to base when needed):

```
buffer_quote = margin_buffer_ratio * unit_volume_quote
unit_volume_quote = unit_volume * (1 if mode=='quote' else ref_price)
```

`unit_volume` here is the **effective** value — `_compute_auto_scale` persists it and updates `self.bot.config['unit_volume']` in memory **before** `_check_balance` runs (see §5.3), so the buffer is computed from the scaled volume, not from the user's original input.

Required:

```
futures:   placed * ref_price / leverage + buffer_quote
spot buy:  placed * ref_price + buffer_quote
spot sell: placed + buffer_quote / ref_price
```

If `available < required` → reject `insufficient_funds` + feedback + `_register_breaker_failure('funds')`.

**Fail-open** on `fetch_balance` exception — returns True (no breaker; actual error will surface on `create_order`).

### 12.6. Margin buffer

`margin_buffer_ratio` (default `0.05`) — quote-balance reserve protecting against commission/slippage-driven rejects.

---

## 13. Configuration

Defaults below reflect the **create/edit form** (`components.py`). Programmatic defaults differ slightly for `poll_divisor` (see note).

### 13.1. Main

| Key | Type | Default | Description |
|---|---|---|---|
| `source_bot_id` | int | — | Grid bot id (required, immutable) |
| `feedback_bot_id` | int \| None | None (= source) | Feedback receiver |
| `exchange` | str | — | ccxt id (immutable) |
| `market_type` | str | `spot` | `spot` \| `futures` (immutable) |
| `symbol` | str | `BTC/USDT` | (immutable) |
| `broker_key_id` | str | **required** | Broker vault key id |
| `leverage` | int | `10` | Clamped `[1, 125]` |
| `stale_price_sec` | int | `5` | Price freshness threshold, `[0, 60]` (`0` = off) |

### 13.2. Sizing

| Key | Default | Notes |
|---|---|---|
| `unit_volume` | `1.0` | Positive float. **Effective value** — auto-scale bumps it up and persists the result. |
| `unit_volume_mode` | `quote` | `quote` \| `base` |
| `unit_volume_auto_scale` | `True` | Raise `unit_volume` to exchange `min_cost` / `min_amount`; persists the bumped value. Reduce-only orders are never bumped. |
| `max_scale` | `0` | `0` = unlimited; otherwise `uv_eff` (final, after precision bump) ≤ `unit_volume × max_scale`. |
| `margin_buffer_ratio` | `0.05` | Clamped `[0, 1]` |

### 13.3. Timing

| Key | Default | Notes |
|---|---|---|
| `source_timeout` | `30` | Signal lifetime (from grid); overwritten at `start()` from grid's `timeout` capability |
| `poll_divisor` | `5` (form) | `[1, 100]`; `poll_interval = source_timeout / poll_divisor` |
| `broker_poll_interval_ms` | *derived* | `(source_timeout / divisor²) * 1000`, clamped `[50, 2000]` |
| `stale_price_sec` | `5` | See above |

**Note:** `_calc_poll_interval` (models.py) uses a fallback of `10` when `poll_divisor` is `None`; the form always sets `5`. Effective default in the UI: `5`. Rate-calculation differs between `_calc_poll_interval` (`source_timeout / n`) and `_poll_interval_calc` (`source_timeout / n²`) — the former drives the main loop, the latter drives `_poll_broker_price` and the reorder cadence.

### 13.4. Retention

| Key | Default | Notes |
|---|---|---|
| `max_order_rows` | `10000` | Clamped `[100, 100000]` |
| `stats_sample_size` | `500` | Clamped `[100, 10000]` |

### 13.5. Breaker

| Key | Default |
|---|---|
| `breaker_invalid_order` | `3` |
| `breaker_insufficient_funds` | `1` |
| `breaker_network` | `5` |

`auth` is hardcoded to `1` and is not configurable.

### 13.6. Immutable after creation

`exchange`, `market_type`, `symbol`. Changing any of them raises `ValueError` in `process_edit_save` — the form is kept open, mood is `cancel`, nothing is persisted.

`source_bot_id` is preserved from the old config on edit (even if the field changed in the form).

`broker_key_id` **may** be changed — a hot-reload path reinitializes `OrderManager` and re-registers with broker; active managers are finalized first.

---

## 14. Environment variables

| Variable | Value | Effect |
|---|---|---|
| `TBOT_WS_DIAG` | `1` | Broker-side WebSocket diagnostics |
| `TBOT_WS_VERBOSE` | `1` | ccxt `verbose=True` + per-tick order logs |
| `TBOT_KEY_PASSWORD` | string | Fallback password for broker's `KeyVault` |

**Note:** execution itself no longer handles API keys and does not read `TBOT_KEY_PASSWORD` directly. Keys and their passwords are entirely broker's responsibility.

Changing env vars requires a process restart.

---

## 15. Database schema

`data/bot_<id>.db`:

### 15.1. `execution_orders`

| Field | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `position_id` | CHAR | UUID (indexed) |
| `signal_number` | INTEGER | 0..N / -1 / -2 / -999999 / < -100 (indexed) |
| `attempt_n` | INTEGER | |
| `exchange_order_id` | CHAR (index) | |
| `client_order_id` | CHAR | `make_client_order_id(...)` |
| `symbol` | CHAR | |
| `side` | SELECT | `buy` / `sell` |
| `order_type` | SELECT | `limit` / `market` |
| `reduce_only` | BOOL | |
| `price_requested` / `amount_requested` / `amount_placed` / `amount_filled` / `avg_fill_price` / `fee` | FLOAT | `amount_requested` = `raw_eff` after pre-flight (see §5.3) |
| `signal_vol_rate` | FLOAT | |
| `signal_timestamp_scale` | CHAR | default `'tbot_utc_ns'` |
| `signal_smart` | BOOL | |
| `signal_retry` | INTEGER | |
| `exchange_state` | SELECT (index) | `not_sent` \| `open` \| `partial` \| `filled` \| `canceled` \| `expired` \| `rejected` \| `not_found` |
| `reject_reason` / `reject_reason_raw` | CHAR | |
| `signal_timestamp` / `deadline_ns` / `created_ns` / `last_sync_ns` | INTEGER | |
| `feedback_state` | SELECT (index) | `none` \| `pending` \| `sent` \| `abandoned` \| `delivery_failed` \| `superseded` |
| `feedback_attempts` / `feedback_last_error` | INT / CHAR | |
| `ui_acknowledged_at_ns` | INTEGER | D3: attention-row dismissal |

### 15.2. `execution_prices`

| Field | Type |
|---|---|
| `id` | INTEGER PK |
| `timestamp` | INTEGER (index) |
| `tbot_utc_stamp_ns` | INTEGER (index) |
| `bid` / `ask` | FLOAT |
| `symbol` | CHAR |

### 15.3. `execution_stats`

| Field | Type |
|---|---|
| `id` | INTEGER PK |
| `action` | CHAR |
| `latency_ms` | FLOAT |
| `timestamp` / `tbot_utc_stamp_ns` | INTEGER |

### 15.4. Retention (`_cleanup_history`)

Runs every 100 poll iterations with a 60 s TTL guard.

- `execution_orders` — when `total > max_order_rows`, deletes terminal rows (`exchange_state in [filled,canceled,rejected,expired,not_found]`, `feedback_state in [sent,abandoned,delivery_failed,superseded]`) older than 24 h, down to `max_order_rows * 0.9`.
- `execution_prices` — tiered bucketing:
  - 0–5 min: no aggregation.
  - 5–60 min: 30 s buckets.
  - 1–6 h: 5 min buckets.
  - 6–24 h: 30 min buckets.
  - `> 24 h`: deleted.
  - Hard cap `PRICE_MAX_ROWS = 10000`.
- `execution_stats` — keeps only the newest `stats_sample_size` rows.

---

## 16. Reject reasons reference

| Reason | Cause |
|---|---|
| `timeout` | Limit did not fill during phase2; finalize reports `canceled` |
| `stale_signal` | Deadline passed before the signal could be processed |
| `scale_impossible` | Auto-scale returned `None`: `placed <= 0`, `placed < min_amount`, `placed × ref_price < min_cost`, `uv_eff > unit_volume × max_scale`, or `unit_volume_auto_scale=False` in quote mode with no reference price |
| `insufficient_funds` | Balance below `required + buffer` |
| `order_rejected` | Exchange returned generic reject |
| `order_expired` | Exchange returned `expired` |
| `rate_limit` | Exchange rate-limit error |
| `network` | Network error |
| `exchange_error` | Generic exchange error |
| `invalid_symbol` | Symbol not found |
| `position_already_flat` | `-2022` reduce-only rejected — position flat on the exchange |
| `panic_prepare_failed` | `fetch_positions` raised during panic |
| `panic_close_failed` | Broker's `panic_close` returned errors |
| `panic_recovery_unconfirmed` | A panic row was recovered without re-execution |
| `stale_recovery` | `not_sent` recovered after deadline |
| `recovery_no_ref` | No `exchange_order_id` in a recovered row |
| `superseded` | Reorder/rebuild replaced the attempt |
| `close_retry_timeout` | A market-close remainder attempt timed out |
| `forced_complete_after_failure` | Market-close attempts exhausted; grid feedback forced |

---

## 17. Diagnostics and telemetry

### 17.1. Capabilities (provided by Execution)

| Capability | Getter | Purpose |
|---|---|---|
| `execution_stats` | `_get_execution_stats` | All rows from `execution.stats` |
| `active_signals` | `_get_active_signals` | Rows with `exchange_state in ('open','partial')` |
| `attention_signals` | `_get_attention_signals` | Rows matching `ATTENTION_DOMAIN` |
| `failures_summary` | `_get_failures_summary` | `Counter(reject_reason)` over failed rows |
| `latency_stats` | `_get_latency_stats` | `config['latency_stats']` (aggregated) |
| `db_fill_stats` | `_get_db_fill_stats` | Row counts and limits for orders/prices/stats; cached 5 s |

### 17.2. `ATTENTION_DOMAIN`

Polish notation: `AND( OR( OR(not_found, AND(filled, feedback_state IN [abandoned, delivery_failed])), AND(not_sent, recovery_no_ref) ), ui_acknowledged_at_ns = 0 )`

Reads as: *"any row needing manual attention that the user has not yet dismissed"*. Dismissal — via `_close_attention_row(row_id)`, writing `ui_acknowledged_at_ns = now_ns` and bumping `ui_event_counter`.

`ATTENTION_FIELDS = ['id','position_id','signal_number','attempt_n','exchange_state','reject_reason']`.

### 17.3. UI indicators

- **Paused badge** — reason, age since pause, countdown until next check, attempt number.
- **Account card** — `balance_snapshot` (free balances per currency, `unit_volume`, buffer, age). The `unit_volume` shown here is the **effective** value (may differ from the user's original input after the first auto-scale bump — see §12.4).
- **DB fill badge** — orders/prices/stats counts.
- **Attention list** — rows needing attention with a **Close** button per row.
- **Latency aggregation** — `latency_stats` in config, updated every 5 min from the last 200 samples (count, mean, stdev, p95).
- **`ui_event_counter`** — polled via `dcc.Store`; increments trigger a graph/dynamic-block refresh (`poll_execution_events`).

### 17.4. Log prefix

All critical SignalManager logs use `_log_prefix()`:

```
pos=<pos> sig=<sig> attempt=<att>
```

`attempt='?'` before `_new_attempt` (e.g. during panic-prepare).

---

## 18. Failure modes and recovery

### 18.1. Loss of connection to the exchange

- **Price**: broker manages WS reconnect internally; execution sees `stale=True` and skips updates.
- **Orders**: broker manages WS reconnect; execution's `_poll_order_updates` re-registers on broker restart (`registered=False` → `register_key_owner` again, `since_id=0`).
- **REST**: broker rate-limits and queues; failure → reject_reason.

### 18.2. Stuck order

- Order watchdog: if `silent_ns > 2 × reorder_interval` — `fetch_order`.
- 3 consecutive failures — watchdog exits, control to `_finalize`.

### 18.3. Cancel not confirmed

- `_await_terminal_state` timeout → `feedback_state='pending'`, `feedback_last_error='cancel_unconfirmed'`.
- Reconciler picks it up on the next cycle.

### 18.4. Process restart

On `start()`:

- `_flush_pending_feedback_once(fire_and_forget=False)`.
- Restore `execution_paused` (`auth`/`funds` → paused with cooldown; `invalid_order_persistent` → never auto-unpause; others cleared).
- If not paused — `_recover_in_flight()`.
- Start background tasks.

### 18.5. Breaker

- Thresholds reached → `_enter_paused`.
- `_paused_watchdog_loop` probes every 5 s.
- `_can_unpause` based on broker status / `fetch_balance`.

### 18.6. Orphaned orders

Reconciler every 5 s:

- `open`/`partial` outside `active_managers`.
- `recovery_no_ref`.
- `filled` without feedback.

### 18.7. Broker down

- `_ensure_broker` in `_poll_broker_price` and `_poll_order_updates`.
- Rate-limited `restart_bot` and `force_restart_bot` at 10 s intervals.
- While broker is down, `_poll_order_updates` doesn't advance `since_id` and re-registers on recovery.

### 18.8. Capped reduce-only fill (Binance)

A reduce-only order that closes a position smaller than `amount_placed`
is reported by Binance as `filled` with `executedQty == amount_placed`.
`order.filled` therefore overstates the true closed volume.

Handled by three layers:
1. `_finalize` treats `filled < amount_requested × (1−REL_TOL)` as
   partial and calls `_maybe_market_close_remainder`;
2. `_maybe_market_close_remainder` re-probes `fetch_positions` after
   every attempt — `_total_filled` is only a fallback;
3. `_build_feedback_payload_async` — second safety net that overrides
   `real_volume = signal_vol_rate` when the probe confirms flat.

---

## 19. Limitations and warnings

### 19.1. One bot — one position

Execution works with a single `position_id` at a time. Do not share one execution bot across grid bots.

### 19.2. Immutable fields

`exchange`, `market_type`, `symbol` — rejected on save (form stays open, mood `cancel`). `source_bot_id` — preserved from old config regardless of the field.

### 19.3. Broker is required

Execution cannot start if broker is missing. It calls `restart_bot`/`force_restart_bot` (rate-limited) but will not start otherwise.

### 19.4. Stale signals

If grid generates a signal after its deadline:

- **Entry (`0`)** — `not_sent` + `stale_signal` + feedback. Position is blocked (`blocked_positions.add(pos)`), grid is asked to `request_close_position`.
- **Averaging (`>0`)** — same for the row, but grid does its own reset via feedback handling (no blocking).
- **Urgent (`<0`)** — switched to `market` with a fresh timestamp.

### 19.5. Leverage

- Applied by broker via `set_leverage` (called by execution through `OrderManager.set_leverage`).
- On Binance USDT-M requires **One-way Mode** (not Hedge).
- Ignored on spot.

### 19.6. Reduce-only

- All negative signals (`signal_number < 0`) are sent with `reduce_only=True`.
- `-2022` / `ReduceOnly rejected` → `position_already_flat` (not an error).
- Auto-scale never bumps `unit_volume` for reduce-only orders.

### 19.7. Panic close

- Executed via broker capability `panic_close` (composite cancel-all + market-close).
- If the execution bot is not running — the exchange position (if any) will **not** be closed by grid.
- **Always runs**, even while paused.

### 19.8. Feedback retry

- Up to 20 attempts (`FEEDBACK_MAX_ATTEMPTS`).
- Maximum age 24 h → `delivery_failed`.
- One network retry = up to 3 sub-attempts inside `_send_feedback_for_row`.

### 19.9. Rate limiting

- Brokers' `RequestCarousel` provides a unified rate-limit (`MIN_REQUEST_INTERVAL_SEC = 0.05` per worker, ccxt `enableRateLimit=True`).
- Execution does not add its own limit.

### 19.10. Order-update queue depth

- Broker's `OrderUpdateQueue` per owner is bounded at 10 000 records; on overflow, oldest is evicted with a 60-second-throttled warning.
- If execution stops consuming for a long time, older updates are lost and reconciler becomes the primary source of truth.

### 19.11. `_session_invalid_order_count` and repause

- 3 paused-invalid_order episodes in one session → `invalid_order_persistent`.
- Auto-unpause is disabled until manual restart. Restarting the bot (stop/start) resets `_session_invalid_order_count`.

### 19.12. `OrderManager` shim

- `.exchange` / `.markets` / `_ensure_exchange()` deliberately **raise**. Do not rely on them from extensions — use broker's `get_market` / `get_markets`.
- `wait_rate_limit()` and `close()` are no-ops.

### 19.13. Analyst interaction

Execution does **not** call an analyst bot — the analyst gate lives in grid.

### 19.14. `unit_volume` ratchet

Auto-scale only bumps `unit_volume` **upward**. Automatic downward adjustment is deliberately disabled to prevent the bot from sliding below real strategy needs on a temporary `min_cost` drop (e.g. after switching to another symbol). To reduce `unit_volume` — edit it manually in the UI; the bot will re-bump on the next entry signal if the new value is below the exchange minimum. See §12.4.

---

## Appendix A. Phase timing diagram

```
Time →
signal_timestamp                                                deadline_ns
│                                                                       │
├─────────────── phase1 ───────────────┤─────────── phase2 ───────────┤── p3 ┤
│                                       │                          │      │
│  Price = min/max(signal, best)        │  Reorder every N ms      │ grace│
│  One POST create_order                │  Cancel + Replace        │      │
│                                       │                          │      │
└──────────────────────────────────────────────────────────────────────┤──────────────┤──────┘
       execution_reserve_sec                 phase2_dur_ns          phase3_ns
       (capped at 70% phase2)                / poll_divisor         10% total
                                                                   clamp 2–3 s
```

## Appendix B. Signal flow

```
source (grid)                                            execution
────────────                                            ──────────
   signal ──║ _poll_source
                    │
                    ├─ paused? ── skip (except -999999)
                    ├─ stale? ── stale_row + fire-and-forget feedback
                    │             (+ blocked_positions + request_close for sig=0)
                    ├─ blocked? ── skip
                    ├─ dedup in active_managers? ── skip
                    │
                    ▼
              _decide_on_signal
                    │
              ┌─────┴─────┬─────┐
              ▼     ▼     ▼     ▼
            skip  noop rebuild new
                        │     │
                        └     │
              _rebuild_pair   │
                              ▼
                     SignalManager(plan)
                              │
                              ▼
                        mgr.run() ─────┐
                              │      │
                              ▼      │
                        _new_attempt │
                              │      │
                              ▼      │
                     _apply_preflight
                              │      │
                              ▼      │
                        _execute_*   │
                              │      │
                              ▼      │
                        _finalize ◀───┘
                              │
                              ▼
                        feedback ──║ grid
```

## Appendix C. PAUSED state machine

```
       breaker threshold reached
              │
              ▼
        _enter_paused
              │
              ├── persist state in config
              ├── update_bot_status('paused')
              └── finalize active_managers
              │
              ▼
       ┌──────────────┐
       │   PAUSED     │◀──────────┐
       └──────┬───────┘           │
              │                   │
              ▼                   │
       _paused_watchdog_loop      │
              │                   │
              ▼                   │
       _can_unpause?              │
       (fetch_balance probe)      │
              │                   │
       ┌──────┤──────┐            │
       ▼             ▼            │
      True         False ─────────┘
       │
       ▼
   _exit_paused
       │
       ├── clear state
       ├── update_bot_status('running')
       ├── _flush_pending_feedback_once(fire_and_forget=False)
       └── _reconcile_orders()
```

---

## Appendix D. Module structure

```
modules/execution/
├── __init__.py        # from . import models, components
├── __manifest__.py    # depends=["grid"]
├── models.py          # ExecutionBot, ExecutionOrder, PriceTick, ExecutionStats
├── components.py      # UI: form, orders chart, paused badge, attention list
└── lib/
    ├── order_manager.py     # thin proxy to broker + make_client_order_id
    └── signal_manager.py    # per-signal orchestration
```

**Runtime dependencies:**

```
modules/grid/                    (signal, timeout, execution_reserve_sec, constants,
                                  record_execution feedback, request_close_position)
modules/broker/                  (get_latest_price, get_broker_status, get_market,
                                  get_markets, create_*_order, cancel_order, fetch_order,
                                  fetch_open_orders, fetch_positions, fetch_balance,
                                  set_leverage, panic_close, get_order_updates,
                                  cleanup_order_updates, register_key_owner,
                                  unregister_key_owner)
core/                            (registry, base_bot, database, exchange, graphics,
                                  styles, colors, logger)
```

---

## ⚠️ Disclaimer (repeat)

**This is a demonstration / educational version, not a finished product.**

**Trading cryptocurrencies involves significant risk and may result in the loss of your entire invested capital.** This module is provided "as is", without warranty of any kind, for educational and research purposes only. The author assumes no responsibility or liability for any financial loss incurred through the use of this code. **Test on testnet and with minimal capital first. Use at your own risk.**