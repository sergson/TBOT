# TimeSyncService — adaptive colony of NTP filters

Precise time synchronization module based on NTP with the architecture of
**competing filter-instances** combined by consensus, and a
**PI clock model** (anchor + rate) for external consumers.

It works as an evolutionary system: instances autonomously filter the NTP signal,
reproduce, die, inherit experience — and through this collectively find
optimal time sources without a central dispatcher. In parallel, the
PI controller tracks the drift of the local clock, and the bias integrator
compensates for the permanent server shift.

---

## Table of Contents

- [Purpose](#purpose)
- [Architecture](#architecture)
- [How it works](#how-it-works)
- [Key mechanisms](#key-mechanisms)
- [Telemetry](#telemetry)
- [Public API](#public-api)
- [Achieved characteristics](#achieved-characteristics)
- [What works, what doesn't](#what-works-what-doesnt)
- [Network topology change](#network-topology-change)
- [Known pitfalls](#known-pitfalls)
- [Roadmap](#roadmap)
- [References](#references)

---

## Purpose

The module solves the problem of obtaining **precise UTC time** under conditions
where NTP servers:

- have different delays (0 ms local, 15 ms near, 60–80 ms far);
- have different jitter (from ±1 ms for the local one to ±10 ms for the far ones);
- may be shifted (bias ≠ 0) or noisy (bias ≈ 0, but σ large);
- some may drop out temporarily or permanently.

**Target scenarios:**
- trading systems where a stable time scale is important;
- distributed computations with consistency requirements;
- any service where the OS system clock is insufficient, and it is important to pass
  a consistent clock snapshot to external consumers (UI, IPC, trading modules).

**Current accuracy:** `offset vs NTP ≈ ±0.3 ms` with `σ_avg ≈ 1.4 ms`
(on 4 servers, 11 hours of observation). The ceiling on public NTP — ~0.7–1.5 ms;
the main lever for improvement is infrastructure (GPS), not the algorithm.

---

## Architecture

### Three levels

```
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
              │ .4 · ntp1 · pool · …    │
              └─────────────────────────┘
```

### Components

| Component | Responsibility |
|---|---|
| **TimeSyncService** | PLL clock model, `get_utc_ns()`, `get_clock_snapshot()`, watchdog, keep-awake, colony bias, singleton |
| **Consensus** | Holds the colony, resolves server collisions, triggers reproduction, votes, applies pre/post gate |
| **AlgorithmInstance** | Autonomous filter: its own threshold, its own history, its own favorite, its own σ_avg |
| **SlewRecord** | Immutable record of a round for telemetry (per-server slice) |
| **ClockSnapshot** | Clock-model snapshot for external consumers (anchor + rate + TTL + accuracy) |
| **2 sync threads** | Parallel rounds with an offset by half a period — for uniformity of PLL updates |
| **Watchdog** | Restarts dead sync threads, diagnoses network "hangs" |
| **Keep-awake** | Windows: remove Power Throttling + ES_SYSTEM_REQUIRED; Linux: check systemd mask |

### Key invariants

1. **Consensus does not intervene in the operation of live instances directly.** Influence
   only through:
   - **Birth** (`_spawn_locked`) — filter of inherited bans.
   - **Divine birth** (`_spawn_divine_locked`) — new lineage root.
   - **Death** (`_kill_locked`) — removal from the population.

2. **The clock snapshot is consistent.** `get_clock_snapshot()` returns
   `(anchor_mono, anchor_offset, rate, ttl, accuracy)` — all fields are written
   in a single critical section. UTC restoration is a pure function.

3. **PLL updates are protected from "stuck" rounds.** If `dt < min_pll_interval`
   (half the interval between sync threads), the update is skipped. Otherwise
   the I-correction at `dt ≈ 0` would instantly saturate the rate.

---

## How it works

### One round

```
round(server_results, t_ref_mono):
    1. _refresh_bans_locked()        # resolve collisions for servers
    2. _process_round_locked()        # each instance filters + voting
    3. _check_triggers_locked()       # reproduction, death, gate, σ_avg
```

### Inside an instance

```
available = server_results − banned_servers − warmup_excluded
filtered  = filter(available, own_threshold_ns)
accepted  = those with |dev| ≤ threshold
new_offset = weighted average (weights ∝ 1/delay)
favorite  = score (d_norm + s_norm) or armed_lock
threshold = max(diff_sigma × stdev(diff_ns), SHRINK_FLOOR × threshold)
```

The threshold narrows **by itself**: the smaller the spread of `diff`, the stricter the filter,
and the less noise in the following `diff`. Positive feedback.
Rate limit of narrowing (`THRESHOLD_SHRINK_FLOOR = 0.95`) — not more than
5% per round.

### Inside Consensus

```
# 1. Trusted votes (or fallback to warming-up)
trusted = instances with captured σ_avg
if not trusted: trusted = outputs_by_inst
outputs = [offset for _, offset in trusted]

# 2. Reference point for the gate
prediction = drift_prediction or short_vote

# 3. Pre-gate: discard outlier votes
if prediction and noise_ref and len(outputs) >= 2:
    filtered = [o for o in outputs if |o − prediction| ≤ PRE_GATE_K · noise_ref]
    outputs = filtered or []           # if empty — hold

# 4. Voting: outputs + short_vote + drift_prediction
median_offset = median(votes)

# 5. Post-gate: clamp the step
if prediction and noise_ref and len(applied) >= 3:
    delta = median_offset − prediction
    if |delta| > POST_GATE_K · noise_ref:
        median_offset = prediction + sign(delta) · POST_GATE_K · noise_ref
```

`noise_ref` — median σ_avg of mature instances. `short_vote` — median
of the last 15 applied median_offset. `drift_prediction` — extrapolation
one step forward by linear regression of the last 300 points.

---

## Key mechanisms

### 1. Cold start

The first round of an instance: `favorite = argmin(delay)`. Usually — the closest
server (0 ms local). This gives the minimum base error,
because the random error `mid_mono = t0 + delay/2` is proportional
to the delay.

### 2. Multi-phase NTP polling

Each server is polled `QUERY_ATTEMPTS = 5` times per round with a spacing
`QUERY_ATTEMPT_SPACING_SEC = 1.0`. All successful attempts are merged by
delay-weighted average into one sample:
- `delay_min` — minimum delay (used as server weight);
- `mid_mono_avg`, `t2_utc_avg`, `proposed_avg` — weighted averages.

Maximum span = 4 s. With typical monotonic drift ~50 ppm this is
≈200 μs — comparable to `THRESHOLD_MIN_NS`, cannot be widened further.

### 3. DNS resolution with cache

`_query_dns_resolv()` once per hour (`NTP_RESOLVING_TIMEOUT_NS`) resolves
all NTP names to IP via `_DNS_poll_executor`. Results are cached in
`ntp_servers_resolved`. Failures — in `dns_fail_servers`, but only if
IP was not previously obtained (otherwise the old one is used).

### 4. Warmup σ_avg

Each instance captures `σ_avg` once — the average stdev of `dev_ns`
over servers for the last `n_min` ✓. While σ_avg is not captured, the instance
is not considered trusted and does not vote in consensus.

**Rules:**

1. **< 2 available servers** — do not exit warmup.
2. **≥ 2 servers collected `SIGMA_WARMUP_RECORDS = 15` ✓** — capture
   over them, without waiting for the rest (speeds up the exit).
3. **All collected ≥ 15 ✓** — compute over all.
4. **Timeout `3 × 15 = 45` ticks** — servers with 0 ✓ go into
   `warmup_excluded` permanently. If after exclusion fewer than 2 remain —
   do not ban, wait.
5. **Sanity threshold** `SIGMA_AVG_SANITY_MAX_NS = 10 ms` — if exceeded,
   capture is cancelled, the instance lives without trusted and dies.

### 5. Dynamic banned_servers

Before each round: `banned = occupied − {own favorite}`.
An instance does not listen to others' favorites, but always hears its own.

**Collision for a server:** the owner is the instance with the **minimum id**
(the oldest one). The loser loses **only** `favorite` +
`armed_lock_until_tick`. Statistics (`history`, `threshold`, `σ_avg`)
are preserved — they describe available servers, not the binding.

### 6. Inheritance of `warmup_excluded`

At birth the child receives the parent's `warmup_excluded`. This speeds up
the exit from warmup several times: children do not re-open garbage
servers already banned by the parent.

Divine birth (`_spawn_divine_locked`) does not inherit — instead it
gets `warmup_excluded = {favorite of all stuck}`.

### 7. Favorite selection (score + armed lock + hysteresis)

Priorities:
1. **Armed-lock** active AND favorite passed the filter → hold favorite.
2. **Score mode**: `score = d_norm + s_norm`, minimum — candidate.
   Warmup (σ-history on < 2 servers): only delay.
3. Favorite filtered out → forced switch without hysteresis.
4. Otherwise — hysteresis: stay on favorite if its score is not worse than
   the candidate by more than `FAVORITE_HYST = 0.15`.

All decisions are reflected in `inst.last_selection_mode`,
`inst.last_best_candidate`, `inst.last_scores`.

### 8. Reproduction

Triggers:
- **dominant_favorite** — `σ_X < DOMINANT_STDEV_RATIO · σ_others`, collected
  `M = max(10, ceil(2·ln(N/α)/Δ²))` observations on X and others.
  During armed-lock the trigger is silent.
- **deathbed** — `low_accept_windows ≥ DEATH_LOW_WINDOWS − 1 = 4`.

Single precondition `_spawn_allowed`:
`reproduction_allowed`, `favorite is not None`,
`reproduced_count < max_offspring`,
`population < max_population`, `occupied < total_servers`,
lag `REPRODUCTION_LAG_L = 5`.

### 9. Divine birth (divine)

If after death the population < `MIN_POPULATION = 3`, and there are slots
in the lineage-root queue (`< DIVINE_QUEUE_MAX = 3`), Consensus spawns new
independent roots with `warmup_excluded = {favorite of all stuck}` — that is,
it excludes exactly those servers on which the dead-end lineage is stuck.

### 10. Death

`accept_rate < DEATH_ACCEPT_THRESHOLD` (5%) over `ACCEPT_WINDOW_SIZE = 10`
rounds, `DEATH_LOW_WINDOWS = 5` windows in a row → `_kill_locked`.

If the last one died — the colony cold-starts (`_spawn_locked(None)`),
increment `cold_start_generation`.

### 11. Reproduction flag (spread gate)

```
if len(spreads) < SPREAD_HISTORY_MIN or noise_ref is None:
    reproduction_allowed = True        # gate is open while data is scarce
else:
    reproduction_allowed = median(spreads) < 2.0 · noise_ref
```

`spreads` — history of stdev between `own_reference_offset` of live instances.
`noise_ref` — median σ_avg of mature instances. While history is scarce — the gate
is open (that very "unblocking test", now permanent behavior).

### 12. Pre/post gate

- **Pre-gate** (`PRE_GATE_K = 3.0`): before the median, votes deviating
  from `prediction` by more than `3·noise_ref` are discarded. If all are discarded —
  hold by prediction.
- **Post-gate** (`POST_GATE_K = 2.0`): after the median, the step relative to
  `prediction` is limited to `2·noise_ref` if ≥3 applied values have already
  accumulated.

They work only if a reference point and ≥2 votes are present.

### 13. History and drift votes

- `short_vote` — median of the last `HISTORY_VOTE_SHORT_WINDOW = 15`
  applied `median_offset`. Activates at ≥15 points.
- `drift_prediction` — extrapolation one step forward via linear regression
  of the last `DRIFT_WINDOW = 300` points. Activates at ≥30 points.

Both votes are added to the votes of live instances when computing the median.

### 14. PLL: rate clock model

After the initial synchronization, the clock model is:
```
utc(mono) = anchor_offset + rate · (mono − anchor_mono)
```

On each PLL update:
```
dt = best_mono − anchor_mono
predicted = anchor_offset + rate · dt
phase_error = new_target − predicted

if |phase_error| > PHASE_JUMP_THRESHOLD_NS (100 ms):
    reset: anchor = new_target, rate = 0       # jump, not drift
else:
    anchor_mono = best_mono
    anchor_offset = predicted
    kp = PLL_KP_MEDIUM (0.40) if |phase_error| > PHASE_MEDIUM_JUMP_NS (20 ms)
         else PLL_KP (0.10)
    anchor_offset += kp · phase_error
    rate += PLL_KI (0.01) · phase_error / dt
    rate = clamp(rate, ±PLL_RATE_LIMIT = ±100 ppm)
```

- **Cut-off of stuck updates**: `dt < min_pll_update_interval_ns`
  (= half the interval between threads, ≥5 s) → PLL is skipped,
  history is written.
- **Anchor reset on a jump** — needed after sleep/resume, system time
  step by w32time, NTP jump on the server side. Otherwise `PLL_KP = 0.10`
  would stretch 400 ms into 35 minutes.

### 15. Colony bias integrator

Estimate of the permanent server shift by the **raw** history
(`_raw_proposed_history` — before bias application). Formula:

```
mean_raw[s]  = mean of raw proposed
sigma[s]     = 1.4826 · MAD(first_diffs) / √2
M            = median(mean_raw) over active
delta[s]     = mean_raw[s] − M
delta_app[s] = delta[s] − sign(delta)·sigma[s],  if |delta| > σ_s
             = 0,                                otherwise
b[s] ← (1 − 0.05)·b[s] + 0.05·delta_app[s], clamp ±50 ms
```

Soft-threshold: a shift that does not protrude from the server's noise is ignored.
The median converges to the common center, noisy servers do not introduce a false shift.
Bias is applied to `server_results` **before** `consensus.round()` and is reset
when `cold_start_generation` changes.

### 16. Clock Snapshot API

`get_clock_snapshot(precision_ns=None)` returns `ClockSnapshot`
with fields `(anchor_mono_ns, anchor_offset_ns, rate, ttl_ns, accuracy_ns)`.

- **precision_ns=None** → TTL by the heuristic of consensus quality:
  `quality = σ_ref / max(σ_recent, σ_ref/2)`, bounded
  `[0.25, 2.0]`; `TTL = 2·sync_interval · quality`, bounded
  `[30 s, 600 s]`.
- **precision_ns=int** → TTL is chosen so that
  `accuracy ≤ precision_ns`: `TTL = (precision − σ_ref) / δ_rate`,
  where `δ_rate = σ_recent / sync_interval_ns`. If `σ_ref ≥ precision` —
  `TTL_ABS_MIN` is returned with actual `accuracy = σ_ref`.
- Cache `{precision_ns → (ttl, accuracy)}` is bounded by
  `CLOCK_SNAPSHOT_PRECISION_CACHE_MAX = 16`, cleared on every PLL update.

`utc_from_snapshot(snap, now_mono)` — pure function for restoring UTC
from the snapshot with a TTL check.

### 17. Watchdog

Every `WATCHDOG_INTERVAL_SEC = 30`:

1. Checks liveness of sync threads, restarts dead ones.
2. Tracks `_last_success_mono_ns`: if no successful rounds for
   `> max(3·sync_interval, 180 s)` — CRITICAL with diagnostics
   (`_last_stalled_servers`).
3. At `> 3·stale_limit` — emergency exit (`os._exit(1)`, currently
   commented out) for restart by supervisor.

### 18. Keep-awake

**Windows** (`_keep_awake_worker`):
- `SetProcessInformation(ProcessPowerThrottling)` — remove EcoQoS
  (otherwise Windows moves to E-cores and limits CPU share).
- `SetPriorityClass(ABOVE_NORMAL_PRIORITY_CLASS)` — priority above
  Normal, but not HIGH.
- `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` —
  block Modern Standby; re-set every 30 s.

Symptoms without this: NTP rounds take 30/60/120/240 s instead of 4,
in the log — round multiples of `sync_interval`, both threads finish in the same ms.

**Linux:** from user-space suspend is not blocked. Checks once at
start that sleep targets are masked (`systemctl mask sleep.target
suspend.target hibernate.target hybrid-sleep.target`), otherwise writes CRITICAL.

Disabled via `TIME_SYNC_KEEP_AWAKE=0`.

---

## Telemetry

`get_sync_telemetry()` returns:

```python
{
    'precise_ns': int,
    'system_ns': int,
    'offset_ns': int,
    'offset_spread_ns': float | None,     # σ of slew corrections
    'slew_error_ns': int,                 # current_offset − target
    'slew_errors_ns': [...SlewRecord],    # consensus-level (instance_id=None)
    'ntp_spread_ns': float | None,        # σ network delay
    'diff_threshold_ns': int | None,      # median of trusted thresholds
    'population': {...},                  # colony summary
    'instances': {...},                   # per-instance full slice
    'colony_bias_ns': {srv: bias_ns},     # current bias
    'colony_noise_ns': {srv: noise_ns},   # robust server noise
    'per_server_delay': {srv: {...}},     # n, last, median, jitter
    'rate_ppm': float,                    # PLL rate
    'phase_error_ns': int | None,         # last phase_error
    'second_sync_thread_delay': int,      # period of the 2nd thread
}
```

`population` (from `Consensus.snapshot()`): `population`, `tick`,
`reproduction_allowed`, `spread_ns`, `favorites`, `occupied`,
`lineage_queue`, `reproduction_gate`, `history_vote`.

`instances` (from `get_instances_telemetry()`): for each live
instance — `lineage_id`, `favorite`, `banned_servers`,
`reference_offset_ns`, `threshold_ns`, `sigma_avg_ns`, `warmup_excluded`,
`dominant_M`, `armed_lock_until_tick`, `reproduced_count`, `accept_rate`,
`last_selection_mode`, `last_best_candidate`, `last_scores` slice,
`own_history`, `own_history_rejected`.

**Two SlewRecord queues for each instance:**
- `own_history` — accepted rounds.
- `own_history_rejected` — all servers rejected by the filter.

Each record contains a per-server slice: `(name, proposed, dev, delay, mark)`,
where mark ∈ `{✓, ×}` for instance-level and `{✓, ?, ×}` for consensus-level.

---

## Public API

```python
# Singleton
svc = TimeSyncService.get_instance()

# Lifecycle
svc.start()
svc.wait_for_first_sync(timeout=30)      # Event, set in _apply_new_sync_locked
svc.stop()

# Precise time
svc.get_utc_ns()                         # lock-free, UTC in nanoseconds
svc.get_clock_snapshot()                 # ClockSnapshot or None (before first sync)
svc.get_clock_snapshot(precision_ns=500_000)  # TTL for the given precision
svc.utc_from_snapshot(snap, now_mono)    # pure function, None if expired

# Telemetry
svc.get_sync_telemetry()
```

### Example of using ClockSnapshot

```python
snap = svc.get_clock_snapshot(precision_ns=1_000_000)  # 1 ms
if snap is not None:
    # At the moment of the snapshot, guarantee: |utc_restored − utc_true| ≤ snap.accuracy_ns
    # inside window [anchor_mono, anchor_mono + ttl_ns]
    ts = svc.utc_from_snapshot(snap, time.monotonic_ns())
    # ts == None if now_mono is outside the TTL
```

Restoring UTC from the snapshot:
```
utc = now_mono + anchor_offset_ns + round(rate · (now_mono − anchor_mono_ns))
```

---

## Achieved characteristics

Based on the results of **11 hours** of operation on 4 servers
(`.4` 0 ms, `ntp1` 15 ms, `pool` 62 ms, `sniim` 78 ms), on the previous
version without PLL and colony bias:

| Metric | Value |
|---|---|
| **Offset vs NTP** | +0.33 ms |
| **Threshold** | ±1.23 ms |
| **σ_avg** | 1.41 ms |
| **Δ slew corrections** | ±0.3–1.3 ms, symmetric |
| **Number of respawns** | 22 cycles |
| **Time to convergence** | 15–30 min after cold start |
| **Dominant** | ARMED, M=10, ratio 0.37 |

The current version adds to this:
- **Rate model** (PLL) — compensation of local clock drift between
  rounds, which reduces the dependence of accuracy on `sync_interval`.
- **Colony bias** — automatic compensation of the permanent server shift.
- **Pre/post gate** — resilience to single outliers.
- **ClockSnapshot API** — external consumers receive a consistent
  snapshot with an explicit accuracy bound.

---

## What works, what doesn't

### ✅ Works

- **Cold start by delay** — always selects `.4`, minimum error.
- **Self-narrowing threshold** — convergence to ±1.2 ms in 15–30 min.
- **Warmup ban** — correctly cleans out noisy servers.
- **Ban inheritance** — children exit warmup in 5–10 ticks.
- **Death/respawn** — natural mechanism for resetting stuck states.
- **Spread gate** — open while data is scarce; does not block reproduction
  in the initial state (a fix of the old version).
- **PLL** — resilient to time jumps, cuts off "stuck" updates.
- **Colony bias** — converges on permanent server shifts (with
  accumulated history).
- **ClockSnapshot** — dynamic TTL and accuracy, cache by precision.
- **Keep-awake** — removes Power Throttling on Windows, diagnoses
  absence of systemd mask on Linux.

### ⚠️ Doesn't work / limitations

- **Inheritance of erroneous bans.** If the parent banned a good
  server (warmup inversion with a shifted reference) — children will inherit the error.
  Partially cured by divine birth (see item 9 "How it works").
- **Colony bias requires a lot of history** (`COLONY_BIAS_MIN_HISTORY = 15`
  rounds per server, or `HISTORY_MAX_LEN // 6`). Not active at start,
  turns on after the raw history accumulates.
- **History votes require warmup.** `drift_prediction` is active from 30
  points, `short_vote` — from 15. Before that, voting proceeds only over live
  instances (trusted or fallback to warming-up).
- **Pre/post gate depend on `noise_ref`.** While the colony has fewer than two
  mature instances with captured σ_avg — `noise_ref = None`, gates are
  silent. The median is computed without protection against a single outlier.
- **`sniim` as a noise source.** With delay ~78 ms its contribution to the median
  is limited, but not excluded. Deferred until measuring `outlier_rate`
  (see plan, B.3).

---

## Network topology change

> **The colony assumes relatively homogeneous network conditions for all
> sources.** If part of the servers go through one route and part through
> another (VPN, uplink change, migration), the colony loses its ability to
> distinguish "the clock is wrong" from "the network is wrong".

This is not a defect of the algorithm. It is a **fundamental limitation of
any NTP consensus**: if the majority of sources are shifted by the same
network cause, the median shifts along with them. The colony votes by
**median of offsets**, not by "truth".

### What happens when the topology changes

| Scenario | Colony behaviour |
|---|---|
| All servers via one symmetric route | ✅ Normal. Common asymmetry, median compensates. |
| **Part of servers via VPN, part locally** | ❌ **Split.** Remote servers shifted by `(d_out − d_in)/2`, local ones accurate. Median drifts to the majority. |
| VPN tunnel with one-way asymmetry | ❌ Stable shift 100 ms – 1 s. Consensus does not see it: all remote servers "agree". |
| Route change without changing the server set | ⚠️ Prolonged transient. `_applied_offsets` pulls the median toward the old norm for hundreds of rounds. |
| Symmetric asymmetry (out ≈ in) | ✅ Error `(d_out − d_in)/2 ≈ 0`. |

### Symptoms in telemetry

- `offset spread (stdev)` grows from single ms to **hundreds of ms**
- `colony_bias` mirrors itself: one server `+X`, another `−X` (both hit
  the `±50 ms` ceiling and rock back and forth)
- `consensus` throws `Δ` by hundreds of ms per round (`+700 / −700 / +1500`)
- The local server (LAN, delay ≈ 0) shows a stable offset relative to the
  median, but **does not participate in consensus** — it is occupied by an
  outlier instance whose narrow threshold rejects everything else
- `accuracy_ns` in the snapshot claims ±5 ms while the real error is
  hundreds of ms
- `warmup_excluded` fills up with servers that "disagree" — instances get
  stuck in warmup and never exit

### A worked example (real log)

Topology: all NTP queries routed through a VPN in Europe instead of Asia.
The local server `192.168.20.4` is reachable directly (delay ≈ 0 ms) and
is the only source whose path does not cross the tunnel.

```
offset spread (stdev) : 914.16 ms
Instance 20 : Δ +49.90 ms   vs colony median  (fav=ntp1.niiftri.irkutsk.ru)
Instance 21 : Δ   0.00 ms   vs colony median  (fav=pool.ntp.org)
Instance 22 : Δ −1557.84 ms vs colony median  (fav=192.168.20.4)

[consensus] [19:03:18] Δ −1.67 ms  ✓pool.ntp.org:+670.8 ms, ✓192.168.20.4:−875.3 ms
[consensus] [18:52:48] Δ +725.8 ms ✓192.168.20.4:−149.7 ms
[consensus] [18:50:48] Δ −0.61 ms  ✓192.168.20.4:−142.8 ms, ✓ntp1.niiftri:+1445.8 ms
```

The VPN introduces a stable **one-way** asymmetry: `(d_out − d_in)/2 ≈ 870 ms`
for every remote server. All six remote servers agree with each other,
because they are all shifted by the same tunnel — so the median drifts
along with them. The single correct source (`192.168.20.4`) is locked
inside Instance 22, whose threshold (±225 ms) considers all other sources
as outliers.

### Why the colony cannot self-correct here

1. **`colony_bias` is structurally too small.** `COLONY_BIAS_MAX_NS = 50 ms`
   cannot reach 870 ms. Worse, the integrator enters a self-referential loop:
   it estimates shifts *after* applying its own bias, so it sees `±28 ms`
   mirrored values instead of the real discrepancy.
2. **`warmup_excluded` vs `banned_servers` deadlock.** Three instances occupy
   three different servers. Instance 22 (the only one anchored on the LAN)
   is permanently excluded from the others' view — nobody can compare
   against the LAN.
3. **Cold start `argmin(delay)` is a lucky-timing bet.** Whichever instance
   happens to be born at the wrong moment anchors on a shifted remote server,
   and thereafter rejects the LAN as an outlier.
4. **`accuracy_ns = 5 ms` is a lie** when the colony is split — external
   consumers get a snapshot that claims 5 ms precision while the real error
   is two orders of magnitude larger.

### What to do when the topology changes

1. **Stop the service.** Change `ntp_servers` to a **homogeneous** set —
   either all via VPN, or all local. Mixed sets are a structural weakness
   of the current design.
2. **Reset the history.** `_applied_offsets`, `_colony_bias`,
   `cold_start_generation++`. Otherwise the colony will keep treating the
   old shift as the norm for hundreds of rounds.
3. **Verify** `get_sync_telemetry()['offset_spread_ns']` — it should be a
   few ms. Hundreds of ms means the topology is still heterogeneous.
4. **If a mixed set is unavoidable**, treat the LAN server as the reference
   manually and expect the median to drift to the majority — see
   "Planned fixes" below.

### Planned fixes (not implemented)

- **LAN-anchored logic.** If a LAN server (`delay < LAN_DELAY_MAX_NS`) is
  stable and disagrees with the consensus in the range
  `[ASYM_TOLERANCE_NS ≈ 20 ms, ASYM_MAX_NS ≈ 2 s]` — that is the fingerprint
  of VPN asymmetry. Use LAN as the anchor, mark remote servers as suspect,
  raise CRITICAL. Outside that range — refuse to publish a snapshot at all
  (`accuracy_ns` degrades to the observed spread).
- **Soft-ban.** `banned_servers` should restrict *anchor selection* but not
  *visibility* — otherwise the colony cannot even compare itself against
  the LAN.
- **Auto-reset on topology change.** A sustained LAN-vs-consensus discrepancy
  over N rounds → reset `_applied_offsets` and `_colony_bias`.
- **Honest `accuracy_ns`.** When a split is detected, do not advertise
  optimistic 5 ms; report the observed spread.
- **Adaptive `COLONY_BIAS_MAX_NS`.** Currently hard-capped at 50 ms.
  Should scale to `max(50 ms, K × spread_ns)`.

### Diagnostic one-liner

If the colony is stable but you suspect a topology issue:

```python
tel = svc.get_sync_telemetry()
print(tel['offset_spread_ns'] / 1e6, 'ms spread')
print({s: v/1e6 for s, v in tel['colony_bias_ns'].items()})
```

A spread in the hundreds of ms **plus** a mirrored `colony_bias` (one server
large positive, another large negative) is the signature of a mixed route.
Do not try to fix it by tuning thresholds — fix the topology or the server set.

---

## Known pitfalls

### 1. Mandatory `is_synced_event.set()`

In the initial synchronization branch of `_apply_new_sync_locked`, the call
`self.is_synced_event.set()` is **mandatory**. Without it:
- `wait_for_first_sync()` always returns `False` by timeout;
- `get_utc_ns()` is forever in the fallback branch `return time.time_ns()`;
- `get_clock_snapshot()` is always `None`;
- watchdog skips progress checks;
- `_calculate_current_offset()` always returns 0.

```python
if not self.is_synced_event.is_set():
    ...
    self._target_offset = new_target_offset
    self.is_synced_event.set()      # ← do not forget
    return
```

### 2. Timeout `wait_for_first_sync(10)` may be too small

One round of `_get_best_ntp_sample` takes up to:
- `DNS_QUERY_TIMEOUT_SEC = 5` s (resolve, once per hour),
- `QUERY_ATTEMPTS · SPACING + TIMEOUT + SAFE = 5 + 2 + 2 = 9` s (poll).

Total up to ~14 s on a bad network. A reasonable minimum is 20–30 s, better —
`sync_interval`.

### 3. Loss of `dt` in the PLL I-correction

Both sync threads can be woken simultaneously (screen sleep, Modern
Standby). Then `dt ≈ 0`, `rate` instantly saturates. Protection —
`_min_pll_update_interval_ns` in `_apply_new_sync_locked`.

### 4. Power Throttling on Windows

Windows classifies the background application as "unimportant", moves it to
E-cores and limits CPU share. Symptom in the log: NTP rounds take
30/60/120/240 seconds instead of 4. Cured by `SetProcessInformation(ProcessPowerThrottling)`.

### 5. Linux suspend

From user-space suspend is not blocked. The only way —
`systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`
as root. Keep-awake checks this at start and writes CRITICAL if
not masked.

### 6. Cyclic deaths and large corrections

Self-narrowing of the threshold (`THRESHOLD_SHRINK_FLOOR = 0.95`) leads to
a cycle: the threshold narrowed → some servers stopped passing the filter →
accept_rate dropped → `DEATH_LOW_WINDOWS` windows in a row → instance death
→ respawn → threshold is rebuilt from `THRESHOLD_MIN_NS`. On 4
servers the cycle is observed once every 30–60 minutes (22 respawns in 11 hours).

**Symptom.** In telemetry, periodic large jumps of
`median_offset` are visible — by tens to hundreds of milliseconds, coinciding with
respawns. Cause: a fresh instance after cold start takes
`argmin(delay)` without history votes, and the PLL picks up the new value
through `phase_error` (if `> PHASE_MEDIUM_JUMP_NS = 20 ms` — with
`PLL_KP_MEDIUM = 0.40`, if `> PHASE_JUMP_THRESHOLD_NS = 100 ms` —
with a full reset of anchor and rate).

**What to do about it:**
- The `PHASE_JUMP_THRESHOLD_NS` threshold dampens jumps >100 ms, but not 20–100 ms.
- `PHASE_MEDIUM_JUMP_NS` + `PLL_KP_MEDIUM` speed up the catch-up, but leave
  steps of 20–100 ms visible to external consumers via `get_utc_ns`.
- External consumers sensitive to large steps should
  use `get_clock_snapshot()` + `utc_from_snapshot()`: the `accuracy_ns` field
  gives the upper error bound inside the TTL, and the snapshot itself
  contains `rate` and `anchor` — the consumer sees that the model
  has been re-established, and can request a fresh snapshot.

**Long-term solution** — B.1 (drift regression) from the plan: a stable
estimate of `rate` on a long window will allow distinguishing "drift" and "colony
step" without magic thresholds `PHASE_*_JUMP_NS`.

### 7. See also: Network topology change

The "colony split" pattern — `offset_spread_ns` in the hundreds of ms, mirrored
`colony_bias`, instances permanently stuck in warmup — is covered in
[Network topology change](#network-topology-change). It is placed as a
top-level section because the remedy is not a parameter tweak but a change
of the server set or a reset of the colony history.

---

## Roadmap

### ✅ Done (in the current version)

- **PLL (PI controller)** — rate clock model instead of linear slewing.
- **Phase jump detection** — 100 ms threshold, anchor reset.
- **Cut-off of stuck updates** — protection from sleep/resume.
- **Colony bias integrator** — soft-threshold by server σ.
- **Pre/post gate** — resilience to outliers via prediction.
- **History votes** — `short_vote` and `drift_prediction` (regression).
- **ClockSnapshot API** — dynamic TTL and accuracy.
- **Unblocking of spread gate** — open while data is scarce.
- **Divine birth** — new lineage root on extinction.
- **Score-based favorite selection** — `d_norm + s_norm` with hysteresis.
- **Multi-phase NTP polling** — 5 attempts per server.
- **DNS cache** — hourly TTL.
- **Keep-awake** — Windows + Linux diagnostics.
- **Watchdog with diagnostics** — `_last_stalled_servers`.

### 🔜 Next steps (Sprint 1 — algorithm)

- **B.0. LAN-anchored logic** (moved up from the deferred list).
  Detect a split between LAN and consensus and use the LAN as the anchor.
  Trigger condition is no longer "when it appears" — it is now a
  **first-class scenario** with a documented symptom (see
  [Network topology change](#network-topology-change)).
- **B.1. Drift regression on a long window.** Queue `_drift_history`
  of pairs `(mono_ns, offset_ns)`, least-squares estimate of `drift_ppm` once per 15–30
  rounds, verification with `stderr(slope)`. On a long horizon use
  `rate` from regression, on a short one — from PLL. Parameters:
  `DRIFT_HISTORY_LEN = 30`, `DRIFT_MIN_POINTS = 15`. Criterion — reduction of
  `std(phase_error)` on 30+ minutes.
- **B.2. Weighted median instead of weighted mean.** Replace
  `new_offset` in `_process_round_locked` with weighted median or trimming
  of top/bottom 10% before the mean. Criterion — the outlier `pool.ntp.org: +599 ms`
  does not affect the instance reference.
- **Measurement of `outlier_rate` for `sniim`** — decision on B.3.
- **B.4. Window separation** — only after B.1 stabilizes.

### 🧱 Sprint 2 — extraction from `app.py`

- **C.1. Splitting `core/time_sync.py`** into a service module and an IPC server.
  Modes via env: `TIME_SYNC_MODE=local|daemon|attached|peer`.
- **C.2. IPC protocol with TTL** — return `(anchor_offset, rate, ttl,
  ts_mono_server)`, the client computes UTC locally. IPC traffic drops
  hundreds of times. Transport: Unix-socket / named pipe, binary protocol.
- **C.3. Testing, systemd unit / Docker.**

### 🧱 Sprint 3 — self-learning

- **D.1–D.2. SQLite schema** in `~/.tbot/config.db`, tables with the prefix
  `timesync_`: `server_samples`, `server_stats`, `dns_history`,
  `decisions`, `colony_bias`.
- **D.3. Daemon thread for collecting raw data** — once per 60 s reads
  `_raw_proposed_history` and `_slew_error_history`, writes in batches.
  Retention: samples — 30 days, stats — a year.
- **D.4. Aggregation** — once per hour, metrics for windows 1h/24h/7d/30d,
  Winsorize tails; once per day — `drift_ppm` via least squares on 7 days,
  `bimodality_score`, flags of candidates for exclusion.
- **D.5. Decisions — rules, not AI.** Hard filters: `success_rate_24h < 0.7`
  → exclude; `|drift_ppm_7d| > 30` and CI does not cross 0 → exclude;
  `bimodality_score > threshold` → exclude. Soft score for ranking.
  Do not filter out more than down to `MIN_SERVERS = 5`.
- **D.6. Cold start — AI server search.** When the public IP
  / provider changes / `TIME_SYNC_FORCE_RESCAN=1`, the AI forms the starting list of
  candidates. Decisions on keeping — statistics, not AI. Export/import
  of profiles via `python -m time_sync.export|import`.

### 🧱 Sprint 4 — inter-module synchronization

- **E.1–E.4. Symmetric exchange via Redis.** Master/Slave rejected —
  channel fluctuations, error inheritance. Symmetric scheme with
  RTT compensation: exchange `(anchor_mono, anchor_offset, rate, ttl,
  instance_id)`, `delta = utc_A − utc_B_compensated`. Threshold
  `INTER_SYNC_THRESHOLD_NS = 5 ms` (narrow down to 1 ms on stabilization),
  if exceeded, both apply half the discrepancy smoothly.
- **E.5–E.7. Edge cases.** Restart of one, complete divergence,
  loss of connection (`MAX_DRIFT_BEFORE_DEGRADE = 100 ms`,
  `RECONNECT_TIMEOUT = 30 s`), alarm in UI. Invariant: **cross-sync is not in
  `get_utc_ns`**, local time — only from local PLL.
- **Dependency:** E after B.1, otherwise two PLL integrators rock
  each other.

### Deferred by symptoms

| Item | Trigger condition |
|---|---|
| A.1 Coherence of a single vote | Appearance of a case of slow drift of one trusted |
| B.3 Pre-filter by delay | `outlier_rate(sniim) < 50%` |
| B.4 Window separation | After B.1 stabilizes |
| Direct TCP instead of Redis in E | If Redis jitter interferes (measurement) |
| ~~B.0 LAN-anchored logic~~ | **Promoted to Sprint 1** — see above |

### What NOT to do

- **Do not touch the threshold logic** (`diff_sigma × stdev`) — it gives convergence.
- **Do not change cold start** — always selects `.4`, this is the accuracy base.
- **Do not let Consensus edit live instances directly.** Only
  through birth, divine birth and death.
- **Do not introduce min-delay protection** — the counterexample (a lying home server
  with delay 0) is valid. Protection from inversion — at the pre/post gate level.
- **Do not strengthen pre/post gate "just in case"** — measure first.
- **Do not ban a server for the single reason "little ✓".**
- **Do not block consensus forever because of a single vote.**
- **Do not fix a topology split by tuning thresholds.** No parameter in the
  current design can compensate an 870 ms one-way VPN asymmetry. Fix the
  server set or reset the colony history — see
  [Network topology change](#network-topology-change).

### Realistic ceiling

| Source | Accuracy |
|---|---|
| Public NTP + any algorithms | 0.7–1.5 ms |
| + PLL + colony bias + gates | 0.4–0.8 ms |
| + GPS server with calibration | 0.05–0.3 ms |
| + PTP with HW timestamping | 1–10 µs |

---

## References

- **Source code:** `core/time_sync.py`
- **Telemetry UI:** separate file with `@app.callback`
- **Improvement plan:** `план доработка часов 18_09_2026.md` (Sprints 1–4)
- **Analogues:** White Rabbit (CERN), Firefly (SIGCOMM 2025),
  BFT-Metronome (2025), Google TrueTime

### Key constants

```python
# Colony
ACCEPT_WINDOW_SIZE = 10                # accept rate window
DEATH_LOW_WINDOWS = 5                  # consecutive windows for death
DEATH_ACCEPT_THRESHOLD = 0.05          # accept rate threshold
REPRODUCTION_LAG_L = 5                 # lag between births
HISTORY_MAX_LEN = 100                  # own_history length

# Warmup / Dominant
ALPHA_SIGNIFICANCE = 0.1
DOMINANT_MIN_HISTORY = 10              # M_min
SIGMA_WARMUP_RECORDS = 15              # minimum ✓ per server
SIGMA_WARMUP_TIMEOUT_MULT = 3          # warmup timeout
SIGMA_WARMUP_MIN_SURVIVORS = 2
WARMUP_MIN_SURVIVORS = 2
DOMINANT_STDEV_RATIO = 0.7
SIGMA_AVG_SANITY_MAX_NS = 10_000_000   # 10 ms

# Reproduction gate
SPREAD_HISTORY_MIN = REPRODUCTION_LAG_L
REPRODUCTION_SPREAD_MULT = 2.0
FAVORITE_HYST = 0.15

# History votes
HISTORY_VOTE_SHORT_WINDOW = 15
HISTORY_VOTE_MIN_SAMPLES = 15
DRIFT_WINDOW = 300
DRIFT_MIN_SAMPLES = 30

# Pre/post gate
PRE_GATE_K = 3.0
POST_GATE_K = 2.0

# PLL
PLL_KP = 0.10
PLL_KI = 0.01
PLL_RATE_LIMIT = 100e-6                # ±100 ppm
PHASE_JUMP_THRESHOLD_NS = 100_000_000  # 100 ms
PHASE_MEDIUM_JUMP_NS = 20_000_000      # 20 ms
PLL_KP_MEDIUM = 0.40

# Threshold
K_THRESHOLD = 1.0
THRESHOLD_MIN_NS = 100_000             # 0.1 ms
THRESHOLD_SHRINK_FLOOR = 0.95

# Colony bias
COLONY_BIAS_GAIN = 0.05
COLONY_BIAS_MAX_NS = 50_000_000        # ±50 ms
COLONY_BIAS_DECAY = 0.99
COLONY_BIAS_DECAY_FLOOR = 10_000       # 10 μs
COLONY_BIAS_MIN_HISTORY = 15
COLONY_BIAS_HISTORY_DIV = 6

# Clock snapshot
CLOCK_SNAPSHOT_TTL_BASE_SEC = 2.0
CLOCK_SNAPSHOT_TTL_QUALITY_FLOOR = 0.25
CLOCK_SNAPSHOT_TTL_QUALITY_CEIL = 2.0
CLOCK_SNAPSHOT_TTL_ABS_MIN_SEC = 30.0
CLOCK_SNAPSHOT_TTL_ABS_MAX_SEC = 600.0
CLOCK_SNAPSHOT_TTL_RECENT_N = 15
CLOCK_SNAPSHOT_ACCURACY_DEFAULT_NS = 5_000_000   # 5 ms
CLOCK_SNAPSHOT_PRECISION_CACHE_MAX = 16

# NTP queries
QUERY_ATTEMPTS = 5
QUERY_ATTEMPT_SPACING_SEC = 1.0
PER_SERVER_HISTORY = 10
NTP_QUERY_TIMEOUT_SEC = 2
DNS_QUERY_TIMEOUT_SEC = 5
NTP_RESOLVING_TIMEOUT_NS = 3600 * 1_000_000_000

# Population
MIN_POPULATION = 3
DIVINE_QUEUE_MAX = 3
DIVINE_ACCEPT_RATE_THRESHOLD = 0.1
MAX_POPULATION_RATIO = 0.7
MAX_POPULATION_MIN_SERVERS = 5

# Watchdog / threads
WATCHDOG_INTERVAL_SEC = 30
WATCHDOG_STOP_JOIN_SEC = 3.0
SYNC_THREAD_STOP_JOIN_SEC = 15.0
PRE_START_JOIN_SEC = 5.0
SYNC_THREAD_SLOTS = 2
DEFAULT_INITIAL_INTERVAL_SEC = 60
KEEP_AWAKE_REFRESH_SEC = 30
```

### Constructor parameters

```python
TimeSyncService(
    ntp_servers=[...],            # list of NTP servers
    sync_interval_sec=60,         # synchronization period (>= 30)
    diff_sigma=1.0,               # threshold multiplier
)
```

### Environment variables

- `TIME_SYNC_KEEP_AWAKE=0` — disable keep-awake (e.g., on a laptop
  where battery matters).
- `TIME_SYNC_MODE=local|daemon|attached|peer` — operation mode
  (after Sprint 2).

---

## Principles that do not change

- Consensus **does not touch live instances** — only the starting conditions
  of new ones (birth, divine birth, death).
- `_defer_log` under the lock, `logger.xxx()` outside the lock.
- `get_utc_ns` — lock-free via `_clock_snapshot`.
- Pre/post gates should not be weakened without measurement.
- Do not ban a server for the single reason "little ✓".
- Cross-sync (Sprint 4) does not block `get_utc_ns`.
- Do not block consensus forever because of a single vote.
- The IPC protocol returns model parameters, not a bare time value.
- **The colony assumes homogeneous network conditions for all sources.**
  A mixed route (VPN + local, or two different uplinks) is not a
  parameter-tuning problem — it is a change of the server set, or a
  LAN-anchored reference. See [Network topology change](#network-topology-change).

---

## Philosophy

This is not a "NTP synchronization program". It is an **adaptive mechanism** that
uses NTP as an environment:

- Instance = organism with private memory.
- Reproduction = transmission of experience (inheritance of `warmup_excluded`).
- Divine birth = mutation, a new lineage root on extinction.
- Death = selection of the unfit.
- Consensus = collective decision without a center.
- Colonial memory = epigenetics (inherited without changing the "genome").
- PLL = internal pendulum estimating its own rate.
- Colony bias = adaptation to the environment (permanent server shift).
- ClockSnapshot = point of consistent observation for the outside world.

The system **finds equilibrium by itself** under the conditions of the environment. Not optimal —
but working and stable. Scaling (4 servers → 100) is a matter of
parameters, not principles.