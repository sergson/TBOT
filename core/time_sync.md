# TimeSyncService — the physics of precise time and the adaptive colony of NTP filters

A module for precise NTP-based time synchronization. The architecture is a **competitive colony of filters** with consensus and a **PI clock model** (anchor + rate) for external consumers.

This document is built **from physics to code**: first — what we measure and how stable time is derived from it, then — the registry of constants and runtime state.

---

## Contents

- [Part I. The physics of the process](#part-i-the-physics-of-the-process)
  - [1. A single NTP exchange: what is physically measured](#1-a-single-ntp-exchange-what-is-physically-measured)
  - [2. What smp\[0..3\] is — the anatomy of one sample](#2-what-smp03-is--the-anatomy-of-one-sample)
  - [3. What "ref" means at each level](#3-what-ref-means-at-each-level)
  - [4. Map of averaging: where each mean is used and why](#4-map-of-averaging-where-each-mean-is-used-and-why)
  - [5. The signal path: from NTP packet to stabilized time](#5-the-signal-path-from-ntp-packet-to-stabilized-time)
  - [6. What each σ means in the system](#6-what-each-σ-means-in-the-system)
  - [7. How the PLL drives the clock](#7-how-the-pll-drives-the-clock)
  - [8. What happens when consensus returns "rejected"](#8-what-happens-when-consensus-returns-rejected)
  - [9. The colony: why it exists and how it evolves](#9-the-colony-why-it-exists-and-how-it-evolves)
- [Part II. Flow logic (diagrams)](#part-ii-flow-logic-diagrams)
- [Part III. Registry of constants](#part-iii-registry-of-constants)
- [Part IV. Runtime state](#part-iv-runtime-state)
- [Part V. Public API](#part-v-public-api)

---

## Part I. The physics of the process

### 1. A single NTP exchange: what is physically measured

The system deals with two independent times:

- **Local monotonic** (`mono`) — what `time.monotonic_ns()` measures in the client OS. It never "jumps", does not depend on the Windows/Linux system clock, but it drifts away from true UTC at the clock's rate (e.g. 10 ppm = 10 µs/s).
- **Server UTC** — the reference we want to catch up with.

A single NTP exchange consists of four timestamps:

| Label | What it is | Units |
|---|---|---|
| `t1` | client **sent** the request | monotonic |
| `t2` | server **received** the request | server UTC |
| `t3` | server **sent** the reply | server UTC |
| `t4` | client **received** the reply | monotonic |

The difference between the scales is what we are after. If the network were ideal (`t2−t1 == t4−t3`), the true offset would be:

```
θ = ((t2 − t1) + (t3 − t4)) / 2
```

This is the **four-timestamp NTP formula** — the heart of the whole module. Everything the code does further is a fight against the fact that `t2−t1 ≠ t4−t3` (network asymmetry, jitter, drift during the exchange).

### 2. What smp[0..3] is — the anatomy of one sample

Each successful NTP exchange becomes a 4-tuple:

```python
smp = (delay_ns, mid_mono_ns, t2_utc_ns, unused)
```

The physical meaning of each field:

| Field | Name | What it is physically |
|---|---|---|
| `smp[0]` | `delay_ns` | **pure network round-trip delay**: `(t4 − t1) − (t3 − t2)`. This is how much "extra" time the network took, excluding server processing. The smaller, the more accurate the estimate. Minimum 1 ns. |
| `smp[1]` | `mid_mono_ns` | **the middle of the round in local monotonic time**: `(t1 + t4) // 2`. The point to which the whole estimate refers. |
| `smp[2]` | `t2_utc_ns` | **server UTC referred to that midpoint**: `θ + mid_mono`. Roughly — "what our clock should show at that moment, according to this server". |
| `smp[3]` | — | reserved for a bias shift (always 0 for now). |

From these numbers comes the key quantity — **`proposed`**:

```
proposed = smp[2] − smp[1] = θ
```

This is the **difference between the scales**: "by how much monotonic time lags UTC, in UTC-scale units" (per this particular server, at this particular moment). It is the server's "proposal" of what the local offset should be.

And the second key quantity — the **deviation** (`dev`), already relative to the current reference of the round (see §3):

```
dev = proposed − ref
```

### 3. What "ref" means at each level

The word "ref" appears in the code in several places and means **different things at different levels**. This is fundamental.

| Where | What `ref` is | Units | Physical meaning |
|---|---|---|---|
| `inst.own_reference_offset` | reference point of the instance | ns (UTC−mono) | "Where, according to this filter, the truth is right now". Delay-weighted mean of accepted `proposed`. |
| `ref` in `_filter_instance` (local var) | same — `inst.own_reference_offset` | ns | Each server is compared against it: `dev = proposed − ref`. |
| `rec.ref_ns` in an accepted record | the **old** `own_reference_offset` (before the recompute) | ns | What the servers were measured against in this round. |
| `rec.ref_ns` in a rejected record | `inst.own_reference_offset` | ns | Same — what was compared, but nothing passed. |
| `rec.ref_ns` in a consensus record (accepted) | `best_utc` | ns | The UTC chosen by consensus. |
| `rec.ref_ns` in a consensus record (post-gate rejected) | `predicted_now` — the clock model's opinion | ns | What the PLL predicted at the moment of rejection. |
| `t_ref_mono` (round argument) | arithmetic mean of `mid_mono` over all samples of the round | mono ns | "The moment to which the whole round refers". |
| `t_ref_mono` in the matrix | same | mono ns | Drift compensation is done against it: `proposed − rate·(mid − t_ref)`. |
| `seed_threshold_ns` | the previous `diff_threshold_ns` | ns | Initial threshold for new instances. |
| `sigma_consensus_ns` | consensus σ | ns | Post-gate limit. |

**The main rule:** `ref` is **not a constant**, it is a point that moves every round. A "cloud" of `proposed` from different servers forms around it, and the instance's filter cuts off those who wandered too far.

### 4. Map of averaging: where each mean is used and why

The system **deliberately** uses different types of averaging — each has its own physical job.

#### 4.1 Arithmetic mean

Used where the data is **already clean** and has uniform trust:

| Where | What is averaged | Why arithmetic specifically |
|---|---|---|
| `t_ref_mono` (`_poll_servers`) | `mid_mono` over all samples of the round | Need a "neutral" moment. No weights needed — just the center of the cloud. |
| `raw_snapshot` (`_get_best_ntp_sample`) | `proposed` over attempts of one server | Within a single server all attempts are equivalent. |
| `consensus_offset` (`_build_consensus_matrix`) | `proposed` over **all L² cells of the matrix** | **The key place.** The matrix is built so that each cell is an independent measurement at its own moment. Arithmetic mean gives balance of time × ensemble of servers. No cell must dominate. |
| `threshold_out` | thresholds of live instances (telemetry) | Diagnostics. |
| `mean_interval` (`matrix_meta`) | intervals between matrix rows | Diagnostics. |

#### 4.2 Delay-weighted mean

Used to compute **the new reference offset of an instance** (`inst.own_reference_offset`):

```python
delays  = [smp[0] for _, smp in accepted]
weights = [1.0 / max(d, 1) for d in delays]
w_norm  = w / sum(weights)
new_offset = Σ w_norm·(smp[2] − smp[1])
```

Physics: **a server with lower network delay deserves more trust**. An exchange over a 5 ms network gives a much blurrier estimate than one over 1 ms — the weight is inversely proportional to delay. This is a deliberate trade-off: we do not know whether the delay is symmetric, but the smaller it is, the smaller the asymmetry's contribution.

#### 4.3 Median

Used wherever there is a **risk of a single outlier** (bad server, DNS glitch, random timeout):

| Where | What is median | Why |
|---|---|---|
| `short_vote` (`_compute_history_votes`) | the last 15 `_recent_observed` (including rejected) | The median is robust to a single anomaly — one "jump" won't pull the prediction. |
| `noise_ref_ns` | σ_avg of live instances | One "frenzied" instance must not spoil the reference noise. |
| `median(spreads)` (spread-gate) | history of stdev(offsets) | Same — one outlier does not block reproduction. |
| `median_mean` (bias estimation) | per-server means of `proposed` | Robust "common center" — a stuck server does not drag the whole estimate. |
| `median_delay`, `median_sigma` (score) | delays / σ of servers | Normalization for favorite scoring. |
| `median` in `_compute_drift_prior` | ppm history from DB | Robust prior estimate. |

#### 4.4 Standard deviation (stdev)

Used where a **measure of the width of the cloud** is needed:

| Where | What σ | Why |
|---|---|---|
| Instance threshold `new_raw` | σ(diffs) × `diff_sigma` | How much the instance "breathes" — to widen/narrow the filter. |
| Instance `σ_avg` (`_capture_sigma_locked`) | **mean** of per-server σ(dev_ns) | Estimate of the "typical" server noise of this instance. Frozen once. |
| `σ_consensus` (`_noise_sigma_ns`) | σ of first differences / √2 | Robust estimate of consensus decision noise without a trend. |
| σ(delays) (`ntp_spread_ns`) | over all delays of the round | Network diagnostics. |

#### 4.5 MAD (median absolute deviation)

A robust (outlier-resistant) substitute for σ:

| Where | What |
|---|---|
| `_update_colony_bias` | σ_s = `1.4826·MAD(first_diffs)/√2` — each server's noise. |
| `_compute_colony_noise_ns` | same, for telemetry. |
| `_compute_drift_prior` | σ from the drift history in the DB. |

Physics: ordinary σ gets "inflated" by a single outlier, MAD does not. For noisy NTP servers this is critical.

#### 4.6 Linear regression (OLS)

Only in one place — **`drift_slope`**:

```python
slope = Σ(dx·dy) / Σ(dx²)         # ns/ns
drift_slope = slope · round_ns    # ns/round
```

Applied to `applied_offsets` (only those approved by the post-gate). Physics: on a long window (300 rounds ≈ 2.5 h) the slope is **the true rate of the clock's departure** from UTC, robust to single glitches.

#### 4.7 Summary table

| Quantity | Type | Window | Why exactly this type |
|---|---|---|---|
| `t_ref_mono` | arithmetic | all samples of the round | neutral round center |
| `raw_snapshot[srv]` | arithmetic | attempts of a single server | attempts are equivalent |
| `own_reference_offset` | **weighted 1/delay** | accepted servers of the instance | fast servers get more trust |
| `consensus_offset` | **arithmetic** | **L² matrix cells** | balance time × ensemble |
| `short_vote` | **median** | last 15 observed | resistance to a single outlier |
| `drift_slope` | **OLS** | 300 applied | true drift rate |
| instance `σ_avg` | mean of per-server σ | all ✓ servers | typical noise |
| `σ_consensus` | σ of first differences | 100 applied | robust consensus noise |
| per-server `σ_s` | MAD | raw history | resistant to outliers |
| `colony_bias[srv]` | **arith. over time, median over servers** | 33+ rounds | common center minus server noise |
| `noise_ref_ns` | **median** of σ_avg | instances | one faulty instance does not spoil the reference |

### 5. The signal path: from NTP packet to stabilized time

Step-by-step — **how a physical sample becomes UTC**:

```
   ┌────────────────────────────────────────────────────────────┐
   │  Step 1. Network exchange                                   │
   │  Each server is polled n_attempts times, spaced by 1 s.    │
   │  We get sample = (delay, mid_mono, t2_utc, 0).             │
   │  proposed = t2_utc − mid_mono — the server's "proposal".   │
   └────────────────────────────────────────────────────────────┘
                             │
                             ▼
   ┌────────────────────────────────────────────────────────────┐
   │  Step 2. Compensation of the known server bias             │
   │  proposed[srv] −= colony_bias[srv]                          │
   │  (the gradually accumulated correction, see §9.4)          │
   └────────────────────────────────────────────────────────────┘
                             │
                             ▼
   ┌────────────────────────────────────────────────────────────┐
   │  Step 3. Per-instance filter                                │
   │  inst takes attempt_idx = k (position in ordered).         │
   │  Collects available = {srv: smp[k]}.                        │
   │  For each server:                                           │
   │    dev = proposed − own_reference_offset                    │
   │    accepted, if |dev| ≤ own_threshold_ns.                   │
   │  → new_offset = delay-weighted mean(proposed over accepted) │
   │  → own_reference_offset := new_offset                       │
   │  → own_threshold_ns evolves from σ(diffs)                   │
   └────────────────────────────────────────────────────────────┘
                             │
                             ▼
   ┌────────────────────────────────────────────────────────────┐
   │  Step 4. Consensus matrix                                   │
   │  Each instance contributes a row of accepted proposed.      │
   │  We collect L rows → L×L matrix.                            │
   │  Drift compensation: proposed −= rate·(mid − t_ref_mono).   │
   │  consensus_offset = arithmetic mean over all L² cells.      │
   └────────────────────────────────────────────────────────────┘
                             │
                             ▼
   ┌────────────────────────────────────────────────────────────┐
   │  Step 5. Prediction and post-gate                           │
   │  prediction = drift_prediction (OLS) or short_vote (median) │
   │  delta = consensus_offset − prediction                      │
   │  limit = POST_GATE_K · σ_consensus                          │
   │  If |delta| > limit → rejected (see §8)                     │
   │  Otherwise → applied (written to applied_offsets)           │
   └────────────────────────────────────────────────────────────┘
                             │
                             ▼
   ┌────────────────────────────────────────────────────────────┐
   │  Step 6. PLL                                                │
   │  predicted = anchor_offset + rate·(t − anchor_mono)         │
   │  phase_err = new_target_offset − predicted                  │
   │  anchor_offset := predicted + KP·phase_err                  │
   │  rate ← corrected (sign-constancy + drift-confirm)          │
   └────────────────────────────────────────────────────────────┘
                             │
                             ▼
   ┌────────────────────────────────────────────────────────────┐
   │  Step 7. Publication of the clock model                     │
   │  get_utc_ns():  utc = mono + anchor_offset +                │
   │                       rate·(mono − anchor_mono)             │
   │  Lock-free. Used by external consumers.                     │
   └────────────────────────────────────────────────────────────┘
```

**The key point:** each level solves its own physical problem.
- Step 3 — "filter out bad servers and build a local reference point for each filter".
- Step 4 — "average the opinions of all filters so that neither a single moment nor a single server dominates".
- Step 5 — "prevent a single outlier from shifting the overall picture".
- Step 6 — "smoothly pull our clock toward that picture, without jumping".

### 6. What each σ means in the system

There are many different σ's in the code, and they must not be confused.

| σ | Where | What it physically measures | Magnitude |
|---|---|---|---|
| **σ(diffs)** | inside an instance | how much `own_reference_offset` "breathes" from round to round | ~units of ms |
| **instance σ_avg** | `inst.own_sigma_avg_ns` | typical scatter of `dev_ns` of one server around the instance's reference | units–tens of ms |
| **σ_consensus** | `_compute_consensus_sigma_ns` | how stable consensus decisions are (`applied_offsets`) | ~0.1–1 ms |
| **per-server σ_s** | in `_update_colony_bias` | a server's own jitter (robustly, via MAD of first differences) | ~units of ms |
| **σ_prior** | from the DB (`drift_history`) | historical scatter of clock drift estimates | ~units of ppm |
| **ntp_spread_ns** | telemetry | scatter of network delays | ~units of ms |
| **offset_spread_ns** | telemetry | σ of consensus decisions around their recent norm | ~0.1–1 ms |
| **σ in quality (TTL)** | `_compute_snapshot_ttl_locked` | short vs long window of σ_consensus | dimensionless |

**Do not confuse:** an instance's `σ_avg` is the **internal** scatter of servers before averaging; `σ_consensus` is the **external** scatter of already-averaged decisions. The first is many times larger than the second (averaging over N servers and M instances suppresses noise).

### 7. How the PLL drives the clock

A PLL (phase-locked loop) is a mechanism for **smooth** adjustment without jumps. The clock model:

```
utc(t) = mono(t) + anchor_offset + rate · (mono(t) − anchor_mono)
```

Where:
- `anchor_offset` — the offset of the scales at the anchor point;
- `anchor_mono` — the moment at which we fixed that offset;
- `rate` — dimensionless drift rate (1 ppm = 1e-6), how far our `mono` leaves UTC per unit time.

Each round:

1. **Prediction.** What does the clock model think now?
   ```
   predicted = anchor_offset + rate·(best_mono − anchor_mono)
   ```
2. **Phase error.** How far has the prediction diverged from the new target offset?
   ```
   phase_err = new_target_offset − predicted
   ```
3. **Correction.** We do not "jump" at once, but pull by a fraction:
   ```
   anchor_offset := predicted + KP · phase_err
   ```
   with `KP = 0.10` normally and `KP = 0.40` for large errors (>20 ms).

4. **Protection against jumps.** If `|phase_err| > 100 ms` — this is not drift but an event (sleep/resume, system clock step, NTP glitch). Then — a hard reset: anchor = target, rate = prior.

5. **Rate correction** — see below.

#### 7.1 How `rate` is estimated

`rate` is **not a direct measurement**, but an estimate built by two mechanisms:

**a) Sign-constancy** (`_apply_rate_sign_constancy_step_locked`).

If `phase_err` within a 30-value window is **persistently of one sign** (t-statistic > 2.5, runs-z < −2), then our `rate` estimate is biased, and we systematically "undershoot" or "overshoot". The correction step:

```
step_ppm = base + confidence·(max − base)
rate += direction · step_ppm · 1e-6
```

Where `base`, `max` are proportional to `σ_prior` (or fall back to 0.02/0.1 ppm). This is **not a PI controller**, but a discrete "spring" that pulls `rate` in the required direction.

**b) Drift confirmation** (`_apply_drift_confirmation_locked`).

`rate` is a fast but runaway-prone integrator. `drift_slope` from OLS is slow but stable. If they diverge by more than 3 ppm:

```
rate ← α·rate + (1 − α)·slope,   α = DRIFT_CONFIRM_ALPHA
```

This is insurance against `rate` "running away" due to a long series of one-sided `phase_err`.

#### 7.2 Protection against PLL "sticking"

If two sync threads wake up simultaneously (screen sleep, Modern Standby) — `dt` in the denominator of the I-correction becomes ~0 and `rate` instantly saturates. The protection:

```python
if dt_ns < self._min_pll_update_interval_ns:
    # skip the PLL, but write to history
```

The threshold is half the interval between threads (15 s at a 60-second cycle).

### 8. What happens when consensus returns "rejected"

#### 8.1 Where reject comes from

In `_process_round_locked`, after the matrix is assembled:

```python
consensus_offset = mean(L² cells)   # the fresh observation
prediction       = drift_prediction | short_vote
delta            = consensus_offset − prediction
limit            = POST_GATE_K · σ_consensus
```

- If `|delta| ≤ limit` → **applied** (the normal path, §7).
- If `|delta| > limit` → **rejected** with a caveat:
  - `_consecutive_rejects += 1`;
  - if the counter reaches `POST_GATE_FORCE_APPLY_AFTER = 50` → **forced apply** (§8.5);
  - otherwise `applied = False`, and the returned tuple carries `rejected_offset_ns` (raw `consensus_offset`) and `rejected_prediction_ns` (the gate's opinion).

The gate's physical meaning: "what the consensus just measured is **too far** from my verified trajectory. Most likely this is an outlier, not a real clock excursion."

#### 8.2 What does NOT happen on reject

| Not updated | Why this is correct |
|---|---|
| `applied_offsets.appendleft(...)` — not called | The "clean" trajectory stays uncontaminated. `σ_consensus`, `drift_slope` (OLS), and `drift_prediction` are all computed from it. If we stuffed rejected outliers in, both σ and the OLS slope would break. |
| `drift_slope` (OLS over 300 applied) | Does not see the rejected value. OLS is sensitive to a single outlier. |
| `drift_prediction` base point | Stays at the last applied offset. It is **extrapolated** via `rate·(t_ref − _last_apply_mono)` so it does not freeze in time. |
| Drift persist to DB (`append_drift_sample`) | The reject branch **returns before** reaching it. |
| `_last_drift_persist_tick` | Not reset. |

#### 8.3 What DOES happen on reject

**(a) `_recent_observed` is still updated.** Right after the matrix is assembled, **before** the gate:

```python
self._recent_observed.appendleft(consensus_offset)
```

This is crucial. `_recent_observed` is a separate window of "all observations, including rejected". `short_vote = median(15)` is computed from it. As a result: the gate's own prediction does not freeze — the median sees fresh data even when `applied_offsets` is stuck.

**(b) A record is written to `_slew_error_history`.** A `SlewRecord` with `rejected_offset_ns ≠ None`, `ref_ns = predicted_now`, and `diff_ns = rejected_offset_ns − predicted_now`. Pure telemetry — it does not enter PLL logic.

**(c) `anchor_offset` shifts, depending on the streak length:**

```python
dt_ns            = best_mono − self._anchor_mono
predicted_now    = self._anchor_offset + rate · dt_ns
phase_err_reject = rejected_offset_ns − predicted_now
```

| streak | What anchor does | Physics |
|---|---|---|
| **streak ≤ 1** | `anchor_offset := rejected_prediction_ns`; `anchor_mono := best_mono` | "This is a single outlier. I do not trust the raw consensus_offset, and I do not shift toward it. I put the anchor at the point that the **verified trajectory** (the gate's prediction) considers correct." The clock keeps running along the old model. |
| **streak ≥ 2** | `anchor_offset := predicted_now + PLL_KP_REJECT · phase_err_reject` | "The streak is confirmed — the gate is stale. I pull the anchor by 30% (`PLL_KP_REJECT = 0.30`) toward the **raw** consensus_offset, but do not jump all the way." Equivalent to `0.7·predicted_now + 0.3·rejected_offset_ns`. |

The key difference: `rejected_prediction_ns` is the gate's prediction (from `applied_offsets` with rate extrapolation), while `predicted_now` is the PLL's own model prediction (`anchor + rate·dt`). They are usually close, but during a long reject series they diverge — the PLL starts to "catch up" to reality via P-correction, while the gate's prediction stays in the past.

**(d) The `_phase_err_window` is fed and `rate` is corrected.** If `dt_ns ≥ _min_pll_update_interval_ns`:

```python
self._apply_rate_sign_constancy_step_locked()
self._phase_err_window.appendleft(phase_err_reject)
```

Reject deltas **participate in the sign-constancy detector** on a par with accept phases. If 20+ reject deltas in a row have the same sign, the `rate` estimate is systematically biased and the spring pulls it. Order matters: the spring is applied first (window without the current value), then the value is appended — so the reject does not vote for its own decision.

**(e) σ_consensus expands — the gate opens itself.** In `_compute_consensus_sigma_ns`:

```python
streak = self._consensus._consecutive_rejects
if streak > 0:
    grow  = (1.0 / THRESHOLD_SHRINK_FLOOR) ** streak
    sigma = int(sigma * grow)
```

With `THRESHOLD_SHRINK_FLOOR = 0.99`:

| streak | σ multiplier | effect on `limit = POST_GATE_K · σ` |
|---|---|---|
| 1 | ×1.01 | barely noticeable |
| 4 | ×1.04 | gate slightly wider |
| 10 | ×1.11 | noticeably wider |
| 50 (force-apply) | ×1.65 | gate wide open |

Plus `short_vote` gradually shifts toward the rejected values (via `_recent_observed`), and `drift_prediction` is extrapolated via `rate`. Together this **prevents the gate from sticking**: sooner or later `|delta|` falls below the new `limit`.

**(f) Reset of `rate` to prior after `REJECT_RATE_RESET_STREAK = 4`:**

```python
if streak >= REJECT_RATE_RESET_STREAK:
    rate_ppm  = self._rate * 1e6
    prior_ppm = self._rate_prior * 1e6
    if abs(rate_ppm − prior_ppm) > REJECT_RATE_RESET_MIN_DIVERGE_PPM:
        new_ppm = (1 − GAIN)·rate_ppm + GAIN·prior_ppm
        self._rate = new_ppm * 1e-6
        self._phase_err_window.clear()
```

With `GAIN = 1.0` this is a **hard reset** `rate := rate_prior`. Logic: 4 rejects in a row means the clock model (and its `rate`) has drifted too far from what history confirms. Safer to return to the DB prior than to keep extrapolating from a skewed estimate.

The developer's note in the code warns directly: if this happens, check hardware/temperature and **clear the `drift_history` table** — the prior itself may be wrong.

**(g) The clock snapshot is published.** If `rate` changed (via sign-constancy or reset-to-prior) or the dt branch was taken, `_publish_clock_and_metrics_locked()` recomputes TTL and accuracy for the new rate/anchor and invalidates `_precision_cache`. Otherwise external consumers of `get_clock_snapshot(precision_ns=...)` would receive stale numbers.

**(h) `_target_offset` — what we consider the truth:**

```python
self._target_offset = (
    rejected_prediction_ns if streak ≤ 1 else rejected_offset_ns
)
```

Telemetry only. Meaning: on a single outlier, "the truth" is the gate's prediction; on a confirmed streak, it is the raw consensus itself.

**(i) Colony bias is updated.** `_update_bias_history_locked(raw_snapshot)` with **raw** `proposed` (before bias subtraction). Even if the consensus rejected this particular point as an instantaneous outlier, the server's long-term behavior is still accumulated.

#### 8.4 When the streak resets (accept after reject)

If the gate passes — `self._consecutive_rejects = 0`. Then: σ_consensus returns to its normal form (`grow = 1`), `applied_offsets` starts receiving points again, `drift_slope` begins to "see" them, and the PLL proceeds normally (`predicted + KP·phase_err`).

#### 8.5 Force-apply after 50 consecutive rejects

```python
if self._consecutive_rejects >= POST_GATE_FORCE_APPLY_AFTER:
    self._defer_log("warning", "... force-apply ...")
    self._consecutive_rejects = 0
    # applied remains True
```

This is **insurance against a dead lock**. Physics: if 50 rounds in a row (~25 minutes at a 30-second interval) were rejected, then `drift_prediction`/`short_vote` are **hopelessly lagging** the real clock excursion (e.g. an external time step, or a sudden drift change). Force-apply means: "enough. Accept `consensus_offset` as is, reset the counter, rebuild the prediction from the new applied". Then σ_consensus is recomputed and the gate restarts with fresh data.

#### 8.6 Reject vs accept: summary table

| Aspect | accept (applied=True) | reject (applied=False) |
|---|---|---|
| `applied_offsets` | appendleft(consensus_offset, t_ref_mono) | **untouched** |
| `_recent_observed` | appendleft(consensus_offset) | appendleft(consensus_offset) — **same** |
| `_consecutive_rejects` | reset to 0 | += 1 |
| `drift_slope` (OLS) | recomputed over applied | not recomputed |
| `drift_prediction` | updated to the last applied + extrapolation | stays at the old one + extrapolation via rate |
| `short_vote` | median(15 observed) — including the current | median(15 observed) — **including the rejected** |
| σ_consensus | `_noise_sigma_ns(applied)` | `_noise_sigma_ns(applied) × (1/0.99)^streak` |
| anchor_offset | `predicted + KP·phase_err` | `streak ≤ 1`: gate prediction; `streak ≥ 2`: `0.7·predicted + 0.3·rejected` |
| `_phase_err_window` | appendleft(phase_err) | appendleft(phase_err_reject) |
| rate: sign-constancy | applied | applied (if dt is sufficient) |
| rate: drift-confirm | applied | **not applied** |
| rate: reset-to-prior | — | when `streak ≥ 4` and diverg > 1 ppm |
| `_target_offset` | new_target_offset | gate prediction (streak≤1) or raw (streak≥2) |
| Drift persist to DB | yes, once every ~300 ticks | **no** |
| Colony bias | updated | updated |
| Publishing the clock snapshot | yes | yes (if rate/anchor changed) |

#### 8.7 The physical meaning of the whole mechanism

The gate protects the PLL from two extremes:

1. **A single outlier** (server blinked, DNS glitch, random interference). The gate says "no", the anchor stays on the verified trajectory, and the clock does not twitch.
2. **A real step / excursion** (sleep-resume, system clock step, sudden drift change). The gate also says "no" at first, but then:
   - `short_vote` gradually sees the new points,
   - σ_consensus expands,
   - `streak ≥ 2` starts P-correction toward the raw consensus,
   - `streak ≥ 4` resets `rate` to prior,
   - `streak ≥ 50` — force-apply.

Thus the system **does not jump on every outlier**, yet **does not stick forever** in the face of a real event. The trade-off between robustness and adaptivity is governed by three numbers: `POST_GATE_K`, `REJECT_RATE_RESET_STREAK`, `POST_GATE_FORCE_APPLY_AFTER`.

### 9. The colony: why it exists and how it evolves

The colony is **not decoration** — it is a mechanism for robust server selection. The idea: different filters with different thresholds and "bindings" give different opinions; consensus averages them; the worst instances die off, the best reproduce.

#### 9.1 Lifecycle

```
COLD START → warmup → MATURE → (reproduce | deathbed) → death
```

- **Cold start.** `own_reference_offset = None`. First round: the server with the minimum delay (argmin(delay)) is taken.
- **Warmup.** History of `✓`-acceptances per server accumulates. Upon reaching `SIGMA_WARMUP_RECORDS = 5` per server — capture of `σ_avg`.
- **MATURE.** The instance works: filters, updates `own_reference_offset`, evaluates a favorite, participates in the matrix.
- **Reproduction.** By favorite dominance (σ_favorite < 0.7·σ_others) or by "deathbed" (accept_rate < 0.05 over 5 windows).
- **Death.** `low_accept_windows ≥ 5` → the instance dies.
- **Divine birth.** If the population drops below `MIN_POPULATION = 5`, a new independent root is created, banning favorites of the stuck instances.

#### 9.2 Favorite — the instance's "preferred server"

A favorite is the server an instance "clings" to: if it passes the filter, the instance holds on to it (armed-lock). Favorite selection:

1. **Armed-lock**: if the favorite was accepted and the lock has not expired — keep it.
2. **Score mode**: `score = d_norm + s_norm`, where `d_norm = delay/median(delay)`, `s_norm = σ_s/median(σ)`. The minimum is the candidate.
3. **Hysteresis**: change the favorite only if the new one is better than the old by `FAVORITE_HYST = 15%`.

Physics: the instance does not "flicker" between servers every second. It holds the chosen server while it is honestly responding.

#### 9.3 Consensus matrix

The matrix is **L×L**, where L is the maximum number for which there are ≥L rows of length ≥L. Each row is a set of `proposed` from one instance. Rows refer to different moments in time (instances poll servers at different moments via `attempt_idx = k`).

`_select_diverse_cells` maximizes diversity: a server does not appear twice in the same row, and its total usage is limited. This guarantees that **different servers at different moments** are represented in the matrix — there are no correlated outliers.

The arithmetic mean over L² cells is a **balanced estimate of true UTC**.

#### 9.4 Colony bias — compensation for a server's constant offset

Some servers lie by a constant amount (e.g. +3 ms). This is not noise — this is a **bias**. It is estimated as follows:

1. From `_raw_proposed_history` (before bias application), the `mean` over time is taken for each server.
2. The `median` of that mean across servers is computed — the "common center".
3. `delta = mean_s − median_mean`.
4. If `|delta| > K · σ_s` — the bias is confirmed; otherwise the server is just noisy, not shifted.
5. `bias[srv] ← (1−k)·bias[srv] + k·delta`, clamped to ±50 ms.

Thereafter every new sample of this server is corrected by `bias[srv]`. Physics: we **subtract the server's systematic error** and work only with its noise.

#### 9.5 Spread-gate — when the colony may reproduce

If instance opinions diverge too much (`median(spreads) ≥ 2·median(σ_avg)`), the colony is out of phase — reproduction is forbidden. This prevents the fixation of "bad" lineages.

---

## Part II. Flow logic (diagrams)

### Full cycle of one round

```
┌──────────────────────────────────────────────────────────────┐
│  _sync_worker (Thread 0 or Thread 1)                         │
│  sleep(sync_interval) → new round                            │
└─────────────────────────────┬────────────────────────────────┘
                              ▼
┌──────────────────────────────────────────────────────────────┐
│  _get_best_ntp_sample()                                      │
│  1. n_attempts = max(MIN_POPULATION, population_size)        │
│  2. _poll_servers(n_attempts)                                │
│  3. raw_snapshot = mean(proposed) per server                 │
│  4. samples_by_srv −= colony_bias[srv]                       │
│  5. σ_consensus = _compute_consensus_sigma_ns() (MAD)        │
└─────────────────────────────┬────────────────────────────────┘
                              ▼
┌──────────────────────────────────────────────────────────────┐
│  Consensus.atom_round(...)                                   │
│  ┌── _refresh_bans_locked()  (favorite ownership)           │
│  │ ┌── _process_round_locked()                               │
│  │ │    for k, inst in ordered:                              │
│  │ │      available = samples[attempt_idx=k]                 │
│  │ │      accepted = {|proposed − ref| ≤ thr}                │
│  │ │      new_offset = Σw·proposed, w ∝ 1/delay              │
│  │ │      ref := new_offset; thr evolves                     │
│  │ │      matrix_rows.append(row)                            │
│  │ ├── _compute_history_votes()                              │
│  │ │    short_vote     = median(15 observed)                 │
│  │ │    drift_slope    = OLS(300 applied)                    │
│  │ │    drift_pred     = last_applied + rate·Δt              │
│  │ ├── _build_consensus_matrix()                             │
│  │ │    size L, compact = diverse_cells                      │
│  │ │    consensus_offset = mean(L² cells)                    │
│  │ ├── post-gate                                              │
│  │ │    delta = consensus − pred; limit = K·σ_consensus       │
│  │ │    |delta| > limit → rejected (see §8)                   │
│  │ └── _check_triggers_locked()                              │
│  │      σ_avg capture, reproduction, death, spread-gate      │
│  └─────────────────────────────────────────────────────────┘
└─────────────────────────────┬────────────────────────────────┘
                              ▼
┌──────────────────────────────────────────────────────────────┐
│  _apply_new_sync_locked(...)                                 │
│  not applied? → reject-streak logic (see §8.3)              │
│  primary sync? → anchor=best_utc, rate=prior                │
│  |phase_err|>100ms? → reset                                 │
│  otherwise: predicted, phase_err, anchor += KP·phase_err,   │
│             sign-constancy, drift-confirmation              │
│             publish clock snapshot, persist drift           │
└──────────────────────────────────────────────────────────────┘
```

### PLL scheme

```
                          ┌──────────────────────────┐
                          │  utc = mono + offset     │
                          │  offset(t) = anchor_o    │
                          │    + rate·(t−anchor_m)   │
                          └─────────────┬────────────┘
                                        │
    new_target_offset ◄── consensus_offset ◄── _process_round_locked
                                        │
                                        ▼
                          ┌──────────────────────────┐
                          │ dt = t_now − anchor_m    │
                          │ predicted = anchor_o +   │
                          │            rate·dt       │
                          │ phase_err = target −     │
                          │             predicted    │
                          └─────────────┬────────────┘
                                        │
                ┌───────────────────────┼───────────────────────┐
                ▼                       ▼                       ▼
        |phase_err|>100ms        |phase_err|>20ms           normal
          RESET:                  kp = 0.40                kp = 0.10
          anchor=target           boosted                 standard
          rate = prior            catch-up
```

### Post-gate reject branch (see §8)

```
┌──────────────────────────────────────────────────────────────┐
│  post-gate: |consensus − pred| > POST_GATE_K·σ_consensus      │
└─────────────────────────────┬────────────────────────────────┘
                              ▼
              ┌──────────────────────────────┐
              │  _consecutive_rejects += 1   │
              └───────────────┬──────────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
     streak < 50                     streak ≥ 50
              │                               │
              ▼                               ▼
   applied = False                  force-apply
   · applied_offsets untouched       · streak = 0
   · _recent_observed updated        · applied = True
   · σ_consensus × (1/0.99)^streak   · normal PLL path
   · anchor:                         · prediction rebuilt
       streak ≤ 1 → gate pred        from the new applied
       streak ≥ 2 → 0.7·pred + 0.3·raw
   · rate reset to prior if streak ≥ 4
   · sign-constancy on phase_err_reject
   · drift-confirm skipped
   · no drift persist to DB
   · publish clock snapshot if rate changed
```

### Instance lifecycle

```
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
```

---

## Part III. Registry of constants

### NTP servers

| Name | Value | Notes |
|---|---|---|
| `DEFAULT_NTP_SERVERS` | 16 active | List of public NTP servers. |
| `NTP_EPOCH_OFFSET_SEC` | 2208988800 | NTP epoch (1900) → Unix (1970) offset. |
| `NTP_PACKET_SIZE` | 48 | NTPv4 packet size. |
| `NTP_PORT` | 123 | NTP port. |
| `PER_SERVER_HISTORY` | `max(len(DEFAULT_NTP_SERVERS), 10)` = 16 | Length of the min-delay queue and accept_window. |
| `QUERY_ATTEMPT_SPACING_SEC` | 1.0 | Spacing between attempts within a round. |
| `QUERY_ATTEMPT_SPACING_NS` | 1_000_000_000 | Same in ns. |
| `NTP_QUERY_TIMEOUT_SEC` | 2 | Timeout of a single NTP request. |
| `DNS_QUERY_TIMEOUT_SEC` | 5 | DNS timeout. |
| `NTP_RESOLVING_TIMEOUT_NS` | 3.6e12 (1 h) | DNS re-resolution period. |
| `VALIDATE_ORIGIN` | `'1'` from env | Origin echo check. |

### Cycle and threads

| Name | Value | Notes |
|---|---|---|
| `DEFAULT_INITIAL_INTERVAL_SEC` | 60 | Main interval between rounds. |
| `SYNC_THREAD_SLOTS` | 2 | Number of parallel threads. |
| `second_sync_thread_delay` | `sync_interval // 2` | Offset of the second thread (30 s). |
| `_min_pll_update_interval_ns` | `max(delay//2, 5)·1e9` | Protection against PLL "sticking" (15 s). |
| `WATCHDOG_INTERVAL_SEC` | 30 | Thread liveness check frequency. |
| `WATCHDOG_STOP_JOIN_SEC` | 3.0 | Watchdog join timeout. |
| `SYNC_THREAD_STOP_JOIN_SEC` | 15.0 | Overall stop timeout. |
| `PRE_START_JOIN_SEC` | 5.0 | Join timeout for "leftover" threads. |
| `KEEP_AWAKE_REFRESH_SEC` | 30 | ES_SYSTEM_REQUIRED re-set. |

### PLL

| Name | Value | Notes |
|---|---|---|
| `PLL_KP` | 0.10 | Fraction of phase_err added to offset per round. |
| `PLL_KP_MEDIUM` | 0.40 | Boosted KP for a medium jump. |
| `PLL_KP_REJECT` | 0.30 | KP for streak ≥ 2 (post-gate reject). |
| `PLL_KI` | 0.01 | Fraction of phase_err/dt going into rate. |
| `PLL_RATE_LIMIT` | 100e-6 (±100 ppm) | Drift estimate limit. |
| `PHASE_JUMP_THRESHOLD_NS` | 100_000_000 (100 ms) | Reset anchor + rate. |
| `PHASE_MEDIUM_JUMP_NS` | 20_000_000 (20 ms) | Boosted KP threshold. |
| `RATE_SPRING_BASE_PPM` | 0.02 | Base rate step (fallback). |
| `RATE_SPRING_MAX_PPM` | 0.10 | Maximum rate step (fallback). |
| `RATE_SPRING_SIGMA_FRACTION_BASE` | 0.05 | Base step = 0.05·σ_prior. |
| `RATE_SPRING_SIGMA_FRACTION_MAX` | 0.25 | Max step = 0.25·σ_prior. |
| `RATE_SPRING_T_SCALE` | 3.0 | Confidence = (|t|−T)/SCALE. |
| `RATE_PRIOR_PULL` | 0.00 | Extra pull of rate toward prior. |
| `RATE_CLAMP_FROM_PRIOR_PPM` | 2.0 | ±window around prior. |
| `RATE_DETECT_WINDOW_N` | 30 | phase_errors window. |
| `RATE_DETECT_MIN_N` | 20 | Minimum for statistics. |
| `RATE_T_THRESHOLD` | 2.5 | Threshold of |t| of the mean. |
| `RATE_Z_THRESHOLD` | −2.0 | Runs z: negative = sign sticking. |

### Colony bias

| Name | Value |
|---|---|
| `COLONY_BIAS_GAIN` | 0.05 |
| `COLONY_BIAS_MAX_NS` | 50_000_000 (50 ms) |
| `COLONY_BIAS_DECAY` | 0.99 |
| `COLONY_BIAS_DECAY_FLOOR` | 10_000 (10 µs) |
| `K_THRESHOLD` | 1.0 |

### Drift confirmation

| Name | Value |
|---|---|
| `DRIFT_CONFIRM_RATE_DIVERGE_PPM` | 3.0 |
| `DRIFT_CONFIRM_ALPHA` | 0.5 |
| `DRIFT_SEED_MIN_SAMPLES` | 5 |
| `DRIFT_COLD_START_DIVERGE_PPM` | 3.0 |
| `DRIFT_OUTLIER_K` | 3.0 |

### Clock Snapshot / TTL

| Name | Value |
|---|---|
| `CLOCK_SNAPSHOT_TTL_BASE_SEC` | 2.0 |
| `CLOCK_SNAPSHOT_TTL_QUALITY_FLOOR` | 0.25 |
| `CLOCK_SNAPSHOT_TTL_QUALITY_CEIL` | 2.0 |
| `CLOCK_SNAPSHOT_TTL_ABS_MIN_SEC` | 30.0 |
| `CLOCK_SNAPSHOT_TTL_ABS_MAX_SEC` | 600.0 |
| `CLOCK_SNAPSHOT_TTL_RECENT_N` | 15 |
| `CLOCK_SNAPSHOT_ACCURACY_DEFAULT_NS` | 5_000_000 (5 ms) |
| `CLOCK_SNAPSHOT_PRECISION_CACHE_MAX` | 16 |

### Colony

| Name | Value |
|---|---|
| `THRESHOLD_MIN_NS` | 1_000 (1 µs) |
| `COLONY_THRESHOLD_FLOOR_NS` | 2_500_000 (2.5 ms) |
| `THRESHOLD_SHRINK_FLOOR` | 0.99 |
| `COHERENCE_THRESHOLD_NS` | 5_000_000 (5 ms) |
| `HISTORY_MAX_LEN` | 100 |
| `ACCEPT_WINDOW_SIZE` | 16 |
| `DEATH_LOW_WINDOWS` | 5 |
| `DEATH_ACCEPT_THRESHOLD` | 0.05 |
| `REPRODUCTION_LAG_L` | 5 |
| `FAVORITE_HYST` | 0.15 |

### Dominance / warmup

| Name | Value |
|---|---|
| `ALPHA_SIGNIFICANCE` | 0.1 |
| `DOMINANT_MIN_HISTORY` | 16 |
| `SIGMA_WARMUP_RECORDS` | 5 |
| `SIGMA_WARMUP_TIMEOUT_MULT` | 3 |
| `SIGMA_WARMUP_MIN_SURVIVORS` | 2 |
| `WARMUP_MIN_SURVIVORS` | 2 |
| `DOMINANT_STDEV_RATIO` | 0.7 |
| `DOMINANT_MIN_SERVERS` | 2 |
| `DOMINANT_MIN_DEVS` | 2 |
| `SIGMA_AVG_SANITY_MAX_NS` | 10_000_000 (10 ms) |

### Population / divine

| Name | Value |
|---|---|
| `MIN_POPULATION` | 5 |
| `DIVINE_QUEUE_MAX` | 5 |
| `DIVINE_ACCEPT_RATE_THRESHOLD` | 0.1 |
| `MAX_POPULATION_RATIO` | 0.7 |
| `MAX_POPULATION_MIN_SERVERS` | 7 |

### History / drift votes

| Name | Value |
|---|---|
| `HISTORY_VOTE_SHORT_WINDOW` | 15 |
| `HISTORY_VOTE_MIN_SAMPLES` | 5 |
| `DRIFT_WINDOW` | 300 |
| `DRIFT_MIN_SAMPLES` | 30 |

### Consensus gate

| Name | Value |
|---|---|
| `POST_GATE_K` | 1.0 |
| `POST_GATE_FORCE_APPLY_AFTER` | 50 |
| `REJECT_RATE_RESET_STREAK` | 4 |
| `REJECT_RATE_RESET_GAIN` | 1.0 |
| `REJECT_RATE_RESET_MIN_DIVERGE_PPM` | 1.0 |
| `DISABLE_POST_GATE` | 0 |
| `DISABLE_SHORT_VOTE` | = DISABLE_POST_GATE |
| `DISABLE_DRIFT_VOTE` | = DISABLE_POST_GATE |
| `CONSENSUS_MATRIX_MIN` | 2 |
| `SPREAD_HISTORY_LEN` | 16 |
| `SPREAD_HISTORY_MIN` | 5 |
| `REPRODUCTION_SPREAD_MULT` | 2.0 |

### Environment flags

| Name | Default | Notes |
|---|---|---|
| `TIME_SYNC_KEEP_AWAKE` | `'1'` | `0` — disable keep-awake. |
| `TIME_SYNC_VALIDATE_ORIGIN` | `'1'` | `0` — disable origin echo. |
| `TIME_SYNC_MODE` | — | Sprint 2, not implemented. |

---

## Part IV. Runtime state

### TimeSyncService

| Name | Init | Type | Notes |
|---|---|---|---|
| `ntp_servers` | list | list[str] | Active servers. |
| `ntp_servers_resolved` | `{}` | dict | DNS → IP. |
| `sync_interval` | 60 | int | Round period. |
| `second_sync_thread_delay` | 30 | int | Second thread offset. |
| `_server_meta` | `{}` | dict | NTP packet metadata. |
| `_anchor_offset` | 0 | int | UTC reference at the anchor. |
| `_anchor_mono` | 0 | int | Anchor point (mono_ns). |
| `_rate` | 0.0 | float | Drift (dimensionless). |
| `_clock_snapshot` | `(0,0,0.0)` | tuple | Lock-free snapshot. |
| `_clock_snapshot_ttl_ns` | 120e9 | int | Snapshot TTL. |
| `_clock_snapshot_accuracy_ns` | 5e6 | int | Snapshot accuracy. |
| `_clock_snapshot_full` | 5-tuple | tuple | Atomic snapshot. |
| `_precision_cache` | OrderedDict | LRU | Precision cache. |
| `_target_offset` | 0 | int | Target offset. |
| `_last_phase_error` | None | Optional[int] | Last phase_err. |
| `_slew_error_history` | deque(100) | SlewRecord | Consensus history. |
| `_colony_bias` | `{}` | dict | Accumulated bias. |
| `_raw_proposed_history` | deque(100) | tuple | Raw history for bias. |
| `_consensus` | Consensus | — | The colony. |
| `_rate_prior` | 0.0 | float | Prior from the DB. |
| `_rate_spring_base_ppm` | computed | float | Base rate step. |
| `_rate_spring_max_ppm` | computed | float | Max rate step. |
| `_last_drift_persist_tick` | 0 | int | Persist pause. |

### Consensus

| Name | Init | Type |
|---|---|---|
| `population` | `[init_pop]` | list[AlgorithmInstance] |
| `_next_id` | init_pop | int |
| `_tick` | 0 | int |
| `reproduction_allowed` | True | bool |
| `_spreads` | deque(16) | deque |
| `_mean_spread_ns` | None | Optional[float] |
| `noise_ref_ns` | None | Optional[float] |
| `_recent_observed` | deque(300) | deque[int] |
| `applied_offsets` | deque(300) | deque[Tuple[int,int]] |
| `_last_short_vote` | None | Optional[int] |
| `_last_drift_prediction` | None | Optional[int] |
| `_last_drift_slope` | None | Optional[float] |
| `_last_apply_mono_ns` | 0 | int |
| `_consecutive_rejects` | 0 | int |
| `_occupied` | `set()` | set |
| `cold_start_generation` | 0 | int |
| `_lineage_queue` | `[id...]` | list[int] |

### AlgorithmInstance

| Name | Init | Type |
|---|---|---|
| `id` | n | int |
| `lineage_id` | n / parent | int |
| `favorite` | None | Optional[str] |
| `banned_servers` | `set(_occupied)` | set |
| `own_history` | deque(100) | SlewRecord |
| `own_history_rejected` | deque(100) | SlewRecord |
| `own_reference_offset` | None | Optional[int] |
| `own_threshold_ns` | None | Optional[int] |
| `own_sigma_avg_ns` | None | Optional[int] |
| `armed_lock_until_tick` | `-1e9` | int |
| `warmup_excluded` | `set()` | set |
| `warmup_started_tick` | `_tick` | int |
| `accept_window` | deque(16) | deque[bool] |
| `low_accept_windows` | 0 | int |
| `deathbed_used` | False | bool |
| `born_tick` | `_tick` | int |
| `reproduced_count` | 0 | int |
| `max_offspring` | 2 | int |
| `last_reproduced_tick` | `-1e9` | int |
| `last_selection_mode` | None | Optional[str] |
| `last_best_candidate` | None | Optional[str] |
| `last_scores` | None | Optional[Dict[str,float]] |

### NamedTuple

**ServerMeta**: leap, version, mode, stratum, poll, precision, root_delay_raw, root_disp_raw, ref_id, ref_ts_ns, last_update_mono_ns. Derived: `root_delay_ns`, `root_disp_ns` (16.16 fixed-point).

**SlewRecord**: timestamp_ns, diff_ns, threshold_ns, instance_id, favorite, servers, ref_ns, is_cold_start, matrix, matrix_meta, is_stale, rejected_offset_ns.

**ClockSnapshot**: anchor_mono_ns, anchor_offset_ns, rate, ttl_ns, accuracy_ns.

---

## Part V. Public API

```python
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
```

### UTC restoration from a snapshot

```
utc = now_mono + anchor_offset_ns + round(rate · (now_mono − anchor_mono_ns))
```

Guarantee: for `now_mono ∈ [anchor_mono, anchor_mono + ttl_ns]`:

```
|utc_restored − utc_true| ≤ accuracy_ns
```

provided that the consensus has not degraded more than at the moment of the snapshot.

---

**Accuracy ceiling:**

| Source | Accuracy |
|---|---|
| Public NTP + algorithms | 0.7–1.5 ms |
| + PLL + colony bias + gates | 0.4–0.8 ms |
| + GPS server with calibration | 0.05–0.3 ms |
| + PTP with HW timestamping | 1–10 µs |

---

### Quick cheat sheet: "what is averaged where"

| What | How | Why |
|---|---|---|
| `t_ref_mono` | arithmetic over all samples of the round | neutral round center |
| `inst.own_reference_offset` | **weighted 1/delay** over accepted servers | fast servers get more trust |
| `consensus_offset` | **arithmetic over L² matrix cells** | balance time × ensemble |
| `short_vote` | **median** of the last 15 observed | resistance to a single outlier |
| `drift_slope` | **OLS** over 300 applied | true drift rate |
| instance `σ_avg` | mean of per-server σ(dev) | typical server noise |
| `σ_consensus` | σ of first differences/√2 | robust consensus noise |
| per-server `σ_s` | MAD of first differences | server noise without outliers |
| `colony_bias[srv]` | arith. over time → median over servers | common center minus noise |
| `noise_ref_ns` | **median** of σ_avg over instances | one faulty instance does not spoil the reference |

### Quick cheat sheet: "what happens on reject"

| Aspect | accept | reject |
|---|---|---|
| `applied_offsets` | updated | **untouched** |
| `_recent_observed` | updated | updated (same) |
| σ_consensus | plain | × (1/0.99)^streak |
| anchor | `predicted + KP·phase_err` | `streak ≤ 1`: gate prediction; `streak ≥ 2`: `0.7·predicted + 0.3·raw` |
| rate | sign-constancy + drift-confirm | sign-constancy only; reset-to-prior at streak ≥ 4 |
| drift persist to DB | yes | no |
| force-apply | — | after 50 consecutive rejects |


# Chrony vs T.B.O.T — A Comparative Overview

This document compares two approaches to NTP-based time synchronization: **Chrony**, a mature, general-purpose implementation, and **T.B.O.T**, a specialized system built around a colony of filter instances.

The comparison is structured in three layers: architectural differences, areas where T.B.O.T offers advantages, and areas where Chrony offers advantages.

---

## 1. Fundamental architectural differences

These are not differences in tuning, but in underlying approach.

### 1.1 Rate estimation

| | Chrony | T.B.O.T |
|---|---|---|
| Method | **Direct linear regression** over the history of a source | **PI-PLL + sign-constancy + drift-confirmation + prior from DB** |
| Output | Offset and rate estimated jointly | Offset and rate estimated separately; rate is a filtered integrator |
| Convergence to a drift change | **Fast** (tens of samples) | **Slow** (hundreds of rounds, bounded to ±2 ppm around prior) |
| Robustness | Moderate — regression is sensitive to clustered outliers | **High** — multi-layer protection |

Chrony trusts the regression: if the slope shifts, the clock's rate has likely changed. This is fast and accurate in clean conditions.

T.B.O.T does not trust any single source of truth about rate. It keeps the rate within a narrow corridor around the prior and admits changes only through three independent triggers. This is slower but more resistant to "phantom" drift changes caused by network noise.

**Consequence:** Chrony performs better on rapid temperature/load changes. T.B.O.T performs better when drift is stable but the network is not.

### 1.2 Source of truth

| | Chrony | T.B.O.T |
|---|---|---|
| Unit | Source | Instance-filter + consensus matrix |
| Selection | Intersection algorithm (mathematically strict) | Colony evolution + post-gate + sign-constancy |
| Falseticker rejection | Via intersection of confidence intervals | Via reject-streak + force-apply |
| Final estimate | Weighted average over survivors | Arithmetic mean over L² cells of the ergodic matrix |

Chrony **discards** incorrect sources entirely (falseticker detection). T.B.O.T **filters** every measurement from every server and builds its estimate from what passes.

The first is cleaner from a statistical standpoint. The second is more robust when "correct" sources are few or are themselves noisy.

### 1.3 Role of history

| | Chrony | T.B.O.T |
|---|---|---|
| Between sessions | `driftfile` — a **single number** (the last rate) | `drift_history` — a **time series** with MAD-based statistics |
| Provides | A coarse starting point | A **statistically meaningful prior** (n, σ, span) |
| Example | — | n=14/14, σ=0.177 ppm, span=131 h |

This is arguably the **most underappreciated advantage of T.B.O.T**. Chrony's `driftfile` is a cache of the last value; if it was written during an anomaly, Chrony will drift at startup. The T.B.O.T prior is a robust median with K·MAD outlier rejection. It does not break from a single bad session.

### 1.4 Polling adaptivity

| | Chrony | T.B.O.T |
|---|---|---|
| Interval | Adaptive, 64–1024 s | Fixed 60 s × 2 threads = 30 s |
| Logic | Stable clock → poll less often | Constant sampling density |

Chrony conserves traffic and CPU. T.B.O.T deliberately maintains **high density** — two samples per minute — so the filter and consensus have data even in noisy periods.

**Consequence:** Chrony is better for mobile devices on battery. T.B.O.T is better for servers where traffic is not a constraint and accuracy matters.

### 1.5 Hardware timestamping

| | Chrony | T.B.O.T |
|---|---|---|
| HW timestamping | **Yes** (`SO_TIMESTAMPING`) | No (`time.monotonic_ns()` around `sendto`/`recvfrom`) |
| Achievable precision | **Down to microseconds** on supported NICs | Limited by syscall and scheduler (tens of microseconds) |

This is a **fundamental** limitation of T.B.O.T. Even with an ideal LAN server, one cannot go below ~50–100 µs without HW timestamping, because every NTP operation incurs 10–50 µs in the OS scheduler.

Chrony is a mature tool in this respect, with two decades of cross-platform optimization.

---

## 2. Advantages of T.B.O.T

### 2.1 Per-server bias integrator

**Unique.** Neither Chrony nor ntpd subtracts a constant server bias as a separate entity. Chrony responds to bias through the common offset, and it is "smeared" across the whole estimate.

T.B.O.T maintains `_colony_bias[srv]` — a separate integrator per server with a soft threshold `|delta| > K·σ_s`. This allows **simultaneous** use of a server with +3 ms bias and a server with zero bias without skewing the estimate.

### 2.2 Multi-layer outlier protection

Chrony defends itself with an intersection algorithm and a median. T.B.O.T uses **four independent layers**:

1. Instance threshold (±2.5–7 ms)
2. Post-gate (K·σ_consensus)
3. Reject-streak reset of rate to prior
4. Force-apply after 50 rejects

The first layer is local, the second is global, the third stabilizes, the fourth guarantees no dead-lock. No comparable composition exists in mainstream NTP implementations.

### 2.3 Dead-lock guarantee

`POST_GATE_FORCE_APPLY_AFTER = 50` — if the gate sticks, after 50 rounds the system **will** apply the consensus and rebuild the prediction. Chrony in a similar situation (prolonged noise) simply converges slowly; ntpd may enter a "panic threshold" and refuse to correct the clock.

The T.B.O.T behavior is explicit: "enough rejection, accept reality and rebuild."

### 2.4 Ergodic consensus matrix

`_select_diverse_cells` maximizes diversity of servers and time moments in an L×L matrix. This is closer in spirit to **ergodic theory** than to classical NTP averaging. Each cell is an independent measurement at its own moment; the mean over L² cells is balanced across time and ensemble.

Chrony weights sources by delay and statistics but does not construct such a structured grid.

### 2.5 Snapshot with an accuracy guarantee

`get_clock_snapshot(precision_ns=500_000)` returns a snapshot **with an explicit guarantee** `accuracy_ns ≤ precision_ns`, or `None`. The consumer receives not "the best available" but an **obligation**: "within this time window the error will not exceed X."

Chrony reports an error estimate via `chronyc tracking`, but without a formal guarantee and without a precision selector.

### 2.6 Telemetry

T.B.O.T's telemetry is at the level of a **research bench**: per-instance histories, matrices, per-server σ, bias, accept rate, favorite selection, armed locks. Chrony's telemetry (`chronyc sources -v`, `chronyc tracking`) exists but is substantially smaller in scope.

### 2.7 Robustness under noise

An observed snapshot: **74% rejects, clock held within 0.96 ms**. This works because rate is pinned to prior and extrapolation is clean. Chrony under 74% source loss falls back to "last known offset + driftfile degradation" and slowly drifts.

---

## 3. Advantages of Chrony

### 3.1 Hardware timestamping

A fundamental precision advantage. Even with a LAN server, T.B.O.T cannot go below ~50–100 µs. Chrony with `hwtimestamp` on an Intel i210 reaches **single-digit microseconds**.

### 3.2 Adaptive polling

Fixed 60 s in T.B.O.T. Chrony adapts: 64 s on stable clocks, 1024 s on very stable ones, faster during divergence. T.B.O.T generates more traffic than necessary in stable mode and does not accelerate in unstable mode.

### 3.3 Simplicity

- ~50 constants in T.B.O.T
- Five distinct σ definitions
- Four types of reject logic
- A colony with evolution, spawn, and divine birth

A misconfigured parameter is difficult to notice without telemetry. Chrony has two decades of testing on millions of systems.

### 3.4 NTP server capability

Chrony can serve time to other machines on the network (`allow`). T.B.O.T exposes `get_utc_ns()` but is not an NTP server without an additional wrapper.

### 3.5 Protocol coverage

Chrony supports NTP, PTP (hardware and software), and reference clocks (GPS, PPS, DCF77). T.B.O.T implements an NTPv4 client only.

### 3.6 Adaptation to drift changes

`RATE_CLAMP_FROM_PRIOR_PPM = 2.0` — a ±2 ppm window around the prior. If the actual drift changes (e.g. a laptop switching power profiles), Chrony catches up in minutes; T.B.O.T takes tens of minutes, or may not adapt at all if the new operating point lies outside the window.

### 3.7 No source specialization

`AlgorithmInstance` objects share the same logic and evolve through death/reproduction over time. This is elegant, but there is **no specialization**: no instances that "trust stratum-1 only," no instances that "work only with LAN." Chrony permits configuring sources with priorities and types.

---

## 4. Summary table

| Criterion | Advantage | Comment |
|---|---|---|
| Accuracy on public NTP | Tie | Both ~1 ms |
| Accuracy on LAN | **Chrony** | HW timestamping reaches µs |
| Accuracy with GPS/PPS | **Chrony** | Reference clocks out of the box |
| Robustness under noise | **T.B.O.T** | 74% rejects → 1 ms |
| Adaptation to drift change | **Chrony** | Regression converges faster |
| Long-term stability | **T.B.O.T** | Prior with MAD statistics |
| Dead-lock protection | **T.B.O.T** | Force-apply |
| Per-server bias | **T.B.O.T** | Unique functionality |
| Filter composition | **T.B.O.T** | Four layers |
| Traffic economy | **Chrony** | Adaptive polling |
| Simplicity | **Chrony** | 20 years of testing |
| Telemetry | **T.B.O.T** | Research-grade |
| Snapshot accuracy guarantee | **T.B.O.T** | `precision_ns` API |
| NTP server | **Chrony** | `allow` |
| PTP/GPS | **Chrony** | Out of the box |
| Cross-platform | **Chrony** | All UNIX, Windows port |
| Windows Modern Standby | **T.B.O.T** | Explicit protection |
| Mobility | **Chrony** | Adaptive polling |

---

## 5. General observation

The two systems address different problems and are not direct competitors.

**Chrony** is a **general-purpose tool**. It is designed to work wherever any time source is available and to extract the maximum achievable precision with minimal intervention. It is a "Swiss army knife" of time synchronization.

**T.B.O.T** is a **specialized system** for a specific scenario: **a Windows/Linux server without GPS, but with a requirement to hold ~1 ms stably under high network noise and constant Modern Standby pressure**. In this scenario it addresses the problem more fundamentally than Chrony because it:

- accounts for per-server bias,
- uses a prior with robust statistics,
- prevents the prediction from drifting without a guaranteed exit,
- operates at 74% rejects,
- provides a formal accuracy guarantee to the consumer.

A task to "build a general-purpose replacement for Chrony" would not be winnable on scope. The T.B.O.T design, judging by the code, targets a different objective: **holding time as robustly as possible in an unfavorable environment**. In that niche, T.B.O.T is objectively stronger.

---

## 6. Possible exchanges

**What T.B.O.T could borrow from Chrony:**

1. Hardware timestamping (would yield 50–100× precision on LAN if a LAN server becomes available).
2. Adaptive polling (would conserve traffic in stable mode).
3. Support for reference clocks (GPS/PPS) — would broaden applicability.

**What Chrony could borrow from T.B.O.T:**

1. Per-server bias integrator.
2. Formal accuracy guarantee for clock snapshots.
3. Robust prior from a time series rather than a single value.
4. Force-apply as a dead-lock protection mechanism.