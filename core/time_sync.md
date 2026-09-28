# TimeSyncService — adaptive colony of NTP filters

Module for precise NTP-based time synchronization with an architecture of
**competing filter instances** combined by consensus, and a
**PI clock model** (anchor + rate) for external consumers.

Operates as an evolutionary system: instances autonomously filter the NTP signal,
reproduce, die, inherit experience — and through this collectively find
optimal time sources without a central dispatcher. In parallel, a
PI controller tracks local clock drift, and a bias integrator
compensates for constant server offset.

---

## Table of contents

- [Logic flow](#logic-flow)
- [Architecture](#architecture)
- [Registry of constants and variables](#registry-of-constants-and-variables)
- [Runtime state](#runtime-state)
- [Public API](#public-api)

---

## Logic flow

### Full cycle of one round (sync thread)

    ┌──────────────────────────────────────┐
    │  _sync_worker (Thread 0 or Thread 1) │
    │  sleep(sync_interval) → new round    │
    └────────────────┬─────────────────────┘
                     │
                     ▼
    ┌────────────────────────────────────────────────────────┐
    │  _get_best_ntp_sample()                                │
    │  1. n_attempts = max(MIN_POPULATION, population_size)  │
    │  2. _poll_servers(n_attempts)                          │
    │  3. raw_snapshot = mean(proposed) per server           │
    │  4. samples_by_srv -= colony_bias[srv]                 │
    │  5. σ_consensus = _compute_consensus_sigma_ns() (MAD)  │
    └────────────────┬───────────────────────────────────────┘
                     │
                     ▼
    ┌────────────────────────────────────────────────────────┐
    │  _poll_servers(n_attempts)                             │
    │  ├─ _query_dns_resolv()      (once per hour)           │
    │  └─ in parallel for each server:                       │
    │     _query_single_server()  — n_attempts times with    │
    │        QUERY_ATTEMPT_SPACING_SEC step                  │
    │     each sample: (delay_net_ns, mid_mono,              │
    │                   t2_utc, unused)                      │
    └────────────────┬───────────────────────────────────────┘
                     │
                     ▼
    ┌────────────────────────────────────────────────────────┐
    │  Consensus.atom_round(...)                             │
    │  ┌──────────────────────────────────────────────────┐  │
    │  │ _refresh_bans_locked()                           │  │
    │  │   • owners of favorite assigned by min(id)       │  │
    │  │   • banned_servers = occupied − {own favorite}   │  │
    │  └──────────────────────────────────────────────────┘  │
    │  ┌──────────────────────────────────────────────────┐  │
    │  │ _process_round_locked()                          │  │
    │  │  ordered = sort(population, by id)               │  │
    │  │  for k, inst in enumerate(ordered):              │  │
    │  │     available = samples_by_srv[attempt_idx=k]    │  │
    │  │     available −= warmup_excluded                 │  │
    │  │     ┌── cold start? → argmin(delay)              │  │
    │  │     │                                            │  │
    │  │     └── no:                                      │  │
    │  │        accepted = {|proposed − ref| ≤ thr}       │  │
    │  │        if not accepted:                          │  │
    │  │           own_history_rejected ← closest_dev     │  │
    │  │        else:                                     │  │
    │  │           new_offset = Σ w·proposed              │  │
    │  │              (w ∝ 1/delay)                       │  │
    │  │           favorite = _select_favorite()          │  │
    │  │              (armed / score / hysteresis)        │  │
    │  │           own_history ← SlewRecord               │  │
    │  │           threshold evolves:                     │  │
    │  │              stdev(diffs) × diff_sigma           │  │
    │  │              ⊓ shrink_floor ⊓ COLONY_FLOOR       │  │
    │  │           matrix_rows.append(row)                │  │
    │  └──────────────────────────────────────────────────┘  │
    │  ┌──────────────────────────────────────────────────┐  │
    │  │ _compute_history_votes()                         │  │
    │  │   short_vote = median(last 15 observed)          │  │
    │  │   drift_slope = OLS over 300 applied             │  │
    │  │   drift_pred  = last applied + rate·Δ            │  │
    │  └──────────────────────────────────────────────────┘  │
    │  ┌──────────────────────────────────────────────────┐  │
    │  │ _build_consensus_matrix()                        │  │
    │  │   size = max L: at least L rows of length ≥ L    │  │
    │  │   compact = _select_diverse_cells(rows, size)    │  │
    │  │   drift comp: proposed − rate·(mid − t_ref)      │  │
    │  │   consensus_offset = mean(L² cells)              │  │
    │  └──────────────────────────────────────────────────┘  │
    │  ┌──────────────────────────────────────────────────┐  │
    │  │ post-gate (if DISABLE_POST_GATE=0)               │  │
    │  │   pred = drift_pred || short_vote                │  │
    │  │   delta = consensus − pred                       │  │
    │  │   limit = POST_GATE_K · σ_consensus              │  │
    │  │   if |delta| > limit:                            │  │
    │  │      if streak ≥ POST_GATE_FORCE_APPLY_AFTER:    │  │
    │  │         force-apply, streak = 0                  │  │
    │  │      else:                                       │  │
    │  │         applied = False                          │  │
    │  │         rejected_offset_ns = consensus           │  │
    │  │   else: streak = 0                               │  │
    │  └──────────────────────────────────────────────────┘  │
    │  applied_offsets.appendleft((offset, t_ref_mono))      │
    │  ┌──────────────────────────────────────────────────┐  │
    │  │ _check_triggers_locked()                         │  │
    │  │   tick++                                         │  │
    │  │   capture σ_avg (warmup rules 1/2b/2/3)          │  │
    │  │   dominant_favorite? → _spawn_locked             │  │
    │  │   deathbed? → _spawn_locked                      │  │
    │  │   dead = [inst: low_accept_windows ≥ 5]          │  │
    │  │   → _kill_locked (divine birth when pop < 5)     │  │
    │  │   spread gate: median(spreads) < 2·median(σ_avg) │  │
    │  └──────────────────────────────────────────────────┘  │
    └────────────────┬───────────────────────────────────────┘
                     │
                     ▼
    ┌────────────────────────────────────────────────────────┐
    │  _apply_new_sync_locked(best_mono, best_utc, ...)      │
    │  ├─ not applied? → reject-streak logic:                │
    │  │    streak ≤ 1  → anchor = gate opinion              │
    │  │    streak ≥ 2  → anchor = predicted + KP_REJECT·err │
    │  │    streak ≥ REJECT_RATE_RESET_STREAK → reset rate   │
    │  ├─ primary sync? → anchor = best_utc, rate = prior    │
    │  ├─ dt < min_pll_interval? → skip PLL (is_stale=True)  │
    │  ├─ |phase_error| > 100 ms? → reset anchor + rate      │
    │  └─ normal update:                                     │
    │       predicted = anchor + rate·dt                     │
    │       phase_err = target − predicted                   │
    │       kp = 0.40 if |phase_err|>20 ms, else 0.10        │
    │       anchor += kp·phase_err                           │
    │       _apply_rate_sign_constancy_step_locked()         │
    │       _apply_drift_confirmation_locked()               │
    │          rate ← α·rate + (1−α)·slope                   │
    │       _phase_err_window.appendleft(phase_err)          │
    │       _publish_clock_and_metrics_locked()              │
    │       _update_bias_history_locked()                    │
    │       persist drift → append_drift_sample()            │
    └────────────────────────────────────────────────────────┘

### PLL scheme (clock model)

                          ┌─────────────────────────┐
                          │  UTC = mono + offset    │
                          │  offset(t) = anchor_o   │
                          │    + rate·(t−anchor_m)  │
                          └───────────┬─────────────┘
                                      │
    new_target_offset ◄── consensus_offset ◄── _process_round_locked
                                      │
                                      ▼
                          ┌─────────────────────────┐
                          │ dt = t_now − anchor_m   │
                          │ predicted = anchor_o +  │
                          │            rate·dt      │
                          │ phase_err = target −    │
                          │             predicted   │
                          └───────────┬─────────────┘
                                      │
                ┌─────────────────────┼─────────────────────┐
                │                     │                     │
                ▼                     ▼                     ▼
        |phase_err|>100ms       |phase_err|>20ms        normal
          RESET:                 kp = 0.40            kp = 0.10
          anchor=target          boosted             standard
          rate = prior           catch-up

### Favorite selection scheme

    _select_favorite(inst, accepted):
    ┌──────────────────────────────────────────────────────────┐
    │  delays = accepted − inst.banned_servers                 │
    │                                                          │
    │  1. armed-lock active AND favorite ∈ delays → keep       │
    │                                                          │
    │  2. no delays → keep current favorite                    │
    │                                                          │
    │  3. score = d_norm + s_norm:                             │
    │     d_norm = delay / median(delay)                       │
    │     s_norm = σ_s / median(σ)  (if σ-history is enough)   │
    │     else delay only (warmup)                             │
    │                                                          │
    │  4. favorite absent → forced change without hysteresis   │
    │                                                          │
    │  5. hysteresis: change only if                         │
    │     score_new < score_cur · (1 − FAVORITE_HYST)          │
    └──────────────────────────────────────────────────────────┘

### Instance lifecycle

         COLD START                         MATURE
       argmin(delay)              threshold narrowed, σ_avg captured
            │                              │
            ▼                              ▼
     ┌──────────────┐               ┌──────────────┐
     │  warmup:     │  15-45 ticks  │   work:      │
     │  σ_avg=None  │──────────────►│  trusted     │
     │  trusted=no  │               │  votes       │
     └──────────────┘               └──────┬───────┘
            ▲                              │
            │                              ├── dominant_favorite → spawn
            │                              │
            │                              ├── deathbed → spawn + die
            │                              │
            │                              └── low_accept_windows ≥ 5
            │                                       │
            │                                       ▼
            │                              ┌──────────────┐
            └──────────────────────────────│  death       │
                   pop < MIN_POPULATION    │  (divine?)   │
                   → divine birth          └──────────────┘

---

## Architecture

### Three levels

    ┌──────────────────────────────────────────────────────────┐
    │              TimeSyncService (orchestration)              │
    │  PLL (anchor + rate) · get_utc_ns · get_clock_snapshot    │
    │  watchdog · keep-awake · start/stop · colony bias         │
    └──────────────────────────┬───────────────────────────────┘
                               │
                               ▼
    ┌──────────────────────────────────────────────────────────┐
    │                   Consensus (observer)                    │
    │   population · occupied · reproduction_allowed            │
    │   pre/post gate · history votes · spread gate             │
    │   lineage queue (divine birth)                            │
    └──────────────────────────┬───────────────────────────────┘
                               │
           ┌───────────────────┼───────────────────┐
           ▼                   ▼                   ▼
    ┌────────────┐      ┌────────────┐      ┌────────────┐
    │ Instance 0 │      │ Instance 1 │      │ Instance N │
    │  favorite  │      │  favorite  │      │  favorite  │
    │  history   │      │  history   │      │  history   │
    │  threshold │      │  threshold │      │  threshold │
    │  σ_avg     │      │  σ_avg     │      │  σ_avg     │
    └────────────┘      └────────────┘      └────────────┘
                               │
                               ▼
                  ┌─────────────────────────┐
                  │      NTP servers        │
                  └─────────────────────────┘

| Component | Responsibility |
|---|---|
| **TimeSyncService** | PLL clock model, `get_utc_ns()`, `get_clock_snapshot()`, watchdog, keep-awake, colony bias, singleton |
| **Consensus** | Holds colony, resolves server collisions, triggers reproduction, votes, applies pre/post gate |
| **AlgorithmInstance** | Autonomous filter: own threshold, own history, own favorite, own σ_avg |
| **SlewRecord** | Immutable round record for telemetry |
| **ClockSnapshot** | Clock model snapshot for external consumers |
| **2 sync threads** | Parallel rounds offset by half a period |
| **Watchdog** | Restarts dead sync threads, diagnostics |
| **Keep-awake** | Windows: disable Power Throttling; Linux: check systemd mask |

---

## Registry of constants and variables

### NTP servers

| Name | Value | Notes |
|---|---|---|
| `DEFAULT_NTP_SERVERS` | 16 active | List of public NTP servers. Commented-out entries excluded due to bias/timeout. |
| `NTP_EPOCH_OFFSET_SEC` | 2208988800 | Offset from NTP epoch (1900) to Unix (1970), sec. |
| `NTP_PACKET_SIZE` | 48 | NTPv4 packet size, bytes. |
| `NTP_PORT` | 123 | NTP port. |
| `PER_SERVER_HISTORY` | `max(len(DEFAULT_NTP_SERVERS), 10)` = 16 | Length of per-server min-delay queue and accept_window. |
| `QUERY_ATTEMPT_SPACING_SEC` | 1.0 | Spacing of attempts within a round, sec. 5 attempts → span 4 s. |
| `QUERY_ATTEMPT_SPACING_NS` | 1_000_000_000 | Same in ns. |
| `NTP_QUERY_TIMEOUT_SEC` | 2 | Timeout of a single NTP request. |
| `NTP_QUERY_TIMEOUT_SAFE_SEC` | 2 | Extra margin for round timeout. |
| `DNS_QUERY_TIMEOUT_SEC` | 5 | DNS resolution timeout. |
| `NTP_RESOLVING_TIMEOUT_NS` | 3.6e12 (1 h) | DNS re-resolution period. |
| `VALIDATE_ORIGIN` | `'1'` from env | Origin echo check. Can be disabled via `TIME_SYNC_VALIDATE_ORIGIN=0`. |

### Sync cycle and threads

| Name | Value | Notes |
|---|---|---|
| `DEFAULT_INITIAL_INTERVAL_SEC` | 60 | Main interval between rounds. |
| `SYNC_THREAD_SLOTS` | 2 | Number of parallel sync threads. |
| `second_sync_thread_delay` | `sync_interval // 2` | Offset of second thread (30 s). |
| `_min_pll_update_interval_ns` | `max(delay//2, 5)·1e9` | Protection against "stuck" PLL updates (15 s). |
| `WATCHDOG_INTERVAL_SEC` | 30 | Thread liveness check frequency. |
| `WATCHDOG_STOP_JOIN_SEC` | 3.0 | Watchdog join timeout on stop. |
| `SYNC_THREAD_STOP_JOIN_SEC` | 15.0 | Overall stop timeout for sync threads. |
| `PRE_START_JOIN_SEC` | 5.0 | Join timeout of "leftover" threads on start. |
| `KEEP_AWAKE_REFRESH_SEC` | 30 | Re-setting ES_SYSTEM_REQUIRED. |

### PLL

| Name | Value | Notes |
|---|---|---|
| `PLL_KP` | 0.10 | Fraction of phase_error added to offset per round (normal). |
| `PLL_KP_MEDIUM` | 0.40 | Boosted KP during medium jump. |
| `PLL_KP_REJECT` | 0.30 | KP applied to raw when post-gate streak ≥ 2. |
| `PLL_KI` | 0.01 | Fraction of phase_error/dt added to rate. |
| `PLL_RATE_LIMIT` | 100e-6 (±100 ppm) | Drift estimate limit. |
| `PHASE_JUMP_THRESHOLD_NS` | 100_000_000 (100 ms) | Reset threshold for anchor + rate. |
| `PHASE_MEDIUM_JUMP_NS` | 20_000_000 (20 ms) | Boosted KP threshold. |
| `RATE_SPRING_BASE_PPM` | 0.02 | Base rate step when prior σ is unavailable. |
| `RATE_SPRING_MAX_PPM` | 0.10 | Maximum rate step when prior σ is unavailable. |
| `RATE_SPRING_SIGMA_FRACTION_BASE` | 0.05 | Base step = 0.05·σ_prior. |
| `RATE_SPRING_SIGMA_FRACTION_MAX` | 0.25 | Max step = 0.25·σ_prior. |
| `RATE_SPRING_T_SCALE` | 3.0 | Confidence = (|t|−T_THRESHOLD)/SCALE. |
| `RATE_PRIOR_PULL` | 0.00 | Extra pull of rate toward prior (optional). |
| `RATE_CLAMP_FROM_PRIOR_PPM` | 2.0 | ±window around prior for rate. |
| `RATE_DETECT_WINDOW_N` | 30 | Length of phase_errors window. |
| `RATE_DETECT_MIN_N` | 20 | Minimum for statistics. |
| `RATE_T_THRESHOLD` | 2.5 | |t| threshold of the mean. |
| `RATE_Z_THRESHOLD` | −2.0 | Runs z: negative = sign sticking. |

### Colony bias

| Name | Value | Notes |
|---|---|---|
| `COLONY_BIAS_GAIN` | 0.05 | Integrator speed per round. |
| `COLONY_BIAS_MAX_NS` | 50_000_000 (50 ms) | Compensation limit. |
| `COLONY_BIAS_DECAY` | 0.99 | Decay of inactive servers. |
| `COLONY_BIAS_DECAY_FLOOR` | 10_000 (10 µs) | Below this — bias is removed. |
| `K_THRESHOLD` | 1.0 | Soft-threshold: \|delta\| > K·σ_srv. |

Note: the minimum history per server for bias estimation is
`HISTORY_MAX_LEN // 3` and there is no separate constant for it.

### Drift confirmation

| Name | Value | Notes |
|---|---|---|
| `DRIFT_CONFIRM_RATE_DIVERGE_PPM` | 3.0 | Threshold of rate↔slope divergence. |
| `DRIFT_CONFIRM_ALPHA` | 0.5 | 1.0 — off; 0.0 — full replacement rate←slope. |
| `DRIFT_SEED_MIN_SAMPLES` | 5 | Minimum for trusting the prior median. |
| `DRIFT_COLD_START_DIVERGE_PPM` | 3.0 | Threshold for resetting rate on cold start. |
| `DRIFT_OUTLIER_K` | 3.0 | K·MAD for filtering prior from DB. |

### Clock Snapshot / TTL

| Name | Value | Notes |
|---|---|---|
| `CLOCK_SNAPSHOT_TTL_BASE_SEC` | 2.0 | Base TTL = 2·sync_interval. |
| `CLOCK_SNAPSHOT_TTL_QUALITY_FLOOR` | 0.25 | Lower bound of quality (noisy σ_recent). |
| `CLOCK_SNAPSHOT_TTL_QUALITY_CEIL` | 2.0 | Upper bound of quality (quiet σ_recent). |
| `CLOCK_SNAPSHOT_TTL_ABS_MIN_SEC` | 30.0 | Absolute TTL minimum. |
| `CLOCK_SNAPSHOT_TTL_ABS_MAX_SEC` | 600.0 | Absolute TTL maximum. |
| `CLOCK_SNAPSHOT_TTL_RECENT_N` | 15 | Window of σ_recent (last rounds). |
| `CLOCK_SNAPSHOT_ACCURACY_DEFAULT_NS` | 5_000_000 (5 ms) | Fallback accuracy before statistics accumulate. |
| `CLOCK_SNAPSHOT_PRECISION_CACHE_MAX` | 16 | LRU cache limit of precision requests. |

### Colony (filter and threshold)

| Name | Value | Notes |
|---|---|---|
| `THRESHOLD_MIN_NS` | 1_000 (1 µs) | Absolute floor of threshold, protection from zero. |
| `COLONY_THRESHOLD_FLOOR_NS` | 2_500_000 (2.5 ms) | Colony threshold floor. Protection against "collapse". |
| `THRESHOLD_SHRINK_FLOOR` | 0.99 | No faster than 1% decrease per round. |
| `COHERENCE_THRESHOLD_NS` | 5_000_000 (5 ms) | Seed threshold of the first round. |
| `HISTORY_MAX_LEN` | 100 | Length of own_history / own_history_rejected. |
| `ACCEPT_WINDOW_SIZE` | = PER_SERVER_HISTORY = 16 | Accept rate window. |
| `DEATH_LOW_WINDOWS` | 5 | Low windows in a row → death. |
| `DEATH_ACCEPT_THRESHOLD` | 0.05 | Threshold of a "low" accept rate. |
| `REPRODUCTION_LAG_L` | = DEATH_LOW_WINDOWS = 5 | Min ticks between births. |
| `FAVORITE_HYST` | 0.15 | Hysteresis of favorite change (15%). |

### Dominance / warmup σ_avg

| Name | Value | Notes |
|---|---|---|
| `ALPHA_SIGNIFICANCE` | 0.1 | Significance level in M = 2·ln(N/α)/Δ². |
| `DOMINANT_MIN_HISTORY` | = ACCEPT_WINDOW_SIZE = 16 | M_min — lower bound of observations. |
| `SIGMA_WARMUP_RECORDS` | = SPREAD_HISTORY_MIN = 5 | ✓ records per server for σ_avg. |
| `SIGMA_WARMUP_TIMEOUT_MULT` | 3 | Warmup timeout = 3×5 = 15 ticks. |
| `SIGMA_WARMUP_MIN_SURVIVORS` | 2 | Rule 2b: 2 full servers are enough. |
| `WARMUP_MIN_SURVIVORS` | 2 | Minimum available servers to leave warmup. |
| `DOMINANT_STDEV_RATIO` | 0.7 | σ_X < 0.7·σ_others — dominance condition. |
| `DOMINANT_MIN_SERVERS` | 2 | Min servers in the last record. |
| `DOMINANT_MIN_DEVS` | 2 | Min observations for stdev. |
| `SIGMA_AVG_SANITY_MAX_NS` | 10_000_000 (10 ms) | Sanity: σ_avg above → capture cancelled. |

### Population and divine

| Name | Value | Notes |
|---|---|---|
| `MIN_POPULATION` | 5 | Below — colony degenerates, divine. |
| `DIVINE_QUEUE_MAX` | = MIN_POPULATION = 5 | Maximum lineage roots. |
| `DIVINE_ACCEPT_RATE_THRESHOLD` | 0.1 | accept_rate < 0.1 → stuck. |
| `MAX_POPULATION_RATIO` | 0.7 | Fraction of the number of servers. |
| `MAX_POPULATION_MIN_SERVERS` | 7 | Below — max_population = len(servers). |

### History and drift votes

| Name | Value | Notes |
|---|---|---|
| `HISTORY_VOTE_SHORT_WINDOW` | 15 | Short median window over `_recent_observed`. |
| `HISTORY_VOTE_MIN_SAMPLES` | = SPREAD_HISTORY_MIN = 5 | Before this short_vote is inactive. |
| `DRIFT_WINDOW` | 300 | Linear regression window over `applied_offsets`. |
| `DRIFT_MIN_SAMPLES` | 30 | Before this drift_pred is inactive. |

Note: drift persistence to the DB is done when
`tick − _last_drift_persist_tick ≥ DRIFT_WINDOW` and
`len(applied_offsets) ≥ DRIFT_WINDOW`; there is no separate interval constant.

### Consensus gate

| Name | Value | Notes |
|---|---|---|
| `POST_GATE_K` | 1.0 | Multiplier of σ_consensus in post-gate. |
| `POST_GATE_FORCE_APPLY_AFTER` | 50 | After this many consecutive rejects — force-apply. |
| `REJECT_RATE_RESET_STREAK` | 4 | Consecutive rejects triggering rate reset. |
| `REJECT_RATE_RESET_GAIN` | 1.0 | 1.0 hard reset; 0.3 soft pull. |
| `REJECT_RATE_RESET_MIN_DIVERGE_PPM` | 1.0 | Do not reset if rate is already close to prior. |
| `DISABLE_POST_GATE` | 0 | 1 — disable post-gate (current: enabled). |
| `DISABLE_SHORT_VOTE` | = DISABLE_POST_GATE | Tied to post-gate flag. |
| `DISABLE_DRIFT_VOTE` | = DISABLE_POST_GATE | Tied to post-gate flag. |
| `CONSENSUS_MATRIX_MIN` | 2 | Minimum size of the L×L matrix. |
| `SPREAD_HISTORY_LEN` | = ACCEPT_WINDOW_SIZE = 16 | Queue of stdev(offsets). |
| `SPREAD_HISTORY_MIN` | = REPRODUCTION_LAG_L = 5 | Minimum rounds before gate activation. |
| `REPRODUCTION_SPREAD_MULT` | 2.0 | median(spreads) < MULT · median(σ_avg). |

### Environment flags

| Name | Default | Notes |
|---|---|---|
| `TIME_SYNC_KEEP_AWAKE` | `'1'` | `0` — disable keep-awake (laptop, battery). |
| `TIME_SYNC_VALIDATE_ORIGIN` | `'1'` | `0` — disable origin echo check. |
| `TIME_SYNC_MODE` | — | `local\|daemon\|attached\|peer` (Sprint 2, not yet implemented). |

---

## Runtime state

### TimeSyncService

| Name | Init | Type | Notes |
|---|---|---|---|
| `ntp_servers` | list | list[str] | Active servers. |
| `ntp_servers_resolved` | `{}` | dict[str,str] | DNS → IP cache. |
| `ntp_servers_last_resolved_ns` | `-NTP_RESOLVING_TIMEOUT_NS` | int | Last resolution timestamp. |
| `sync_interval` | 60 | int | Round period. |
| `second_sync_thread_delay` | 30 | int | Offset of second thread. |
| `_server_meta` | `{}` | dict[str,ServerMeta] | Metadata of NTP packets. |
| `_executors_shutdown` | False | bool | State of thread pools. |
| `dns_fail_servers` | `set()` | set | Servers that failed DNS. |
| `running` | False | bool | Running flag. |
| `is_synced_event` | Event | — | Set after first sync. |
| `_stop_event` | Event | — | Stop signal. |
| `_threads` | `{}` | dict[int,Thread] | Sync threads by slot. |
| `_last_success_mono_ns` | 0 | int | 0 = no success yet. |
| `_last_stalled_servers` | `()` | tuple | Diagnostics. |
| `_delay_history` | deque(20) | deque[int] | All round delays. |
| `_phase_err_window` | deque(30) | deque[int] | Phase errors for sign-constancy. |
| `_per_server_delay` | defaultdict(deque 16) | dict | Min-delay per server. |
| `_anchor_offset` | 0 | int | UTC reference at anchor. |
| `_anchor_mono` | 0 | int | Anchor point (mono_ns). |
| `_rate` | 0.0 | float | Drift estimate (dimensionless). |
| `_clock_snapshot` | `(0,0,0.0)` | tuple | Lock-free snapshot for get_utc_ns. |
| `_clock_snapshot_ttl_ns` | 120e9 | int | Current snapshot TTL. |
| `_clock_snapshot_accuracy_ns` | 5e6 | int | Current snapshot accuracy. |
| `_clock_snapshot_full` | 5-tuple | tuple | Atomic snapshot (anchor, offset, rate, ttl, acc). |
| `_precision_cache` | OrderedDict | — | LRU cache of precision requests. |
| `_target_offset` | 0 | int | Target offset (telemetry). |
| `_last_phase_error` | None | Optional[int] | Last phase_error. |
| `_slew_error_history` | deque(100) | SlewRecord | Consensus-level history. |
| `_colony_bias` | `{}` | dict[str,int] | Accumulated bias. |
| `_last_cold_gen` | 0 | int | Colony generation. |
| `_raw_proposed_history` | deque(100) | tuple | Raw history for bias. |
| `_diff_sigma` | 1.0 | float | Threshold multiplier. |
| `_last_diff_threshold` | None | Optional[int] | Last round threshold (seed). |
| `_consensus` | Consensus | — | Colony. |
| `_rate_prior` | 0.0 | float | Prior from drift DB. |
| `_rate_spring_base_ppm` | 0.02 or 0.05·σ_prior | float | Rate step base. |
| `_rate_spring_max_ppm` | 0.1 or 0.25·σ_prior | float | Rate step max. |
| `_last_drift_persist_tick` | 0 | int | Drift persist pause. |

### Consensus

| Name | Init | Type | Notes |
|---|---|---|---|
| `sync_interval` | 60 | int | Sync interval (for ns/round conversion). |
| `servers` | list | list[str] | Server list (fixed). |
| `max_population` | calc | int | Population ceiling. |
| `divine_queue_max` | max(5, max_pop) | int | Lineage ceiling. |
| `diff_sigma` | 1.0 | float | stdev multiplier. |
| `population` | `[init_pop]` | list[AlgorithmInstance] | Live instances. |
| `_next_id` | init_pop | int | Id counter. |
| `_tick` | 0 | int | Round counter. |
| `_pending_logs` | `[]` | list | Deferred logs. |
| `reproduction_allowed` | True | bool | Spread gate flag. |
| `_spreads` | deque(16) | deque | stdev(offsets) history. |
| `_mean_spread_ns` | None | Optional[float] | median(spreads). |
| `noise_ref_ns` | None | Optional[float] | median(σ_avg) of live instances. |
| `_recent_observed` | deque(300) | deque[int] | All observed consensus offsets. |
| `applied_offsets` | deque(300) | deque[Tuple[int,int]] | (offset, t_ref_mono) trajectory of applied. |
| `_last_short_vote` | None | Optional[int] | Short vote. |
| `_last_drift_prediction` | None | Optional[int] | Drift prediction. |
| `_last_drift_slope` | None | Optional[float] | Regression slope, ns/round. |
| `_last_apply_mono_ns` | 0 | int | Mono of last applied (for extrapolation). |
| `_consecutive_rejects` | 0 | int | Post-gate reject streak. |
| `_occupied` | `set()` | set | Occupied servers. |
| `cold_start_generation` | 0 | int | Full colony respawn counter. |
| `_lineage_queue` | `[id...]` | list[int] | Queue of lineage roots. |

### AlgorithmInstance

| Name | Init | Type | Notes |
|---|---|---|---|
| `id` | n | int | Unique id. |
| `lineage_id` | n / parent | int | Root id of the lineage. |
| `favorite` | None | Optional[str] | Currently bound server. |
| `banned_servers` | `set(_occupied)` | set | Others' favorites (only selection ban). |
| `own_history` | deque(100) | SlewRecord | Accepted rounds. |
| `own_history_rejected` | deque(100) | SlewRecord | All rejected by filter. |
| `own_reference_offset` | None | Optional[int] | Current reference. |
| `own_threshold_ns` | None | Optional[int] | Filter threshold. |
| `own_sigma_avg_ns` | None | Optional[int] | Captured σ_avg (once). |
| `armed_lock_until_tick` | `-1e9` | int | Until which tick we hold favorite. |
| `warmup_excluded` | `set()` | set | Permanently banned servers. |
| `warmup_started_tick` | `_tick` | int | Warmup start. |
| `warmup_stuck_logged` | False | bool | Anti-spam flag. |
| `accept_window` | deque(16) | deque[bool] | Accept rate window. |
| `low_accept_windows` | 0 | int | Consecutive low windows. |
| `deathbed_used` | False | bool | Already gave birth to deathbed. |
| `born_tick` | `_tick` | int | Birth tick. |
| `reproduced_count` | 0 | int | How many children produced. |
| `max_offspring` | 2 | int | Children limit. |
| `last_reproduced_tick` | `-1e9` | int | Lag control. |
| `last_selection_mode` | None | Optional[str] | `warmup\|score\|armed_lock\|no_free\|single\|*_forced\|*_hysteresis\|*_hold\|*_no_favorite` |
| `last_best_candidate` | None | Optional[str] | Last round candidate. |
| `last_scores` | None | Optional[Dict[str,float]] | Scores of all candidates. |

### NamedTuple

**ServerMeta**: leap, version, mode, stratum, poll, precision, root_delay_raw, root_disp_raw, ref_id, ref_ts_ns, last_update_mono_ns. Derived: `root_delay_ns`, `root_disp_ns` (16.16 fixed-point).

**SlewRecord**: timestamp_ns, diff_ns, threshold_ns, instance_id, favorite, servers, ref_ns, is_cold_start, matrix, matrix_meta, is_stale, rejected_offset_ns.

**ClockSnapshot**: anchor_mono_ns, anchor_offset_ns, rate, ttl_ns, accuracy_ns.

---

## Public API

    svc = TimeSyncService.get_instance()

    svc.start()
    svc.wait_for_first_sync(timeout=30)
    svc.stop()

    svc.get_utc_ns()                                 # lock-free UTC ns
    svc.get_utc_ns_with_precision(precision_ns)      # UTC ns | None (strict SLA)
    svc.get_clock_snapshot()                         # ClockSnapshot | None
    svc.get_clock_snapshot(precision_ns=500_000)     # TTL fits precision
    TimeSyncService.utc_from_snapshot(snap, now_mono)  # pure function
    svc.get_sync_telemetry()                         # full dump

### UTC restoration from a snapshot

    utc = now_mono + anchor_offset_ns + round(rate · (now_mono − anchor_mono_ns))

Guarantee: for `now_mono ∈ [anchor_mono, anchor_mono + ttl_ns]`:
`|utc_restored − utc_true| ≤ accuracy_ns` provided that consensus
has not degraded more than at the moment of the snapshot.

---

**Accuracy ceiling:**

| Source | Accuracy |
|---|---|
| Public NTP + algorithms | 0.7–1.5 ms |
| + PLL + colony bias + gates | 0.4–0.8 ms |
| + GPS server with calibration | 0.05–0.3 ms |
| + PTP with HW timestamping | 1–10 µs |

---