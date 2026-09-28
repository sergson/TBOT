# core/time_sync.py
# Copyright (c) 2026 sergson (https://github.com)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.
#
# Architecture: a colony of autonomous filter instances. Each instance
# filters its own raw observations (attempt_idx = position in ordered)
# and passes only those that passed the filter into consensus.
# Consensus assembles a square Size×Size matrix, where
# each row differs by the moment of polling the series of NTP servers,
# the number of servers equals the number of moments — balance across time and ensemble.
# From the matrix, an arithmetic mean correction is computed — the target correction.

# Telemetry: each queue instance is in SlewRecord.
# Each record is tagged with instance_id,
# allowing analysis of the trajectory of an individual instance.

# Time correction rules:
#diff_ns > 0  ⟺  TBOT lags ⟺ utc_NTP > utc_TBOT ⟺ TBOT time must be moved FORWARD (in +)
#diff_ns < 0  ⟺  TBOT is fast ⟺  TBOT time must be moved BACK (in −)

# Windows classifies background applications as "not important",
# moves them to E-cores and limits CPU share.
# But _keep_awake_worker disables this via SetProcessInformation(ProcessPowerThrottling)
# and raises priority to ABOVE_NORMAL.
# If you see the following anomalies: NTP rounds take 30/60/120/240 seconds instead of 4 in the log
# — check: Windows version (10 1709+ required) and whether enterprise policy blocks it.

import os
import sys
import math
import time
import threading
import struct
from typing import Optional, Tuple, Dict, List, Any
import statistics
from collections import deque, defaultdict, OrderedDict
from .logger import perf_logger
from .database import load_drift_history, append_drift_sample
import concurrent.futures
from typing import NamedTuple
from dataclasses import dataclass, field
import socket, ipaddress
from datetime import datetime, timezone

logger = perf_logger.get_logger('time_sync', 'time')

# Deferred logging: inside critical sections (under lock)
# we do not use perf_logger, but accumulate messages in a list.
# Then — emission, perf_logger strictly outside locks.
# This eliminates both types of deadlocks: self-deadlock (Lock vs RLock) and
# lock-ordering deadlock between the module lock and the logger lock.
def _emit_deferred_logs(logs: List[Tuple[str, str]]) -> None:
    for level, msg in logs:
        getattr(logger, level)(msg)

#Default NTP servers, similar to testing
DEFAULT_NTP_SERVERS=[
            'time.google.com',
            'ntp3.vniiftri.ru',
            'vniiftri.khv.ru',
            'ptbtime1.ptb.de',
            'time.cloudflare.com',
            'time.aws.com',
            'ntp.msk-ix.ru',
            'ts1.aco.net',
            'time1.ams-ix.net',
            'ntp.se',
            'ntp1.inrim.it',
            'ntp.metas.ch',
            'ntp.kriss.re.kr',
        ]

# Windows Modern Standby: by default we ask the system not to go into
# idle-standby while the service is alive. Disabled via env
# TIME_SYNC_KEEP_AWAKE=0 (for example, on a laptop where battery matters).
KEEP_AWAKE_ENABLED = os.environ.get('TIME_SYNC_KEEP_AWAKE', '1') == '1'
KEEP_AWAKE_REFRESH_SEC = 30   # frequency of re-setting the request

# Main interval between cycles (ticks) by default
DEFAULT_INITIAL_INTERVAL_SEC = 60

# Offset between NTP epoch (1900-01-01) and Unix epoch (1970-01-01), seconds.
NTP_EPOCH_OFFSET_SEC = 2208988800

# --- NTP polling parameters ---
# Spacing between attempts within a round
# Recommendation: maximum spacing = (ATTEMPTS−1)·SPACING = 4 s.
# At typical drift ~50 ppm this is ≈200 µs,
# further increase of spacing without drift compensation will lead to error.
QUERY_ATTEMPT_SPACING_SEC = 1.0   #seconds
QUERY_ATTEMPT_SPACING_NS = int(QUERY_ATTEMPT_SPACING_SEC * 1_000_000_000) #nanoseconds

#Server history queue length
PER_SERVER_HISTORY = max(len(DEFAULT_NTP_SERVERS),10)

NTP_QUERY_TIMEOUT_SEC = 2          # timeout of a single NTP request
NTP_QUERY_TIMEOUT_SAFE_SEC = NTP_QUERY_TIMEOUT_SEC     # protective timeout for NTP requests
NTP_RESOLVING_TIMEOUT_NS = 3600 * 1000_000_000     # DNS resolution timeout for NTP servers, nanoseconds
DNS_QUERY_TIMEOUT_SEC = 5 # timeout of a single DNS request

# Origin echo validation (protection against race/spoofing). Can be disabled for debugging.
VALIDATE_ORIGIN = os.environ.get('TIME_SYNC_VALIDATE_ORIGIN', '1') == '1'
# --- NTP ---
NTP_PACKET_SIZE = 48 #NTP packet length
NTP_PORT = 123 #NTP port

# --- Parameters of automatic time adjustment control, PLL ---
# Proportional-Integral controller (PLL).
# Time constant ~ 1/PLL_KP rounds until convergence of time shift (phase, anchor),
# Time constant ~ 1/PLL_KI rounds until drift (rate) convergence.
PLL_KP         = 0.10        # coefficient (fraction) of error introduced into shift correction per round
PHASE_MEDIUM_JUMP_NS = 20_000_000       # threshold > 20 ms — for applying boosted coefficient
PLL_KP_MEDIUM = 0.40                    # coefficient (fraction) of error introduced into shift correction per round, boosted
PLL_KP_REJECT = 0.3          # coefficient (fraction) of error introduced into correction per round for rejected rounds
PLL_KI         = 0.01        # coefficient (fraction) of error introduced into drift (rate) per round
PLL_RATE_LIMIT = 100e-6      # ±100 ppm — upper limit of drift (rate)
RATE_SPRING_BASE_PPM  = 0.02    # base drift step (rate), ppm/round
RATE_SPRING_MAX_PPM   = 0.1    # base maximum drift step (rate) at full confidence, ppm/round
RATE_SPRING_SIGMA_FRACTION_BASE = 0.05   # coefficients to historical drift values base = 0.05·σ_prior
RATE_SPRING_SIGMA_FRACTION_MAX  = 0.25   # coefficients to historical drift values max  = 0.25·σ_prior
RATE_SPRING_T_SCALE   = 3.0    # at |t| > threshold+SCALE — full confidence
RATE_PRIOR_PULL       = 0.00   # additional pull of drift (rate) to historical value, fraction per round, optional
RATE_CLAMP_FROM_PRIOR_PPM = 2.0  # maximum deviation of drift (rate) from historical ppm ≈ 5σ_prior

# --- PLL protection against "stuck" updates and phase jumps ---
# If less than this interval has passed between two PLL updates —
# skip the update. For example, when both sync streams were suspended
# (screen sleep, Modern Standby) and woken simultaneously: their rounds
# complete in the same millisecond, dt in the denominator of I-correction
# becomes ~0, rate instantly saturates to ±PLL_RATE_LIMIT.
# Default value = half the interval between sync streams,
# computed in __init__ as self._min_pll_update_interval_ns.

# Phase jump threshold.
# If |phase_error| is larger — this is not drift but an external event:
# sleep/resume, system time step from w32time,
# NTP jump on the server side.
# Catching up slowly is not acceptable — it will take tens of minutes.
# Reset anchor to the new value, zero out rate.
PHASE_JUMP_THRESHOLD_NS = 100_000_000   # > 100 ms — reset

#Detector of sign constancy of correction for PLL rate
RATE_DETECT_WINDOW_N   = 30     # length of phase_errors window
RATE_DETECT_MIN_N      = 20     # minimum for statistics
RATE_T_THRESHOLD       = 2.5    # |t| of the mean
RATE_Z_THRESHOLD       = -2.0   # runs z: negative = sign sticking

# Colony bias integrator (constant server shift), additional control:
COLONY_BIAS_GAIN = 0.05            # integrator speed per round
COLONY_BIAS_MAX_NS = 50_000_000    # compensation limit ±50 ms
COLONY_BIAS_DECAY = 0.99           # leakage of bias value of inactive servers per round
COLONY_BIAS_DECAY_FLOOR = 10_000   # below 10 µs — bias is removed
K_THRESHOLD = 1.0                  # soft-threshold: |delta| > K·σ_srv, threshold separating noise/bias

# --- Colony parameters ---
# Absolute floor of the filter threshold of an individual instance, ns.
# Protects against zero/negative threshold; in normal operation
# it does not trigger.
THRESHOLD_MIN_NS = 1_000         # 1 µs

# Colony filter floor, ns. Below this value the instance filter threshold
# does not drop — neither during initialization nor during
# evolution. Protection against "collapse" of the colony into a narrow mode: when
# instances accept only servers very close to the reference,
# diff_ns become small → stdev small → threshold
# narrows → even fewer servers pass → self-sustaining loop.
# Starting estimate — COHERENCE_THRESHOLD_NS (5 ms): typical
# jitter of public NTP servers. On a LAN stratum-1
# can be lowered to 1–2 ms, on a slow channel — raised to
# 8–10 ms.
COLONY_THRESHOLD_FLOOR_NS = 2_500_000         # 2.5 ms
# Rate limit of filter narrowing: the threshold per round cannot drop
# by more than this factor. 0.99= 1% per round. Slower = reference manages
# to adapt, but accuracy comes later.
THRESHOLD_SHRINK_FLOOR = 0.99

ACCEPT_WINDOW_SIZE = PER_SERVER_HISTORY # length of accept-history window of an instance, ticks
DEATH_LOW_WINDOWS = 5              # how many low accept-windows in a row → death
DEATH_ACCEPT_THRESHOLD = 0.05      # fraction of accepted in window below which the window is "low"
REPRODUCTION_LAG_L = DEATH_LOW_WINDOWS # minimum ticks between two births
COHERENCE_THRESHOLD_NS = 5_000_000 # Starting estimate, ns
HISTORY_MAX_LEN = 100               # length of round history queue
# Reproduction filters.
SPREAD_HISTORY_LEN = ACCEPT_WINDOW_SIZE     # length of stdev(offsets) queue
SPREAD_HISTORY_MIN = REPRODUCTION_LAG_L     # minimum rounds before reproduction is active
REPRODUCTION_SPREAD_MULT = 2.0              # median(spreads) < MULT · median(σ_avg)
# Hysteresis of favorite selection by score (d_norm + s_norm).
FAVORITE_HYST = 0.15               # change only if score_new < score_cur·(1−H)

# Warmup rule: if at least this many servers have accumulated
# SIGMA_WARMUP_RECORDS ✓ — capture σ_avg from them,
# without waiting for the rest.
# Speeds up warmup exit, reduces the number of instances that died without children
SIGMA_WARMUP_MIN_SURVIVORS = 2
WARMUP_MIN_SURVIVORS = 2 #minimum available servers — we do not exit warmup

# Sanity threshold of σ_avg. A server with such σ (spread) physically cannot be a source of
# precise time — bimodal LAN or broken channel.
# σ_avg capture is not performed, the instance lives without trusted-status and dies through death spiral.
SIGMA_AVG_SANITY_MAX_NS = 10_000_000   # 10 ms

# --- Favorite (dominant) parameters ---
ALPHA_SIGNIFICANCE = 0.1
DOMINANT_MIN_HISTORY = ACCEPT_WINDOW_SIZE          # M_min — lower bound of observations per server
SIGMA_WARMUP_RECORDS = SPREAD_HISTORY_MIN           # ✓ records per server needed for σ_avg
SIGMA_WARMUP_TIMEOUT_MULT = 3      # warmup timeout: 3×SIGMA_WARMUP_RECORDS
DOMINANT_STDEV_RATIO = 0.7         # σ_X < 0.7·σ_others — dominance condition
DOMINANT_MIN_SERVERS = 2           # minimum required number of servers
DOMINANT_MIN_DEVS = 2              # minimum number of suitable servers

# --- Maximum population parameters ---
MAX_POPULATION_RATIO = 0.7         # fraction of number of servers when > MIN_SERVERS
MAX_POPULATION_MIN_SERVERS = 7     # below — max_population = len(servers)

# --- Divine birth parameters ---
MIN_POPULATION = 5                 # below this — colony degenerates
DIVINE_QUEUE_MAX = MIN_POPULATION  # maximum simultaneous lineage-roots
DIVINE_ACCEPT_RATE_THRESHOLD = 0.1 # accept_rate < this → stuck

# --- Consensus parameters ---
CONSENSUS_MATRIX_MIN = 2 #minimum size of consensus matrix
# Post gate filters of consensus.
# Coefficient in standard deviations of previous consensus corrections, fixes
# by how many standard deviations the new correction may go
# beyond the prediction
POST_GATE_K = 1.0
# Number of consecutive post-gate rejects after which the gate
# forcibly passes consensus_offset. Needed for the case when
# prediction has drifted away from the real drift (extrapolation did not keep up,
# external step, ...), and the gate is stuck in a closed state.
POST_GATE_FORCE_APPLY_AFTER = 50
#Parameters for resetting rate to historical value when consensus filter triggers
REJECT_RATE_RESET_STREAK = 4      # how many rejects in a row
REJECT_RATE_RESET_GAIN   = 1.0    # 1.0 = hard reset of PLL rate, 0.3 = soft pull
# threshold of closeness of rate to historical, do not reset if rate is already close to historical
REJECT_RATE_RESET_MIN_DIVERGE_PPM = 1.0

# --- Flags for disabling consensus filters---
DISABLE_POST_GATE  = 0 #1 - disables Post gate consensus filter
DISABLE_SHORT_VOTE = DISABLE_POST_GATE
DISABLE_DRIFT_VOTE = DISABLE_POST_GATE

# Consensus history vote — predictions until drift is established.
HISTORY_VOTE_SHORT_WINDOW = 15     # window for computing median, rounds
DRIFT_WINDOW = 300                 # window for computing linear regression (drift), rounds
HISTORY_VOTE_MIN_SAMPLES = SPREAD_HISTORY_MIN # minimum history, rounds, until this short_vote is not active
DRIFT_MIN_SAMPLES = 30             #minimum history, rounds, until this drift is not active
DRIFT_OUTLIER_K = 3.0            # outlier cutoff by K·MAD analysis of data stored in database

# --- Runtime drift confirmation ---
# Soft pull of self._rate to consensus._last_drift_slope on divergence.
# drift_slope — robust linear regression of applied_offsets over 300 points,
# resistant to single outliers. self._rate — I-integrator of PLL, subject to
# runaway during prolonged one-sided phase_error (bias, step).
# Activated only if |rate_ppm − slope_ppm| > DRIFT_CONFIRM_RATE_DIVERGE_PPM.
# If divergence is below threshold, rate is not touched.
# ALPHA ∈ [0, 1]:
#   0.0 → rate := slope               (full replacement)
#   0.5 → rate := 0.5·rate + 0.5·slope (50/50)
#   1.0 → mechanism disabled           (rate := rate)
DRIFT_CONFIRM_RATE_DIVERGE_PPM = 3.0 # relies on observation — if rate is noisy within ±3 ppm, and drift_slope is stable ±0.5 ppm
DRIFT_CONFIRM_ALPHA = 0.5 # 1.0 — fully disables the mechanism (rate = rate), 0.0 — fully replaces rate with slope
DRIFT_SEED_MIN_SAMPLES = 5       # minimum history, if less, we do not trust the median

# Threshold for resetting rate in _rate_prior at colony cold start.
# If |rate_ppm − slope_ppm| > threshold and drift_slope exists → rate = prior.
# If drift_slope is close or absent → rate is preserved.
DRIFT_COLD_START_DIVERGE_PPM = 3.0

# --- TTL of the clock model snapshot for external consumers ---
# PLL guarantees estimation accuracy on the horizon of the order of sync_interval;
# 2× interval — compromise between query traffic and staleness risk.
# At sync_interval=60 → TTL=120 s.
# Dynamic TTL of the clock model snapshot.
# TTL_BASE — base value (2× synchronization interval).
# quality ∈ [FLOOR, CEIL] reflects current consensus quality:
#   σ_cons_recent << σ_cons_ref  →  quality → CEIL
#   σ_cons_recent ≈ σ_cons_ref   →  quality = 1.0
#   σ_cons_recent >> σ_cons_ref  →  quality → FLOOR
CLOCK_SNAPSHOT_TTL_BASE_SEC     = 2.0    # multiplied by sync_interval
CLOCK_SNAPSHOT_TTL_QUALITY_FLOOR = 0.25
CLOCK_SNAPSHOT_TTL_QUALITY_CEIL  = 2.0
CLOCK_SNAPSHOT_TTL_ABS_MIN_SEC   = 30.0   # cannot be lower — spamming queries
CLOCK_SNAPSHOT_TTL_ABS_MAX_SEC   = 600.0  # cannot be higher — trust in stale data
CLOCK_SNAPSHOT_TTL_RECENT_N      = 15     # σ_recent window (last rounds)
CLOCK_SNAPSHOT_ACCURACY_DEFAULT_NS = 5_000_000  # 5 ms, allowable drift of the snapshot with default TTL
CLOCK_SNAPSHOT_PRECISION_CACHE_MAX = 16 #Cache length limit

# --- Watchdog and thread stop parameters ---
WATCHDOG_INTERVAL_SEC = 30         # thread liveness check frequency, 5 for test, 30 normal
WATCHDOG_STOP_JOIN_SEC = 3.0       # join timeout for watchdog on stop
SYNC_THREAD_STOP_JOIN_SEC = 15.0   # general timeout for waiting threads on stop
PRE_START_JOIN_SEC = 5.0           # join timeout for "leftover" threads on start
SYNC_THREAD_SLOTS = 2              # number of sync-thread slots

@dataclass
class ServerMeta:
    """Persistent fields of the NTP packet from the server. Updated on each
    successful response; used for diagnostics and (in the future) weighting."""
    leap: int = 0
    version: int = 0
    mode: int = 0
    stratum: int = 0
    poll: int = 0
    precision: int = 0
    root_delay_raw: int = 0
    root_disp_raw: int = 0
    ref_id: str = ""
    ref_ts_ns: int = 0
    last_update_mono_ns: int = 0

    @property
    def root_delay_ns(self) -> int:
        # 16.16 fixed-point
        sec  = self.root_delay_raw >> 16
        frac = self.root_delay_raw & 0xFFFF
        return sec * 1_000_000_000 + (frac * 1_000_000_000) // 65536

    @property
    def root_disp_ns(self) -> int:
        sec  = self.root_disp_raw >> 16
        frac = self.root_disp_raw & 0xFFFF
        return sec * 1_000_000_000 + (frac * 1_000_000_000) // 65536

class SlewRecord(NamedTuple):
    """
        Record of synchronization round history.

        Unified sign convention for all records:

            diff_ns > 0 ⟺ observed > predicted ⟺ TBOT lags NTP
                        ⟺ offset must be moved FORWARD (in +)
            diff_ns < 0 ⟺ TBOT is fast
                        ⟺ offset must be moved BACK (in −)

        Fields:
        timestamp_ns    — record moment (UTC), ns
        diff_ns         — observed − predicted, ns (see convention above);
                          None only for cold-start instance records
        threshold_ns    — round filter threshold (None — not applied)
        instance_id     — instance id; None — consensus-level record
        favorite        — favorite name (None for rejected/consensus)
        servers         — per-server slice (name, proposed, dev, delay, mark)
        ref_ns          — for accept: observed (new_target_offset);
                          for reject: gate opinion (rejected_prediction_ns)
        is_cold_start   — True for cold start record
        matrix          — ergodic consensus matrix: tuple of rows,
                          row = tuple (name, proposed_ns, delay_ns, mid_mono_ns).
                          None for instance records and for rounds
                          where the matrix was not assembled (Size < 3).
        """
    timestamp_ns: int
    diff_ns: Optional[int]
    threshold_ns: Optional[int]
    instance_id: Optional[int] = None
    favorite: Optional[str] = None
    servers: Optional[tuple] = None
    ref_ns: Optional[int] = None
    is_cold_start: bool = False
    # Ergodic matrix for consensus records.
    # Format: tuple of rows, row = tuple of (name, proposed_ns, delay_ns, mid_mono_ns).
    # None for instance records.
    matrix: Optional[tuple] = None
    matrix_meta: Optional[tuple] = None
    is_stale: bool = False #indicator of "stuck" rounds
    # None — post-gate did not trigger (or gate is off, or instance-level record).
    # int  — raw consensus_offset BEFORE saturation; ref_ns then = post-gate.
    rejected_offset_ns: Optional[int] = None

class ClockSnapshot(NamedTuple):
    """Consistent snapshot of the clock model for external consumers.

    All time fields — in nanoseconds. rate — dimensionless (1 ppm = 1e-6).

    Attributes:
        anchor_mono_ns   — reference point (local monotonic_ns).
        anchor_offset_ns — UTC reference: utc = mono + offset at the anchor point.
        rate             — dimensionless drift estimate, 1 ppm = 1e-6.
        ttl_ns           — validity window of the snapshot, ns.
        accuracy_ns      — declared upper bound of |utc_true − utc_restored|
                           within window [anchor_mono, anchor_mono + ttl_ns].

    Formula for UTC restoration:
        utc = now_mono + anchor_offset_ns + rate * (now_mono - anchor_mono_ns)

    Accuracy guarantee:
        For any now_mono within ttl_ns:
            |utc_restored − utc_true| ≤ accuracy_ns (provided that
            consensus did not degrade more than at the moment of snapshot).
    """
    anchor_mono_ns: int
    anchor_offset_ns: int
    rate: float
    ttl_ns: int
    accuracy_ns: int

# =====================================================================
# AlgorithmInstance — minimal autonomous filter object
# =====================================================================

@dataclass
class AlgorithmInstance:
    """
    Filter instance. Works autonomously, reads pool of samples,
    keeps its own histories and reference_offset. Does not slew.
    """
    id: int
    lineage_id: int  # id of the root of the lineage (id of the root instance)
    favorite: Optional[str] = None
    banned_servers: set = field(default_factory=set)

    # Accepted rounds (SlewRecord with instance_id == id).
    own_history: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAX_LEN))
    # Rounds where all available servers were rejected by the instance filter.
    own_history_rejected: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAX_LEN))

    own_reference_offset: Optional[int] = None
    own_threshold_ns: Optional[int] = None
    own_sigma_avg_ns: Optional[int] = None       # captured once
    armed_lock_until_tick: int = -10**9          # until which tick we hold armed-lock

    # σ_avg warmup: permanently excluded servers and warmup start marker.
    warmup_excluded: set = field(default_factory=set)
    warmup_started_tick: int = -10 ** 9
    warmup_stuck_logged: bool = False

    accept_window: deque = field(default_factory=lambda: deque(maxlen=ACCEPT_WINDOW_SIZE))
    low_accept_windows: int = 0
    deathbed_used: bool = False

    born_tick: int = 0
    reproduced_count: int = 0
    max_offspring: int = 2
    last_reproduced_tick: int = -10**9

    # Diagnostics of the last favorite selection (for telemetry).
    last_selection_mode: Optional[str] = None  # 'warmup' | 'score' | 'armed_lock' | '*_forced' | '*_hysteresis'
    last_best_candidate: Optional[str] = None
    last_scores: Optional[Dict[str, float]] = None

# =====================================================================
# Consensus — colony observer
# =====================================================================

class Consensus:
    """
    Holds the population of instances, the shared occupied pool and reproduction flag.
    The only point that generates (best_mono, best_utc) for the service.
    """

    def __init__(self, servers, max_population, diff_sigma,
                 initial_population: Optional[int] = None,
                 sync_interval: int = DEFAULT_INITIAL_INTERVAL_SEC):
        self.sync_interval = sync_interval
        self.servers: List[str] = list(servers)
        self.max_population: int = max_population
        self.divine_queue_max = max(DIVINE_QUEUE_MAX, self.max_population)
        self.diff_sigma: float = diff_sigma

        self.population: List[AlgorithmInstance] = []
        self._next_id: int = 0
        self._tick: int = 0
        self._lock: threading.RLock = threading.RLock()
        self._pending_logs: List[Tuple[str, str]] = []

        self.reproduction_allowed: bool = True
        self._spreads: deque = deque(maxlen=SPREAD_HISTORY_LEN)

        self._mean_spread_ns: Optional[float] = None
        self.noise_ref_ns: Optional[float] = None
        # Trajectory of all observed mean_offset (applied + rejected).
        # Feeds only short_vote/drift_prediction — prediction must not
        # get stuck when gate freezes applied_offsets.
        self._recent_observed: deque[int] = deque(maxlen=DRIFT_WINDOW)

        # Trajectory of applied mean_offset
        self.applied_offsets: deque[Tuple[int, int]] = deque(maxlen=DRIFT_WINDOW)
        self._last_short_vote: Optional[int] = None
        self._last_drift_prediction: Optional[int] = None
        self._last_drift_slope: Optional[float] = None

        # --- Extrapolation of prediction to the time since last applied ---
        # monotonic mark t_ref_mono of the last applied round.
        # Prediction (short_vote / drift_prediction) by construction
        # is tied to the moment of the last applied. During a long series
        # of rejects it "freezes" in time, while the clock continues
        # to drift. We extrapolate it by rate·(t_ref − last_apply).
        self._last_apply_mono_ns: int = 0

        # Counter of consecutive post-gate rejects.
        # Used:
        #   • to extend σ_consensus (see TimeSyncService._compute_consensus_sigma_ns);
        #   • for emergency force-apply when the gate is stuck.
        self._consecutive_rejects: int = 0

        self._occupied: set = set()
        self._prev_favorites: Dict[int, Optional[str]] = {}
        self.cold_start_generation: int = 0

        # Queue of lineage-roots (divine and initial). Maximum DIVINE_QUEUE_MAX.
        self._lineage_queue: List[int] = []

        # Cold start: immediately init_pop roots.
        # initial_population defaults to MIN_POPULATION (emergency floor),
        # but TimeSyncService passes max_population, so the colony starts
        # immediately at working size.
        init_pop = max(MIN_POPULATION,
                       initial_population if initial_population is not None
                       else MIN_POPULATION)
        for _ in range(init_pop):
            self._spawn_locked(None)

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    def atom_round(self,
               samples_by_srv,
               t_ref_mono=None,
               rate=0.0,
               seed_threshold_ns=None,
               sigma_consensus_ns: Optional[int] = None,
                   ) -> Optional[
        Tuple[int, int, List[int], int, tuple, Optional[int], Optional[tuple], Optional[tuple]]]:
        """
        Atomic round: refresh_bans → process_round → check_triggers.
        t_ref_mono — the moment of monotonic time to which the offset of the
        round is referred (delay-weighted mean mid_mono of responding servers).
        Return contract: (best_mono, best_utc, delays, best_offset_ns,
                    servers, threshold, matrix)
        seed_threshold_ns — reference for initializing own_threshold_ns of new
        instances (no history data). None — old path via stdev.
        """
        logs: List[Tuple[str, str]] = []
        try:
            with self._lock:
                self._refresh_bans_locked()
                result = self._process_round_locked(
                    samples_by_srv,
                    t_ref_mono=t_ref_mono,
                    rate=rate,
                    seed_threshold_ns=seed_threshold_ns,
                    sigma_consensus_ns=sigma_consensus_ns,
                )
                self._check_triggers_locked()
                logs = self._drain_pending_logs()
                return result
        finally:
            _emit_deferred_logs(logs)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            offsets = [i.own_reference_offset for i in self.population
                       if i.own_reference_offset is not None]
            spread = statistics.stdev(offsets) if len(offsets) >= 2 else None

            thr = (REPRODUCTION_SPREAD_MULT * self.noise_ref_ns
                   if self.noise_ref_ns is not None else None)
            ratio = (self._mean_spread_ns / thr
                     if (self._mean_spread_ns is not None and thr and thr > 0)
                     else None)

            return {
                'population': len(self.population),
                'tick': self._tick,
                'reproduction_allowed': self.reproduction_allowed,
                'spread_ns': spread,
                'favorites': [i.favorite for i in self.population],
                'occupied': sorted(self._occupied),
                'lineage_queue': list(self._lineage_queue),
                'reproduction_gate': {
                    'history_len': len(self._spreads),
                    'history_min': SPREAD_HISTORY_MIN,
                    'median_spread_ns': self._mean_spread_ns,
                    'noise_ref_ns': self.noise_ref_ns,
                    'threshold_ns': thr,
                    'ratio': ratio,
                },
                'history_vote': {
                    'short_vote_ns': self._last_short_vote,
                    'drift_prediction_ns': self._last_drift_prediction,
                    'drift_slope_ns_per_round': self._last_drift_slope,
                    'applied_len': len(self.applied_offsets),
                    'last_applied_ns': (self.applied_offsets[0][0]
                                        if self.applied_offsets else None),
                },
            }

    def get_instances_telemetry(self) -> Dict[int, Dict[str, Any]]:
        """
        Full telemetry for each live instance: both SlewRecord queues
        (with instance_id inside), filter state and dominance statistics.
        """
        with self._lock:
            out: Dict[int, Dict[str, Any]] = {}
            for inst in self.population:
                n = len(inst.accept_window)
                rate = (sum(inst.accept_window) / n) if n else None
                min_samples = self._compute_dominant_m(inst)
                out[inst.id] = {
                    'lineage_id': inst.lineage_id,
                    'favorite': inst.favorite,
                    'banned_servers': sorted(inst.banned_servers),
                    'reference_offset_ns': inst.own_reference_offset,
                    'threshold_ns': inst.own_threshold_ns,
                    'sigma_avg_ns': inst.own_sigma_avg_ns,
                    'warmup_excluded': sorted(inst.warmup_excluded),
                    'warmup_started_tick': inst.warmup_started_tick,
                    'dominant_min_samples': min_samples,
                    'armed_lock_until_tick': inst.armed_lock_until_tick,
                    'reproduced_count': inst.reproduced_count,
                    'max_offspring': inst.max_offspring,
                    'low_accept_windows': inst.low_accept_windows,
                    'deathbed_used': inst.deathbed_used,
                    'born_tick': inst.born_tick,
                    'accept_rate': rate,
                    'last_selection_mode': inst.last_selection_mode,
                    'last_best_candidate': inst.last_best_candidate,
                    'last_favorite_score': (
                        inst.last_scores.get(inst.favorite)
                        if (inst.last_scores and inst.favorite in inst.last_scores)
                        else None
                    ),
                    'last_candidate_score': (
                        inst.last_scores.get(inst.last_best_candidate)
                        if (inst.last_scores and inst.last_best_candidate in inst.last_scores)
                        else None
                    ),
                    'own_history': list(inst.own_history),
                    'own_history_rejected': list(inst.own_history_rejected),
                }
            return out

    def refresh_bans(self) -> None:
        logs: List[Tuple[str, str]] = []
        try:
            with self._lock:
                self._refresh_bans_locked()
                logs = self._drain_pending_logs()
        finally:
            _emit_deferred_logs(logs)

    def check_triggers(self) -> None:
        logs: List[Tuple[str, str]] = []
        try:
            with self._lock:
                self._check_triggers_locked()
                logs = self._drain_pending_logs()
        finally:
            _emit_deferred_logs(logs)

    def get_population_size(self) -> int:
        with self._lock:
            return len(self.population)

    # -----------------------------------------------------------------
    # Internal methods (call under _lock)
    # -----------------------------------------------------------------

    def _refresh_bans_locked(self) -> None:
        """
        Updates occupied and banned_servers, ensuring the invariant
        "one server — one owner of the favorite".

        occupied — set of servers selected by someone's favorite.

        banned_servers of an instance — occupied without its own
        favorite. This is the list of servers forbidden to the instance ONLY
        for selection as its own favorite. Reading measurements from these
        servers is allowed: each instance's data is built on its own
        attempt_idx, and instance filtering/history now include
        servers occupied by others.

        Losers in the conflict for favorite lose only favorite
        (set it to None); own_reference_offset/own_threshold_ns/
        own_sigma_avg_ns/histories/accept_window/low_accept_windows
        are preserved — they describe observations of servers, not
        the current binding. On the next round the instance will again select
        a favorite from free servers.
        """
        # Owner of each server — minimum id.
        owners: Dict[str, int] = {}
        for inst in sorted(self.population, key=lambda i: i.id):
            fav = inst.favorite
            if fav is not None and fav not in owners:
                owners[fav] = inst.id

        # Take away the favorite from losers.
        for inst in self.population:
            fav = inst.favorite
            if fav is not None and owners.get(fav) != inst.id:
                self._defer_log("debug",
                                f"Consensus: instance {inst.id} loses favorite {fav} "
                                f"(owner — {owners[fav]}); server remains in its "
                                f"available for reading, but cannot be selected as favorite"
                                )
                inst.favorite = None
                inst.armed_lock_until_tick = -10 ** 9

        # occupied — only servers of actual owners.
        occupied = set(owners.keys())
        self._occupied = occupied

        # banned = occupied − {own favorite}; for an instance without a favorite
        # banned = occupied entirely.
        for inst in self.population:
            own = {inst.favorite} if inst.favorite is not None else set()
            inst.banned_servers = occupied - own

    def _spawn_locked(self, parent: Optional[AlgorithmInstance]) -> AlgorithmInstance:
        """
        Birth of a child.

        Parent=None — cold start (initial or after complete extinction).
                       Creates a new lineage-root, registers it in the queue.
        Parent=<obj> — normal birth (dominant / deathbed). Child
                       inherits lineage_id of the parent; queue is unchanged.
        """
        if parent is None:
            inherited_bans = set()
            lineage_id = self._next_id          # root = own id
        else:
            inherited_bans = set(parent.warmup_excluded)
            lineage_id = parent.lineage_id

        inst = AlgorithmInstance(
            id=self._next_id,
            lineage_id=lineage_id,
            banned_servers=set(self._occupied),
            born_tick=self._tick,
            warmup_started_tick=self._tick,
            warmup_excluded=inherited_bans,
        )
        self._next_id += 1
        self.population.append(inst)

        if parent is None:
            if len(self._lineage_queue) < self.divine_queue_max:
                self._lineage_queue.append(inst.id)
            else:
                self._defer_log("warning",
                                f"Cold start: root queue is full "
                                f"{self._lineage_queue}; new lineage {inst.id} not registered"
                                )
        else:
            parent.reproduced_count += 1
            parent.last_reproduced_tick = self._tick

        self._defer_log("debug",
                        f"Consensus: instance {inst.id} born"
                        + (f" from {parent.id}" if parent else " (cold start)")
                        + f", lineage={inst.lineage_id}"
                        + (f", inherited unavailable: {len(inherited_bans)}" if inherited_bans else "")
                        + f", population={len(self.population)}"
                        )
        return inst

    def _spawn_divine_locked(self, excluded: set[str]
                             ) -> AlgorithmInstance:
        """
        Divine birth: new independent lineage-root.

        Does not inherit warmup_excluded of the parent. Instead receives
        warmup_excluded = {favorite of all stuck instances} — i.e.,
        excludes from warmup exactly those servers on which the
        dead-end lineage is stuck. Creates a new root in the queue.
        """
        new_id = self._next_id
        inst = AlgorithmInstance(
            id=new_id,
            lineage_id=new_id,
            banned_servers=set(self._occupied),
            born_tick=self._tick,
            warmup_started_tick=self._tick,
            warmup_excluded=set(excluded),
        )
        self._next_id += 1
        self.population.append(inst)
        if len(self._lineage_queue) < self.divine_queue_max:
            self._lineage_queue.append(new_id)

        self._defer_log("info",
                        f"Consensus: divine birth — instance {inst.id} "
                        f"(lineage {inst.lineage_id}), warmup_excluded={sorted(excluded)}, "
                        f"population={len(self.population)}, queue={self._lineage_queue}"
                        )
        return inst

    def _kill_locked(self, inst: AlgorithmInstance) -> None:

        # 1. Candidates for stuck — before removal.
        stuck_candidates = [inst]  # dying is stuck by definition
        for i in self.population:
            if i is inst:
                continue
            w = i.accept_window
            if w and sum(w) / len(w) < DIVINE_ACCEPT_RATE_THRESHOLD:
                stuck_candidates.append(i)

        # 2. Remove.
        try:
            self.population.remove(inst)
        except ValueError:
            return
        self._defer_log("info",
                        f"Consensus: instance {inst.id} died "
                        f"(lineage={inst.lineage_id}, "
                        f"low_accept_windows={inst.low_accept_windows}, "
                        f"reproduced={inst.reproduced_count}"
                        f"filter threshold={inst.own_threshold_ns/1e6:.2f}ms)"
                        )

        # 3. Lineage cleanup: if the lineage is extinct — remove the root from the queue.
        if not any(i.lineage_id == inst.lineage_id for i in self.population):
            if inst.lineage_id in self._lineage_queue:
                self._lineage_queue.remove(inst.lineage_id)
                self._defer_log("info",
                                f"Consensus: lineage {inst.lineage_id} extinct, "
                                f"queue={self._lineage_queue}"
                                )

        # 4. Divine trigger: fill the population up to MIN_POPULATION,
        #    while there are slots in the root queue.
        if len(self.population) < MIN_POPULATION:
            excluded = {i.favorite for i in stuck_candidates if i.favorite}
            if len(self.servers) - len(excluded) >= 1:
                spawned = False
                while (len(self.population) < MIN_POPULATION
                       and len(self._lineage_queue) < self.divine_queue_max):
                    self._spawn_divine_locked(excluded)
                    spawned = True
                if spawned:
                    return

        # 5. Normal cold start — only if population is empty
        #    and divine did not save.
        if not self.population:
            self._defer_log("warning", "Consensus: last instance died, cold start")
            self.cold_start_generation += 1
            self._refresh_bans_locked()
            self._spawn_locked(None)

    @staticmethod
    def _update_low_accept_windows(inst: AlgorithmInstance) -> None:
        if len(inst.accept_window) < ACCEPT_WINDOW_SIZE:
            return
        rate = sum(inst.accept_window) / len(inst.accept_window)
        if rate < DEATH_ACCEPT_THRESHOLD:
            inst.low_accept_windows += 1
        else:
            inst.low_accept_windows = 0

    @staticmethod
    def _filter_instance(inst: AlgorithmInstance,
                         available: List[Tuple[str, Tuple[int, int, int, int]]]
                         ) -> Dict[str, Any]:
        """
        Run of one instance over the available samples.
        Returns:
            {
              'accepted':   [(srv, sample), ...],
              'rejected':   [(srv, deviation_ns), ...],   # proposed - reference
              'is_cold_start': bool,
            }
        """
        if inst.own_reference_offset is None:
            # Cold start: take the best by delay.
            best_srv, best_smp = min(available, key=lambda x: x[1][0])
            return {'accepted': [(best_srv, best_smp)],
                    'rejected': [],
                    'is_cold_start': True}

        ref = inst.own_reference_offset
        thr = inst.own_threshold_ns
        accepted: List[Tuple[str, Tuple[int, int, int, int]]] = []
        rejected: List[Tuple[str, int]] = []
        for srv, smp in available:
            # proposed = t2_utc - mid_mono
            dev = int(smp[2] - smp[1] - ref)
            if thr is None or abs(dev) <= thr:
                accepted.append((srv, smp))
            else:
                rejected.append((srv, dev))
        return {'accepted': accepted, 'rejected': rejected, 'is_cold_start': False}

    def _compute_favorite_scores(self, inst: AlgorithmInstance,
                                 delays: Dict[str, int]) -> Optional[Dict[str, float]]:
        """
        Server score = d_norm + s_norm, where
            d_norm = delay_s / median(delays),
            s_norm = σ_s / median(σ)  for servers with history ≥ SIGMA_WARMUP_RECORDS,
                   = 1.0             for servers without sufficient history
                                      (neutral — does not win or lose).
        Returns None if σ-history is available on fewer than 2 servers (warmup).
        """
        if len(delays) < 2:
            return None
        per_srv_devs = self._collect_per_server_devs(inst)
        sigmas = {
            srv: statistics.stdev(devs)
            for srv, devs in per_srv_devs.items()
            if srv in delays and len(devs) >= SIGMA_WARMUP_RECORDS
        }
        if len(sigmas) < 2:
            return None
        median_delay = statistics.median(delays.values())
        median_sigma = statistics.median(sigmas.values())
        if median_delay <= 0 or median_sigma <= 0:
            return None
        scores: Dict[str, float] = {}
        for srv, d in delays.items():
            d_norm = d / median_delay
            s_norm = (sigmas[srv] / median_sigma) if srv in sigmas else 1.0
            scores[srv] = d_norm + s_norm
        return scores

    def _select_favorite(self, inst: AlgorithmInstance,
                         accepted: List[Tuple[str, Tuple[int, int, int, int]]]
                         ) -> Optional[str]:
        """
        Selection of instance favorite for this round.

        Priorities:
          1. Armed-lock active AND favorite passed the filter → keep favorite.
          2. Score-mode: score = d_norm + s_norm, minimum — candidate.
             Warmup (σ-history on < 2 servers): only delay, as before.
          3. If favorite is filtered out — forced change without hysteresis.
          4. Otherwise — hysteresis: stay on favorite if its score
             is not worse than candidate by more than FAVORITE_HYST.

        All decisions are reflected in inst.last_selection_mode,
        inst.last_best_candidate, inst.last_scores — read by telemetry.
        NOTE: accepted may contain servers occupied by other
        instances — they passed the instance filter and are recorded in its
        histories, but are forbidden for selection as its own favorite.
        Filtering by inst.banned_servers is performed here, not in
        _process_round_locked.
        """
        # Servers free for selection. inst.favorite by construction
        # is not in banned (banned = occupied − {favorite}), therefore
        # if it is accepted — it is in delays.
        delays = {srv: smp[0] for srv, smp in accepted
              if srv not in inst.banned_servers}

        # 1. Armed-lock
        if self._tick < inst.armed_lock_until_tick and inst.favorite in delays:
            inst.last_selection_mode = 'armed_lock'
            inst.last_best_candidate = inst.favorite
            inst.last_scores = None
            return inst.favorite

        # If no free server passed the filter — no change.
        # Keep current (may be None if taken by another instance).
        if not delays:
            inst.last_selection_mode = 'no_free'
            inst.last_best_candidate = None
            inst.last_scores = None
            return inst.favorite

        # 2. Candidate
        scores = self._compute_favorite_scores(inst, delays)
        if scores is not None:
            best_candidate = min(scores, key=scores.get)
            mode = 'score'
        else:
            best_candidate = min(delays, key=delays.get)
            mode = 'warmup' if inst.own_sigma_avg_ns is None else 'single'

        inst.last_scores = scores
        inst.last_best_candidate = best_candidate

        # 3. Favorite is filtered out — forced change
        if inst.favorite is None:
            inst.last_selection_mode = f'{mode}_no_favorite'
            return best_candidate
        if inst.favorite not in delays:
            favorite_gone = (inst.favorite in inst.warmup_excluded
                             or inst.favorite in inst.banned_servers)
            if len(delays) >= 2 or favorite_gone:
                inst.last_selection_mode = f'{mode}_forced'
                return best_candidate
            inst.last_selection_mode = f'{mode}_hold'
            return inst.favorite

        # 4. Hysteresis (only in score-mode)
        if scores is not None and best_candidate != inst.favorite:
            if scores[best_candidate] >= scores[inst.favorite] * (1.0 - FAVORITE_HYST):
                inst.last_selection_mode = f'{mode}_hysteresis'
                return inst.favorite

        inst.last_selection_mode = mode
        return best_candidate

    @staticmethod
    def _build_instance_servers(available: List[Tuple[str, Tuple[int, int, int, int]]],
            ref: int,
            accepted_set: set,
    ) -> tuple:
        """
        Per-server structured slice of the round from the instance's point of view.
        Element: (name, proposed_ns, dev_ns, delay_ns, mark)
        mark: '✓' — accepted by filter, '×' — rejected.
        """
        out = []
        for srv, smp in available:
            proposed = int(smp[2] - smp[1])
            dev = int(proposed - ref)
            mark = '✓' if srv in accepted_set else '×'
            out.append((srv, proposed, dev, int(smp[0]), mark))
        return tuple(out)

    @staticmethod
    def _select_diverse_cells(
            rows: List[List[Tuple[str, int, int, int]]], size: int
    ) -> Optional[List[List[Tuple[str, int, int, int]]]]:
        """
        Packing of a size×size matrix with maximum server diversification.

        rows: size rows, each of length >= size. Row = [(srv, proposed, delay, mid), ...]
        size: L.

        Returns a compact matrix size×size (exactly L cells per row),
        or None if packing is impossible.

        Algorithm — greedy row-major:
          1. Quota q_target = min(L, ceil(L² / N_distinct)) — how many times
             a server is allowed to appear in the matrix (no more than L, because
             within a row the server is unique).
          2. For each row, candidates are sorted by the number of uses
             of the server (rarer — first).
          3. Phase A: selection respecting q_target.
          4. Phase B: if row is not full — without q_target, but without repeating a server
             in the row.
          5. Phase C: last resort — we allow repeating a server in the row.

        Guarantees:
          • each row gives exactly L cells
          • N_distinct is maximum possible when L² ≤ Σ min(L, rows_avail_s)
          • spread of server uses ≤ 1 in the norm
        """
        if not rows or size < 1:
            return None

        # How many rows are available to each server
        srv_avail_rows: Dict[str, int] = {}
        for row in rows:
            seen_in_row = set()
            for cell in row:
                srv = cell[0]
                if srv not in seen_in_row:
                    srv_avail_rows[srv] = srv_avail_rows.get(srv, 0) + 1
                    seen_in_row.add(srv)

        n_distinct = len(srv_avail_rows)
        if n_distinct == 0:
            return None

        # Target quota per server. No more than L, because within a row
        # a server cannot appear twice.
        q_target = max(1, min(size, math.ceil((size * size) / n_distinct)))

        selected: List[List[Tuple[str, int, int, int]]] = [[] for _ in range(size)]
        srv_used: Dict[str, int] = {s: 0 for s in srv_avail_rows}

        for i in range(size):
            # Candidates of this row, sorted by server usage
            candidates = sorted(rows[i], key=lambda c: srv_used.get(c[0], 0))
            taken_srv: set = set()

            # Phase A: respecting the quota
            for cell in candidates:
                if len(selected[i]) >= size:
                    break
                srv = cell[0]
                if srv in taken_srv:
                    continue
                if srv_used[srv] >= q_target:
                    continue
                selected[i].append(cell)
                taken_srv.add(srv)
                srv_used[srv] += 1

            # Phase B: without quota, but without repeating a server in the row
            if len(selected[i]) < size:
                for cell in candidates:
                    if len(selected[i]) >= size:
                        break
                    srv = cell[0]
                    if srv in taken_srv:
                        continue
                    selected[i].append(cell)
                    taken_srv.add(srv)
                    srv_used[srv] += 1

            # Phase C: last resort — we allow repetition (only if
            # the row physically has < L unique servers)
            if len(selected[i]) < size:
                for cell in rows[i]:
                    if len(selected[i]) >= size:
                        break
                    selected[i].append(cell)

        return selected

    @staticmethod
    def _build_consensus_matrix(rows, t_ref_mono, rate):
        if not rows:
            return None

        lengths = sorted([len(r) for r in rows], reverse=True)
        size = 0
        for k in range(1, len(lengths) + 1):
            if lengths[k - 1] >= k:
                size = k
            else:
                break

        if size < CONSENSUS_MATRIX_MIN:
            return None

        # Take the first `size` rows of length >= size
        kept = []
        for row in rows:
            if len(row) >= size:
                kept.append(row)
                if len(kept) == size:
                    break
        if len(kept) < size:
            return None

        # Diversification: server round-robin instead of median trimming
        compact = Consensus._select_diverse_cells(kept, size)
        if compact is None:
            return None

        # Drift compensation
        corrected = []
        for row in compact:
            new_row = []
            for name, proposed, delay, mid in row:
                if rate != 0.0:
                    proposed -= round(rate * (mid - t_ref_mono))
                new_row.append((name, proposed, delay, mid))
            corrected.append(new_row)

        flat = [v for r in corrected for _, v, _, _ in r]
        mean_offset = int(statistics.mean(flat))
        return corrected, size, mean_offset

    def _process_round_locked(self,
                              samples_by_srv,
                              t_ref_mono=None,
                              rate=None,
                              seed_threshold_ns=None,
                              sigma_consensus_ns: Optional[int] = None,
                              ) -> Optional[Tuple[int, int, List[int], int, tuple, Optional[int], Optional[tuple],
    Optional[tuple], Optional[int], Optional[int], Optional[bool]]]:

        """
        Round processing.

        Input:  samples_by_srv[srv] = list of length n_attempts, where element k —
               sample of attempt k of the same server, or None if the attempt failed.
               Sample format: (delay_net_ns, mid_mono_ns, t2_utc_ns, unused).

        An instance with position k in ordered takes attempt_idx = k. It filters
        available cells with its own threshold and passes accepted into matrix_rows.

        Output: (best_mono, best_utc, delays, mean_offset, servers, threshold, matrix, matrix_meta)
        matrix     — tuple of L rows, row = tuple of (name, proposed, delay, mid_mono).
        matrix_meta — (tuple_of_mids, round_width_ns), None if Size < CONSENSUS_MATRIX_MIN.
        """

        ordered = sorted(self.population, key=lambda i: i.id)

        outputs_by_inst: List[Tuple[AlgorithmInstance, int]] = []
        matrix_rows: List[List[Tuple[str, int, int, int]]] = []  # NEW

        for k, inst in enumerate(ordered):
            # Each instance takes its attempt_idx by position in ordered.
            # n_attempts >= len(ordered), so there are no collisions.
            attempt_idx = k
            available: List[Tuple[str, Tuple[int, int, int, int]]] = []
            for srv, lst in samples_by_srv.items():
                # banned_servers forbids only SELECTION of a favorite (see _select_favorite).
                # Data from servers occupied by other instances is read freely:
                # attempt_idx is unique to each instance, there is no competition for the measurement.
                if attempt_idx >= len(lst):
                    continue
                smp = lst[attempt_idx]
                if smp is None:
                    continue
                available.append((srv, smp))

            if inst.warmup_excluded:
                filtered = [(s, smp) for s, smp in available
                            if s not in inst.warmup_excluded]
                if filtered:
                    available = filtered

            if not available:
                inst.accept_window.append(False)
                self._update_low_accept_windows(inst)
                matrix_rows.append([])  # NEW: empty row in the matrix
                continue

            f = self._filter_instance(inst, available)
            accepted_set = {s for s, _ in f['accepted']}

            if not f['accepted']:
                _, closest_dev = min(f['rejected'], key=lambda item: abs(item[1]))
                ref = inst.own_reference_offset
                servers = self._build_instance_servers(available, ref, accepted_set)
                inst.own_history_rejected.appendleft(SlewRecord(
                    timestamp_ns=time.time_ns(),
                    diff_ns=int(closest_dev),
                    threshold_ns=inst.own_threshold_ns,
                    instance_id=inst.id,
                    servers=servers,
                    ref_ns=ref,
                ))
                inst.accept_window.append(False)
                self._update_low_accept_windows(inst)
                matrix_rows.append([])  # NEW
                continue

            # NEW: matrix row from accepted cells
            row = [(srv, int(smp[2] - smp[1]), int(smp[0]), int(smp[1]))
                   for srv, smp in f['accepted']]
            matrix_rows.append(row)

            # --- Further without changes — instance logic ---
            delays = [smp[0] for _, smp in f['accepted']]
            weights = [1.0 / max(d, 1) for d in delays]
            total = sum(weights)
            weights = [w / total for w in weights]
            new_offset = int(sum(w * (smp[2] - smp[1])
                                 for w, (_, smp) in zip(weights, f['accepted'])))

            best_srv = self._select_favorite(inst, f['accepted'])
            old_ref = inst.own_reference_offset

            if f['is_cold_start']:
                servers = self._build_instance_servers(available, new_offset, accepted_set)
                inst.own_history.appendleft(SlewRecord(
                    timestamp_ns=time.time_ns(),
                    diff_ns=None,
                    threshold_ns=None,
                    instance_id=inst.id,
                    favorite=best_srv,
                    servers=None,
                    ref_ns=new_offset,
                    is_cold_start=True,
                ))
            else:
                diff = int(old_ref - new_offset)
                servers = self._build_instance_servers(available, old_ref, accepted_set)
                inst.own_history.appendleft(SlewRecord(
                    timestamp_ns=time.time_ns(),
                    diff_ns=diff,
                    threshold_ns=inst.own_threshold_ns,
                    instance_id=inst.id, favorite=best_srv,
                    servers=servers, ref_ns=old_ref, is_cold_start=False,
                ))
                diffs_recent_first = [r.diff_ns for r in inst.own_history
                                      if r.diff_ns is not None]
                # The first diff after cold-start is an artifact of transition from reference
                # of a single server (min-delay) to weighted-mean of the whole pool. This is not
                # drift, but a change in the definition of reference. It is always anomalously large
                # and inflates the starting stdev. Exclude the oldest diff from the estimate.
                diffs = list(reversed(diffs_recent_first))[1:]  # oldest-first, without [0]

                if len(diffs) >= 2:
                    stdev = statistics.stdev(diffs)
                    new_raw = max(int(self.diff_sigma * stdev), THRESHOLD_MIN_NS)
                    if inst.own_threshold_ns is None:
                        # Primary initialization. Seed from the service (consensus
                        # threshold or COHERENCE_THRESHOLD_NS on the first round).
                        # Lower bound: COLONY_THRESHOLD_FLOOR_NS. Also
                        # take max with new_raw — if own stdev of the first
                        # two diffs turned out wider, use it.
                        if seed_threshold_ns is not None:
                            inst.own_threshold_ns = max(
                                int(seed_threshold_ns), new_raw, COLONY_THRESHOLD_FLOOR_NS,
                            )
                        else:
                            inst.own_threshold_ns = max(new_raw, COLONY_THRESHOLD_FLOOR_NS)
                    else:
                        # Evolution: no faster than 1% down per round, and not below
                        # the colony floor.
                        shrink_floor = int(inst.own_threshold_ns * THRESHOLD_SHRINK_FLOOR)
                        inst.own_threshold_ns = max(
                            new_raw, shrink_floor, COLONY_THRESHOLD_FLOOR_NS,
                        )
            inst.own_reference_offset = new_offset
            inst.favorite = best_srv
            inst.accept_window.append(True)
            self._update_low_accept_windows(inst)
            outputs_by_inst.append((inst, new_offset))

        # --- History and drift votes ---
        short_vote, drift_prediction, drift_slope = self._compute_history_votes(
            t_ref_mono=t_ref_mono, rate=rate or 0.0)
        self._last_short_vote = short_vote
        self._last_drift_prediction = drift_prediction
        self._last_drift_slope = drift_slope

        # --- Ergodic matrix ---
        built = self._build_consensus_matrix(matrix_rows, t_ref_mono, rate)
        if built is None:
            self._defer_log("debug",
                            f"Consensus: matrix not assembled (Size<3), round skipped")
            return None

        compact_rows, size, consensus_offset = built

        # ----- Time center of each row of the ergodic matrix, for telemetry ----

        all_mids = []
        for row in compact_rows:
            mids = [mid for _, _, _, mid in row]
            if not mids:
                continue
            all_mids.append(statistics.median(mids))
        all_mids.sort()

        intervals = [b - a for a, b in zip(all_mids[:-1], all_mids[1:])]

        if len(intervals) >= 2:
            mean_interval = statistics.mean(intervals)
            stdev_interval = statistics.stdev(intervals)
            round_stdev_ns = round(stdev_interval)
        elif len(intervals) == 1:
            mean_interval = intervals[0]
            round_stdev_ns = 0
        else:
            mean_interval = 1.0
            round_stdev_ns = 0

        matrix_meta = (
            tuple(all_mids),  # row centers (N values)
            (mean_interval, round_stdev_ns),  # (mean period, stdev)
        )

        # Observation — into a separate window for prediction.
        # This does NOT replace applied_offsets; drift/sigma/TTL continue
        # to read applied_offsets, cleared of outliers.
        self._recent_observed.appendleft(consensus_offset)

        # --- Post-gate: split into applied / rejected ---
        prediction = None
        if not DISABLE_DRIFT_VOTE and drift_prediction is not None:
            prediction = drift_prediction
        elif not DISABLE_SHORT_VOTE and short_vote is not None:
            prediction = short_vote

        applied = True
        rejected_offset_ns = None
        rejected_prediction_ns = None

        if (not DISABLE_POST_GATE
                and prediction is not None
                and sigma_consensus_ns is not None
                and sigma_consensus_ns > 0
                and len(self.applied_offsets) >= HISTORY_VOTE_SHORT_WINDOW):
            delta = consensus_offset - prediction
            limit = int(POST_GATE_K * sigma_consensus_ns)
            if abs(delta) > limit:
                self._consecutive_rejects += 1
                if self._consecutive_rejects >= POST_GATE_FORCE_APPLY_AFTER:
                    # Prediction has been detached from reality for too long —
                    # not an outlier, but a step / drift that history-vote
                    # does not see. Accept consensus_offset forcibly,
                    # reset the counter; prediction will be rebuilt on the new
                    # applied. σ_consensus is extended by (1/THRESHOLD_SHRINK_FLOOR)^streak
                    # (see _compute_consensus_sigma_ns) — the gate will open.
                    self._defer_log(
                        "warning",
                        f"Consensus: post-gate force-apply after "
                        f"{self._consecutive_rejects} consecutive rejects "
                        f"(raw={consensus_offset} pred={prediction} "
                        f"delta={delta} limit=±{limit}); streak reset",
                    )
                    self._consecutive_rejects = 0
                    # applied remains True — we pass consensus_offset
                else:
                    applied = False
                    rejected_offset_ns = consensus_offset
                    rejected_prediction_ns = prediction
                    self._defer_log("debug",
                                    f"Consensus: post-gate reject raw={consensus_offset} "
                                    f"pred={prediction} delta={delta} limit=±{limit} "
                                    f"(streak={self._consecutive_rejects})")
            else:
                # Gate passed — series of rejects is interrupted.
                self._consecutive_rejects = 0

        if applied:
            self.applied_offsets.appendleft((consensus_offset, t_ref_mono if t_ref_mono is not None else time.monotonic_ns()))
            self._last_apply_mono_ns = self.applied_offsets[0][1]
            # Fix t_ref_mono — extrapolation of prediction in subsequent
            # rounds is counted from it. If t_ref_mono is not specified
            # (old call), fall back to current monotonic.

        thresholds = [inst.own_threshold_ns for inst, _ in outputs_by_inst
                      if inst.own_threshold_ns is not None]
        #Telemetry, diagnostics, selection - mean.
        threshold_out = int(statistics.mean(thresholds)) if thresholds else None

        servers = self._build_consensus_servers(samples_by_srv, consensus_offset)
        delays_all = [smp[0] for lst in samples_by_srv.values()
                      for smp in lst if smp is not None]

        # Matrix packing for SlewRecord
        matrix_tuple = tuple(
            tuple((name, proposed, delay, mid) for name, proposed, delay, mid in row)
            for row in compact_rows
        )

        if t_ref_mono is None:
            t_ref_mono = time.monotonic_ns()

        return (t_ref_mono, t_ref_mono + consensus_offset, delays_all,
                consensus_offset, servers, threshold_out, matrix_tuple, matrix_meta,
                rejected_offset_ns, rejected_prediction_ns, applied)

    def _build_consensus_servers(
            self,
            samples_by_srv: Dict[str, List[Optional[Tuple[int, int, int, int]]]],
            consensus_offset: int,
    ) -> tuple:
        """
        Per-server slice of the consensus level — for SlewRecord telemetry.
        For each server, take its attempt 0 (if failed — the first successful).
        Element: (name, proposed_ns, dev_ns, delay_ns, mark)
        """
        out = []
        for srv, lst in samples_by_srv.items():
            if not lst:
                continue
            smp = lst[0] if len(lst) > 0 and lst[0] is not None else None
            if smp is None:
                # fallback: first successful attempt
                smp = next((x for x in lst if x is not None), None)
            if smp is None:
                continue

            proposed = int(smp[2] - smp[1])
            dev = int(proposed - consensus_offset)

            if any(i.favorite == srv for i in self.population):
                mark = '✓'
            elif any(srv not in i.banned_servers for i in self.population):
                mark = '?'
            else:
                mark = '×'

            out.append((srv, proposed, dev, int(smp[0]), mark))
        return tuple(out)

    def _compute_history_votes(
            self,
            t_ref_mono: Optional[int] = None,
            rate: float = 0.0,
    ) -> Tuple[Optional[int], Optional[int], Optional[float]]:
        """
        (short_vote, drift_prediction, drift_slope).

        Sources are separated:
          short_vote       — _recent_observed[:HISTORY_VOTE_SHORT_WINDOW]
                             (all observed consensus_offset, including
                             rejected). median for resistance to
                             single NTP outliers.
          drift_prediction — linear regression over applied_offsets
                             (only gate-approved, clean data).
          drift_slope      — the same slope; goes to _last_drift_slope
                             → PLL rate confirmation + persist to drift_history.
                             UNITS: ns/round (for compatibility with
                             existing consumers of _last_drift_slope).

        Regression is computed on REAL monotonic time, not on
        index. applied_offsets is appended only on accept-rounds, so
        between adjacent records 30 s may pass, or hours (if
        gate rejects for a long time). With regression by index, the slope is divided by
        30 s regardless of the real step, which gives an overestimate by (real
        step / 30 s) times. This is exactly what gave slope −77 ppm at rate −9 ppm.

        For this reason, applied_offsets is stored as deque[Tuple[int,int]]:
        (consensus_offset_ns, t_ref_mono_ns). All other consumers
        (σ, TTL, accuracy, drift confirm, persist) must unpack
        offset via `o for o, _ in ...`.

        Why short_vote — by observed:
            While the gate is stuck (series of rejects), applied_offsets is frozen,
            and the mean over it does not reflect the current clock position. This gave
            a deadlock: prediction is not updated → gate does not open.
            The observed window is fed independently of the gate decision.

        Why drift_* — by applied:
            Regression does not suppress single outliers the way median does. Mixing
            rejected outliers into slope is not allowed — it would distort the estimate of
            physical drift, which goes into persist and rate confirmation.

        Extrapolation rate·(t_ref − _last_apply) is applied to both:
        compensates for the time since the last applied round. For drift_prediction
        the base point is the LAST applied-offset, so extrapolation
        brings it exactly to the moment t_ref without double-counting one round.

        Returns
        -------
        short_vote       : int | None   — absolute offset, ns
        drift_prediction : int | None   — absolute offset, ns
        drift_slope      : float | None — ns/round (NOT ns/index, NOT ns/ns)
        """
        # --- short_vote: by observed (applied + rejected) ---
        n_obs = len(self._recent_observed)
        short_vote: Optional[int] = None
        if n_obs >= HISTORY_VOTE_MIN_SAMPLES:
            window = list(self._recent_observed)[:HISTORY_VOTE_SHORT_WINDOW]
            # median always: the observed window contains outliers that
            # would not have entered applied_offsets.
            short_vote = int(statistics.median(window))

        # --- drift_prediction / drift_slope: by applied (clean) ---
        n_app = len(self.applied_offsets)
        drift_prediction: Optional[int] = None
        drift_slope: Optional[float] = None
        if n_app >= DRIFT_MIN_SAMPLES:
            series = list(reversed(self.applied_offsets))  # oldest-first
            t0 = series[0][1]
            y0 = series[0][0]

            xs = [t - t0 for _, t in series]  # ns from the start
            ys = [y - y0 for y, _ in series]  # ns from the start

            x_mean = statistics.mean(xs)
            y_mean = statistics.mean(ys)
            num = 0.0
            den = 0.0
            for x, y in zip(xs, ys):
                dx = x - x_mean
                num += dx * (y - y_mean)
                den += dx * dx
            if den > 0.0:
                slope = num / den  # ns/ns, dimensionless

                # Conversion to ns/round for existing consumers of
                # _last_drift_slope (_apply_drift_confirmation, cold-start
                # reset, drift persist — all of them compute
                # slope_ppm = drift_slope / (round_sec * 1e3)).
                round_ns = self.sync_interval * 1_000_000_000 // SYNC_THREAD_SLOTS
                drift_slope = slope * round_ns

                # Prediction base point: offset at the moment of the last
                # applied. The shift to t_ref is done by the extrapolation block
                # below (rate · (t_ref − last_apply)). We do NOT add
                # one round here — otherwise it would be counted twice.
                drift_prediction = series[-1][0]

        # --- Extrapolation to the time since the last applied ---
        if t_ref_mono is not None and self._last_apply_mono_ns > 0:
            elapsed_ns = t_ref_mono - self._last_apply_mono_ns
            if elapsed_ns > 0:
                correction = round(rate * elapsed_ns)
                if correction != 0:
                    if short_vote is not None:
                        short_vote += correction
                    if drift_prediction is not None:
                        drift_prediction += correction

        return short_vote, drift_prediction, drift_slope

    # -----------------------------------------------------------------
    # Dominance: statistics collection, σ_avg capture, M computation, trigger
    # -----------------------------------------------------------------
    @staticmethod
    def _collect_per_server_devs(inst: AlgorithmInstance
                                 ) -> Dict[str, List[int]]:
        """
        Dictionary server → list of dev_ns from ✓ records of accepted-history.
        Cold start is excluded. Filter by mark == '✓' means that
        only servers that passed the filter at their moment of time are taken.
        """
        out: Dict[str, List[int]] = {}
        for rec in inst.own_history:
            if rec.is_cold_start or rec.servers is None:
                continue
            for name, _proposed, dev, _delay, mark in rec.servers:
                if mark == '✓':
                    out.setdefault(name, []).append(dev)
        return out

    @staticmethod
    def _collect_fav_devs(inst: AlgorithmInstance
                          ) -> Tuple[List[int], List[int]]:
        """
        (on_X, others): dev_ns of current favorite X and all other servers
        from ✓ records of accepted-history. ALL servers of the round are
        considered, not only favorite — this eliminates selection bias
        by favorite.
        """
        fav_name = inst.favorite
        on_favorite: List[int] = []
        others: List[int] = []
        if fav_name is None:
            return on_favorite, others
        for rec in inst.own_history:
            if rec.is_cold_start or rec.servers is None:
                continue
            for name, _proposed, dev, _delay, mark in rec.servers:
                if mark != '✓':
                    continue
                if name == fav_name:
                    on_favorite.append(dev)
                else:
                    others.append(dev)
        return on_favorite, others

    @staticmethod
    def _compute_dominant_m(inst: AlgorithmInstance) -> Optional[int]:
        """
        M = max(DOMINANT_MIN_HISTORY, ceil(2·ln(N/α)/Δ²)),
        where N — number of servers in the last record (available to the instance),
            Δ = σ_avg / threshold.
        Returns None if there is not enough data.
        """
        if inst.own_threshold_ns is None or inst.own_threshold_ns <= 0:
            return None
        if inst.own_sigma_avg_ns is None or inst.own_sigma_avg_ns <= 0:
            return None
        if not inst.own_history:
            return None
        last = inst.own_history[0]
        if last.servers is None:
            return None
        number = len(last.servers)
        if number < DOMINANT_MIN_SERVERS:
            return None
        delta = inst.own_sigma_avg_ns / inst.own_threshold_ns
        if delta <= 0:
            return None
        raw = 2.0 * math.log(number / ALPHA_SIGNIFICANCE) / (delta * delta)
        return max(DOMINANT_MIN_HISTORY, int(math.ceil(raw)))

    def _maybe_capture_sigma_avg(self, inst: AlgorithmInstance) -> None:
        """
        σ_avg capture under warmup rules:
          1. < WARMUP_MIN_SURVIVORS available servers — we do not exit warmup.
          2b. >= SIGMA_WARMUP_MIN_SURVIVORS servers accumulated
              SIGMA_WARMUP_RECORDS ✓ — capture from them, without waiting for
              the rest.
          2. All accumulated >= SIGMA_WARMUP_RECORDS ✓ — capture from all.
          3. After SIGMA_WARMUP_TIMEOUT_MULT·SIGMA_WARMUP_RECORDS ticks
             servers with 0 ✓ go to warmup_excluded permanently. If after
             exclusion fewer than 2 remain — do not ban, wait.
        """
        if inst.own_sigma_avg_ns is not None:
            return
        if inst.own_threshold_ns is None or inst.own_threshold_ns <= 0:
            return
        if not inst.own_history:
            return
        last = inst.own_history[0]
        if last.servers is None or last.is_cold_start:
            return

        available_now = [name for name, _, _, _, _ in last.servers
                         if name not in inst.warmup_excluded]

        if len(available_now) < WARMUP_MIN_SURVIVORS:  # rule 1
            return

        devs_by_server = self._collect_per_server_devs(inst)
        counts = {srv: len(devs_by_server.get(srv, [])) for srv in available_now}

        # Rule: enough "full" servers — capture from them.
        full = [srv for srv in available_now
                if counts[srv] >= SIGMA_WARMUP_RECORDS]
        if len(full) >= SIGMA_WARMUP_MIN_SURVIVORS:
            self._capture_sigma_locked(
                inst,
                {srv: counts[srv] for srv in full},
                devs_by_server,
            )
            return

        # Rule: all accumulated.
        if all(c >= SIGMA_WARMUP_RECORDS for c in counts.values()):
            self._capture_sigma_locked(inst, counts, devs_by_server)
            return

        # Grace until timeout.
        if self._tick - inst.warmup_started_tick \
                < SIGMA_WARMUP_TIMEOUT_MULT * SIGMA_WARMUP_RECORDS:
            return

        # Rule: ban only those with 0 ✓.
        below = [srv for srv, c in counts.items() if c == 0]

        if not below:
            # Nothing to ban — all have ≥1 ✓, but someone did not reach SIGMA_WARMUP_RECORDS.
            # Wait silently: spamming the log every 30 sec is not needed.
            return

        survivors = [srv for srv in available_now if srv not in below]

        if len(survivors) < SIGMA_WARMUP_MIN_SURVIVORS:  # rule 3b
            if not inst.warmup_stuck_logged:
                self._defer_log("info",
                                f"Instance {inst.id}: warmup took too long, ban would collapse "
                                f"the pool to {len(survivors)}; waiting for conditions to improve"
                                )
                inst.warmup_stuck_logged = True
            return

        inst.warmup_excluded.update(below)
        self._defer_log("info",
                        f"Instance {inst.id}: warmup timeout, permanently excluded "
                        f"{sorted(below)}; survived {sorted(survivors)}"
                        )
        self._capture_sigma_locked(
            inst,
            {srv: counts[srv] for srv in survivors},
            devs_by_server,
        )

    def _capture_sigma_locked(self, inst: AlgorithmInstance,
                              counts: Dict[str, int],
                              devs_by_server: Dict[str, List[int]]) -> None:
        """
        σ_avg over the tail n_min = min(counts). History most-recent-first.
        If the mean σ exceeds SIGMA_AVG_SANITY_MAX_NS — capture
        is cancelled (instance lives without trusted-status and dies).
        """
        n_min = min(counts.values())
        sigmas: List[float] = []
        for srv in counts:
            devs = devs_by_server[srv][:n_min]
            if len(devs) >= 2:
                sigmas.append(statistics.stdev(devs))
        if not sigmas:
            return
        avg = int(sum(sigmas) / len(sigmas))
        if avg > SIGMA_AVG_SANITY_MAX_NS:
            self._defer_log("warning",
                            f"Instance {inst.id}: σ_avg={avg} ns > "
                            f"{SIGMA_AVG_SANITY_MAX_NS} ns — capture cancelled, "
                            f"instance is not fit"
                            )
            return
        inst.own_sigma_avg_ns = avg
        self._defer_log("info",
                        f"Instance {inst.id}: captured σ_avg={inst.own_sigma_avg_ns} ns "
                        f"over {len(sigmas)} servers (N={len(counts)}, n_min={n_min})"
                        )

    def _dominant_favorite_trigger(self, inst: AlgorithmInstance
                                   ) -> Optional[Tuple[str, str, int]]:
        """
        Returns ("dominant_favorite", X, M) if the instance is ready
        for reproduction by dominance, otherwise None.
        During armed-lock the trigger is silent.
        """
        if not self._spawn_allowed(inst):
            return None
        if self._tick < inst.armed_lock_until_tick:
            return None
        fav_name = inst.favorite
        if fav_name is None:
            return None

        min_samples = self._compute_dominant_m(inst)
        if min_samples is None:
            return None

        on_favorite, others = self._collect_fav_devs(inst)
        if len(on_favorite) < min_samples or len(others) < min_samples:
            return None
        if len(on_favorite) < DOMINANT_MIN_DEVS or len(others) < DOMINANT_MIN_DEVS:
            return None

        stdev_favorite = statistics.stdev(on_favorite)
        stdev_others = statistics.stdev(others)
        if stdev_others == 0:
            return None
        if stdev_favorite < stdev_others * DOMINANT_STDEV_RATIO:
            return "dominant_favorite", fav_name, min_samples
        return None

    def _check_triggers_locked(self) -> None:
        self._tick += 1
        self._refresh_bans_locked()

        # Attempt to capture σ_avg for each instance
        for inst in self.population:
            self._maybe_capture_sigma_avg(inst)

        ordered = sorted(self.population, key=lambda i: i.id)

        candidate: Optional[AlgorithmInstance] = None
        reason: Optional[str] = None
        armed_min_samples: Optional[int] = None

        # Priority 1: favorite dominance
        for inst in ordered:
            r = self._dominant_favorite_trigger(inst)
            if r is not None:
                _, _X, armed_min_samples = r
                candidate, reason = inst, "dominant_favorite"
                break

        # Priority 2: deathbed
        if candidate is None:
            for inst in ordered:
                if self._deathbed_trigger(inst):
                    candidate, reason = inst, "deathbed"
                    break

        if candidate is not None:
            # Child is cold, parent continues to live with its favorite.
            self._spawn_locked(candidate)
            if reason == "dominant_favorite" and armed_min_samples is not None:
                candidate.armed_lock_until_tick = self._tick + armed_min_samples
                self._defer_log("debug",
                                f"Consensus: instance {candidate.id} armed on "
                                f"{candidate.favorite}; lock until tick "
                                f"{candidate.armed_lock_until_tick} (M={armed_min_samples})"
                                )
            elif reason == "deathbed":
                candidate.deathbed_used = True

        # Death
        dead = [inst for inst in self.population if self._should_die(inst)]
        for inst in dead:
            self._kill_locked(inst)

        if (len(self.population) <= MIN_POPULATION
                and not self._lineage_queue
                and not self.reproduction_allowed):
            self._defer_log("critical",
                            f"Consensus: colony on the brink of extinction "
                            f"(population={len(self.population)}, "
                            f"lineage_queue={self._lineage_queue}, "
                            f"reproduction_allowed=False)"
                            )

        # Gate: mean(spreads) < MULT · mean(σ_avg).
        # Until spread history is accumulated or there are at least two σ_avg — gate is open.
        offsets = [i.own_reference_offset for i in self.population
                   if i.own_reference_offset is not None]
        if len(offsets) >= 2:
            self._spreads.append(statistics.stdev(offsets))

        sigmas = [i.own_sigma_avg_ns for i in self.population
                  if i.own_sigma_avg_ns is not None]
        #σ_avg of instances is fundamentally NOT homogeneous (stuck instance). This parameter defines gate thresholds, choose median
        noise_ref = statistics.median(sigmas) if len(sigmas) >= 2 else None

        # Snapshot for telemetry (even if gate is not yet active).
        self.noise_ref_ns = noise_ref
        # Reproduction gate compares median(spreads) with REPRODUCTION_SPREAD_MULT·σ_ref.
        # Equivalently: ratio = median/ (MULT·σ_ref) < 1.0.
        # A single discord in the colony should not block the gate for long — median
        # is resistant to a single outlier, unlike mean.
        self._mean_spread_ns = (statistics.median(self._spreads)
                                  if self._spreads else None)

        if len(self._spreads) < SPREAD_HISTORY_MIN or noise_ref is None:
            self.reproduction_allowed = True
        else:
            self.reproduction_allowed = (
                    statistics.median(self._spreads) < REPRODUCTION_SPREAD_MULT * noise_ref
            )

    # --- Triggers ---

    def _spawn_allowed(self, inst: AlgorithmInstance) -> bool:
        """
        Unified precondition for all reproduction triggers.
        Order — from cheap to expensive.
        Offspring ceiling — shared across all triggers: over its lifetime the instance
        produces from 0 to max_offspring children, including the deathbed one.
        """
        if not self.reproduction_allowed:
            return False
        if inst.favorite is None:
            return False
        if inst.reproduced_count >= inst.max_offspring:
            return False
        if len(self.population) >= self.max_population:
            return False
        if len(self._occupied) >= len(self.servers):
            return False
        if self._tick - inst.last_reproduced_tick < REPRODUCTION_LAG_L:
            return False
        return True

    def _deathbed_trigger(self, inst) -> bool:
        if not self._spawn_allowed(inst):
            return False
        if inst.deathbed_used:
            return False
        return inst.low_accept_windows >= DEATH_LOW_WINDOWS - 1

    @staticmethod
    def _should_die(inst: AlgorithmInstance) -> bool:
        return inst.low_accept_windows >= DEATH_LOW_WINDOWS

    def _defer_log(self, level: str, msg: str) -> None:
        """Accumulate the message. Inside the critical section — only this, without logger.*."""
        self._pending_logs.append((level, msg))

    def _drain_pending_logs(self) -> List[Tuple[str, str]]:
        """Take and clear the buffer. Call under _lock."""
        logs = self._pending_logs
        self._pending_logs = []
        return logs

# =====================================================================
# TimeSyncService — orchestration, slewing, get_utc_ns, watchdog
# =====================================================================

class TimeSyncService:
    """
    Precise time service based on NTP.
    Filtering and target offset selection are delegated to Consensus; the service
    is responsible only for slewing and storing the history of applied corrections.
    """
    _instance: Optional['TimeSyncService'] = None
    _instance_lock = threading.Lock()

    def __init__(self, ntp_servers: list = None, sync_interval_sec: int = DEFAULT_INITIAL_INTERVAL_SEC,
                 diff_sigma: float = 1.0) -> None:
        if sync_interval_sec < 30:
            raise ValueError(
                f"sync_interval_sec must be >= 30 seconds, got {sync_interval_sec}"
            )

        self.ntp_servers = ntp_servers or DEFAULT_NTP_SERVERS
        self.ntp_servers_resolved = {} #DNS -> IP of NTP servers
        self.ntp_servers_last_resolved_ns: int = -NTP_RESOLVING_TIMEOUT_NS #Timestamp of last DNS -> IP resolution
        self.sync_interval: int = sync_interval_sec
        self.second_sync_thread_delay: int = sync_interval_sec // 2
        # Threshold of "stuck" PLL updates: half the interval between sync streams.
        # At sync_interval=60 and two streams with 30 s delay, normal PLL updates
        # occur every ~30 s; anything shorter than 15 s is an anomaly.
        self._min_pll_update_interval_ns: int = (
                max(self.second_sync_thread_delay // 2, 5) * 1_000_000_000
        )
        self._server_meta: Dict[str, ServerMeta] = {}
        self._server_meta_lock: threading.Lock = threading.Lock()
        self._executors_shutdown = False
        #Thread pool for polling NTP servers
        self._NTP_poll_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=len(self.ntp_servers) + 2,
            thread_name_prefix="ntp-poll",
        )
        # Thread pool for polling DNS servers
        self._DNS_poll_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=len(self.ntp_servers),
            thread_name_prefix="dns-poll",
        )
        self.dns_fail_servers = set() #servers that failed DNS resolution
        self.lock: threading.Lock = threading.Lock()
        self._pending_logs: List[Tuple[str, str]] = []
        self._threads_lock: threading.Lock = threading.Lock()
        self.running: bool = False

        self.is_synced_event: threading.Event = threading.Event()
        self._stop_event: threading.Event = threading.Event()

        self._threads: Dict[int, threading.Thread] = {}
        self._watchdog_thread: Optional[threading.Thread] = None
        self._keep_awake_thread: Optional[threading.Thread] = None
        self._last_success_mono_ns: int = 0  # 0 = no success yet
        self._last_stalled_servers: tuple = ()  # for diagnostics

        self._delay_history: deque[int] = deque(maxlen=20)

        # Window of phase_errors for the rate sign constancy detector.
        # Populated on each normal PLL update (after _rate_sign_constancy,
        # so the current phase_error does not affect its own decision).
        # Cleared on phase-jump reset, cold-start reset and reject-streak
        # reset — there the context is broken, old values are unrepresentative.
        self._phase_err_window: deque[int] = deque(maxlen=RATE_DETECT_WINDOW_N)

        # Per-server history of minimum delays (for honest estimation of
        # jitter per server, not a mixture of servers).
        self._per_server_delay: Dict[str, deque] = defaultdict(lambda: deque(maxlen=PER_SERVER_HISTORY))

        # Rate-model offset: offset(m) = anchor_offset + rate·(m − anchor_mono).
        # rate — dimensionless relative drift (1 ppm = 1e-6).
        self._anchor_offset: int = 0
        self._anchor_mono: int = 0
        self._rate: float = 0.0
        self._clock_snapshot = (0, 0, 0.0)  # (anchor_mono, anchor_offset, rate)
        # Dynamic TTL and declared snapshot accuracy.
        # Updated in _apply_new_sync_locked under self.lock,
        # read lock-free in get_clock_snapshot.
        self._clock_snapshot_ttl_ns: int = 2 * self.sync_interval * 1_000_000_000
        self._clock_snapshot_accuracy_ns: int = int(CLOCK_SNAPSHOT_ACCURACY_DEFAULT_NS)

        # Full atomic snapshot: (anchor_mono, anchor_offset, rate, ttl_ns, accuracy_ns).
        # Published with a single assignment. Read by get_clock_snapshot(precision_ns=None)
        # without lock — guarantees consistency of the five fields from one clock state.
        self._clock_snapshot_full: Tuple[int, int, float, int, int] = (
            self._clock_snapshot[0],
            self._clock_snapshot[1],
            self._clock_snapshot[2],
            self._clock_snapshot_ttl_ns,
            self._clock_snapshot_accuracy_ns,
        )

        # Cache of TTL/accuracy computations under a given precision.
        # Key — precision_ns. Value — (ttl_sec, accuracy_ns).
        # Cleared in _publish_clock_and_metrics_locked — after each
        # change of anchor/rate, data about consensus noise and model
        # estimates become stale.
        # Consumers are usually 1–3 (UI, IPC, trading modules), so
        # the cache size is naturally bounded. If needed — LRU.
        self._precision_cache: OrderedDict[int, Tuple[float, int]] = OrderedDict()

        self._target_offset: int = 0  # for telemetry
        self._last_phase_error: Optional[int] = None

        # History of applied corrections at consensus level:
        # SlewRecord(instance_id=None) records.
        self._slew_error_history: deque[SlewRecord] = deque(maxlen=HISTORY_MAX_LEN)

        # Accumulated bias (permanent shift) of servers (ns). Updated when history is full.
        self._colony_bias: Dict[str, int] = {}
        #Colony generation
        self._last_cold_gen: int = 0
        # Raw history of proposed (before bias application) — only for bias estimation.
        # The cleaned _slew_error_history is used by the colony for its decisions,
        # but it is not suitable for the integrator: it contains our own past corrections.
        self._raw_proposed_history: deque[tuple] = deque(maxlen=HISTORY_MAX_LEN)

        self._diff_sigma: float = diff_sigma
        self._last_diff_threshold: Optional[int] = None

        # Colony of filter instances.
        n_servers = len(self.ntp_servers)
        if n_servers > MAX_POPULATION_MIN_SERVERS:
            max_pop = max(int(n_servers * MAX_POPULATION_RATIO),
                          REPRODUCTION_LAG_L)
        else:
            max_pop = n_servers
        # Do not let population drop below MIN_POPULATION.
        max_pop = max(max_pop, MIN_POPULATION)

        self._consensus = Consensus(
            servers=self.ntp_servers,
            max_population=max_pop,
            diff_sigma=diff_sigma,
            initial_population=max_pop,
            sync_interval=sync_interval_sec,
        )

        #Processing of known drift values
        prior = self.compute_drift_prior()
        if (prior['n'] >= DRIFT_SEED_MIN_SAMPLES
                and prior['median_ppm'] is not None
                and prior['sigma_ppm'] is not None
                and prior['sigma_ppm'] < PLL_RATE_LIMIT * 1e6):
            self._rate_prior = prior['median_ppm'] * 1e-6
            self._defer_log("info",
                f"Drift prior: {prior['median_ppm']:+.3f} ppm ±{prior['sigma_ppm']:.3f} "
                f"(n={prior['kept']}/{prior['n']}, span={prior['span_sec'] / 3600:.1f}h)"
            )
        else:
            self._rate_prior = 0.0
            if prior['n'] > 0:
                self._defer_log("info",
                    f"Drift prior ignored (n={prior['n']}, "
                    f"median={prior['median_ppm']}, sigma={prior['sigma_ppm']})"
                )
            else:
                self._defer_log("info", "Drift prior: empty history, rate_prior=0")

        # Setting the rate correction step of the PLL
        self._rate_spring_base_ppm = RATE_SPRING_SIGMA_FRACTION_BASE * prior['sigma_ppm'] if prior['sigma_ppm'] else RATE_SPRING_BASE_PPM
        self._rate_spring_max_ppm = RATE_SPRING_SIGMA_FRACTION_MAX * prior['sigma_ppm'] if prior['sigma_ppm'] else RATE_SPRING_MAX_PPM

        self._last_drift_persist_tick: int = 0

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------
    @classmethod
    def get_instance(cls) -> 'TimeSyncService':
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def wait_for_first_sync(self, timeout: Optional[float] = None) -> bool:
        return self.is_synced_event.wait(timeout)

    def start(self) -> None:
        if self.running:
            return

        self._ensure_executors()  # Check the state of permanent thread pools

        with self._threads_lock:
            leftover = [t for t in self._threads.values() if t.is_alive()]
        if leftover:
            logger.warning(
                f"Unfinished threads from previous session detected: "
                f"{[t.name for t in leftover]}; waiting for completion..."
            )
            for t in leftover:
                t.join(timeout=PRE_START_JOIN_SEC)
                if t.is_alive():
                    logger.error(f"Thread {t.name} did not finish, restart cancelled")
                    return

        self.running = True
        self._stop_event.clear()

        self._start_sync_thread(0, 0.0)
        self._start_sync_thread(1, self.second_sync_thread_delay)

        self._watchdog_thread = threading.Thread(
            target=self._watchdog_worker,
            daemon=True,
            name="TimeSyncWatchdog"
        )
        self._watchdog_thread.start()

        if KEEP_AWAKE_ENABLED:
            self._keep_awake_thread = threading.Thread(
                target=self._keep_awake_worker,
                daemon=True,
                name="TimeSyncKeepAwake",
            )
            self._keep_awake_thread.start()

    def stop(self) -> None:
        self.running = False
        self._NTP_poll_executor.shutdown(wait=False, cancel_futures=True)
        self._DNS_poll_executor.shutdown(wait=False, cancel_futures=True)
        self._executors_shutdown = True
        self._stop_event.set()

        with self._threads_lock:
            threads_to_join = list(self._threads.values())
            self._threads.clear()
            watchdog = self._watchdog_thread
            keep_awake = self._keep_awake_thread

        deadline = time.monotonic() + SYNC_THREAD_STOP_JOIN_SEC
        for t in threads_to_join:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if t.is_alive():
                t.join(timeout=remaining)
            if t.is_alive():
                logger.warning(f"Thread {t.name} did not finish within the allotted time")

        if watchdog and watchdog.is_alive():
            watchdog.join(timeout=WATCHDOG_STOP_JOIN_SEC)
            if watchdog.is_alive():
                logger.warning(f"Watchdog {watchdog.name} did not finish within the allotted time")

        if keep_awake and keep_awake.is_alive():
            keep_awake.join(timeout=WATCHDOG_STOP_JOIN_SEC)
            if keep_awake.is_alive():
                logger.warning(f"Keep-awake {keep_awake.name} did not finish within the allotted time")

        self.is_synced_event.clear()

    def get_sync_telemetry(self) -> Dict[str, Any]:
        """
        Full telemetry: consensus-level summary + slice per each
        instance (both SlewRecord queues with instance_id inside).
        """
        with self.lock:
            precise, system = self._get_times_locked()
            offset = precise - system
            target_offset = self._target_offset

            now_mono = time.monotonic_ns()
            current_offset = self._calculate_current_offset(now_mono)
            slew_error_ns = int(current_offset - target_offset)

            slew_error_history_snapshot = list(self._slew_error_history)
            delay_history_snapshot = list(self._delay_history)
            diff_threshold = self._last_diff_threshold
            raw_snapshot_list = list(self._raw_proposed_history)

            per_server_delay_snapshot = {
                srv: list(d) for srv, d in self._per_server_delay.items()
            }

            server_meta_snapshot = {
                srv: {
                    'leap': m.leap, 'version': m.version, 'mode': m.mode,
                    'stratum': m.stratum, 'poll': m.poll, 'precision': m.precision,
                    'root_delay_ns': m.root_delay_ns, 'root_disp_ns': m.root_disp_ns,
                    'ref_id': m.ref_id, 'ref_ts_ns': m.ref_ts_ns,
                    'age_sec': (time.monotonic_ns() - m.last_update_mono_ns) / 1e9
                    if m.last_update_mono_ns else None,
                }
                for srv, m in self._server_meta.items()
            }

        consensus_snapshot = self._consensus.snapshot()
        instances_telemetry = self._consensus.get_instances_telemetry()

        # Std. dev. of T.B.O.T time Δ — real spread of consensus decisions
        # around their recent norm, not PLL residuals.
        # Interpretation: true time lies within ±σ of T.B.O.T with ~66% probability
        # (assuming that consensus_offset is unbiased).
        with self._consensus._lock:
            applied_recent = list(self._consensus.applied_offsets)[:HISTORY_MAX_LEN]
        offset_spread_ns = self._noise_sigma_ns(applied_recent)
        ntp_spread_ns = statistics.stdev(delay_history_snapshot) if len(delay_history_snapshot) >= 2 else None

        per_server_delay_stats: Dict[str, Dict[str, Any]] = {}
        for srv, vals in per_server_delay_snapshot.items():
            if not vals:
                continue
            per_server_delay_stats[srv] = {
                'n': len(vals),
                'last_ns': vals[-1],
                'mean_ns': int(statistics.mean(vals)) if len(vals) >= 2 else None,
                'jitter_ns': int(statistics.stdev(vals)) if len(vals) >= 2 else 0,
            }

        return {
            'precise_ns': precise,
            'system_ns': system,
            'offset_ns': offset,
            'offset_spread_ns': offset_spread_ns,
            'slew_error_ns': slew_error_ns,
            'slew_errors_ns': slew_error_history_snapshot,  # consensus-level (instance_id=None)
            'ntp_spread_ns': ntp_spread_ns,
            'diff_threshold_ns': diff_threshold,
            'population': consensus_snapshot,
            'instances': instances_telemetry,
            'colony_bias_ns': dict(self._colony_bias),
            'colony_noise_ns': self._compute_colony_noise_ns(raw_snapshot_list),
            'per_server_delay': per_server_delay_stats,
            'rate_ppm': self._rate * 1e6,
            'phase_error_ns': self._last_phase_error,
            'second_sync_thread_delay': self.second_sync_thread_delay,
            'server_meta': server_meta_snapshot
        }

    def get_utc_ns(self) -> int:
        """Lock-free. Returns precise UTC time.
        Uses _calculate_current_offset for consistency with telemetry."""
        if not self.is_synced_event.is_set():
            return time.time_ns()
        now = time.monotonic_ns()
        return now + self._calculate_current_offset(now)

    def get_utc_ns_with_precision(self, precision_ns: int) -> Optional[int]:
        """Returns UTC with guarantee |utc − utc_true| ≤ precision_ns.

        Returns None if:
          • synchronization has not been performed yet;
          • the declared precision is unattainable (accuracy_ns > precision_ns);
          • the snapshot is stale (now_mono > anchor_mono + ttl_ns).

        Differs from get_utc_ns in that it does not return "the best there is" —
        the result is returned only if the declared precision is met.
        Convenient for consumers with strict SLA (trading modules, IPC).
        """
        snap = self.get_clock_snapshot(precision_ns)
        if snap is None:
            return None
        if snap.accuracy_ns > precision_ns:
            return None
        return self.utc_from_snapshot(snap, time.monotonic_ns())

    def get_clock_snapshot(
            self, precision_ns: Optional[int] = None
    ) -> Optional[ClockSnapshot]:
        """Returns the clock model snapshot.

        Args:
            precision_ns:
                None — return snapshot with TTL computed by the consensus
                       quality heuristic. accuracy_ns field — declared
                       error limit. Read as a single atomic snapshot
                       from self._clock_snapshot_full; lock is not taken.
                int  — return snapshot with TTL selected so that
                       accuracy_ns ≤ precision_ns. Takes self.lock
                       (cache _precision_cache under lock).
                       NOTE: if σ_cons (consensus noise) is already greater than
                       precision_ns, the precision is unattainable — a minimum TTL
                       will be returned with accuracy_ns = σ_cons,
                       i.e., snap.accuracy_ns > precision_ns.
                       The caller MUST check
                       snap.accuracy_ns ≤ precision_ns if it needs a
                       strict guarantee. See get_utc_ns_with_precision.

        Returns None if synchronization has not been performed yet.

        Field consistency guarantee:
            anchor_mono_ns, anchor_offset_ns, rate, ttl_ns, accuracy_ns
            are obtained from one clock state (one assignment
            _clock_snapshot_full under self.lock; in CPython reading one
            tuple is atomic).
        """
        if not self.is_synced_event.is_set():
            return None

        if precision_ns is None:
            # Single atomic read of the five fields — no "torn" pairs.
            (anchor_mono, anchor_offset, rate,
             ttl_ns, accuracy) = self._clock_snapshot_full
        else:
            with self.lock:
                (anchor_mono, anchor_offset, rate,
                 _, _) = self._clock_snapshot_full
                cached = self._precision_cache.get(precision_ns)
                if cached is None:
                    ttl_ns, accuracy = self._ttl_for_precision_locked(precision_ns)
                    self._precision_cache[precision_ns] = (ttl_ns, accuracy)
                    self._precision_cache.move_to_end(precision_ns)
                    if len(self._precision_cache) > CLOCK_SNAPSHOT_PRECISION_CACHE_MAX:
                        self._precision_cache.popitem(last=False)
                else:
                    self._precision_cache.move_to_end(precision_ns)
                    ttl_ns, accuracy = cached

        return ClockSnapshot(
            anchor_mono_ns=anchor_mono,
            anchor_offset_ns=anchor_offset,
            rate=rate,
            ttl_ns=ttl_ns,
            accuracy_ns=accuracy,
        )

    @staticmethod
    def compute_drift_prior() -> Dict[str, Any]:
        """Robust median + MAD-based sigma over full history."""
        history = load_drift_history()
        n = len(history)
        if n == 0:
            return {'median_ppm': None, 'sigma_ppm': None, 'n': 0, 'kept': 0,
                    'span_sec': 0.0}
        values = [v for _, v in history]
        med = statistics.median(values)
        mad = statistics.median(abs(v - med) for v in values) * 1.4826
        if mad > 0:
            kept = [v for v in values if abs(v - med) <= DRIFT_OUTLIER_K * mad]
            if len(kept) >= 2:
                med = statistics.median(kept)
                mad = statistics.median(abs(v - med) for v in kept) * 1.4826
            else:
                kept = values
        else:
            kept = values
        return {
            'median_ppm': med,
            'sigma_ppm': mad,
            'n': n,
            'kept': len(kept),
            'span_sec': (history[-1][0] - history[0][0]) / 1e9 if n >= 2 else 0.0,
        }

    # -----------------------------------------------------------------
    # Internal methods
    # -----------------------------------------------------------------

    @staticmethod
    def utc_from_snapshot(snap: Optional[ClockSnapshot],
                          now_mono: int) -> Optional[int]:
        """Restores UTC from the snapshot. None if the snapshot is absent
        or stale (now_mono - anchor_mono > ttl_ns).

        Does not take locks, does not access service state — pure function.
        """
        if snap is None:
            return None
        elapsed = now_mono - snap.anchor_mono_ns
        if elapsed < 0:
            return None
        if elapsed > snap.ttl_ns:
            return None
        return now_mono + snap.anchor_offset_ns + round(snap.rate * elapsed)

    @staticmethod
    def _compute_colony_noise_ns (raw_snapshots: list) -> Dict[str, int]:
        """
        Robust jitter estimate of each server by first differences
        of raw proposed. NOT standard deviation: 1.4826·MAD(first_diffs)/√2.
        """
        per_server: Dict[str, List[int]] = {}
        for snap in raw_snapshots:
            for srv, proposed in snap:
                per_server.setdefault(srv, []).append(proposed)
        out: Dict[str, int] = {}
        for srv, vals in per_server.items():
            if len(vals) < 2:
                out[srv] = 0
                continue
            diffs = [vals[i] - vals[i + 1] for i in range(len(vals) - 1)]
            if len(diffs) >= 2:
                #robust, choice, median
                med_d = statistics.median(diffs)
                mad = statistics.median(abs(d - med_d) for d in diffs)
                out[srv] = int(1.4826 * mad / math.sqrt(2.0))
            else:
                out[srv] = 0
        return out

    @staticmethod
    def _build_ntp_request(xmt_mono_ns: int) -> bytes:
        """NTPv4 client request (48 bytes). Put our mono timestamp into transmit —
        the server will return it in origin (echo)."""
        msg = bytearray(NTP_PACKET_SIZE)
        msg[0] = 0x23  # LI=0, VN=4, Mode=3 (client)
        sec = (xmt_mono_ns // 1_000_000_000) + NTP_EPOCH_OFFSET_SEC
        frac = ((xmt_mono_ns % 1_000_000_000) * (1 << 32)) // 1_000_000_000
        struct.pack_into('!II', msg, 40, sec & 0xFFFFFFFF, frac & 0xFFFFFFFF)
        return bytes(msg)

    @staticmethod
    def _ntp_query_raw(server_ip: str, timeout_sec: float, xmt_mono_ns: int) -> Optional[bytes]:
        """Sends NTPv4 client request, receives 48-byte response."""
        try:
            req = TimeSyncService._build_ntp_request(xmt_mono_ns)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(timeout_sec)
                sock.sendto(req, (server_ip, NTP_PORT))
                data, _ = sock.recvfrom(NTP_PACKET_SIZE)
                return data
        except (socket.timeout, OSError):
            return None

    @staticmethod
    def _parse_ntp_packet(data: bytes) -> Optional[Dict[str, Any]]:
        """Parses 48-byte NTPv4 response. Returns dict of fields, or None."""
        if len(data) < NTP_PACKET_SIZE:
            return None
        try:
            u = struct.unpack('!B B B b 11I', data[:NTP_PACKET_SIZE])
        except struct.error:
            return None
        first = u[0]
        li = (first >> 6) & 0x3
        vn = (first >> 3) & 0x7
        mode = first & 0x7
        stratum = u[1]
        poll = u[2]
        precision = u[3]
        root_delay_raw = u[4]
        root_disp_raw = u[5]
        ref_id_raw = u[6]
        ref_sec, ref_frac = u[7], u[8]
        orig_sec, orig_frac = u[9], u[10]
        rec_sec, rec_frac = u[11], u[12]
        xmt_sec, xmt_frac = u[13], u[14]

        def ts_to_unix_ns(sec: int, frac: int) -> int:
            # sec — 32-bit (unsigned), frac — 32-bit fraction of second
            return (sec - NTP_EPOCH_OFFSET_SEC) * 1_000_000_000 + ((frac * 1_000_000_000) >> 32)

        ref_id_bytes = struct.pack('!I', ref_id_raw)
        if stratum <= 1:
            try:
                ref_id = ref_id_bytes.decode('ascii', errors='replace').strip()
            except Exception:
                ref_id = ref_id_bytes.hex()
        else:
            try:
                ref_id = socket.inet_ntoa(ref_id_bytes)
            except Exception:
                ref_id = ref_id_bytes.hex()

        return {
            'leap': li, 'version': vn, 'mode': mode,
            'stratum': stratum, 'poll': poll, 'precision': precision,
            'root_delay_raw': root_delay_raw,
            'root_disp_raw': root_disp_raw,
            'ref_id': ref_id,
            'ref_ts_ns': ts_to_unix_ns(ref_sec, ref_frac),
            'orig_ts_raw': (orig_sec << 32) | orig_frac,
            'rec_ts_raw': (rec_sec << 32) | rec_frac,
            'xmt_ts_raw': (xmt_sec << 32) | xmt_frac,
            'rec_utc_ns': ts_to_unix_ns(rec_sec, rec_frac),
            'xmt_utc_ns': ts_to_unix_ns(xmt_sec, xmt_frac),
        }

    @staticmethod
    def _check_origin_echo(pkt: Dict[str, Any], expected_mono_ns: int) -> bool:
        """Origin in the response must match our transmit (echo-check)."""
        if not VALIDATE_ORIGIN:
            return True
        sec = (expected_mono_ns // 1_000_000_000) + NTP_EPOCH_OFFSET_SEC
        frac = ((expected_mono_ns % 1_000_000_000) * (1 << 32)) // 1_000_000_000
        expected = ((sec & 0xFFFFFFFF) << 32) | (frac & 0xFFFFFFFF)
        got = pkt.get('orig_ts_raw', 0)
        if got == expected:
            return True
        # tolerance 1 s (some servers may normalize the fraction)
        exp_sec = expected >> 32
        got_sec = got >> 32
        return abs(got_sec - exp_sec) <= 1

    @staticmethod
    def _ntp_ts_diff_ns(a_raw: int, b_raw: int) -> int:
        a_sec = a_raw >> 32
        a_frac = a_raw & 0xFFFFFFFF
        b_sec = b_raw >> 32
        b_frac = b_raw & 0xFFFFFFFF
        sec_diff = a_sec - b_sec
        frac_diff = a_frac - b_frac
        return sec_diff * 1_000_000_000 + (frac_diff * 1_000_000_000) // (1 << 32)

    def _update_server_meta(self, server: str, pkt: Dict[str, Any]) -> None:
        with self._server_meta_lock:
            meta = self._server_meta.get(server)
            if meta is None:
                meta = ServerMeta()
                self._server_meta[server] = meta
            meta.leap = pkt['leap']
            meta.version = pkt['version']
            meta.mode = pkt['mode']
            meta.stratum = pkt['stratum']
            meta.poll = pkt['poll']
            meta.precision = pkt['precision']
            meta.root_delay_raw = pkt['root_delay_raw']
            meta.root_disp_raw = pkt['root_disp_raw']
            meta.ref_id = pkt['ref_id']
            meta.ref_ts_ns = pkt['ref_ts_ns']
            meta.last_update_mono_ns = time.monotonic_ns()

    @staticmethod
    def _resolve_server(host: str) -> Optional[str]:

        try:
            infos = socket.getaddrinfo(host, 123, type=socket.SOCK_DGRAM)
            if not infos:
                return None
            return infos[0][4][0]
        except socket.gaierror as e:
            logger.warning(f"System DNS error resolving IP address for {host}: {e}")
            return None
        except Exception as e:
            logger.error(f"DNS error for {host}: {e}")
            return None

    @staticmethod
    def _noise_sigma_ns(values: list) -> Optional[int]:
        """Estimate of σ of noise around the trend by first differences.
        values — newest-first. Resistant to linear drift.
        None if there is not enough data."""
        if len(values) < 3:
            return None
        if values and isinstance(values[0], tuple):
            vals = [v[0] for v in values]
        else:
            vals = list(values)
        diffs = [a - b for a, b in zip(vals[:-1], vals[1:])]
        if len(diffs) < 2:
            return None
        med = statistics.median(diffs)
        centered = [d - med for d in diffs]
        return int(statistics.stdev(centered) / math.sqrt(2.0))

    def _query_dns_resolv(self) -> set:
        """Periodic DNS resolution for NTP servers"""
        start_mono = time.monotonic_ns()
        with self.lock:
            if start_mono - self.ntp_servers_last_resolved_ns < NTP_RESOLVING_TIMEOUT_NS:
                return self.dns_fail_servers.copy()         # Return current state of excluded servers
            self.ntp_servers_last_resolved_ns = start_mono  # Fix the DNS request attempt

        executor = self._DNS_poll_executor  # Use permanent thread pool
        future_to_server = {}
        for server in self.ntp_servers:
            if not self.running:
                break
            future = executor.submit(self._resolve_server, server)
            future_to_server[future] = server

        try:
            done, not_done = concurrent.futures.wait(
                future_to_server.keys(),
                timeout=DNS_QUERY_TIMEOUT_SEC
            )
            for future in done:
                server = future_to_server[future]
                try:
                    server_ip = future.result()
                    ipaddress.ip_address(server_ip)
                    self.ntp_servers_resolved[server] = server_ip
                    with self.lock:
                        self.dns_fail_servers.discard(server)
                except Exception as e:
                    logger.warning(f"DNS error obtaining IP address for {server}: {e}")
                    with self.lock:
                        if server not in self.ntp_servers_resolved:
                            self.dns_fail_servers.add(server)
                    # otherwise — use previous IP, do not exclude

            for future in not_done:
                server = future_to_server[future]
                logger.warning(
                    f"DNS server did not respond to name resolution request {server} within {DNS_QUERY_TIMEOUT_SEC} seconds")
                future.cancel()
                with self.lock:
                    if server not in self.ntp_servers_resolved:
                        self.dns_fail_servers.add(server)
        except Exception as e:
            logger.error(f"Error processing DNS request thread: {e}")
        finally:
            return self.dns_fail_servers.copy()

    def _query_single_server(self, server: str,
                             t_round_start_mono: int,
                             n_attempts: int,
                             ) -> Optional[List[Optional[Tuple[int, int, int, int]]]]:
        """
        Polls the NTP server n_attempts times with spacing QUERY_ATTEMPT_SPACING_NS.

        Each successful attempt forms a time sample (tuple) linking the
        client's local monotonic scale to the server's absolute UTC scale.

        Time label notation (in nanoseconds):
          t1 - t1_mono: Local monotonic time of the client at the moment of sending the request.
          t2 - rec_utc_ns: Server UTC time at the moment of receiving the request (pkt['rec_utc_ns']).
          t3 - xmt_utc_ns: Server UTC time at the moment of sending the response (pkt['xmt_utc_ns']).
          t4 - t4_mono: Local monotonic time of the client at the moment of receiving the response.

        Math and invariants of the method:
          delay_ns     = (t4 − t1) − T_proc  (pure network RTT, without processing)
          mid_mono_ns  = (t1 + t4) // 2      (midpoint of the round on the client side)
          t2_utc_ns    = θ_ntp + mid_mono_ns (restored UTC time for this midpoint)
          proposed     = t2_utc_ns − mid_mono_ns = θ_ntp (difference between client and server clocks)

          θ_ntp = ((t2 − t1) + (t3 − t4)) / 2  — 4-timestamp time offset formula.
            Since t2 and t3 are measured in UTC, and t1 and t4 — in Monotonic, θ_ntp is computed
            as a gigantic number (~1.79e18 ns). This is the base epoch offset (Anchor Offset),
            necessary for downstream PLL loop algorithms to maintain the link: UTC = mono + offset.

        Method output (What is returned):
          * List of length n_attempts. Contains tuples for successful attempts
            and None for unsuccessful/filtered ones. Format of a successful sample:
              (delay_ns, mid_mono_ns, t2_utc_ns, offset_unused_ns)
              where:
                delay_ns         - pure network RTT (int, minimum 1)
                mid_mono_ns      - monotonic measurement point (int)
                t2_utc_ns        - UTC restored by the formula for this point (int)
                0                - unused field for bias shift (always 0 in this method)
          * None if absolutely all attempts of the round failed.
        """
        server_ip = self.ntp_servers_resolved.get(server)
        if server_ip is None:
            logger.warning(f"Server {server}: no known IP")
            return None

        samples: List[Optional[Tuple[int, int, int, int]]] = [None] * n_attempts
        for k in range(n_attempts):
            if not self.running:
                break

            t_query_mono = t_round_start_mono + k * QUERY_ATTEMPT_SPACING_NS
            wait_sec = (t_query_mono - time.monotonic_ns()) / 1e9
            if wait_sec > 0:
                if self._stop_event.wait(timeout=wait_sec):
                    break
            if not self.running:
                break

            try:
                # Fix local monotonic labels of sending (t1) and receiving (t4)
                t1_mono = time.monotonic_ns()
                raw = self._ntp_query_raw(server_ip, NTP_QUERY_TIMEOUT_SEC, t1_mono)
                t4_mono = time.monotonic_ns()

                if raw is None:
                    continue
                pkt = self._parse_ntp_packet(raw)
                if pkt is None:
                    continue

                # Sanity-filters of the packet
                if pkt['stratum'] == 0 or pkt['stratum'] >= 16:
                    continue
                if pkt['mode'] != 4:
                    continue
                if pkt['leap'] == 3:
                    continue

                # Origin echo
                if not self._check_origin_echo(pkt, t1_mono):
                    logger.debug(f"{server}: origin mismatch, discard")
                    continue

                # Persistent fields — update
                self._update_server_meta(server, pkt)

                # Extract server UTC labels (t2 and t3)
                t2_utc_ns = pkt['rec_utc_ns']
                t3_utc_ns = pkt['xmt_utc_ns']

                # Compute 4-timestamp offset (theta) in monotonic reference scale
                theta_ns = ((t2_utc_ns - t1_mono) + (t3_utc_ns - t4_mono)) // 2

                # Compute monotonic midpoint of the round
                mid_mono_ns = (t1_mono + t4_mono) // 2

                # Compute t2_utc_ns (restored UTC) for the midpoint of the round
                t2_utc_conv_ns = theta_ns + mid_mono_ns  # so that proposed = theta

                # Server processing time via the original method
                t_proc_ns = self._ntp_ts_diff_ns(pkt['xmt_ts_raw'], pkt['rec_ts_raw'])

                # Compute pure network RTT delay
                delay_net_ns = (t4_mono - t1_mono) - t_proc_ns
                if delay_net_ns < 1:
                    delay_net_ns = 1

                # Assemble the final round sample
                samples[k] = (int(delay_net_ns), int(mid_mono_ns), int(t2_utc_conv_ns), 0)
            except Exception as e:
                logger.debug(f"NTP request to {server} (attempt {k + 1}) failed: {e}")

        if all(s is None for s in samples):
            return None
        return samples

    def _poll_servers(self, n_attempts: int
                      ) -> Tuple[Dict[str, List[Optional[Tuple[int, int, int, int]]]], int]:
        """
        One network poll per round. Returns:
            samples_by_srv: {server: [sample_attempt_0, sample_attempt_1, ...]}
            t_ref_mono:     delay-weighted mean mid_mono over all samples
        """
        failed_servers = self._query_dns_resolv()
        t_round_start_mono = time.monotonic_ns()
        samples_by_srv: Dict[str, List[Optional[Tuple[int, int, int, int]]]] = {}

        executor = self._NTP_poll_executor
        filtered = [s for s in self.ntp_servers if s not in failed_servers]
        future_to_server = {
            executor.submit(self._query_single_server, s, t_round_start_mono, n_attempts): s
            for s in filtered
        }
        not_done_futures = set()
        try:
            timeout = (n_attempts * QUERY_ATTEMPT_SPACING_SEC
                       + NTP_QUERY_TIMEOUT_SEC + NTP_QUERY_TIMEOUT_SAFE_SEC)
            done, not_done = concurrent.futures.wait(future_to_server, timeout=timeout)
            for future in done:
                server = future_to_server[future]
                try:
                    result = future.result()
                except Exception as e:
                    logger.warning(f"Poll thread error {server}: {e}")
                    continue
                if result is not None and any(s is not None for s in result):
                    samples_by_srv[server] = result
                else:
                    logger.warning(f"Server {server} unavailable after {n_attempts} attempts")

            for future in not_done:
                not_done_futures.add(future)
                future.cancel()
                srv = future_to_server[future]
                logger.warning(f"Server {srv}: round timeout, result discarded")
        except Exception as e:
            logger.error(f"Error processing NTP request thread: {e}")
        finally:
            with self.lock:
                self._last_stalled_servers = tuple(
                    future_to_server[f] for f in not_done_futures
                )

        # Per-server delay history (best delay of this round)
        with self.lock:
            for srv, lst in samples_by_srv.items():
                if lst:
                    delays_ok = [smp[0] for smp in lst if smp is not None]
                    if delays_ok:
                        self._per_server_delay[srv].append(min(delays_ok))

        # t_ref_mono — over all samples
        all_samples = [(srv, smp) for srv, lst in samples_by_srv.items()
                       for smp in lst if smp is not None]
        if all_samples:
            t_ref_mono = int(statistics.mean(smp[1] for _, smp in all_samples))
        else:
            t_ref_mono = t_round_start_mono

        return samples_by_srv, t_ref_mono

    def _compute_consensus_sigma_ns(self) -> Optional[int]:
        """
        σ of consensus — stdev of the last HISTORY_MAX_LEN applied
        consensus_offset. This is the real spread of colony decisions around
        their norm over ~50 min (at 30 s/round), not PLL residuals.

        Window HISTORY_MAX_LEN = 100 gives stdev with relative error ~7%,
        vs ~19% with a window of 15.

        On a series of rejects σ is extended: prediction is detached from
        the fresh consensus_offset, the gate is too narrow. We extend σ
        by (1/THRESHOLD_SHRINK_FLOOR)^streak times, so that the gate gradually
        opens until prediction catches up with reality (via
        rate extrapolation, see Consensus._compute_history_votes).
        """
        with self.lock:
            recent = list(self._consensus.applied_offsets)[:HISTORY_MAX_LEN]
        if len(recent) < HISTORY_VOTE_SHORT_WINDOW:
            return None
        sigma = self._noise_sigma_ns(recent)

        # Extension of σ by the length of the reject series.
        # streak is read without lock — int counter, race is not critical.
        streak = self._consensus._consecutive_rejects
        if streak > 0:
            grow = (1.0 / THRESHOLD_SHRINK_FLOOR) ** streak
            sigma = int(sigma * grow)
        return sigma

    def _get_best_ntp_sample(self) -> Tuple[Optional[tuple], tuple]:
        """One round: poll → snapshot of raw → bias → consensus.round()."""

        # Number of attempts = current population size, but not less than MIN_POPULATION.
        # Each instance gets its own attempt_idx = k (position in ordered).
        pop = self._consensus.get_population_size()
        n_attempts = max(MIN_POPULATION, pop) if pop else MIN_POPULATION

        samples_by_srv, t_ref_mono = self._poll_servers(n_attempts)
        if not samples_by_srv:
            logger.error(
                f"Failed to synchronize with any NTP server: {self.ntp_servers}"
            )
            raise RuntimeError("All NTP requests failed")

        # Snapshot of RAW proposed — mean over attempts per server (for colony_bias).
        #Averaging attempts within a round — ergodicity, choose mean
        raw_snapshot = tuple(
            (srv, int(statistics.mean(smp[2] - smp[1]
                                      for smp in lst if smp is not None)))
            for srv, lst in samples_by_srv.items()
            if any(s is not None for s in lst)
        )

        # Apply bias to each sample (as before, but to the list).
        active_bias = {srv: self._colony_bias[srv]
                       for srv in self._colony_bias
                       if self._colony_bias[srv] != 0}
        if active_bias:
            adjusted: Dict[str, List[Tuple[int, int, int, int]]] = {}
            for srv, lst in samples_by_srv.items():
                b = active_bias.get(srv, 0)
                if b == 0:
                    adjusted[srv] = lst
                else:
                    adjusted[srv] = [
                        smp if smp is None else (smp[0], smp[1], smp[2] - b, smp[3] - b)
                        for smp in lst
                    ]
            samples_by_srv = adjusted

        # Seed for initializing the threshold of new instances.
        # _last_diff_threshold — mean threshold of live instances of the previous round
        # (already verified working value). On the very first round of the service
        # it does not exist yet — take COHERENCE_THRESHOLD_NS as a reasonable fallback,
        # so that a new instance does not start with the stdev of a random pair of diffs.
        seed_threshold_ns = (
            self._last_diff_threshold
            if self._last_diff_threshold is not None
            else COHERENCE_THRESHOLD_NS
        )

        # σ of consensus for post-gate filter — robust estimate (MAD).
        # See _compute_consensus_sigma_ns.
        sigma_consensus_ns = self._compute_consensus_sigma_ns()

        result = self._consensus.atom_round(
            samples_by_srv,
            t_ref_mono=t_ref_mono,
            rate=self._rate,
            seed_threshold_ns=seed_threshold_ns,
            sigma_consensus_ns=sigma_consensus_ns,
        )
        return result, raw_snapshot

    def _calculate_current_offset(self, now_mono: int) -> int:
        """Drift compensation. The only point of offset calculation.
        Call from any context — lock-free via _clock_snapshot."""
        if not self.is_synced_event.is_set():
            return 0
        anchor_mono, anchor_offset, rate = self._clock_snapshot
        return anchor_offset + round(rate * (now_mono - anchor_mono))

    def _compute_snapshot_ttl_locked(self) -> int:
        """Dynamic TTL of the clock model snapshot.

        Returns: TTL in nanoseconds.

        Logic:
            σ_cons_ref    — long-term reference of consensus noise:
                            _noise_sigma_ns over window HISTORY_MAX_LEN
                            applied_offsets.
            σ_cons_recent — same noise over short window
                            CLOCK_SNAPSHOT_TTL_RECENT_N.
            quality       — σ_cons_ref / max(σ_cons_recent, σ_cons_ref/2),
                            bounded [FLOOR, CEIL].
            TTL           — TTL_BASE · quality, bounded [ABS_MIN, ABS_MAX].

        Both σ — consensus level (same domain as anchor_offset).
        Per-instance σ_ref (noise_ref_ns) is not used for TTL: it
        measures the spread of per-server dev inside an instance before averaging
        over N servers and M instances, an order or two larger than the actual
        uncertainty of anchor_offset — which is why quality hit
        CEIL and TTL was always equal to 2·base. σ_ref remains only
        as reference for the reproduction gate.

        Early states (little data) — base TTL.
        Called under self.lock.
        """
        base_ttl_ns = int(
            self.sync_interval * CLOCK_SNAPSHOT_TTL_BASE_SEC * 1_000_000_000
        )
        abs_min_ns = int(CLOCK_SNAPSHOT_TTL_ABS_MIN_SEC * 1_000_000_000)
        abs_max_ns = int(CLOCK_SNAPSHOT_TTL_ABS_MAX_SEC * 1_000_000_000)

        long_window = list(self._consensus.applied_offsets)[:HISTORY_MAX_LEN]
        if len(long_window) < 3:
            return base_ttl_ns

        sigma_cons_ref = self._noise_sigma_ns(long_window)
        if sigma_cons_ref is None or sigma_cons_ref <= 0:
            return base_ttl_ns

        recent = long_window[:CLOCK_SNAPSHOT_TTL_RECENT_N]
        sigma_cons_recent = self._noise_sigma_ns(recent) or 0

        # Protection against division by too small σ_cons_recent: floor = σ_cons_ref / 2.
        denom = max(sigma_cons_recent, sigma_cons_ref * 0.5)
        quality = sigma_cons_ref / denom
        quality = max(CLOCK_SNAPSHOT_TTL_QUALITY_FLOOR,
                      min(CLOCK_SNAPSHOT_TTL_QUALITY_CEIL, quality))

        ttl_ns = int(base_ttl_ns * quality)
        return max(abs_min_ns, min(abs_max_ns, ttl_ns))

    def _start_sync_thread(self, slot_idx: int, initial_delay_sec: float) -> threading.Thread:
        t = threading.Thread(
            target=self._sync_worker,
            args=(slot_idx, initial_delay_sec),
            daemon=True,
            name=f"TimeSync-{slot_idx}"
        )
        t.start()
        with self._threads_lock:
            self._threads[slot_idx] = t
        return t

    def _update_bias_history_locked(self, raw_snapshot: Optional[tuple]) -> None:
        """Updates the raw-history for the bias integrator; when
        length is sufficient — recomputes bias. Call under self.lock."""
        if raw_snapshot is None:
            return
        cur_gen = self._consensus.cold_start_generation
        if cur_gen != self._last_cold_gen:
            self._defer_log("info",
                            f"Bias colony: cold start "
                            f"(gen {self._last_cold_gen} -> {cur_gen}); "
                            f"reset of accumulated bias"
                            )
            self._colony_bias.clear()
            self._raw_proposed_history.clear()
            self._phase_err_window.clear()
            # Rate-decision before clearing applied_offsets — otherwise _last_drift_slope
            # can no longer be read.
            self._maybe_reset_rate_on_cold_start_locked()

            # Offset trajectory is broken by a step (old composition → new).
            # Linear regression would see this step as a fake slope
            # for 30+ rounds. Clear — slope will be rebuilt over DRIFT_MIN_SAMPLES.
            self._consensus.applied_offsets.clear()
            self._consensus._recent_observed.clear()
            self._consensus._last_drift_slope = None
            self._consensus._last_drift_prediction = None
            self._consensus._last_apply_mono_ns = 0
            self._consensus._consecutive_rejects = 0

            # Pause for drift persist: do not write slope computed from fragments
            # until the next full window.
            self._last_drift_persist_tick = self._consensus._tick
            self._last_cold_gen = cur_gen
        self._raw_proposed_history.appendleft(raw_snapshot)
        if len(self._raw_proposed_history) >= HISTORY_MAX_LEN // 3:
            self._update_colony_bias()

    def _maybe_reset_rate_on_cold_start_locked(self) -> None:
        """
        Colony cold start — whether to reset self._rate to _rate_prior.

        rate carries accumulated measurement of physical drift (useful), but
        if PLL has drifted (bias, runaway I-integrator, step) — it may
        be garbage. Check the last robust drift_slope:
          • slope absent → no reason to trust rate, rate := prior.
          • |rate − slope| > DRIFT_COLD_START_DIVERGE_PPM → rate drifted away, reset to prior.
          • |rate − slope| ≤ threshold → rate is consistent with the measurement, keep.
        """
        slope_ns = self._consensus._last_drift_slope # race of _last_drift_slope is not critical
        rate_ppm = self._rate * 1e6

        if slope_ns is None:
            self._defer_log("info",
                            f"Cold start: drift_slope absent, "
                            f"rate {rate_ppm:+.3f} ppm → prior "
                            f"{self._rate_prior * 1e6:+.3f} ppm")
            self._rate = self._rate_prior
            return

        round_sec = self.sync_interval / SYNC_THREAD_SLOTS
        slope_ppm = slope_ns / (round_sec * 1e3)
        diverge = abs(rate_ppm - slope_ppm)
        if diverge > DRIFT_COLD_START_DIVERGE_PPM:
            self._defer_log("warning",
                            f"Cold start: rate {rate_ppm:+.3f} ppm diverges "
                            f"from drift_slope {slope_ppm:+.3f} ppm "
                            f"({diverge:.2f} ppm) — reset to prior "
                            f"{self._rate_prior * 1e6:+.3f} ppm")
            self._rate = self._rate_prior
        else:
            self._defer_log("info",
                            f"Cold start: rate {rate_ppm:+.3f} ppm consistent "
                            f"with drift_slope {slope_ppm:+.3f} ppm, keeping")

    def _rate_sign_constancy(self) -> Tuple[bool, int, float, float]:
        """
        Returns (triggered, direction, t_stat, runs_z).
        direction: +1 if mean(pe) > 0 (rate underestimated),
                   -1 if mean(pe) < 0 (rate overestimated),
                    0 if not significant.
        """
        w = list(self._phase_err_window)
        n = len(w)
        if n < RATE_DETECT_MIN_N:
            return False, 0, 0.0, 0.0
        mean_pe = statistics.mean(w)
        std_pe = statistics.stdev(w)
        if std_pe <= 0:
            return False, 0, 0.0, 0.0
        t = mean_pe / (std_pe / math.sqrt(n))

        signs = [1 if x > 0 else -1 for x in w if x != 0]
        if len(signs) < 2:
            return False, 0, t, 0.0
        runs = 1 + sum(1 for i in range(1, len(signs))
                       if signs[i] != signs[i - 1])
        E_r = len(signs) / 2.0 + 1.0
        V_r = (len(signs) - 1) / 4.0
        z = (runs - E_r) / math.sqrt(V_r) if V_r > 0 else 0.0

        triggered = (abs(t) > RATE_T_THRESHOLD) and (z < RATE_Z_THRESHOLD)
        direction = (1 if mean_pe > 0 else -1) if triggered else 0
        return triggered, direction, t, z

    def _apply_drift_confirmation_locked(self) -> None:
        """
        Pull of self._rate to consensus._last_drift_slope.

        rate    — I-integrator of PLL, reactive (quickly catches changes),
                  but subject to runaway and bias accumulation.
        slope   — linear regression of applied_offsets over DRIFT_WINDOW points,
                  robust to outliers, but does not see fast changes.

        If they are close — the divergence is within normal noise,
        do not touch: rate carries its own PLL information.
        If they diverged beyond threshold — consider rate drifted away and pull
        to slope with weight (1−ALPHA):
            rate ← ALPHA·rate + (1−ALPHA)·slope

        ALPHA=0 → full replacement; ALPHA=1 → mechanism disabled.
        """
        if DRIFT_CONFIRM_ALPHA >= 1.0:
            return
        if len(self._consensus.applied_offsets) < DRIFT_WINDOW:
            return
        slope_ns = self._consensus._last_drift_slope
        if slope_ns is None:
            return

        round_sec = self.sync_interval / SYNC_THREAD_SLOTS
        slope_ppm = slope_ns / (round_sec * 1e3)
        rate_ppm = self._rate * 1e6
        if abs(rate_ppm - slope_ppm) <= DRIFT_CONFIRM_RATE_DIVERGE_PPM:
            return

        new_rate_ppm = (DRIFT_CONFIRM_ALPHA * rate_ppm
                        + (1.0 - DRIFT_CONFIRM_ALPHA) * slope_ppm)
        new_rate = max(-PLL_RATE_LIMIT,
                       min(PLL_RATE_LIMIT, new_rate_ppm * 1e-6))
        self._defer_log("info",
                        f"Drift confirm: rate {rate_ppm:+.3f} → "
                        f"{new_rate * 1e6:+.3f} ppm "
                        f"(slope {slope_ppm:+.3f}, "
                        f"α={DRIFT_CONFIRM_ALPHA:.2f}, "
                        f"Δ={rate_ppm - slope_ppm:+.2f} ppm)")
        self._rate = new_rate

    def _publish_clock_and_metrics_locked(self) -> None:
        """Recomputes ttl/accuracy from current anchor/rate and publishes
        the consistent clock snapshot with a single assignment.

        Call under self.lock after ANY change of _anchor_mono,
        _anchor_offset or _rate. Guarantees: _clock_snapshot_full
        contains five fields computed from one clock state;
        in CPython reading one tuple is atomic.

        Side effect: invalidates _precision_cache — it stores
        TTL/accuracy computed for the previous noise/rate state.
        """
        self._clock_snapshot_ttl_ns = self._compute_snapshot_ttl_locked()
        self._clock_snapshot_accuracy_ns = self._compute_snapshot_accuracy_locked(
            self._clock_snapshot_ttl_ns
        )
        self._clock_snapshot = (self._anchor_mono, self._anchor_offset, self._rate)
        self._clock_snapshot_full = (
            self._anchor_mono,
            self._anchor_offset,
            self._rate,
            self._clock_snapshot_ttl_ns,
            self._clock_snapshot_accuracy_ns,
        )
        if self._precision_cache:
            self._precision_cache.clear()

    def _apply_rate_sign_constancy_step_locked(self) -> None:
        """
        I-correction of rate by sign constancy of phase_error.

        Called both from the normal PLL update (accept-branch) and from
        the post-gate reject branch — reject-deltas (raw consensus_offset minus
        model prediction) carry the same information about whether the current rate is right
        as accept-deltas.

        Reads self._phase_err_window, which must be formed by the
        caller BEFORE the call: in the accept-branch the current phase_error
        is added to the window AFTER the call, so that it does not influence its own
        decision; in the reject-branch — BEFORE the call.

        Call under self.lock.
        """
        triggered, direction, t, z = self._rate_sign_constancy()
        if not triggered:
            return

        confidence = min(1.0, (abs(t) - RATE_T_THRESHOLD) / RATE_SPRING_T_SCALE)
        step_ppm = (self._rate_spring_base_ppm
                    + confidence * (self._rate_spring_max_ppm
                                    - self._rate_spring_base_ppm))
        delta_ppm = direction * step_ppm
        new_rate_ppm = self._rate * 1e6 + delta_ppm
        # Additional soft pull to prior — insurance against runaway
        new_rate_ppm += RATE_PRIOR_PULL * (self._rate_prior * 1e6 - new_rate_ppm)
        # Clamp around prior
        lo = self._rate_prior * 1e6 - RATE_CLAMP_FROM_PRIOR_PPM
        hi = self._rate_prior * 1e6 + RATE_CLAMP_FROM_PRIOR_PPM
        new_rate_ppm = max(lo, min(hi, new_rate_ppm))
        self._rate = new_rate_ppm * 1e-6
        self._defer_log("debug",
                        f"rate_sign_constancy: t={t:+.2f} z={z:+.2f} "
                        f"dir={direction:+d} step={step_ppm:.2f} ppm")

    def _apply_new_sync_locked(self, best_mono, best_utc,
                               servers=None, diff_threshold_ns=None,
                               raw_snapshot=None, matrix=None, matrix_meta=None,
                               rejected_offset_ns: Optional[int] = None,
                               rejected_prediction_ns: Optional[int] = None,
                               applied: bool = True) -> None:
        new_target_offset = int(best_utc - best_mono)

        # --- Rejected by post-gate: write to history, touch PLL carefully ---
        if not applied:
            dt_ns = best_mono - self._anchor_mono
            predicted_now = self._anchor_offset + int(self._rate * dt_ns)
            phase_err_reject = int(rejected_offset_ns - predicted_now)

            self._slew_error_history.appendleft(SlewRecord(
                timestamp_ns=time.time_ns(),
                diff_ns=phase_err_reject,
                threshold_ns=int(diff_threshold_ns) if diff_threshold_ns is not None else None,
                instance_id=None,
                servers=servers,
                ref_ns=predicted_now,
                matrix=matrix,
                matrix_meta=matrix_meta,
                is_stale=False,
                rejected_offset_ns=rejected_offset_ns,
            ))
            self._update_bias_history_locked(raw_snapshot)
            rate_at_entry = self._rate
            streak = self._consensus._consecutive_rejects
            rate_before_streak_ppm = rate_at_entry * 1e6
            if streak >= REJECT_RATE_RESET_STREAK:
                rate_ppm = self._rate * 1e6
                prior_ppm = self._rate_prior * 1e6
                diverge = abs(rate_ppm - prior_ppm)
                if diverge > REJECT_RATE_RESET_MIN_DIVERGE_PPM:
                    new_ppm = (1 - REJECT_RATE_RESET_GAIN) * rate_ppm \
                              + REJECT_RATE_RESET_GAIN * prior_ppm
                    self._rate = new_ppm * 1e-6
                    self._phase_err_window.clear()
                    self._defer_log("warning",
                                    f"Post-gate reject-streak={streak}: rate "
                                    f"{rate_ppm:+.3f} → {new_ppm:+.3f} ppm "
                                    f"(prior {prior_ppm:+.3f}, Δ={diverge:.2f} ppm). "
                                    f"Check hardware/temperature or clear the timesync_config.db database, "
                                    f"the drift_history table (otherwise historical data may be erroneous)")

            rate_before_ppm = self._rate * 1e6
            self._last_phase_error = phase_err_reject

            if dt_ns >= self._min_pll_update_interval_ns:
                # Strategy selection by reject series length:
                #   streak == 1  → a single outlier is not fully excluded,
                #                  keep anchor at gate opinion (trusted
                #                  trajectory from applied_offsets), do NOT pull to raw.
                #   streak >= 2  → the series is confirmed, the gate is stale, raw carries
                #                  the actual clock position. P-correction
                #                  to the RAW observation.
                if streak <= 1:
                    self._anchor_offset = int(rejected_prediction_ns)
                else:
                    self._anchor_offset = (
                            predicted_now + int(PLL_KP_REJECT * phase_err_reject)
                    )
                self._anchor_mono = best_mono

                # rate-detector — BEFORE append, as in the accept-branch:
                # the current observation must not vote for its own
                # decision (unified semantics in all branches).
                self._apply_rate_sign_constancy_step_locked()
                self._phase_err_window.appendleft(phase_err_reject)

            # Publish the consistent snapshot if anchor or rate changed.
            # rate changes not only inside the dt-branch (sign_constancy),
            # but also in the reject-streak reset above — therefore we check both conditions.
            # _publish_clock_and_metrics_locked also invalidates
            # _precision_cache, which is critical: old TTL/accuracy were
            # computed under the previous rate.
            if (self._rate != rate_at_entry
                    or dt_ns >= self._min_pll_update_interval_ns):
                self._publish_clock_and_metrics_locked()

            rate_after_ppm = self._rate * 1e6
            rate_delta_ppm = rate_after_ppm - rate_before_ppm
            win_len = len(self._phase_err_window)
            moved = "moved" if abs(rate_delta_ppm) > 1e-9 else "unchanged"

            self._defer_log("info",
                            f"Post-gate rejected raw={rejected_offset_ns} ns; "
                            f"predicted_model={predicted_now} ns; "
                            f"new_anchor={self._anchor_offset} ns; "
                            f"streak={streak} "
                            f"({'gate' if streak <= 1 else 'P-to-raw'}); "
                            f"phase_err={phase_err_reject} ns; "
                            f"rate {rate_before_streak_ppm:+.3f}→{rate_after_ppm:+.3f} ppm "
                            f"streak-reset {rate_before_streak_ppm:+.3f}→{rate_before_ppm:+.3f} ppm; "
                            f"sign-const {rate_before_ppm:+.3f}→{rate_after_ppm:+.3f} ppm "
                            f"(Δ={rate_delta_ppm:+.3f}, {moved}, "
                            f"window={win_len}/{RATE_DETECT_MIN_N})"
                            )
            # target follows what we actually consider to be truth:
            #   streak ≤ 1  → gate opinion (trust gate)
            #   streak ≥ 2  → raw (the model has already moved toward it via P-correction)
            self._target_offset = (
                int(rejected_prediction_ns) if streak <= 1 else int(rejected_offset_ns)
            )
            return

        # --- Primary synchronization ---
        if not self.is_synced_event.is_set():
            self._defer_log("info",
                            f"Primary time synchronization completed, "
                            f"offset={new_target_offset} ns"
                            )
            self._anchor_offset = new_target_offset
            self._anchor_mono = best_mono  # anchor — moment of measurement, not now
            self._rate = self._rate_prior
            self._publish_clock_and_metrics_locked()
            self._target_offset = new_target_offset
            self.is_synced_event.set()
            return

        # --- Cutoff of "stuck" updates ---
        # Both sync streams could be woken simultaneously (screen sleep,
        # Modern Standby). Then dt in the denominator of I-correction is ~0, rate
        # instantly saturates. Skip PLL, but write to history.
        dt_ns = best_mono - self._anchor_mono
        if dt_ns < self._min_pll_update_interval_ns:
            self._slew_error_history.appendleft(SlewRecord(
                timestamp_ns=time.time_ns(),
                diff_ns=int(self._last_phase_error or 0),
                threshold_ns=int(diff_threshold_ns) if diff_threshold_ns is not None else None,
                instance_id=None,
                servers=servers,
                ref_ns=new_target_offset,
                matrix=matrix,
                matrix_meta = matrix_meta,
                is_stale = True,
                rejected_offset_ns=None
            ))
            self._update_bias_history_locked(raw_snapshot)
            self._target_offset = new_target_offset
            self._defer_log("debug",
                            f"PLL: skipping short dt={dt_ns / 1e6:.1f}ms "
                            f"(< {self._min_pll_update_interval_ns / 1e6:.0f}ms), "
                            f"rate not updated"
                            )
            return

        # --- Prediction and phase error ---
        predicted = self._anchor_offset + int(self._rate * dt_ns)
        phase_error = new_target_offset - predicted  # observed − predicted
        self._last_phase_error = int(phase_error)

        # --- Phase jump detection ---
        if abs(phase_error) > PHASE_JUMP_THRESHOLD_NS:
            self._defer_log("warning",
                            f"PLL: phase jump {phase_error / 1e6:+.1f}ms "
                            f"(> ±{PHASE_JUMP_THRESHOLD_NS / 1e6:.0f}ms) — "
                            f"reset anchor, rate reset")
            self._anchor_offset = new_target_offset
            self._anchor_mono = best_mono
            self._rate = self._rate_prior
            self._publish_clock_and_metrics_locked()
            self._target_offset = new_target_offset

            self._slew_error_history.appendleft(SlewRecord(
                timestamp_ns=time.time_ns(),
                diff_ns=int(phase_error),  # ← was -phase_error
                threshold_ns=int(diff_threshold_ns) if diff_threshold_ns is not None else None,
                instance_id=None,
                servers=servers,
                ref_ns=new_target_offset,
                matrix=matrix,
                matrix_meta=matrix_meta,
                is_stale=False,
                rejected_offset_ns=None,
            ))
            self._update_bias_history_locked(raw_snapshot)
            self._phase_err_window.clear()
            return

        # --- Normal PLL update ---
        self._anchor_mono = best_mono
        self._anchor_offset = predicted

        if abs(phase_error) > PHASE_MEDIUM_JUMP_NS:
            kp = PLL_KP_MEDIUM
        else:
            kp = PLL_KP
        self._anchor_offset += int(kp * phase_error)

        self._apply_rate_sign_constancy_step_locked()
        self._apply_drift_confirmation_locked()
        self._phase_err_window.appendleft(phase_error)
        self._publish_clock_and_metrics_locked()
        self._slew_error_history.appendleft(SlewRecord(
            timestamp_ns=time.time_ns(),
            diff_ns=int(phase_error),  # ← was diff = -phase_error, int(diff)
            threshold_ns=int(diff_threshold_ns) if diff_threshold_ns is not None else None,
            instance_id=None,
            servers=servers,
            ref_ns=new_target_offset,
            matrix=matrix,
            matrix_meta=matrix_meta,
            is_stale=False,
            rejected_offset_ns=rejected_offset_ns,
        ))
        self._update_bias_history_locked(raw_snapshot)

        self._target_offset = new_target_offset
        self._defer_log("debug",
                        f"Synchronization updated: offset={new_target_offset} ns, "
                        f"phase_err={phase_error} ns, rate={self._rate * 1e6:+.3f} ppm, "
                        f"threshold=±{diff_threshold_ns} ns, "
                        f"ttl={self._clock_snapshot_ttl_ns / 1e9:.1f}s, "
                        f"acc={self._clock_snapshot_accuracy_ns / 1e6:.2f}ms")

        tick = self._consensus._tick #Race of reading _consensus._tick is not critical
        slope_ns_per_round = self._consensus._last_drift_slope
        if (tick - self._last_drift_persist_tick >= DRIFT_WINDOW
                and slope_ns_per_round is not None
                and len(self._consensus.applied_offsets) >= DRIFT_WINDOW):
            self._last_drift_persist_tick = tick
            round_sec = self.sync_interval / SYNC_THREAD_SLOTS
            slope_ppm = slope_ns_per_round / (round_sec * 1e3)
            try:
                append_drift_sample(slope_ppm)
                self._defer_log("info",
                                f"Drift persist: slope={slope_ppm:+.3f} ppm "
                                f"({slope_ns_per_round:.1f} ns/round, round={round_sec:.0f}s), "
                                f"tick={tick}")
            except Exception as e:
                self._defer_log("warning", f"Drift persist failed: {e}")

    def _sync_worker(self, thread_id: int, initial_delay_sec: float) -> None:
        if initial_delay_sec > 0:
            if self._stop_event.wait(timeout=initial_delay_sec):
                return

        while self.running:
            t_start = time.monotonic()
            logs: List[Tuple[str, str]] = []
            try:
                payload = self._get_best_ntp_sample()
                result, raw_snapshot = payload

                if result is None:
                    # outside lock — normal logger, no deadlock
                    logger.debug(f"Thread {thread_id}: round skipped by consensus")
                    with self.lock:
                        self._raw_proposed_history.appendleft(raw_snapshot)
                        if len(self._raw_proposed_history) >= HISTORY_MAX_LEN // 3:
                            self._update_colony_bias()
                        logs = self._drain_pending_logs()
                else:
                    (best_mono, best_utc, delays, best_offset_ns,
                     servers, diff_threshold_ns, matrix, matrix_meta,
                     rejected_offset_ns, rejected_prediction_ns, applied) = result
                    logger.debug(
                        f"Thread {thread_id}: round took "
                        f"{time.monotonic() - t_start:.2f}s, responses: {len(delays)}"
                    )
                    with self.lock:
                        self._last_diff_threshold = diff_threshold_ns
                        self._apply_new_sync_locked(
                            best_mono, best_utc, servers,
                            diff_threshold_ns, raw_snapshot, matrix, matrix_meta,
                            rejected_offset_ns=rejected_offset_ns, rejected_prediction_ns=rejected_prediction_ns,
                            applied=applied,
                        )
                        self._delay_history.extend(delays)
                        self._last_success_mono_ns = time.monotonic_ns()
                        logs = self._drain_pending_logs()
            except Exception as e:
                if self.running:
                    logger.warning(
                        f"Synchronization thread {thread_id} failed to synchronize: {e}"
                    )
            finally:
                _emit_deferred_logs(logs)

            elapsed = time.monotonic() - t_start
            remaining = max(0.0, self.sync_interval - elapsed)
            if self._stop_event.wait(timeout=remaining):
                break

    def _get_times_locked(self) -> Tuple[int, int]:
        now_mono = time.monotonic_ns()
        system = time.time_ns()
        if not self.is_synced_event.is_set():
            return system, system
        current_offset = self._calculate_current_offset(now_mono)
        return int(now_mono + current_offset), system

    def _update_colony_bias(self) -> None:
        """
        Estimation of constant server shift from RAW history
        (_raw_proposed_history, before bias application).

        Raw history excludes self-feedback: the integrator does not see
        its own past corrections, therefore convergence is guaranteed.

        Formula:
            mean_raw[s]  = mean of raw proposed
            sigma[s]     = server noise by raw proposed
            M            = median(mean_raw) over active
            delta[s]     = mean_raw[s] − M
            delta_app[s] = delta[s] − sign(delta)·sigma[s],  if |delta| > sigma
                         = 0,                                otherwise
            b[s] ← (1-k)·b[s] + k·delta_app[s], clamp ±1 ms

        Soft-threshold: a shift that does not stick out of the server noise
        is ignored. The median converges to the common center, noisy servers
        do not introduce a false shift.
        """

        per_server: Dict[str, List[int]] = {}
        for snapshot in self._raw_proposed_history:
            for srv, proposed in snapshot:
                per_server.setdefault(srv, []).append(proposed)

        min_history = HISTORY_MAX_LEN // 3

        stats: Dict[str, Tuple[float, float]] = {}
        for srv, vals in per_server.items():
            if len(vals) < min_history:
                continue
            # mean over levels: contains reference drift, but it is common for all
            # servers and cancels in delta = mean_s − median_mean.
            #Averaging over time within a server — ergodicity, choose mean.
            mean_s = statistics.mean(vals)
            # sigma over differences of adjacent rounds: removes reference drift,
            # leaves pure server jitter.
            diffs = [vals[i] - vals[i + 1] for i in range(len(vals) - 1)]
            if len(diffs) >= 2:
                med_d = statistics.median(diffs)
                mad = statistics.median(abs(d - med_d) for d in diffs)
                sigma_s = 1.4826 * mad / math.sqrt(2.0)
            else:
                sigma_s = 0.0
            stats[srv] = (mean_s, sigma_s)

        active = list(stats.keys())
        if not active:
            return
        #Common center for robust shift of servers. Median will not "move" from a stuck server.
        median_mean = statistics.median(stats[s][0] for s in active)

        k_integrator = COLONY_BIAS_GAIN
        leak = 1.0 - k_integrator

        updated = set()
        if len(active) >= 2:
            for srv in active:
                mean_s, sigma_s = stats[srv]
                delta = mean_s - median_mean
                # Soft-threshold: remove the noise threshold of the server.
                if abs(delta) > K_THRESHOLD * sigma_s:
                    delta_applied = delta
                else:
                    delta_applied = 0.0

                prev = self._colony_bias.get(srv, 0)
                b = round(leak * prev + k_integrator * delta_applied)
                b = max(-COLONY_BIAS_MAX_NS, min(COLONY_BIAS_MAX_NS, b))
                self._colony_bias[srv] = b
                updated.add(srv)

        # Decay for servers not updated by the integrator
        for srv in list(self._colony_bias.keys()):
            if srv in updated:
                continue
            new_val = round(self._colony_bias[srv] * COLONY_BIAS_DECAY)
            if abs(new_val) < COLONY_BIAS_DECAY_FLOOR:
                del self._colony_bias[srv]
            else:
                self._colony_bias[srv] = new_val

        if active:
            self._defer_log("debug",
                            f"Bias colony: "
                            f"{ {s: round(self._colony_bias.get(s, 0) / 1e6, 3) for s in active} } ms "
                            f"| sigma: "
                            f"{ {s: round(stats[s][1] / 1e6, 3) for s in active} } ms"
                            )

    def _watchdog_worker(self) -> None:
        # 3 intervals without success, but not less than 3 minutes
        stale_limit_sec = max(3 * self.sync_interval, 180)
        # 3× of stale — emergency exit (optional)
        hard_limit_sec = 3 * stale_limit_sec

        while self.running:
            if self._stop_event.wait(timeout=WATCHDOG_INTERVAL_SEC):
                break

            # 1. Thread liveness — as before
            for slot_idx in range(SYNC_THREAD_SLOTS):
                with self._threads_lock:
                    thread = self._threads.get(slot_idx)
                if thread is None or not thread.is_alive():
                    if not self.running:
                        break
                    logger.warning(f"Synchronization thread {slot_idx} is dead, restarting...")
                    with self._threads_lock:
                        self._threads.pop(slot_idx, None)
                    initial_delay = 0.0 if slot_idx == 0 else self.second_sync_thread_delay
                    self._start_sync_thread(slot_idx, initial_delay)

            # 2. Progress: time since the last successful round
            if not self.is_synced_event.is_set():
                # primary synchronization has not yet occurred — do not panic
                continue

            with self.lock:
                last_ok = self._last_success_mono_ns
                stalled = self._last_stalled_servers

            if last_ok == 0:
                continue

            stale_sec = (time.monotonic_ns() - last_ok) / 1e9

            if stale_sec >= stale_limit_sec:
                logger.critical(
                    f"No successful rounds for {stale_sec:.1f}s "
                    f"(> {stale_limit_sec:.0f}s). "
                    f"Stuck: {stalled or '(not recorded)'}. "
                    f"Check DNS/getaddrinfo and availability of NTP servers."
                )

            if stale_sec >= hard_limit_sec:
                # External supervisor (systemd/supervisor) will restart the process.
                # os._exit does not run atexit/join — needed here.
                logger.critical(
                    f"Synchronization is not recovering for {stale_sec:.1f}s — "
                    f"emergency exit for supervisor restart"
                )
                #os._exit(1)

    def _keep_awake_worker(self) -> None:
        """Keep the system in a working state while the service is alive.

        Windows:
            SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) —
            affects the whole process, blocks Modern Standby. The flag
            is periodically re-set: some Windows builds
            reset it when exiting idle. On stop() the flag is cleared.

        Linux:
            Suspend cannot be blocked from user-space. The only correct
            way — mask systemd sleep targets from root:
                systemctl mask sleep.target suspend.target \\
                    hibernate.target hybrid-sleep.target
            Here we only check this once at startup and write CRITICAL
            if not masked. After that the thread terminates — there is no point
            in keeping it alive.

        Other OS — no-op.
        """
        # --- Linux: one-time check ---
        if sys.platform.startswith("linux"):
            problem = self._linux_suspend_not_masked()
            if problem is None:
                self._defer_log(
                    "info",
                    "keep-awake: Linux — sleep targets are masked, "
                    "suspend is blocked at the system level",
                )
            else:
                self._defer_log(
                    "critical",
                    f"keep-awake: Linux — suspend is NOT blocked ({problem}). "
                    f"Run as root: systemctl mask sleep.target suspend.target "
                    f"hibernate.target hybrid-sleep.target"
                )
            with self.lock:
                logs = self._drain_pending_logs()
            _emit_deferred_logs(logs)
            return

        # --- Windows: real keep-awake ---
        if not sys.platform.startswith("win"):
            return

        import ctypes
        from ctypes import wintypes

        # IMPORTANT: use_last_error=True — otherwise ctypes.get_last_error()
        # always returns 0, because the private copy of the error code
        # is not synchronized with the real GetLastError() from Windows.
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

        # IMPORTANT: explicit argtypes/restype — without them ctypes by default
        # uses c_int for everything, which on x64 can truncate 64-bit
        # HANDLE and lead to call failures without visible error.
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.argtypes = []

        kernel32.SetProcessInformation.restype = wintypes.BOOL
        kernel32.SetProcessInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]

        kernel32.SetPriorityClass.restype = wintypes.BOOL
        kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]

        kernel32.SetThreadExecutionState.restype = wintypes.DWORD
        kernel32.SetThreadExecutionState.argtypes = [wintypes.DWORD]

        h_process = kernel32.GetCurrentProcess()

        # --- Disable Windows Power Throttling (EcoQoS) for the process ---
        # Without this, Windows classifies the background process as "not important",
        # moves it to E-cores and limits CPU share. Symptom in logs:
        # rounds lasting 36/66/120/240 seconds instead of 4, round multiples
        # of sync_interval, both sync threads complete in one millisecond,
        # watchdog is also silent for hours.

        try:
            PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
            PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
            PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION = 0x4
            ProcessPowerThrottling = 4

            class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
                # Explicit 4-byte alignment, so that the size is exactly 12
                # and matches C-ABI (three ULONG).
                _pack_ = 4
                _fields_ = [
                    ('Version', ctypes.c_uint32),
                    ('ControlMask', ctypes.c_uint32),
                    ('StateMask', ctypes.c_uint32),
                ]

            state = PROCESS_POWER_THROTTLING_STATE()
            state.Version = PROCESS_POWER_THROTTLING_CURRENT_VERSION
            state.StateMask = 0
            struct_size = ctypes.sizeof(state)

            self._defer_log(
                "debug",
                f"keep-awake: PROCESS_POWER_THROTTLING_STATE size={struct_size}",
            )

            # List of ControlMask variants in descending order of functionality.
            # IGNORE_TIMER_RESOLUTION is not supported by all Windows builds;
            # if the system does not know it — we get ERROR_INVALID_PARAMETER (87)
            # and try the simpler variant.
            variants = [
                (
                    PROCESS_POWER_THROTTLING_EXECUTION_SPEED
                    | PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION,
                    "EXECUTION_SPEED | IGNORE_TIMER_RESOLUTION",
                ),
                (
                    PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
                    "EXECUTION_SPEED",
                ),
            ]

            applied = False
            last_err = 0
            for control_mask, desc in variants:
                state.ControlMask = control_mask
                ctypes.set_last_error(0)
                ok = kernel32.SetProcessInformation(
                    h_process,
                    ProcessPowerThrottling,
                    ctypes.byref(state),
                    struct_size,
                )
                if ok:
                    self._defer_log(
                        "info",
                        f"keep-awake: Power Throttling disabled ({desc})",
                    )
                    applied = True
                    break
                err = ctypes.get_last_error()
                last_err = err
                if err != 87:
                    # Real error (not "flag not supported") —
                    # there is no point in trying other variants.
                    err_text = ctypes.FormatError(err).strip() if err else "(no code)"
                    self._defer_log(
                        "warning",
                        f"keep-awake: SetProcessInformation failed "
                        f"with {desc} (GetLastError={err} {err_text})",
                    )
                    break
                # err == 87 — the system does not understand this variant,
                # try the next one.

            if not applied and last_err == 87:
                self._defer_log(
                    "warning",
                    "keep-awake: Power Throttling could not be disabled "
                    "(all variants returned ERROR_INVALID_PARAMETER). "
                    "Perhaps Windows is too old for "
                    "SetProcessInformation(ProcessPowerThrottling); "
                    "check winver. Other measures (priority, "
                    "ES_SYSTEM_REQUIRED) are applied."
                )
        except Exception as e:
            self._defer_log(
                "warning",
                f"keep-awake: exception while disabling Power Throttling: {e}",
            )

        # --- Priority ABOVE_NORMAL ---
        # Not HIGH, so as not to take CPU from user tasks, but
        # enough so that the scheduler does not postpone the process behind other
        # background ones. In a separate try/except: if it does not work, keep-awake
        # must still start, otherwise Modern Standby returns.
        try:
            ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
            ctypes.set_last_error(0)
            ok = kernel32.SetPriorityClass(h_process, ABOVE_NORMAL_PRIORITY_CLASS)
            if not ok:
                err = ctypes.get_last_error()
                err_text = ctypes.FormatError(err).strip() if err else "(no code)"
                self._defer_log(
                    "warning",
                    f"keep-awake: SetPriorityClass failed "
                    f"(GetLastError={err} {err_text}). "
                    f"Priority remains Normal."
                )
            else:
                self._defer_log("info", "keep-awake: priority class = ABOVE_NORMAL")
        except Exception as e:
            self._defer_log(
                "warning",
                f"keep-awake: exception while raising priority: {e}",
            )

        # --- SetThreadExecutionState: block Modern Standby ---
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001

        self._defer_log("info", "keep-awake: ES_SYSTEM_REQUIRED request set")
        with self.lock:
            logs = self._drain_pending_logs()
        _emit_deferred_logs(logs)

        try:
            while self.running:
                kernel32.SetThreadExecutionState(
                    ES_CONTINUOUS | ES_SYSTEM_REQUIRED
                )
                if self._stop_event.wait(timeout=KEEP_AWAKE_REFRESH_SEC):
                    break
        finally:
            kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            self._defer_log("info", "keep-awake: request cleared")
            with self.lock:
                logs = self._drain_pending_logs()
            _emit_deferred_logs(logs)

    @staticmethod
    def _linux_suspend_not_masked() -> Optional[str]:
        """Checks whether systemd sleep targets are masked.

        Returns:
            None  — all ok, suspend is blocked;
            str   — description of the problem (what is not masked / systemctl
                    unavailable / call failed).

        Masking (`systemctl mask sleep.target ...`) — the only way
        from user-space without root to prevent suspend. From application code
        this cannot be done: privileges are required, therefore we only check and
        warn once at startup.
        """
        import shutil
        import subprocess
        # NOTE: shutil.which('systemctl') — pass str, not PathLike.
        # PyCharm inspector may complain about PathLike case from Python < 3.12
        # on Windows, but it is not applicable here: the code is executed only on Linux.
        if shutil.which("systemctl") is None:
            return "systemctl not found (not a systemd system or container)"

        targets = [
            "sleep.target",
            "suspend.target",
            "hibernate.target",
            "hybrid-sleep.target",
        ]
        not_masked: List[str] = []
        for t in targets:
            try:
                r = subprocess.run(
                    ["systemctl", "show", "-p", "LoadState", "--value", t],
                    capture_output=True, text=True, timeout=3,
                )
            except Exception as e:
                return f"failed to execute systemctl show {t}: {e}"
            state = (r.stdout or "").strip()
            # Normal state — 'masked'. 'masked-runtime' is also accepted:
            # valid until reboot, but right now suspend is blocked.
            if state not in ("masked", "masked-runtime"):
                not_masked.append(f"{t}={state or 'unknown'}")

        if not_masked:
            return ", ".join(not_masked)
        return None

    def _defer_log(self, level: str, msg: str) -> None:
        self._pending_logs.append((level, msg))

    def _drain_pending_logs(self) -> List[Tuple[str, str]]:
        logs = self._pending_logs
        self._pending_logs = []
        return logs

    def _ensure_executors(self):
        if self._NTP_poll_executor is None or self._executors_shutdown:
            self._NTP_poll_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=len(self.ntp_servers) + 2,
            thread_name_prefix="ntp-poll",
        )
        if self._DNS_poll_executor is None or self._executors_shutdown:
            self._DNS_poll_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=len(self.ntp_servers),
            thread_name_prefix="dns-poll",
        )
        self._executors_shutdown = False

    def _compute_snapshot_accuracy_locked(self, ttl_ns: int) -> int:
        """Upper bound of |utc − utc_true| within TTL.

        Error model:
            err(TTL) ≈ σ_cons + δ_rate · TTL
        where:
            σ_cons  — observed consensus_offset noise (ns), computed as
                      _noise_sigma_ns(applied_offsets) — actual spread of
                      colony decisions around their norm. It is this noise, and not
                      per-server σ inside an instance, that determines the uncertainty of
                      anchor_offset: consensus averages N servers and M
                      instances, and after averaging the per-server contribution is suppressed.
            δ_rate  — rate extrapolation error, dimensionless (ns/ns),
                      estimated via σ_cons / round_ns.
            round_ns — real interval between adjacent applied (two
                      sync streams with offset sync_interval/2).

        Early start (few applied_offsets): return
        CLOCK_SNAPSHOT_ACCURACY_DEFAULT_NS as a reasonable default.
        """
        recent = list(self._consensus.applied_offsets)[:CLOCK_SNAPSHOT_TTL_RECENT_N]
        if len(recent) < 3:
            return int(CLOCK_SNAPSHOT_ACCURACY_DEFAULT_NS)

        sigma_cons = self._noise_sigma_ns(recent) or 0
        if sigma_cons <= 0:
            return int(CLOCK_SNAPSHOT_ACCURACY_DEFAULT_NS)

        round_ns = (self.sync_interval * 1_000_000_000) // SYNC_THREAD_SLOTS
        delta_rate = sigma_cons / max(round_ns, 1)
        err = sigma_cons + delta_rate * ttl_ns
        return int(err)

    def _ttl_for_precision_locked(self, precision_ns: int) -> Tuple[int, int]:
        """TTL at which accuracy_ns ≤ precision_ns.

        Returns (ttl_ns, accuracy_ns), both in ns.

        If σ_cons already exceeds the requested precision — the precision
        is unattainable at any TTL. Return the minimum TTL
        (CLOCK_SNAPSHOT_TTL_ABS_MIN_SEC) and the honest accuracy_ns = σ_cons:
        the caller must check snap.accuracy_ns ≤ precision_ns.
        """
        abs_min_ns = int(CLOCK_SNAPSHOT_TTL_ABS_MIN_SEC * 1_000_000_000)
        abs_max_ns = int(CLOCK_SNAPSHOT_TTL_ABS_MAX_SEC * 1_000_000_000)

        recent = list(self._consensus.applied_offsets)[:CLOCK_SNAPSHOT_TTL_RECENT_N]
        if len(recent) < 3:
            return abs_min_ns, int(CLOCK_SNAPSHOT_ACCURACY_DEFAULT_NS)

        sigma_cons = self._noise_sigma_ns(recent) or 0
        if sigma_cons <= 0:
            return abs_max_ns, int(CLOCK_SNAPSHOT_ACCURACY_DEFAULT_NS)

        if sigma_cons >= precision_ns:
            return abs_min_ns, int(sigma_cons)

        round_ns = (self.sync_interval * 1_000_000_000) // SYNC_THREAD_SLOTS
        delta_rate = sigma_cons / max(round_ns, 1)
        if delta_rate <= 0:
            return abs_max_ns, int(sigma_cons)

        budget = precision_ns - sigma_cons
        ttl_ns = int(budget / delta_rate)
        ttl_ns = max(abs_min_ns, min(abs_max_ns, ttl_ns))
        actual = int(sigma_cons + delta_rate * ttl_ns)
        return ttl_ns, actual

# Global instance
time_sync_service = TimeSyncService.get_instance()

#Telemetry output

def _ns_to_utc_str(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)\
                   .strftime('%Y-%m-%d %H:%M:%S.%f')[:-3] + " UTC"

def _ts_to_hms(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)\
                   .strftime('%H:%M:%S.%f')[:-3]

def _fmt_ms(v):
    return f"{v / 1e6:.6f} ms" if v is not None else "N/A"

def _fmt_s(v):
    return f"{v / 1e9:.6f} s" if v is not None else "N/A"

def _fmt_ppm(v):
    return f"{v:+.3f} ppm" if v is not None else "N/A"

def _fmt_consensus_series(records) -> str:
    if not records:
        return "(empty)"

    def _one(r):
        if r.diff_ns is None:
            return "n/a"
        return f"{r.diff_ns / 1e6:+.3f}ms"

    diffs_str = ", ".join(_one(r) for r in records)
    t_newest = _ts_to_hms(records[0].timestamp_ns)
    t_oldest = _ts_to_hms(records[-1].timestamp_ns)
    return f"[{t_newest}  {diffs_str}  {t_oldest}]"

#Generalized telemetry
def build_short_telemetry_content():
    """Short telemetry: only top-level results.
    Without matrices, without per-server, without per-instance, without consensus history.
    """
    try:
        telemetry = time_sync_service.get_sync_telemetry()
    except Exception as e:
        logger.error(f"Time sync telemetry error: {e}")
        return "Time sync not available"

    precise_ns = telemetry.get('precise_ns')
    sys_ns = telemetry.get('system_ns')
    if precise_ns is None or sys_ns is None:
        return "Time sync not available (first sync pending)"

    offset_ns         = telemetry.get('offset_ns')
    slew_error_ns     = telemetry.get('slew_error_ns')
    offset_spread_ns  = telemetry.get('offset_spread_ns')
    ntp_spread_ns     = telemetry.get('ntp_spread_ns')
    diff_threshold_ns = telemetry.get('diff_threshold_ns')
    rate_ppm          = telemetry.get('rate_ppm')
    phase_error_ns    = telemetry.get('phase_error_ns')
    population        = telemetry.get('population') or {}
    gate              = population.get('reproduction_gate') or {}
    history_vote      = population.get('history_vote') or {}
    instances         = telemetry.get('instances') or {}
    slew_errors_ns = telemetry.get('slew_errors_ns') or []
    hv_last = history_vote.get('last_applied_ns')
    second_sync_thread_delay = telemetry.get('second_sync_thread_delay')

    # Split applied / rejected by rejected_offset_ns (as in the full version).
    # diff_ns for both types — deviation of post-gate from prediction, negative sign
    # is not confusing: it is the sign of the phase error, not "bad/good".
    applied_records = [
        r for r in slew_errors_ns
        if r.rejected_offset_ns is None and r.diff_ns is not None
    ]
    rejected_records = [
        r for r in slew_errors_ns
        if r.rejected_offset_ns is not None
    ]

    if slew_error_ns == 0 and offset_spread_ns is not None:
        slew_error = "compensated"
    elif slew_error_ns == 0:
        slew_error = "N/A"
    else:
        slew_error = _fmt_ms(slew_error_ns)

    # Colony
    pop_n     = population.get('population')
    tick      = population.get('tick')
    spread_ns = population.get('spread_ns')
    gate_allowed = population.get('reproduction_allowed')

    med_sp = gate.get('median_spread_ns')
    noise_r = gate.get('noise_ref_ns')
    hist_len = gate.get('history_len', 0)
    hist_min = gate.get('history_min', SPREAD_HISTORY_MIN)

    if noise_r is None:
        n_sigma = sum(1 for i in instances.values() if i.get('sigma_avg_ns'))
        gate_line = f"warming up — need σ_avg on ≥2 instances (have {n_sigma})"
    elif med_sp is None:
        gate_line = (f"warming up — collecting spread history "
                     f"({hist_len}/{hist_min} rounds)")
    else:
        state = 'allowed' if gate_allowed else 'blocked'
        ratio = gate.get('ratio')
        tail = (f" (spread/noise = {ratio:.3f}, need < 1.0)"
                if ratio is not None else "")
        gate_line = f"{state}{tail}"

    # History predictions (history votes)
    hv_len   = history_vote.get('applied_len', 0)
    hv_short = history_vote.get('short_vote_ns')
    if hv_short is not None and hv_last is not None:
        short_str = f"{_fmt_ms(hv_short - hv_last)} (vs last)"
    else:
        short_str = "N/A"
    hv_slope = history_vote.get('drift_slope_ns_per_round')
    slope_ns = hv_slope
    round_sec = second_sync_thread_delay
    slope_ppm = (slope_ns / (round_sec * 1e3)
                 if (round_sec and slope_ns is not None) else None)
    slope_str = (f"{slope_ns:+.0f} ns/round ({slope_ppm:+.3f} ppm)"
                 if slope_ns is not None and slope_ppm is not None else "N/A")

    # Number of "occupied" servers (how many unique favorites in the colony)
    occupied = population.get('occupied') or []

    flags_off = []
    if DISABLE_POST_GATE:  flags_off.append("post_gate")
    if DISABLE_SHORT_VOTE: flags_off.append("short_vote")
    if DISABLE_DRIFT_VOTE: flags_off.append("drift_vote")

    lines = [
        "TIME SYNC — SUMMARY",
        "─" * 50,
        f"Precise time (T.B.O.T time) : {_ns_to_utc_str(precise_ns)}",
        f"System time (OS time)       : {_ns_to_utc_str(sys_ns)}",
        f"Current OS time Δ           : {_fmt_ms(offset_ns)}",
        f"Current T.B.O.T time Δ      : {slew_error}",
        f"Estimated clock rate vs UTC : {_fmt_ppm(rate_ppm)}",
        f"Last phase error (residual) : {_fmt_ms(phase_error_ns)}",
        f"Std. dev. of T.B.O.T time Δ : {_fmt_ms(offset_spread_ns)}",
        f"Filter threshold (accepted) : "
        f"{'±' + _fmt_ms(diff_threshold_ns) if diff_threshold_ns is not None else 'N/A'}",
        f"NTP delay spread       : {_fmt_ms(ntp_spread_ns)}",
        "",
        "COLONY",
        "─" * 50,
        f"Instances alive            : {pop_n}  (tick {tick})",
        f"Occupied servers           : {len(occupied)}",
        f"Offsets spread (stdev)     : {_fmt_ms(spread_ns)}",
        f"Colony reproduction        : {gate_line}",
        f"Predictions (history votes): len={hv_len} "
        f"short={short_str} "
        f"slope={slope_str }",
        f"Consensus Δ applied in last {HISTORY_MAX_LEN} rounds  :{_fmt_consensus_series(applied_records)}",
        f"Consensus Δ rejected in last {HISTORY_MAX_LEN} rounds : {_fmt_consensus_series(rejected_records)}",
    ]
    if flags_off:
        lines.append(f"Disabled filters       : {', '.join(flags_off)}")
    lines.append("")
    lines.append(f"→ click Time again for full telemetry "
                 f"({len(instances)} instances, matrices, history)")
    return "\n".join(lines)

def build_full_telemetry_content():
    try:
        telemetry = time_sync_service.get_sync_telemetry()
    except Exception as e:
        logger.error(f"Time sync telemetry error: {e}")
        return "Time sync not available"

    precise_ns = telemetry.get('precise_ns')
    sys_ns = telemetry.get('system_ns')
    if precise_ns is None or sys_ns is None:
        return "Time sync not available (first sync pending)"

    offset_ns         = telemetry.get('offset_ns')
    slew_error_ns     = telemetry.get('slew_error_ns')
    slew_errors_ns    = telemetry.get('slew_errors_ns') or []
    offset_spread_ns  = telemetry.get('offset_spread_ns')
    ntp_spread_ns     = telemetry.get('ntp_spread_ns')
    diff_threshold_ns = telemetry.get('diff_threshold_ns')
    population        = telemetry.get('population') or {}
    gate = (population.get('reproduction_gate') or {}) if population else {}
    instances         = telemetry.get('instances') or {}
    colony_bias_ns    = telemetry.get('colony_bias_ns') or {}
    colony_noise_ns   = telemetry.get('colony_noise_ns') or {}
    per_server_delay  = telemetry.get('per_server_delay') or {}
    history_vote      = (population.get('history_vote') or {}) if population else {}
    rate_ppm = telemetry.get('rate_ppm')
    phase_error_ns = telemetry.get('phase_error_ns')
    second_sync_thread_delay = telemetry.get('second_sync_thread_delay')
    server_meta       = telemetry.get('server_meta') or {}
    all_consensus_records = slew_errors_ns or []
    applied_history = [r for r in all_consensus_records if r.rejected_offset_ns is None]
    rejected_history = [r for r in all_consensus_records if r.rejected_offset_ns is not None]

    sys_str     = _ns_to_utc_str(sys_ns)
    precise_str = _ns_to_utc_str(precise_ns)

    offset_spread = _fmt_ms(offset_spread_ns)
    ntp_spread    = _fmt_ms(ntp_spread_ns)
    offset_ms     = _fmt_ms(offset_ns)

    if slew_error_ns == 0 and offset_spread_ns is not None:
        slew_error = "compensated"
    elif slew_error_ns == 0:
        slew_error = "N/A"
    else:
        slew_error = _fmt_ms(slew_error_ns)

    diff_threshold_str = (f"±{_fmt_ms(diff_threshold_ns)}"
                          if diff_threshold_ns is not None else "N/A")

    # ---- Render per-server slice from the structured tuple ----
    def fmt_servers(servers):
        if not servers:
            return "—"
        items = sorted(servers, key=lambda x: abs(x[2]))
        return ", ".join(
            f"{mark}{name}:{dev / 1e6:+.3f}ms/{delay / 1e6:.1f}ms"
            for name, _proposed, dev, delay, mark in items
        )

    def fmt_threshold(_thr_ns):
        return f"thr ±{_thr_ns / 1e6:.6f} ms" if _thr_ns is not None else "thr N/A"

    def fmt_record(rec):
        inst_mark = (f"[inst {rec.instance_id}]" if rec.instance_id is not None
                     else "[consensus]")
        if rec.rejected_offset_ns is not None:
            thr_str = (f"±{rec.threshold_ns / 1e6:.3f} ms"
                       if rec.threshold_ns is not None else "N/A")
            return (f"{inst_mark} [{_ts_to_hms(rec.timestamp_ns)}]  "
                    f"REJECTED  Δ {rec.diff_ns / 1e6:+.6f} ms  "
                    f"(thr {thr_str})")
        fav_mark = f" fav={rec.favorite}" if rec.favorite else ""
        if rec.diff_ns is None:
            diff_str = "Δ cold-start"
        else:
            diff_str = f"Δ {rec.diff_ns / 1e6:+.6f} ms"

        # --- post-gate ---
        if rec.rejected_offset_ns is not None and rec.ref_ns is not None:
            clamp_ns = rec.ref_ns - rec.rejected_offset_ns
            gate_tail = (f"  [post-gate: raw={rec.rejected_offset_ns / 1e6:+.6f}ms → "
                         f"{rec.ref_ns / 1e6:+.6f}ms, clamp={clamp_ns / 1e6:+.6f}ms]")
        else:
            gate_tail = ""

        # if this is a consensus record with a matrix — render the matrix
        if rec.instance_id is None and rec.matrix:
            size = len(rec.matrix)
            # consensus_offset = mean(proposed) over all cells of the matrix
            # (by construction in _build_consensus_matrix). Show per-cell
            # correction relative to this center — compact, in ms, with sign.
            all_proposed = [p for row in rec.matrix for _, p, _, _ in row]
            if all_proposed:
                center = statistics.mean(all_proposed)
            else:
                center = 0.0
            rows = [
                f"{inst_mark} [{_ts_to_hms(rec.timestamp_ns)}] "
                f"{diff_str} ({fmt_threshold(rec.threshold_ns)})  "
                f"[Size={size}]{gate_tail}"
            ]
            for row_idx, row in enumerate(rec.matrix):
                cells = ", ".join(
                    f"{name}:{(proposed - center) / 1e6:+.3f}ms/{delay / 1e6:.1f}ms"
                    for name, proposed, delay, t_mono in row
                )
                rows.append(f"          r{row_idx}: {cells}")

            return "\n".join(rows)

        # Fallback to the old format
        return (f"{inst_mark} [{_ts_to_hms(rec.timestamp_ns)}] "
                f"{diff_str} ({fmt_threshold(rec.threshold_ns)}){fav_mark} "
                f"{fmt_servers(rec.servers)}{gate_tail}")

    def render_history(records, limit=HISTORY_MAX_LEN):
        if not records:
            return "      (empty)"
        return "\n".join("      " + fmt_record(rec) for rec in records[:limit])

    def render_dominant_analysis(_inst, _current_tick):
        """Unchanged — left as in the original."""
        _hist = _inst.get('own_history') or []
        cur_fav = _inst.get('favorite')
        _dominant_min_samples = _inst.get('dominant_min_samples')
        _sigma_avg = _inst.get('sigma_avg_ns')
        _thr_ns = _inst.get('threshold_ns')
        _lock_until = _inst.get('armed_lock_until_tick', -10 ** 9)

        _lines = []

        if _current_tick < _lock_until:
            _lines.append(
                f"      armed-lock active: {_lock_until - _current_tick} ticks left "
                f"(until tick {_lock_until})"
            )

        if _thr_ns is None or _thr_ns <= 0:
            _lines.append("      → not armed: threshold not established yet")
            return "\n".join(_lines)
        if _sigma_avg is None or _sigma_avg <= 0:
            _warmup_exc = _inst.get('warmup_excluded') or []
            _warmup_start = _inst.get('warmup_started_tick')
            last = _hist[0] if _hist else None
            avail = [_n for _n, *_ in (last.servers or ())] if last else []
            avail = [_n for _n in avail if _n not in _warmup_exc]

            grace_left = None
            if _warmup_start is not None:
                grace_left = (_warmup_start + SIGMA_WARMUP_TIMEOUT_MULT * SIGMA_WARMUP_RECORDS
                              - _current_tick)

            counts: dict = {}
            for rec in _hist:
                if rec.is_cold_start or not rec.servers:
                    continue
                for name, _p, _d, _dl, mark in rec.servers:
                    if mark == '✓':
                        counts[name] = counts.get(name, 0) + 1

            _lines.append(
                f"      → warming up: σ_avg not captured "
                f"(need {SIGMA_WARMUP_RECORDS} ✓/srv, "
                f"grace left: {grace_left if grace_left is None else max(grace_left, 0)} ticks)"
            )
            if len(avail) < WARMUP_MIN_SURVIVORS:
                _lines.append(
                    f"      rule 1: only {len(avail)} available "
                    f"(< {WARMUP_MIN_SURVIVORS}), stay in warmup"
                )
            for name in avail:
                c = counts.get(name, 0)
                mark = "" if c >= SIGMA_WARMUP_RECORDS else f" (need {SIGMA_WARMUP_RECORDS - c} more)"
                _lines.append(f"      {name}: {c}/{SIGMA_WARMUP_RECORDS}{mark}")
            if _warmup_exc:
                _lines.append(f"      permanently excluded: {', '.join(_warmup_exc)}")
            return "\n".join(_lines)

        if _dominant_min_samples is None:
            _lines.append(f"      → not armed: M undefined (N<{DOMINANT_MIN_SERVERS} or no history)")
            return "\n".join(_lines)

        last = _hist[0] if _hist else None
        srv_num = len(last.servers) if (last and last.servers) else 0
        _delta = _sigma_avg / _thr_ns
        _lines.append(
            f"      M={_dominant_min_samples}  σ_avg={_sigma_avg / 1e6:.3f} ms  "
            f"thr=±{_thr_ns / 1e6:.3f} ms  Δ={_delta:.3f}  N={srv_num}"
        )

        per_srv: dict = {}
        for rec in _hist:
            if rec.is_cold_start or not rec.servers:
                continue
            for name, _proposed, dev, _delay, mark in rec.servers:
                if mark == '✓':
                    per_srv.setdefault(name, []).append(dev)

        if not per_srv:
            _lines.append("      (no ✓ server data yet)")
            return "\n".join(_lines)

        groups = []
        for name, devs in per_srv.items():
            sd = statistics.stdev(devs) if len(devs) >= 2 else None
            groups.append((name, len(devs), sd))
        groups.sort(key=lambda g: (g[0] != cur_fav, -g[1]))

        for name, cnt, sd in groups:
            sd_str = f"σ={sd / 1e6:.3f} ms" if sd is not None else "σ=N/A"
            mark = "  ← current" if name == cur_fav else ""
            if name == cur_fav:
                progress = (f"✓ {cnt}/{_dominant_min_samples}" if cnt >= _dominant_min_samples
                            else f"{cnt}/{_dominant_min_samples} (need {_dominant_min_samples - cnt} more)")
            else:
                progress = f"{cnt}"
            _lines.append(f"      {name}: {progress}, {sd_str}{mark}")

        if cur_fav is None or cur_fav not in per_srv:
            _lines.append("      → not armed: current favorite absent in ✓ history")
            return "\n".join(_lines)

        on_favorite = per_srv[cur_fav]
        others = [d for _n, dl in per_srv.items() if _n != cur_fav for d in dl]

        if len(on_favorite) < _dominant_min_samples:
            _lines.append(f"      → not armed: need {_dominant_min_samples - len(on_favorite)} more on favorite")
        elif len(others) < _dominant_min_samples:
            _lines.append(f"      → not armed: only {len(others)}/{_dominant_min_samples} on others")
        elif len(on_favorite) < DOMINANT_MIN_DEVS or len(others) < DOMINANT_MIN_DEVS:
            _lines.append(
                f"      → not armed: insufficient data for stdev "
                f"(need ≥{DOMINANT_MIN_DEVS} on favorite and others)"
            )
        else:
            sx = statistics.stdev(on_favorite)
            so = statistics.stdev(others)
            if so == 0:
                _lines.append("      → not armed: σ_others=0")
            else:
                _ratio = sx / so
                verdict = "ARMED" if _ratio < DOMINANT_STDEV_RATIO else "not dominant"
                _lines.append(
                    f"      → {verdict}: σ_X/σ_others={_ratio:.3f} "
                    f"(need <{DOMINANT_STDEV_RATIO})"
                )
        return "\n".join(_lines)

    all_refs = [i.get('reference_offset_ns') for i in instances.values()
                if i.get('reference_offset_ns') is not None]
    # all_refs — per-instance own_reference_offset, each = delay-weighted mean
    # proposed over accepted servers of this instance. Median over instances (not over
    # servers!) — robust reference of the colony: an individual outlying instance will not
    # shift it, unlike mean.
    median_ref = int(statistics.median(all_refs)) if all_refs else None

    #consensus_history_str = render_history(slew_errors_ns)
    applied_str = render_history(applied_history, limit=HISTORY_MAX_LEN)
    rejected_str = render_history(rejected_history, limit=HISTORY_MAX_LEN)

    # ---- Bias ----
    if not colony_bias_ns:
        colony_bias_str = "      (empty — history not full yet)"
    else:
        lines = []
        for srv, b_ns in sorted(colony_bias_ns.items(), key=lambda kv: abs(kv[1])):
            lines.append(f"      {srv:<30s} : {b_ns / 1e6:+9.6f} ms")
        colony_bias_str = "\n".join(lines)

    if colony_noise_ns:
        lines = []
        for srv, s_ns in sorted(colony_noise_ns.items(), key=lambda kv: kv[1]):
            lines.append(f"      {srv:<30s} : {s_ns / 1e6:7.3f} ms")
        colony_noise_str = "\n".join(lines)
    else:
        colony_noise_str = "      (empty)"

    # ---- Per-server min delay ----
    if per_server_delay:
        lines = []
        for srv, st in sorted(per_server_delay.items(),
                              key=lambda kv: (kv[1].get('jitter_ns') or 0)):
            mean = st.get('mean_ns')
            jit = st.get('jitter_ns') or 0
            n = st.get('n', 0)
            med_str = f"{mean / 1e6:7.3f}" if mean is not None else "    N/A"
            lines.append(
                f"      {srv:<30s} : med {med_str} ms, "
                f"jitter {jit / 1e6:7.3f} ms (n={n})"
            )
        per_server_delay_str = "\n".join(lines)
    else:
        per_server_delay_str = "      (empty — no successful rounds yet)"

    # ---- NEW: NTP server metadata from packet headers ----
    if server_meta:
        lines = [f"      {'name':<30s} | {'str':>3s} {'mode':>4s} {'prec':>4s} "
                 f"{'root_dly':>10s} {'root_disp':>10s}  ref_id          age_s"]
        # Sort by stratum, then by name
        for srv, m in sorted(server_meta.items(),
                              key=lambda kv: (kv[1].get('stratum') or 999, kv[0])):
            stratum = m.get('stratum', '?')
            mode    = m.get('mode', '?')
            prec    = m.get('precision', '?')
            rd_ns   = m.get('root_delay_ns')
            rp_ns   = m.get('root_disp_ns')
            ref_id  = (m.get('ref_id') or '—')[:15]
            age_s   = m.get('age_sec')

            rd_str = f"{rd_ns / 1e6:>8.3f}ms" if rd_ns is not None else f"{'N/A':>10s}"
            rp_str = f"{rp_ns / 1e6:>8.3f}ms" if rp_ns is not None else f"{'N/A':>10s}"
            age_str = f"{age_s:>6.1f}" if age_s is not None else f"{'N/A':>6s}"

            lines.append(
                f"      {srv:<30s} | {stratum:>3} {mode:>4} {prec:>4} "
                f"{rd_str} {rp_str}  {ref_id:<15s} {age_str}"
            )
        server_meta_str = "\n".join(lines)
    else:
        server_meta_str = "      (empty — no NTP packets parsed yet)"

    # ---- Predictions (history votes) ----
    hv_len = history_vote.get('applied_len', 0)
    hv_short = history_vote.get('short_vote_ns')
    hv_drift = history_vote.get('drift_prediction_ns')
    hv_slope = history_vote.get('drift_slope_ns_per_round')
    hv_last = history_vote.get('last_applied_ns')

    if hv_short is None and hv_drift is None:
        history_vote_str = (
            f"      (history collecting: {hv_len}/{HISTORY_VOTE_MIN_SAMPLES} "
            f"for short vote, {DRIFT_MIN_SAMPLES} for drift)"
        )
    else:
        lines = [f"      applied history length      : {hv_len}"]
        if hv_short is not None:
            delta = (hv_short - hv_last) / 1e6 if hv_last is not None else None
            tail = (f"  (vs last consensus: {delta:+.3f} ms)"
                    if delta is not None else "")
            lines.append(
                f"      short vote (median {HISTORY_VOTE_SHORT_WINDOW:>3}) : "
                f"{hv_short / 1e6:+.6f} ms{tail}"
            )
        else:
            lines.append(
                f"      short vote                  : N/A "
                f"({hv_len}/{HISTORY_VOTE_MIN_SAMPLES})"
            )

        if hv_drift is not None and hv_slope is not None:
            slope_us = hv_slope / 1000.0
            ppm = (hv_slope / (second_sync_thread_delay * 1000.0)
                   if second_sync_thread_delay else None)
            vs_short = ((hv_drift - hv_short) / 1e6
                        if hv_short is not None else None)
            tail = (f"  (vs short vote: {vs_short:+.3f} ms)"
                    if vs_short is not None else "")
            lines.append(
                f"      drift prediction (next step) : "
                f"{hv_drift / 1e6:+.6f} ms{tail}"
            )
            ppm_tail = f"{ppm:+.3f} ppm" if ppm is not None else "ppm N/A"
            lines.append(
                f"      drift slope                 : "
                f"{slope_us:+.3f} μs/round  ({ppm_tail})"
            )
        else:
            lines.append(f"      drift prediction            : N/A "
                         f"({hv_len}/{DRIFT_MIN_SAMPLES})")
        history_vote_str = "\n".join(lines)

    # ---- Colony summary ----
    if not population:
        colony_str = "N/A"
    else:
        spread_ns = population.get('spread_ns')
        favorites = population.get('favorites') or []
        occupied = population.get('occupied') or []

        gate_allowed = population.get('reproduction_allowed')
        med_sp = gate.get('median_spread_ns')
        noise_r = gate.get('noise_ref_ns')
        hist_len = gate.get('history_len', 0)
        hist_min = gate.get('history_min', SPREAD_HISTORY_MIN)
        thr_ns = gate.get('threshold_ns')
        ratio = gate.get('ratio')

        if noise_r is None:
            n_sigma = sum(1 for i in instances.values() if i.get('sigma_avg_ns'))
            gate_str = (f"reproduction gate : warming up — "
                        f"need σ_avg on ≥2 instances (have {n_sigma})")
        elif med_sp is None:
            gate_str = (f"reproduction gate : warming up — "
                        f"collecting spread history ({hist_len}/{hist_min} rounds)")
        else:
            gate_str = (
                f"reproduction gate                    : "
                f"{'allowed' if gate_allowed else 'blocked'}\n"
                f"  median spread (last {hist_len:>2})   : {_fmt_ms(med_sp)}\n"
                f"  noise ref (median σ)                 : {_fmt_ms(noise_r)}\n"
                f"  threshold ({REPRODUCTION_SPREAD_MULT}·σ_ref)               : {_fmt_ms(thr_ns)}\n"
                f"  ratio                                : "
                f"{ratio:.3f}  (need < 1.0)"
            )

        matrix_recent = [rec for rec in slew_errors_ns
                         if rec.instance_id is None and rec.matrix is not None]
        if matrix_recent:
            size_last = len(matrix_recent[0].matrix)
            size_hist = [len(rec.matrix) for rec in matrix_recent[:10]]
            mean_last = matrix_recent[0].matrix_meta[1][0]  # mean period between rows
            jitter_last = matrix_recent[0].matrix_meta[1][1]  # absolute spread (stdev)

            # Mean period is taken from matrix_meta, not from (max-min)/(size-1)
            matrix_time = mean_last

            def _cv_perc(rec):
                """CV = 100 * stdev / mean. stdev in matrix_meta is already rounded — ok for telemetry."""
                mean, sd = rec.matrix_meta[1][0], rec.matrix_meta[1][1]
                return (100.0 * sd / mean) if mean else 0.0

            matrix_times_covar_perc = [
                f"±{_cv_perc(rec):.1f}" for rec in matrix_recent[:10]
            ]

            matrix_str = (
                f'  matrix (last consensus)   : size={size_last} '
                f'(cells={size_last * size_last})\n'
                f'time interval between matrix lines: {_fmt_s(matrix_time)} '
                f'±{_fmt_ms(jitter_last)}\n'
                f'matrix rows period coefficient of variation '
                f'history (last {len(size_hist)}): {matrix_times_covar_perc} %\n'
                f'matrix size history (last {len(size_hist)}): {size_hist}\n'
            )
        else:
            matrix_str = "  matrix (last consensus)   : —"

        colony_str = (
            f"instances alive           : {population.get('population')}\n"
            f"  current tick              : {population.get('tick')}\n"
            f"  {gate_str}\n"
            f"{matrix_str}\n"
            f"  offsets spread (stdev)     : "
            f"{_fmt_ms(spread_ns) if spread_ns is not None else 'N/A'}\n"
            f"  favorites (per instance)  : "
            f"[{', '.join(str(f) if f else '—' for f in favorites) or '—'}]\n"
            f"  occupied servers          : "
            f"[{', '.join(occupied) or '—'}]"
        )

    # ---- Block per each instance ----
    instance_blocks = []
    current_tick = population.get('tick', 0)
    for pos, inst_id in enumerate(sorted(instances.keys())):
        inst = instances[inst_id]
        ref_ns     = inst.get('reference_offset_ns')
        thr_ns     = inst.get('threshold_ns')
        repro_done = inst.get('reproduced_count')
        repro_max  = inst.get('max_offspring')
        rate       = inst.get('accept_rate')
        born_tick  = inst.get('born_tick')
        banned     = inst.get('banned_servers') or []
        hist       = inst.get('own_history') or []
        hist_rej   = inst.get('own_history_rejected') or []
        warmup_exc = inst.get('warmup_excluded') or []
        warmup_start = inst.get('warmup_started_tick')

        if ref_ns is None:
            ref_str = "N/A (cold start)"
        elif median_ref is None:
            ref_str = f"{ref_ns / 1e9:.3f} s (absolute)"
        else:
            ref_str = f"Δ {(ref_ns - median_ref) / 1e6:+.6f} ms vs median(instance refs)"

        sigma_avg = inst.get('sigma_avg_ns')
        dominant_min_samples = inst.get('dominant_min_samples')
        lock_until = inst.get('armed_lock_until_tick', -10 ** 9)
        lock_str = (f"until tick {lock_until} ({lock_until - current_tick} left)"
                    if current_tick < lock_until else "off")

        sel_mode = inst.get('last_selection_mode') or 'N/A'
        sel_cand = inst.get('last_best_candidate') or '—'
        fav_score = inst.get('last_favorite_score')
        cand_score = inst.get('last_candidate_score')
        if fav_score is not None and cand_score is not None:
            score_tail = (f"  (fav score={fav_score:.3f}, "
                          f"cand score={cand_score:.3f})")
        else:
            score_tail = ""
        sel_line = (f"  favorite selection        : {sel_mode}, "
                    f"best candidate={sel_cand}{score_tail}\n")

        #attempt index (instance k takes attempt k % QUERY_ATTEMPTS)
        attempt_idx = pos
        attempt_line = (
            f"  attempt index             : {attempt_idx} "
            f"(of {len(instances)}, per-server attempt)\n"
        )

        header = (
            f"Instance {inst_id}\n"
            f"{attempt_line}"
            f"  favorite server           : {inst.get('favorite') or '—'}\n"
            f"{sel_line}"
            f"  reference offset          : {ref_str}\n"
            f"                              (delay-weighted mean over accepted servers)\n"
            f"  filter threshold          : "
            f"{'±' + _fmt_ms(thr_ns) if thr_ns is not None else 'N/A'}\n"
            f"  σ_avg (captured)          : "
            f"{_fmt_ms(sigma_avg) if sigma_avg is not None else 'N/A (warming up)'}\n"
            f"  dominant minimum samples                : {dominant_min_samples if dominant_min_samples is not None else 'N/A'}\n"
            f"  armed lock                : {lock_str}\n"
            f"  offspring produced (cap)  : {repro_done} / {repro_max}"
            f"{' (cap reached)' if repro_done >= repro_max else ''}\n"
            f"  born at tick              : {born_tick}  "
            f"(age: {current_tick - born_tick if born_tick is not None else '?'} ticks)\n"
            f"  accept rate (last {PER_SERVER_HISTORY})     : "
            f"{f'{rate * 100:.0f}%' if rate is not None else 'N/A'}\n"
            f"  consecutive low windows   : {inst.get('low_accept_windows')}\n"
            f"  deathbed trigger used     : "
            f"{'yes' if inst.get('deathbed_used') else 'no'}\n"
            f"  banned (occupied by others): [{', '.join(banned) or '—'}]\n"
            f"  warmup excluded           : [{', '.join(warmup_exc) or '—'}]\n"
            f"  warmup started at tick    : "
            f"{warmup_start}  "
            f"(age: {current_tick - warmup_start if warmup_start is not None else '?'} ticks, "
            f"grace: {SIGMA_WARMUP_TIMEOUT_MULT * SIGMA_WARMUP_RECORDS} ticks)\n"
            f"  Dominant analysis:\n{render_dominant_analysis(inst, current_tick)}"
        )

        accepted_block = f"  Accepted rounds ({len(hist)}):\n{render_history(hist)}"
        rejected_block = f"  Rejected-all rounds ({len(hist_rej)}):\n{render_history(hist_rej)}"

        instance_blocks.append("\n".join([header, accepted_block, rejected_block]))

    instances_str = "\n\n".join(instance_blocks) if instance_blocks else "(no instances)"

    # ---- line about gate flags (only if any is disabled) ----
    flags_off = []
    if DISABLE_POST_GATE:  flags_off.append("post_gate")
    if DISABLE_SHORT_VOTE: flags_off.append("short_vote")
    if DISABLE_DRIFT_VOTE: flags_off.append("drift_vote")
    flags_str = ""
    if flags_off:
        flags_str = f"DISABLED in consensus: {', '.join(flags_off)}\n"

    return (
        f"Spread of per-server min delay (mixed servers, per-round min-of-5): {ntp_spread}\n"
        f"{flags_str}"
        f"\nConsensus predictions (history vote & drift):\n"
        f"{history_vote_str}\n"
        f"Precise time (T.B.O.T time): {precise_str}\n"
        f"System time (OS time): {sys_str}\n"
        f"Current OS time offset, precise time - system time = {offset_ms}\n"
        f"Current T.B.O.T time offset, precise time - NTP time = {slew_error}\n"
        f"Estimated clock rate vs UTC: "
        f"{f'{rate_ppm:+.3f} ppm' if rate_ppm is not None else 'N/A'}\n"
        f"Last phase error (residual): "
        f"{_fmt_ms(phase_error_ns) if phase_error_ns is not None else 'N/A'}\n"
        f"Standard deviation of T.B.O.T time offsets: {offset_spread}\n"
        f"Mean filter threshold (accepted instances): {diff_threshold_str}\n"
        f"\nColony bias (accumulated, applied to raw proposed):\n"
        f"{colony_bias_str}\n"
        f"\nColony noise estimate (robust: 1.4826·MAD(first diffs)/√2, active only):\n"
        f"{colony_noise_str}\n"
        f"\nPer-server min delay (last 10 rounds each, mixed servers):\n"
        f"{per_server_delay_str}\n"
        f"\nNTP server metadata (from packet headers, all fields):\n"
        f"{server_meta_str}\n"
        f"\nColony state:\n"
        f"  {colony_str}\n"
        f"\nConsensus history (applied, post-gate):\n"
        f"{applied_str}\n"
        f"\nConsensus history (rejected by post-gate):\n"
        f"{rejected_str}\n"
        f"\nPer-instance telemetry:\n"
        f"{instances_str}"
    )