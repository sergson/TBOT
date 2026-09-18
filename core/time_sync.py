# core/time_sync.py
# Copyright (c) 2026 sergson (https://github.com)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.
#
# Dependencies: ntplib (pip install ntplib)
#
# Architecture: a colony of autonomous filter-instances combined by
# consensus. Slewing, get_utc_ns, watchdog — unchanged; only the source
# (best_mono, best_utc) for _apply_new_sync_locked changes.
#
# Telemetry: each instance maintains two queues of SlewRecord —
# own_history (accepted rounds) and own_history_rejected (all available
# servers rejected by the filter). Each record is tagged with instance_id,
# which allows analyzing the trajectory of an individual instance.

# Windows classifies a background application as "unimportant",
# moves it to E-cores and limits CPU share.
# _keep_awake_worker disables this via SetProcessInformation(ProcessPowerThrottling)
# and raises priority to ABOVE_NORMAL.
# If you see such anomalies:
# NTP rounds take 30/60/120/240 seconds instead of 4
# in the log — check: windows version (10 1709+ is required)
# and whether enterprise policy is blocking it.

import os
import sys
import math
import time
import threading
import ntplib
from typing import Optional, Tuple, Dict, List, Any
import statistics
from collections import deque, defaultdict, OrderedDict
from .logger import perf_logger
import concurrent.futures
from typing import NamedTuple
from dataclasses import dataclass, field
import socket, ipaddress

logger = perf_logger.get_logger('time_sync', 'time')
# Deferred logging: inside critical sections we do not call the logger,
# only accumulate messages in a list. Emission — strictly outside locks.
# This eliminates both types of deadlocks: self-deadlock (Lock vs RLock) and
# lock-ordering deadlock between the module lock and the logger lock.
def _emit_deferred_logs(logs: List[Tuple[str, str]]) -> None:
    for level, msg in logs:
        getattr(logger, level)(msg)

# Windows Modern Standby: by default we ask the system not to go into
# idle-standby while the service is alive. Disabled via env
# TIME_SYNC_KEEP_AWAKE=0 (e.g., on a laptop where battery matters).

KEEP_AWAKE_ENABLED = os.environ.get('TIME_SYNC_KEEP_AWAKE', '1') == '1'
KEEP_AWAKE_REFRESH_SEC = 30   # frequency of re-setting the request

DEFAULT_INITIAL_INTERVAL_SEC = 60   # Main interval between cycles (ticks) by default

# Offset between the NTP epoch (1900-01-01) and Unix epoch (1970-01-01), seconds.
NTP_EPOCH_OFFSET_SEC = 2208988800

# PI controller for the rate-model of offset (PLL).
# tau_phase ~ 1/KP rounds to phase convergence, tau_rate ~ 1/KI.
PLL_KP         = 0.10        # fraction of phase error contributed to offset per round
PLL_KI         = 0.01        # fraction of phase error contributed to rate per round
PLL_RATE_LIMIT = 100e-6      # ±100 ppm — upper bound of drift estimate

# TTL of the clock-model snapshot for external consumers.
# PLL guarantees estimate accuracy on the horizon of order sync_interval;
# 2× the interval — a compromise between request traffic and staleness risk.
# With sync_interval=60 → TTL=120 s.
CLOCK_SNAPSHOT_TTL_SEC = 2.0 * DEFAULT_INITIAL_INTERVAL_SEC

# Dynamic TTL of the clock-model snapshot.
# TTL_BASE — base value (2× sync interval).
# quality ∈ [FLOOR, CEIL] reflects the current consensus quality:
#   σ_recent << σ_ref  →  quality → CEIL  (stable, trust longer)
#   σ_recent ≈ σ_ref   →  quality = 1.0   (base TTL)
#   σ_recent >> σ_ref  →  quality → FLOOR (noisy, trust less)
CLOCK_SNAPSHOT_TTL_BASE_SEC     = 2.0    # multiplied by sync_interval
CLOCK_SNAPSHOT_TTL_QUALITY_FLOOR = 0.25
CLOCK_SNAPSHOT_TTL_QUALITY_CEIL  = 2.0
CLOCK_SNAPSHOT_TTL_ABS_MIN_SEC   = 30.0   # cannot go lower — request spam
CLOCK_SNAPSHOT_TTL_ABS_MAX_SEC   = 600.0  # cannot go higher — trust in stale data
CLOCK_SNAPSHOT_TTL_RECENT_N      = 15     # σ_recent window (last rounds)
CLOCK_SNAPSHOT_ACCURACY_DEFAULT_NS = 5_000_000  # 5 ms, allowed drift of snapshot frequency with default ttl
CLOCK_SNAPSHOT_PRECISION_CACHE_MAX = 16 # Cache length limit

# --- Protection of PLL from "stuck" updates and phase jumps ---
# If less than this interval elapsed between two PLL updates —
# skip the update. Needed when both sync threads were suspended
# (screen sleep, Modern Standby) and woken simultaneously: their rounds
# finish in the same millisecond, dt in the denominator of the I-correction
# becomes ~0, rate instantly saturates to ±PLL_RATE_LIMIT.
# Default value = half the interval between sync threads,
# computed in __init__ as self._min_pll_update_interval_ns.

# Phase jump threshold. If |phase_error| is greater — this is not drift,
# but an external event: sleep/resume, system-time step by w32time,
# NTP jump on the server side. Catching up slowly is not allowed — it would
# take tens of minutes. Reset anchor to the new value, zero rate.
PHASE_JUMP_THRESHOLD_NS = 100_000_000   # > 100 ms — reset
PHASE_MEDIUM_JUMP_NS = 20_000_000       # > 20 ms — boosted KP
PLL_KP_MEDIUM = 0.40                    # 40% per round instead of 10%

# --- NTP polling parameters ---
QUERY_ATTEMPTS = 5                # attempts per server per round
QUERY_ATTEMPT_SPACING_SEC = 1.0   # spacing of attempts within a round
PER_SERVER_HISTORY = 10            # server history queue length
QUERY_ATTEMPT_SPACING_NS = int(QUERY_ATTEMPT_SPACING_SEC * 1_000_000_000)
# Max span = (ATTEMPTS−1)·SPACING = 4 s. With typical monotonic drift
# ~50 ppm this is ≈200 μs — comparable to THRESHOLD_MIN_NS.
# Going further is not allowed without drift compensation.

# --- Colony (Consensus) parameters ---
ACCEPT_WINDOW_SIZE = PER_SERVER_HISTORY # length of instance's accept-history window, ticks
DEATH_LOW_WINDOWS = 5              # how many consecutive low accept windows → death
DEATH_ACCEPT_THRESHOLD = 0.05      # accepted fraction in window below which the window is "low"
REPRODUCTION_LAG_L = DEATH_LOW_WINDOWS # minimum ticks between two births
COHERENCE_THRESHOLD_NS = 5_000_000 # |median − pred| in absence of gate, ns
HISTORY_MAX_LEN = 100               # length of own_history / own_history_rejected

# Colony bias integrator (permanent server shift):
COLONY_BIAS_GAIN = 0.05            # integrator speed per round
COLONY_BIAS_MAX_NS = 50_000_000    # compensation limit ±50 ms
COLONY_BIAS_DECAY = 0.99           # decay bias of inactive servers per round
COLONY_BIAS_DECAY_FLOOR = 10_000   # below 10 μs — bias is removed
COLONY_BIAS_MIN_HISTORY = 15       # minimum rounds per server for bias estimate
COLONY_BIAS_HISTORY_DIV = 6        # alternative: HISTORY_MAX_LEN // 6
K_THRESHOLD = 1.0                  # soft-threshold: |delta| > K·σ_srv
THRESHOLD_MIN_NS = 100_000         # absolute floor of the filter threshold, 0.1 ms

# Rate limit of filter narrowing: the threshold per round cannot fall
# by more than this factor. 0.95 = 5% per round. Slower = reference has time
# to adjust, but accuracy arrives later.
THRESHOLD_SHRINK_FLOOR = 0.95

# Spread-gate of reproduction.
SPREAD_HISTORY_LEN = ACCEPT_WINDOW_SIZE            # length of stdev(offsets) queue
SPREAD_HISTORY_MIN = REPRODUCTION_LAG_L             # minimum rounds before the reproduction gate activates
REPRODUCTION_SPREAD_MULT = 2.0     # median(spreads) < MULT · median(σ_avg)

# Hysteresis of favorite selection by score (d_norm + s_norm).
FAVORITE_HYST = 0.15               # switch only if score_new < score_cur·(1−H)

# Consensus history vote.
HISTORY_VOTE_SHORT_WINDOW = 15     # short-median window, rounds
DRIFT_WINDOW = 300                 # linear-regression window (was 100)
HISTORY_VOTE_MIN_SAMPLES = SPREAD_HISTORY_MIN       # short_vote is inactive until this
DRIFT_MIN_SAMPLES = 30             # drift_pred is inactive until this

# Pre/post gate.
PRE_GATE_K = 3.0                   # |voice − pred| > K·noise_ref → discard
POST_GATE_K = 2.0                  # |median − pred| > K·noise_ref → clamp

# --- Dominant parameters ---
ALPHA_SIGNIFICANCE = 0.1
DOMINANT_MIN_HISTORY = ACCEPT_WINDOW_SIZE          # M_min — lower bound of observations per server
SIGMA_WARMUP_RECORDS = HISTORY_VOTE_MIN_SAMPLES           # ✓ records per server needed for σ_avg
SIGMA_WARMUP_TIMEOUT_MULT = 3      # warmup timeout: 3×SIGMA_WARMUP_RECORDS
DOMINANT_STDEV_RATIO = 0.7         # σ_X < 0.7·σ_others — dominant condition
DOMINANT_MIN_SERVERS = 2           # minimum required number of servers
DOMINANT_MIN_DEVS = 2              # minimum number of suitable servers

# Rule 2b of warmup: if at least this many servers collected
# SIGMA_WARMUP_RECORDS ✓ — capture σ_avg over them, without waiting for
# the rest. Speeds up warmup exit, reduces the number of instances that died
# with reproduced=0.
SIGMA_WARMUP_MIN_SURVIVORS = 2
WARMUP_MIN_SURVIVORS = 2 # minimum available servers — do not exit warmup

# Sanity threshold of σ_avg. A server with such σ physically cannot be a source of
# precise time — bimodal LAN or a broken channel. Capture of σ_avg is not
# performed, the instance lives without trusted status and dies through the death spiral.
SIGMA_AVG_SANITY_MAX_NS = 10_000_000   # 10 ms

# --- Divine birth parameters ---
MIN_POPULATION = 3                 # below this — the colony degenerates
DIVINE_QUEUE_MAX = 3               # maximum concurrent lineage roots
DIVINE_ACCEPT_RATE_THRESHOLD = 0.1 # accept_rate < this → stuck

# --- max_population parameters ---
MAX_POPULATION_RATIO = 0.7         # fraction of server count when > MIN_SERVERS
MAX_POPULATION_MIN_SERVERS = 5     # below — max_population = len(servers)

# --- NTP polling, DNS parameters ---
NTP_QUERY_TIMEOUT_SEC = 2          # timeout of a single NTP request
NTP_QUERY_TIMEOUT_SAFE_SEC = NTP_QUERY_TIMEOUT_SEC     # safety timeout of NTP requests
NTP_RESOLVING_TIMEOUT_NS = 3600 * 1000_000_000     # DNS resolution timeout for NTP servers in nanoseconds
DNS_QUERY_TIMEOUT_SEC = 5 # timeout of a single DNS request

# --- Watchdog and thread shutdown parameters ---
WATCHDOG_INTERVAL_SEC = 30         # thread liveness check frequency, 5 for testing, 30 normal
WATCHDOG_STOP_JOIN_SEC = 3.0       # watchdog join timeout on stop
SYNC_THREAD_STOP_JOIN_SEC = 15.0   # overall timeout for waiting threads on stop
PRE_START_JOIN_SEC = 5.0           # join timeout of "leftover" threads on start
SYNC_THREAD_SLOTS = 2              # number of sync thread slots

class SlewRecord(NamedTuple):
    """
    Record of a synchronization round in history.

    Attributes:
        timestamp_ns   — precise UTC time of record insertion (time.time_ns()), ns
        diff_ns        — for accepted rounds: reference - new target offset, ns;
                         for cold start: None;
                         for rejected: the deviation closest to zero among
                         rejected (proposed - reference), ns;
                         for consensus: current_slew_offset - new_target_offset, ns
        threshold_ns   — filter threshold applied in the round (None — filter was off)
        instance_id    — instance identifier; None — consensus-level record
        favorite       — name of the favorite server in this round; None for rejected records
                         and for consensus records
        servers        — tuple of per-server slices of the round:
                         ((name, proposed_ns, dev_ns, delay_ns, mark), ...)
                         proposed_ns = t2_utc - mid_mono
                         dev_ns      = proposed_ns - ref_ns
                         mark        — '✓' accepted, '×' rejected (instance);
                                       '✓' favorite, '?' available, '×' unavailable (consensus)
        ref_ns         — reference relative to which dev_ns was computed:
                         instance: own_reference_offset (or new_offset for cold start);
                         consensus: median_offset of the round
        is_cold_start  — True for the cold-start record
    """
    timestamp_ns: int
    diff_ns: Optional[int]
    threshold_ns: Optional[int]
    instance_id: Optional[int] = None
    favorite: Optional[str] = None
    servers: Optional[tuple] = None
    ref_ns: Optional[int] = None
    is_cold_start: bool = False

class ClockSnapshot(NamedTuple):
    """Consistent snapshot of the clock model for external consumers.

    All time fields — in nanoseconds. rate — dimensionless (1 ppm = 1e-6).

    Attributes:
        anchor_mono_ns   — reference point (local monotonic_ns).
        anchor_offset_ns — UTC anchor: utc = mono + offset at point anchor.
        rate             — dimensionless drift estimate, 1 ppm = 1e-6.
        ttl_ns           — validity window of the snapshot, ns.
        accuracy_ns      — declared upper bound |utc_true − utc_restored|
                           inside window [anchor_mono, anchor_mono + ttl_ns].

    UTC restoration formula:
        utc = now_mono + anchor_offset_ns + rate * (now_mono - anchor_mono_ns)

    Accuracy guarantee:
        For any now_mono within ttl_ns:
            |utc_restored − utc_true| ≤ accuracy_ns (provided
            that consensus has not degraded more than at the moment of the snapshot).
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
    Filter-instance. Works autonomously, reads the shared sample pool,
    maintains its own histories and reference_offset. Does not perform slewing.
    """
    id: int
    lineage_id: int  # id of the root of the lineage (id of the root instance)
    favorite: Optional[str] = None
    banned_servers: set = field(default_factory=set)

    # Accepted rounds (SlewRecord with instance_id == id).
    own_history: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAX_LEN))
    # Rounds where all available servers were rejected by the instance's filter.
    own_history_rejected: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAX_LEN))

    own_reference_offset: Optional[int] = None
    own_threshold_ns: Optional[int] = None
    own_sigma_avg_ns: Optional[int] = None       # captured once
    armed_lock_until_tick: int = -10**9          # until which tick we hold the armed lock

    # Warmup of σ_avg: permanently excluded servers and warmup start marker.
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
    Holds the population of instances, the shared occupied-pool and the reproduction flag.
    The only point producing (best_mono, best_utc) for the service.
    """

    def __init__(self, servers: List[str], max_population: int, diff_sigma: float) -> None:
        self.servers: List[str] = list(servers)
        self.max_population: int = max_population
        self.diff_sigma: float = diff_sigma

        self.population: List[AlgorithmInstance] = []
        self._next_id: int = 0
        self._tick: int = 0
        self._lock: threading.RLock = threading.RLock()
        self._pending_logs: List[Tuple[str, str]] = []

        self.reproduction_allowed: bool = True
        self._spreads: deque = deque(maxlen=SPREAD_HISTORY_LEN)

        self._median_spread_ns: Optional[float] = None
        self._noise_ref_ns: Optional[float] = None

        # Trajectory of applied median_offset (for history vote and drift).
        self._applied_offsets: deque[int] = deque(maxlen=DRIFT_WINDOW)
        self._last_short_vote: Optional[int] = None
        self._last_drift_prediction: Optional[int] = None
        self._last_drift_slope: Optional[float] = None

        self._occupied: set = set()
        self._prev_favorites: Dict[int, Optional[str]] = {}
        self.cold_start_generation: int = 0

        # Queue of lineage roots (divine and initial). Maximum DIVINE_QUEUE_MAX.
        self._lineage_queue: List[int] = []

        # Cold start: immediately MIN_POPULATION roots. On a single instance
        # bootstrap is fragile — if it gets stuck before reproduction, the colony
        # will not get out: die will not trigger (accept rate > DEATH_ACCEPT_THRESHOLD),
        # divine will not trigger (population >= 1), no deaths.
        for _ in range(MIN_POPULATION):
            self._spawn_locked(None)

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    def round(self, server_results: List[Tuple[str, Tuple[int, int, int, int]]],
              t_ref_mono: Optional[int] = None
              ) -> Optional[Tuple[int, int, List[int], int, tuple, Optional[int]]]:
        """
        Atomic round: refresh_bans → process_round → check_triggers.
        t_ref_mono — the monotonic time to which the offset of the round is
        referred (delay-weighted average of mid_mono of responders).
        Return contract: (best_mono, best_utc, delays, best_offset_ns, servers, threshold)
        """
        logs: List[Tuple[str, str]] = []
        try:
            with self._lock:
                self._refresh_bans_locked()
                result = self._process_round_locked(server_results, t_ref_mono=t_ref_mono)
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

            thr = (REPRODUCTION_SPREAD_MULT * self._noise_ref_ns
                   if self._noise_ref_ns is not None else None)
            ratio = (self._median_spread_ns / thr
                     if (self._median_spread_ns is not None and thr and thr > 0)
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
                    'median_spread_ns': self._median_spread_ns,
                    'noise_ref_ns': self._noise_ref_ns,
                    'threshold_ns': thr,
                    'ratio': ratio,
                },
                'history_vote': {
                    'short_vote_ns': self._last_short_vote,
                    'drift_prediction_ns': self._last_drift_prediction,
                    'drift_slope_ns_per_round': self._last_drift_slope,
                    'applied_len': len(self._applied_offsets),
                    'last_applied_ns': (self._applied_offsets[0]
                                        if self._applied_offsets else None),
                },
            }

    def get_instances_telemetry(self) -> Dict[int, Dict[str, Any]]:
        """
        Full telemetry per each live instance: both SlewRecord queues
        (with instance_id inside), filter state and dominant statistics.
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
                    'dominant_M': min_samples,
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

    # -----------------------------------------------------------------
    # Internal methods (call under _lock)
    # -----------------------------------------------------------------

    def _refresh_bans_locked(self) -> None:
        """
        Updates occupied and banned_servers, ensuring the invariant
        "one server — one favorite".

        If several instances point to the same server, the owner is
        the instance with the smallest id. Losers lose only
        favorite (set to None); the instance statistics are NOT reset:
        own_reference_offset/own_threshold_ns/own_sigma_avg_ns/histories/
        accept_window/low_accept_windows are preserved, because they describe
        the available servers, not the current binding. On the next round
        the instance will re-select a favorite from free servers.

        Reset of all fields happens only on birth (new instance)
        and after death (kill+respawn).
        """
        # Owner of each server — minimal id.
        owners: Dict[str, int] = {}
        for inst in sorted(self.population, key=lambda i: i.id):
            fav = inst.favorite
            if fav is not None and fav not in owners:
                owners[fav] = inst.id

        # Take away the favorite from the losers.
        for inst in self.population:
            fav = inst.favorite
            if fav is not None and owners.get(fav) != inst.id:
                self._defer_log("info",
                                f"Consensus: instance {inst.id} loses favorite {fav} "
                                f"(owner — {owners[fav]}); statistics preserved"
                                )
                inst.favorite = None
                inst.armed_lock_until_tick = -10 ** 9

        # occupied — only servers of actual owners.
        occupied = set(owners.keys())
        self._occupied = occupied

        # banned = occupied − {own favorite}; for an instance without a favorite
        # banned = occupied as a whole.
        for inst in self.population:
            own = {inst.favorite} if inst.favorite is not None else set()
            inst.banned_servers = occupied - own

    def _spawn_locked(self, parent: Optional[AlgorithmInstance]) -> AlgorithmInstance:
        """
        Birth of a child.

        Parent=None — cold start (initial or after complete extinction).
                       Creates a new lineage root, registers it in the queue.
        Parent=<obj> — normal birth (dominant / deathbed). The child
                       inherits the parent's lineage_id; queue does not change.
        """
        if parent is None:
            inherited_bans = set()
            lineage_id = self._next_id          # root = its own id
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
            if len(self._lineage_queue) < DIVINE_QUEUE_MAX:
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
        Divine birth: a new independent lineage root.

        Does not inherit parent's warmup_excluded. Instead it gets
        warmup_excluded = {favorite of all stuck instances} — that is,
        excludes from warmup exactly those servers on which the dead-end
        lineage is stuck. Creates a new root in the queue.
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
                        f"reproduced={inst.reproduced_count})"
                        )

        # 3. Lineage cleanup: if the lineage died out — remove the root from the queue.
        if not any(i.lineage_id == inst.lineage_id for i in self.population):
            if inst.lineage_id in self._lineage_queue:
                self._lineage_queue.remove(inst.lineage_id)
                self._defer_log("info",
                                f"Consensus: lineage {inst.lineage_id} died out, "
                                f"queue={self._lineage_queue}"
                                )

        # 4. Divine trigger: fill population up to MIN_POPULATION,
        #    while there are slots in the root queue.
        if len(self.population) < MIN_POPULATION:
            excluded = {i.favorite for i in stuck_candidates if i.favorite}
            if len(self.servers) - len(excluded) >= 1:
                spawned = False
                while (len(self.population) < MIN_POPULATION
                       and len(self._lineage_queue) < DIVINE_QUEUE_MAX):
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
        Run of one instance over available samples.
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
                                      (neutral — neither wins nor loses).
        Returns None if σ-history is on fewer than 2 servers (warmup).
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
                         ) -> str:
        """
        Selection of the instance's favorite for this round.

        Priorities:
          1. Armed-lock active AND favorite passed the filter → hold favorite.
          2. Score mode: score = d_norm + s_norm, minimum — candidate.
             Warmup (σ-history on < 2 servers): only delay, as before.
          3. If favorite was filtered out — forced switch without hysteresis.
          4. Otherwise — hysteresis: stay on favorite if its score
             is not worse than the candidate by more than FAVORITE_HYST.

        All decisions are reflected in fields inst.last_selection_mode,
        inst.last_best_candidate, inst.last_scores — read by telemetry.
        """
        delays = {srv: smp[0] for srv, smp in accepted}

        # 1. Armed-lock
        if self._tick < inst.armed_lock_until_tick and inst.favorite in delays:
            inst.last_selection_mode = 'armed_lock'
            inst.last_best_candidate = inst.favorite
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

        # 3. Favorite filtered out — forced switch
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

        # 4. Hysteresis (only in score mode)
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
        mark: '✓' — accepted by the filter, '×' — rejected.
        """
        out = []
        for srv, smp in available:
            proposed = int(smp[2] - smp[1])
            dev = int(proposed - ref)
            mark = '✓' if srv in accepted_set else '×'
            out.append((srv, proposed, dev, int(smp[0]), mark))
        return tuple(out)

    def _process_round_locked(self, server_results,
                              t_ref_mono: Optional[int] = None
                              ) -> Optional[Tuple[int, int, List[int], int, tuple, Optional[int]]]:

        outputs_by_inst: List[Tuple[AlgorithmInstance, int]] = []

        for inst in self.population:
            available = [(s, smp) for s, smp in server_results
                         if s not in inst.banned_servers]

            # Warmup filter: permanently excluded servers do not participate.
            # If the filter empties the set — do not apply it.
            if inst.warmup_excluded:
                filtered = [(s, smp) for s, smp in available
                            if s not in inst.warmup_excluded]
                if filtered:
                    available = filtered

            if not available:
                inst.accept_window.append(False)
                self._update_low_accept_windows(inst)
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
                continue

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
                    servers=servers,
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
                    instance_id=inst.id,
                    favorite=best_srv,
                    servers=servers,
                    ref_ns=old_ref,
                    is_cold_start=False,
                ))
                diffs = [r.diff_ns for r in inst.own_history
                         if r.diff_ns is not None]
                if len(diffs) >= 2:
                    stdev = statistics.stdev(diffs)
                    new_raw = max(int(self.diff_sigma * stdev), THRESHOLD_MIN_NS)
                    if inst.own_threshold_ns is None:
                        inst.own_threshold_ns = new_raw
                    else:
                        shrink_floor = int(inst.own_threshold_ns * THRESHOLD_SHRINK_FLOOR)
                        inst.own_threshold_ns = max(new_raw, shrink_floor)

            inst.own_reference_offset = new_offset
            inst.favorite = best_srv
            inst.accept_window.append(True)
            self._update_low_accept_windows(inst)
            outputs_by_inst.append((inst, new_offset))

        # --- History and drift votes are always available ---
        short_vote, drift_prediction, drift_slope = self._compute_history_votes()
        self._last_short_vote = short_vote
        self._last_drift_prediction = drift_prediction
        self._last_drift_slope = drift_slope

        # Only instances with σ_avg vote. If there are none — fall back to
        # warming-up votes (only while history is empty).
        trusted = [(inst, off) for inst, off in outputs_by_inst
                   if inst.own_sigma_avg_ns is not None]
        if not trusted:
            trusted = outputs_by_inst
        outputs = [off for _, off in trusted]

        # Reference point for the gate: drift_pred if history has accumulated,
        # otherwise short_vote. If neither — gate does not work
        # (colony cold start).
        prediction = drift_prediction if drift_prediction is not None else short_vote

        # Colony noise scale — median σ_avg of mature instances.
        sigmas = [i.own_sigma_avg_ns for i in self.population
                  if i.own_sigma_avg_ns is not None]
        noise_ref = statistics.median(sigmas) if len(sigmas) >= 2 else None

        # --- Pre-gate: discard outlier votes before the median ---
        # Active only with a reference point and ≥2 votes. With a single
        # vote we trust it — no statistics to separate signal from noise.
        if prediction is not None and noise_ref is not None and len(outputs) >= 2:
            limit = int(PRE_GATE_K * noise_ref)
            filtered = [off for off in outputs if abs(off - prediction) <= limit]
            if filtered:
                if len(filtered) < len(outputs):
                    self._defer_log("debug",
                                    f"Consensus: pre-gate discarded "
                                    f"{len(outputs) - len(filtered)} vote(s) out of "
                                    f"{len(outputs)} (limit=±{limit}ns)"
                                    )
                outputs = filtered
            else:
                self._defer_log("debug",
                                f"Consensus: pre-gate discarded all {len(outputs)} "
                                f"vote(s); hold by prediction"
                                )
                outputs = []

        votes = list(outputs)
        if short_vote is not None:
            votes.append(short_vote)
        if drift_prediction is not None:
            votes.append(drift_prediction)

        if not votes:
            return None

        new_median = int(statistics.median(votes))

        # --- Post-gate: limit the step relative to the prediction ---
        # Only if there is a reference point and ≥3 applied values have accumulated
        # (otherwise there is nothing to limit, the colony is still building).
        if (prediction is not None and noise_ref is not None
                and len(self._applied_offsets) >= 3):
            delta = new_median - prediction
            limit = int(POST_GATE_K * noise_ref)
            if abs(delta) > limit:
                new_median = prediction + (1 if delta > 0 else -1) * limit
                self._defer_log("debug",
                                f"Consensus: post-gate clamp {delta:+d} → "
                                f"{new_median - prediction:+d}ns (limit=±{limit}ns)"
                                )

        median_offset = new_median
        self._applied_offsets.appendleft(median_offset)

        thresholds = [inst.own_threshold_ns for inst, _ in trusted
                      if inst.own_threshold_ns is not None]
        threshold_out = int(statistics.median(thresholds)) if thresholds else None

        servers = self._build_consensus_servers(server_results, median_offset)
        delays_all = [smp[0] for _, smp in server_results]

        if t_ref_mono is None:
            t_ref_mono = time.monotonic_ns()
        return (t_ref_mono, t_ref_mono + median_offset, delays_all,
                median_offset, servers, threshold_out)

    def _build_consensus_servers(
            self,
            server_results: List[Tuple[str, Tuple[int, int, int, int]]],
            median_offset: int,
    ) -> tuple:
        """
        Per-server structured slice of the consensus-level round.
        Element: (name, proposed_ns, dev_ns, delay_ns, mark)
        dev_ns = proposed_ns - median_offset.
        """
        out = []
        for srv, smp in server_results:
            proposed = int(smp[2] - smp[1])
            dev = int(proposed - median_offset)
            if any(i.favorite == srv for i in self.population):
                mark = '✓'
            elif any(srv not in i.banned_servers for i in self.population):
                mark = '?'
            else:
                mark = '×'
            out.append((srv, proposed, dev, int(smp[0]), mark))
        return tuple(out)

    def _compute_history_votes(self
                               ) -> Tuple[Optional[int], Optional[int], Optional[float]]:
        """
        (short_vote, drift_prediction, drift_slope):
            short_vote  — median of the last HISTORY_VOTE_SHORT_WINDOW
                          applied median_offset. None when < MIN_SAMPLES.
            drift_prediction  — extrapolation one step forward via linear regression
                          of the last DRIFT_WINDOW points. None when < DRIFT_MIN_SAMPLES
                          or degenerate fit.
            drift_slope — regression slope, ns/round.
        """
        n = len(self._applied_offsets)

        short_vote: Optional[int] = None
        if n >= HISTORY_VOTE_MIN_SAMPLES:
            window = list(self._applied_offsets)[:HISTORY_VOTE_SHORT_WINDOW]
            short_vote = int(statistics.median(window))

        drift_prediction: Optional[int] = None
        drift_slope: Optional[float] = None
        if n >= DRIFT_MIN_SAMPLES:
            series = list(reversed(self._applied_offsets))   # oldest-first
            m = len(series)
            base = series[0]
            ys = [y - base for y in series]                  # subtract baseline
            x_mean = (m - 1) / 2.0
            y_mean = statistics.mean(ys)
            num = 0.0
            den = 0.0
            for i, yy in enumerate(ys):
                dx = i - x_mean
                num += dx * (yy - y_mean)
                den += dx * dx
            if den > 0.0:
                slope = num / den
                drift_slope = slope
                drift_prediction = base + int(ys[-1] + slope)

        return short_vote, drift_prediction, drift_slope

    # -----------------------------------------------------------------
    # Dominant: statistics collection, σ_avg capture, M computation, trigger
    # -----------------------------------------------------------------
    @staticmethod
    def _collect_per_server_devs(inst: AlgorithmInstance
                                 ) -> Dict[str, List[int]]:
        """
        Dictionary server → list of dev_ns from ✓ records of accepted history.
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
        (on_X, others): dev_ns of the current favorite X and all other servers
        from ✓ records of accepted history. ALL servers of the round are counted,
        not only favorite — this eliminates the selection bias toward the favorite.
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
        Capture of σ_avg under warmup rules:
          1. < WARMUP_MIN_SURVIVORS available servers — do not exit warmup.
          2b. >= SIGMA_WARMUP_MIN_SURVIVORS servers collected
              SIGMA_WARMUP_RECORDS ✓ — capture over them, without waiting
              for the rest.
          2. All collected >= SIGMA_WARMUP_RECORDS ✓ — capture over all.
          3. After SIGMA_WARMUP_TIMEOUT_MULT·SIGMA_WARMUP_RECORDS ticks
             servers with 0 ✓ go into warmup_excluded permanently. If after
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

        # Rule 2b: enough "full" servers — capture over them.
        full = [srv for srv in available_now
                if counts[srv] >= SIGMA_WARMUP_RECORDS]
        if len(full) >= SIGMA_WARMUP_MIN_SURVIVORS:
            self._capture_sigma_locked(
                inst,
                {srv: counts[srv] for srv in full},
                devs_by_server,
            )
            return

        # Rule 2: all collected.
        if all(c >= SIGMA_WARMUP_RECORDS for c in counts.values()):
            self._capture_sigma_locked(inst, counts, devs_by_server)
            return

        # Grace until timeout.
        if self._tick - inst.warmup_started_tick \
                < SIGMA_WARMUP_TIMEOUT_MULT * SIGMA_WARMUP_RECORDS:
            return

        # Rule 3a: ban only those with 0 ✓.
        below = [srv for srv, c in counts.items() if c == 0]

        if not below:
            # Nothing to ban — everyone has ≥1 ✓, but someone did not reach SIGMA_WARMUP_RECORDS.
            # Wait silently: no need to spam the log every 30 seconds.
            return

        survivors = [srv for srv in available_now if srv not in below]

        if len(survivors) < SIGMA_WARMUP_MIN_SURVIVORS:  # rule 3b
            if not inst.warmup_stuck_logged:
                self._defer_log("info",
                                f"Instance {inst.id}: warmup dragged on, ban would collapse "
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
        If the average σ exceeds SIGMA_AVG_SANITY_MAX_NS — capture
        is cancelled (the instance lives without trusted status and dies).
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
                            f"instance not usable"
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
        to reproduce by dominance, otherwise None.
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

        # Attempt σ_avg capture for each instance
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
            # The child is cold, the parent continues to live with its favorite.
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
                            f"Consensus: colony is on the brink of extinction "
                            f"(population={len(self.population)}, "
                            f"lineage_queue={self._lineage_queue}, "
                            f"reproduction_allowed=False)"
                            )

        # Gate: median(spreads) < MULT · median(σ_avg).
        # While spread history is not accumulated or there are fewer than two σ_avg — gate is open.
        offsets = [i.own_reference_offset for i in self.population
                   if i.own_reference_offset is not None]
        if len(offsets) >= 2:
            self._spreads.append(statistics.stdev(offsets))

        sigmas = [i.own_sigma_avg_ns for i in self.population
                  if i.own_sigma_avg_ns is not None]
        noise_ref = statistics.median(sigmas) if len(sigmas) >= 2 else None

        # Snapshot for telemetry (even if the gate is not yet active).
        self._noise_ref_ns = noise_ref
        self._median_spread_ns = (statistics.median(self._spreads)
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
        Single precondition for all reproduction triggers.
        Order — from cheap to expensive.
        The offspring cap is shared across all triggers: over its lifetime an instance
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
        """Accumulate message. Inside critical section — only this, no logger.*."""
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
    Precise-time service based on NTP.
    Filtering and target-offset selection are delegated to Consensus; the service
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

        self.ntp_servers = ntp_servers or [
            'pool.ntp.org',
            'ptbtime1.ptb.de',
            'ptbtime2.ptb.de',
			'time.nist.gov',
			'ntp1.sp.se'
			'ntp2.sp.se'
			'time-a-g.nist.gov'
			'ntp-p1.obspm.fr'
			'ntp.metas.ch'
        ]
        self.ntp_servers_resolved = {} # DNS -> IP of NTP servers
        self.ntp_servers_last_resolved_ns: int = -NTP_RESOLVING_TIMEOUT_NS # Timestamp of last DNS -> IP resolution
        self.sync_interval: int = sync_interval_sec
        self.second_sync_thread_delay: int = sync_interval_sec // 2
        # Threshold of "stuck" PLL updates: half the interval between sync threads.
        # With sync_interval=60 and two threads with 30 s delay, normal PLL updates
        # go every ~30 s; anything shorter than 15 s — anomaly.
        self._min_pll_update_interval_ns: int = (
                max(self.second_sync_thread_delay // 2, 5) * 1_000_000_000
        )
        self.client: ntplib.NTPClient = ntplib.NTPClient() # NTP client instance
        self._executors_shutdown = False
        # Thread pool for polling NTP servers
        self._NTP_poll_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=len(self.ntp_servers) + 2,
            thread_name_prefix="ntp-poll",
        )
        # Thread pool for polling DNS servers
        self._DNS_poll_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=len(self.ntp_servers),
            thread_name_prefix="dns-poll",
        )
        self.dns_fail_servers = set() # servers that failed DNS resolution
        self.lock: threading.Lock = threading.Lock()
        self._pending_logs: List[Tuple[str, str]] = []
        self._threads_lock: threading.Lock = threading.Lock()
        self.running: bool = False

        self.is_synced_event: threading.Event = threading.Event()
        self._stop_event: threading.Event = threading.Event()

        self._threads: Dict[int, threading.Thread] = {}
        self._watchdog_thread: Optional[threading.Thread] = None
        self._keep_awake_thread: Optional[threading.Thread] = None
        self._last_success_mono_ns: int = 0  # 0 = not a single success
        self._last_stalled_servers: tuple = ()  # for diagnostics

        self._delay_history: deque[int] = deque(maxlen=20)

        # Per-server history of minimal delays (for honest jitter estimate
        # per server, not a mixture of servers).
        self._per_server_delay: Dict[str, deque] = defaultdict(lambda: deque(maxlen=PER_SERVER_HISTORY))

        # Rate-model of offset: offset(m) = anchor_offset + rate·(m − anchor_mono).
        # rate — dimensionless relative drift (1 ppm = 1e-6).
        self._anchor_offset: int = 0
        self._anchor_mono: int = 0
        self._rate: float = 0.0
        self._clock_snapshot = (0, 0, 0.0)  # (anchor_mono, anchor_offset, rate)
        # Dynamic TTL and declared accuracy of the snapshot.
        # Updated in _apply_new_sync_locked under self.lock,
        # read lock-free in get_clock_snapshot.
        self._clock_snapshot_ttl_ns: int = 2 * self.sync_interval * 1_000_000_000
        self._clock_snapshot_accuracy_ns: int = int(CLOCK_SNAPSHOT_ACCURACY_DEFAULT_NS)
        # Cache of TTL/accuracy computations for a given precision.
        # Key — precision_ns. Value — (ttl_sec, accuracy_ns).
        # Cleared in _apply_new_sync_locked — after each PLL update
        # the consensus noise data changes, the cache becomes stale.
        # Consumers are usually 1–3 (UI, IPC, trading modules), so
        # the cache size is naturally bounded. If needed — LRU.
        self._precision_cache: OrderedDict[int, Tuple[float, int]] = OrderedDict()
        self._target_offset: int = 0  # for telemetry
        self._last_phase_error: Optional[int] = None

        # History of applied corrections at the consensus level:
        # records SlewRecord(instance_id=None).
        self._slew_error_history: deque[SlewRecord] = deque(maxlen=HISTORY_MAX_LEN)

        # Accumulated bias (permanent shift) of servers (ns). Updated when history is full.
        self._colony_bias: Dict[str, int] = {}
        # Colony generation
        self._last_cold_gen: int = 0
        # Raw history of proposed (before bias application) — only for bias estimation.
        # The cleaned _slew_error_history is used by the colony for its decisions,
        # but is not suitable for the integrator: it contains our own past corrections.
        self._raw_proposed_history: deque[tuple] = deque(maxlen=HISTORY_MAX_LEN)

        self._diff_sigma: float = diff_sigma
        self._last_diff_threshold: Optional[int] = None

        # Colony of filter-instances.
        n_servers = len(self.ntp_servers)
        if n_servers > MAX_POPULATION_MIN_SERVERS:
            max_pop = max(int(n_servers * MAX_POPULATION_RATIO),
                          REPRODUCTION_LAG_L)
        else:
            max_pop = n_servers
        # Do not let population fall below MIN_POPULATION.
        max_pop = max(max_pop, MIN_POPULATION)

        self._consensus = Consensus(
            servers=self.ntp_servers,
            max_population=max_pop,
            diff_sigma=diff_sigma,
        )

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

        self._ensure_executors()  # Check the state of persistent thread pools

        with self._threads_lock:
            leftover = [t for t in self._threads.values() if t.is_alive()]
        if leftover:
            logger.warning(
                f"Detected unfinished threads of the previous session: "
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

        consensus_snapshot = self._consensus.snapshot()
        instances_telemetry = self._consensus.get_instances_telemetry()

        diff_values = [rec.diff_ns for rec in slew_error_history_snapshot]
        offset_spread_ns = statistics.stdev(diff_values) if len(diff_values) >= 2 else None
        ntp_spread_ns = statistics.stdev(delay_history_snapshot) if len(delay_history_snapshot) >= 2 else None

        per_server_delay_stats: Dict[str, Dict[str, Any]] = {}
        for srv, vals in per_server_delay_snapshot.items():
            if not vals:
                continue
            per_server_delay_stats[srv] = {
                'n': len(vals),
                'last_ns': vals[-1],
                'median_ns': int(statistics.median(vals)) if len(vals) >= 2 else None,
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
            'second_sync_thread_delay': self.second_sync_thread_delay
        }

    def get_utc_ns(self) -> int:
        """Lock-free. Returns precise UTC time.
        Uses _calculate_current_offset for consistency with telemetry."""
        if not self.is_synced_event.is_set():
            return time.time_ns()
        now = time.monotonic_ns()
        return now + self._calculate_current_offset(now)

    def get_clock_snapshot(
            self, precision_ns: Optional[int] = None
    ) -> Optional[ClockSnapshot]:
        """Returns a clock-model snapshot.

        Args:
            precision_ns:
                None — return a snapshot with TTL computed by the heuristic of consensus
                       quality. In the accuracy_ns field — the declared error bound.
                int  — return a snapshot with TTL chosen so that the average
                       error inside the window does not exceed precision_ns. If the accuracy
                       is unattainable due to σ_ref, the minimal TTL is returned
                       (CLOCK_SNAPSHOT_TTL_ABS_MIN_SEC), and in accuracy_ns —
                       the actual bound (σ_ref > precision_ns).

        Lock-free: reads the atomic _clock_snapshot, _clock_snapshot_ttl_sec,
        _clock_snapshot_accuracy_ns. With precision_ns=None without locks at all.
        With precision_ns — uses the already computed fields, also lock-free.

        Returns None if synchronization has not yet been performed.
        """
        if not self.is_synced_event.is_set():
            return None

        anchor_mono, anchor_offset, rate = self._clock_snapshot

        ttl_ns = None
        accuracy = None
        if precision_ns is None:
            ttl_ns = self._clock_snapshot_ttl_ns
            accuracy = self._clock_snapshot_accuracy_ns
        else:
            # Cache: precision_ns → (ttl_sec, accuracy_ns).
            # There are usually not many consumers, the size is bounded
            # naturally. The cache is invalidated in
            # _apply_new_sync_locked on each PLL update.
            with self.lock:
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

    # -----------------------------------------------------------------
    # Internal methods
    # -----------------------------------------------------------------

    @staticmethod
    def utc_from_snapshot(snap: Optional[ClockSnapshot],
                          now_mono: int) -> Optional[int]:
        """Restores UTC from a snapshot. None if the snapshot is absent
        or expired (now_mono - anchor_mono > ttl_ns).

        Does not take locks, does not access service state — a pure function.
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
    def _ntp_tx_utc_ns(response) -> int:
        """Raw 64-bit NTP stamp → UTC-nanoseconds."""
        tx_ts = getattr(response, 'tx_timestamp', None)
        if isinstance(tx_ts, int) and tx_ts > 0:
            secs_since_1900 = tx_ts >> 32
            frac = tx_ts & 0xFFFFFFFF
            unix_secs = secs_since_1900 - NTP_EPOCH_OFFSET_SEC
            frac_ns = (frac * 1_000_000_000) >> 32
            return unix_secs * 1_000_000_000 + frac_ns
        return int(round(response.tx_time * 1e9))

    @staticmethod
    def _compute_colony_noise_ns (raw_snapshots: list) -> Dict[str, int]:
        """
        Robust estimate of jitter of each server by first differences
        of raw proposed. NOT the standard deviation: 1.4826·MAD(first_diffs)/√2.
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
                med_d = statistics.median(diffs)
                mad = statistics.median(abs(d - med_d) for d in diffs)
                out[srv] = int(1.4826 * mad / math.sqrt(2.0))
            else:
                out[srv] = 0
        return out

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

    def _query_dns_resolv(self) -> set:
        """Periodic resolution of DNS queries for NTP servers"""
        start_mono = time.monotonic_ns()
        with self.lock:
            if start_mono - self.ntp_servers_last_resolved_ns < NTP_RESOLVING_TIMEOUT_NS:
                return self.dns_fail_servers.copy()         # Return current state of excluded servers
            self.ntp_servers_last_resolved_ns = start_mono  # Record the DNS query attempt

        executor = self._DNS_poll_executor  # Use the persistent thread pool
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
                    logger.warning(f"DNS error obtaining IP address {server}: {e}")
                    with self.lock:
                        if server not in self.ntp_servers_resolved:
                            self.dns_fail_servers.add(server)
                    # otherwise — use the previous IP, do not exclude

            for future in not_done:
                server = future_to_server[future]
                logger.warning(
                    f"DNS server did not respond to name resolution request {server} within {DNS_QUERY_TIMEOUT_SEC} seconds")
                future.cancel()
                with self.lock:
                    if server not in self.ntp_servers_resolved:
                        self.dns_fail_servers.add(server)
        except Exception as e:
            logger.error(f"Error while processing DNS request thread: {e}")
        finally:
            return self.dns_fail_servers.copy()

    def _query_single_server(self, server: str,
                             t_round_start_mono: int
                             ) -> Optional[Tuple[int, int, int, int]]:
        """
        Polls the server QUERY_ATTEMPTS times with QUERY_ATTEMPT_SPACING_SEC spacing.
        All attempts are merged by delay-weighted average into one sample.

        Returns (delay_min, mid_mono_avg, t2_utc_avg, proposed_avg):
            delay_min    = min delay among successful attempts (for weight in consensus);
            mid_mono_avg = delay-weighted average mid_mono;
            t2_utc_avg   = proposed_avg + mid_mono_avg  (invariant proposed = t2_utc − mid_mono);
            proposed_avg = delay-weighted average (t2_utc − mid_mono).
        """

        server_ip = self.ntp_servers_resolved.get(server)
        if server_ip is None:
            logger.warning(f"Server {server}: no known IP")
            return None

        samples: List[Tuple[int, int, int, int]] = []  # (proposed, mid_mono, mid_sys, delay)
        for k in range(QUERY_ATTEMPTS):
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
                t0_mono = time.monotonic_ns()
                t0_sys = time.time_ns()
                response = self.client.request(server_ip, version=4, timeout=NTP_QUERY_TIMEOUT_SEC)
                t3_mono = time.monotonic_ns()
                t3_sys = time.time_ns()

                t2_utc = self._ntp_tx_utc_ns(response)
                delay_ns = t3_mono - t0_mono
                mid_mono = t0_mono + (delay_ns // 2)
                mid_sys = (t0_sys + t3_sys) // 2
                proposed = t2_utc - mid_mono
                samples.append((proposed, mid_mono, mid_sys, delay_ns))
            except Exception as e:
                logger.debug(f"NTP request to {server} (attempt {k + 1}) failed: {e}")

        if not samples:
            return None

        weights = [1.0 / max(d, 1) for _, _, _, d in samples]
        summ_weights = sum(weights)
        proposed_avg = int(sum(w * p for w, (p, _, _, _) in zip(weights, samples)) / summ_weights)
        mid_mono_avg = int(sum(w * m for w, (_, m, _, _) in zip(weights, samples)) / summ_weights)
        mid_sys_avg = int(sum(w * s for w, (_, _, s, _) in zip(weights, samples)) / summ_weights)
        delay_min = min(d for _, _, _, d in samples)

        t2_utc_avg = proposed_avg + mid_mono_avg
        offset_avg = t2_utc_avg - mid_sys_avg  # slot 3 — preserve the original semantics

        return delay_min, mid_mono_avg, t2_utc_avg, offset_avg

    def _poll_servers(self) -> Tuple[List[Tuple[str, Tuple[int, int, int, int]]], int]:
        """
        One network poll per round: all servers in parallel, each with
        QUERY_ATTEMPTS attempts on the shared schedule t_round_start + k·spacing.

        Returns (server_results, t_ref_mono), where t_ref_mono —
        delay-weighted average mid_mono of responding servers.
        """
        failed_servers = self._query_dns_resolv()
        t_round_start_mono = time.monotonic_ns()
        server_results: List[Tuple[str, Tuple[int, int, int, int]]] = []

        executor = self._NTP_poll_executor # Use the persistent thread pool
        filtered_ntp_servers = [item for item in self.ntp_servers if item not in failed_servers]
        future_to_server = {executor.submit(self._query_single_server, s, t_round_start_mono): s
            for s in filtered_ntp_servers}
        not_done_futures = set()
        try:
            done, not_done = concurrent.futures.wait(future_to_server,
                                                     timeout=QUERY_ATTEMPTS * QUERY_ATTEMPT_SPACING_SEC +
                                                             NTP_QUERY_TIMEOUT_SEC + NTP_QUERY_TIMEOUT_SAFE_SEC)
            for future in done:
                server = future_to_server[future]
                try:
                    result = future.result()
                except Exception as e:
                    logger.warning(f"Poll thread error {server}: {e}")
                    continue
                if result is not None:
                    server_results.append((server, result))
                else:
                    logger.warning(f"Server {server} unavailable after "
                                   f"{QUERY_ATTEMPTS} attempts")

            for future in not_done:
                # Cannot cancel a running one, but at least mark it.
                not_done_futures.add(future)
                future.cancel()
                srv = future_to_server[future]
                logger.warning(f"Server {srv}: round timeout, result discarded")

        except Exception as e:
            logger.error(f"Error while processing NTP request thread: {e}")

        finally:
            with self.lock:
                self._last_stalled_servers = tuple(future_to_server[f] for f in not_done_futures)

        if server_results:
            ws = [1.0 / max(smp[0], 1) for _, smp in server_results]
            summ_ws = sum(ws)
            t_ref_mono = int(
                sum(w * smp[1] for w, (_, smp) in zip(ws, server_results)) / summ_ws
            )
        else:
            t_ref_mono = t_round_start_mono

        return server_results, t_ref_mono

    def _get_best_ntp_sample(self) -> Tuple[Optional[tuple], tuple]:
        """One round: poll → raw snapshot → apply bias → round().
        Raw proposed snapshot = (name, proposed_ns)[] goes to _apply_new_sync_locked
        for accumulating raw-history (bias estimation reads exactly it,
        not the clean _slew_error_history)."""
        server_results, t_ref_mono = self._poll_servers()
        if not server_results:
            logger.error(
                f"Failed to synchronize with any NTP server: {self.ntp_servers}"
            )
            raise RuntimeError("All NTP requests failed")

        with self.lock:
            for srv, smp in server_results:
                self._per_server_delay[srv].append(int(smp[0]))

        # Snapshot of RAW proposed — before applying corrections.
        raw_snapshot = tuple(
            (srv, smp[2] - smp[1])
            for srv, smp in server_results
        )

        active_bias = {
            srv: self._colony_bias[srv]
            for srv in self._colony_bias
            if self._colony_bias[srv] != 0
        }
        if active_bias:
            server_results = [
                (
                    srv,
                    smp
                    if not (b := active_bias.get(srv, 0))
                    else (smp[0], smp[1], smp[2] - b, smp[3] - b),
                )
                for srv, smp in server_results
            ]

        result = self._consensus.round(server_results, t_ref_mono=t_ref_mono)
        return result, raw_snapshot

    def _calculate_current_offset(self, now_mono: int) -> int:
        """Drift compensation. The single point of offset computation.
        Call from any context — lock-free via _clock_snapshot."""
        if not self.is_synced_event.is_set():
            return 0
        anchor_mono, anchor_offset, rate = self._clock_snapshot
        return anchor_offset + round(rate * (now_mono - anchor_mono))

    def _compute_snapshot_ttl_locked(self) -> int:
        """Dynamic TTL of the clock-model snapshot.

        Returns: TTL in nanoseconds.

        Logic:
            σ_ref     — long-term reference noise of the colony
                        (median σ_avg over live Consensus instances).
            σ_recent  — standard deviation of the last N applied_offsets
                        of consensus.
            quality   — σ_ref / max(σ_recent, σ_ref/2), bounded [FLOOR, CEIL].
            TTL       — TTL_BASE · quality, bounded [ABS_MIN, ABS_MAX].

        Internal constants (ABS_MIN_SEC, ABS_MAX_SEC, TTL_BASE_SEC) —
        in seconds for readability; converted to ns on output.

        Early states (little data, no σ_ref) — base TTL.
        Called under self.lock.
        """
        sigma_ref = self._consensus._noise_ref_ns
        base_ttl_ns = int(
            self.sync_interval * CLOCK_SNAPSHOT_TTL_BASE_SEC * 1_000_000_000
        )
        if sigma_ref is None or sigma_ref <= 0:
            return base_ttl_ns

        recent = list(self._consensus._applied_offsets)[:CLOCK_SNAPSHOT_TTL_RECENT_N]
        if len(recent) < 3:
            return base_ttl_ns

        sigma_recent = statistics.stdev(recent)

        # Protection from division by too small σ_recent: floor = σ_ref / 2.
        denom = max(sigma_recent, sigma_ref * 0.5)
        quality = sigma_ref / denom
        quality = max(CLOCK_SNAPSHOT_TTL_QUALITY_FLOOR,
                      min(CLOCK_SNAPSHOT_TTL_QUALITY_CEIL, quality))

        ttl_ns = int(base_ttl_ns * quality)
        abs_min_ns = int(CLOCK_SNAPSHOT_TTL_ABS_MIN_SEC * 1_000_000_000)
        abs_max_ns = int(CLOCK_SNAPSHOT_TTL_ABS_MAX_SEC * 1_000_000_000)
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
        """Updates raw-history for the bias integrator; when length is sufficient —
        recomputes bias. Call under self.lock."""
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
            self._last_cold_gen = cur_gen
        self._raw_proposed_history.appendleft(raw_snapshot)
        if len(self._raw_proposed_history) >= HISTORY_MAX_LEN // 3:
            self._update_colony_bias()

    def _publish_clock_locked(self) -> None:
        """Publishes the consistent clock snapshot for lock-free readers.
        Call under self.lock after any changes to anchor/rate."""
        self._clock_snapshot = (
            self._anchor_mono,
            self._anchor_offset,
            self._rate,
        )

    def _apply_new_sync_locked(self, best_mono: int, best_utc: int,
                               servers: Optional[tuple] = None,
                               diff_threshold_ns: Optional[int] = None,
                               raw_snapshot: Optional[tuple] = None) -> None:
        new_target_offset = int(best_utc - best_mono)
        # Any update changes the consensus noise estimate — the cache
        # of precision requests becomes invalid.
        if self._precision_cache:
            self._precision_cache.clear()

        # --- Initial synchronization ---
        if not self.is_synced_event.is_set():
            self._defer_log("info",
                            f"Initial time synchronization completed, "
                            f"offset={new_target_offset} ns"
                            )
            self._anchor_offset = new_target_offset
            self._anchor_mono = best_mono  # anchor — moment of measurement, not now
            self._rate = 0.0
            self._publish_clock_locked()  # fix the full snapshot of values
            self._clock_snapshot_ttl_ns = self._compute_snapshot_ttl_locked()
            self._clock_snapshot_accuracy_ns = self._compute_snapshot_accuracy_locked(
                self._clock_snapshot_ttl_ns
            )
            self._target_offset = new_target_offset
            self.is_synced_event.set()
            return

        # --- Cut-off of "stuck" updates ---
        # Both sync threads could be woken simultaneously (screen sleep,
        # Modern Standby). Then dt in the denominator of the I-correction is ~0, rate
        # instantly saturates. Skip PLL, but write history.
        dt_ns = best_mono - self._anchor_mono
        if dt_ns < self._min_pll_update_interval_ns:
            self._slew_error_history.appendleft(SlewRecord(
                timestamp_ns=time.time_ns(),
                diff_ns=int(self._last_phase_error or 0),
                threshold_ns=int(diff_threshold_ns) if diff_threshold_ns is not None else None,
                instance_id=None,
                servers=servers,
                ref_ns=new_target_offset,
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
        phase_error = new_target_offset - predicted
        self._last_phase_error = int(phase_error)
        diff = -phase_error  # preserve the old semantics (predicted − new)

        # --- Phase jump detection ---
        if abs(phase_error) > PHASE_JUMP_THRESHOLD_NS:
            # Not drift, but an external event: sleep/resume, system
            # time step, NTP jump on the server side. Catching up slowly
            # is not allowed — PLL_KP=0.10 would stretch 400 ms into 35 minutes.
            # Reset anchor and rate — PLL will re-estimate drift from scratch.
            self._defer_log("warning",
                            f"PLL: phase jump {phase_error / 1e6:+.1f}ms "
                            f"(> ±{PHASE_JUMP_THRESHOLD_NS / 1e6:.0f}ms) — "
                            f"reset anchor, rate zeroed"
                            )
            self._anchor_offset = new_target_offset
            self._anchor_mono = best_mono
            self._rate = 0.0
            self._publish_clock_locked()  # fix the full snapshot of values
            self._clock_snapshot_ttl_ns = self._compute_snapshot_ttl_locked()
            self._clock_snapshot_accuracy_ns = self._compute_snapshot_accuracy_locked(
                self._clock_snapshot_ttl_ns
            )
            self._target_offset = new_target_offset

            self._slew_error_history.appendleft(SlewRecord(
                timestamp_ns=time.time_ns(),
                diff_ns=int(diff),
                threshold_ns=int(diff_threshold_ns) if diff_threshold_ns is not None else None,
                instance_id=None,
                servers=servers,
                ref_ns=new_target_offset,
            ))
            self._update_bias_history_locked(raw_snapshot)
            return

        # --- Normal PLL update ---
        # Shift anchor to best_mono (moment of measurement) by the old model.
        # Not to now_mono — otherwise between measurement and application
        # rate × (now − best) of systematic error accumulates.
        self._anchor_mono = best_mono
        self._anchor_offset = predicted

        # P-correction: part of the phase error is contributed to offset
        if abs(phase_error) > PHASE_MEDIUM_JUMP_NS:
            kp = PLL_KP_MEDIUM
        else:
            kp = PLL_KP
        self._anchor_offset += int(kp * phase_error)

        # I-correction: part of the phase error goes into rate
        if dt_ns > 0:
            self._rate += PLL_KI * phase_error / dt_ns
            self._rate = max(-PLL_RATE_LIMIT, min(PLL_RATE_LIMIT, self._rate))
        self._publish_clock_locked()  # fix the full snapshot of values
        self._clock_snapshot_ttl_ns = self._compute_snapshot_ttl_locked()
        self._clock_snapshot_accuracy_ns = self._compute_snapshot_accuracy_locked(
            self._clock_snapshot_ttl_ns
        )

        self._slew_error_history.appendleft(SlewRecord(
            timestamp_ns=time.time_ns(),
            diff_ns=int(diff),
            threshold_ns=int(diff_threshold_ns) if diff_threshold_ns is not None else None,
            instance_id=None,
            servers=servers,
            ref_ns=new_target_offset,
        ))
        self._update_bias_history_locked(raw_snapshot)

        self._target_offset = new_target_offset
        self._defer_log("debug",
                        f"Synchronization updated: offset={new_target_offset} ns, "
                        f"phase_err={phase_error} ns, rate={self._rate * 1e6:+.3f} ppm, "
                        f"threshold=±{diff_threshold_ns} ns, "
                        f"ttl={self._clock_snapshot_ttl_ns / 1e9:.1f}s, "
                        f"acc={self._clock_snapshot_accuracy_ns / 1e6:.2f}ms"
                        )

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
                    # outside the lock — normal logger, no deadlock
                    logger.debug(f"Thread {thread_id}: round skipped by consensus")
                    with self.lock:
                        self._raw_proposed_history.appendleft(raw_snapshot)
                        if len(self._raw_proposed_history) >= HISTORY_MAX_LEN // 3:
                            self._update_colony_bias()
                        logs = self._drain_pending_logs()
                else:
                    (best_mono, best_utc, delays, best_offset_ns,
                     servers, diff_threshold_ns) = result
                    logger.debug(
                        f"Thread {thread_id}: round took "
                        f"{time.monotonic() - t_start:.2f}s, servers responded: {len(delays)}"
                    )
                    with self.lock:
                        self._last_diff_threshold = diff_threshold_ns
                        self._apply_new_sync_locked(
                            best_mono, best_utc, servers,
                            diff_threshold_ns, raw_snapshot,
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
        Estimate of the permanent server shift by the RAW history
        (_raw_proposed_history, before bias application).

        Raw history excludes self-feedback: the integrator does not see
        its own past corrections, so convergence is guaranteed.

        Formula:
            mean_raw[s]  = average of raw proposed
            sigma[s]     = server noise over raw proposed
            M            = median(mean_raw) over active
            delta[s]     = mean_raw[s] − M
            delta_app[s] = delta[s] − sign(delta)·sigma[s],  if |delta| > sigma
                         = 0,                                otherwise
            b[s] ← (1-k)·b[s] + k·delta_app[s], clamp ±1 ms

        Soft-threshold: a shift that does not protrude from the server's noise
        is ignored. The median converges to the common center, noisy servers
        do not introduce a false shift.
        """

        per_server: Dict[str, List[int]] = {}
        for snapshot in self._raw_proposed_history:
            for srv, proposed in snapshot:
                per_server.setdefault(srv, []).append(proposed)

        min_history = max(COLONY_BIAS_MIN_HISTORY,
                          HISTORY_MAX_LEN // COLONY_BIAS_HISTORY_DIV)

        stats: Dict[str, Tuple[float, float]] = {}
        for srv, vals in per_server.items():
            if len(vals) < min_history:
                continue
            # mean by levels: contains the drift of the reference, but it is common to all
            # servers and cancels out in delta = mean_s − median_mean.
            mean_s = statistics.mean(vals)
            # sigma by differences of adjacent rounds: removes the reference drift,
            # leaving pure server jitter.
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
                # initial synchronization has not yet happened — do not panic
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
                    f"Check DNS/getaddrinfo and NTP server availability."
                )

            if stale_sec >= hard_limit_sec:
                # External supervisor (systemd/supervisor) will restart the process.
                # os._exit does not run atexit/join — needed here.
                logger.critical(
                    f"Synchronization does not recover for {stale_sec:.1f}s — "
                    f"emergency exit for restart by supervisor"
                )
                #os._exit(1)

    def _keep_awake_worker(self) -> None:
        """Keep the system running while the service is alive.

        Windows:
            SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) —
            affects the entire process, blocks Modern Standby. The flag
            is periodically re-set: some Windows builds
            reset it when exiting idle. On stop() the flag is cleared.

        Linux:
            From user-space suspend cannot be blocked. The only correct
            way — mask systemd sleep targets as root:
                systemctl mask sleep.target suspend.target \\
                    hibernate.target hybrid-sleep.target
            Here we only check this once at start and write CRITICAL
            if not masked. Then the thread finishes — keeping it
            alive makes no sense.

        Other OS — no-op.
        """
        # --- Linux: one-time check ---
        if sys.platform.startswith("linux"):
            problem = self._linux_suspend_not_masked()
            if problem is None:
                self._defer_log(
                    "info",
                    "keep-awake: Linux — sleep targets masked, "
                    "suspend blocked at the system level",
                )
            else:
                self._defer_log(
                    "critical",
                    f"keep-awake: Linux — suspend NOT blocked ({problem}). "
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
        # HANDLE and lead to call failure without a visible error.
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
        # Without this, Windows classifies the background process as "unimportant",
        # moves it to E-cores and limits CPU share. Symptom in logs:
        # rounds with duration 36/66/120/240 seconds instead of 4, round multiples
        # of sync_interval, both sync threads finish in the same millisecond,
        # watchdog is also silent for hours.

        try:
            PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
            PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
            PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION = 0x4
            ProcessPowerThrottling = 4

            class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
                # Explicit 4-byte alignment so that the size is exactly 12
                # and matches the C-ABI (three ULONGs).
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
            # and will try a simpler variant.
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
                    # A real error (not "flag not supported") —
                    # no point in trying other variants.
                    err_text = ctypes.FormatError(err).strip() if err else "(no code)"
                    self._defer_log(
                        "warning",
                        f"keep-awake: SetProcessInformation failed "
                        f"at {desc} (GetLastError={err} {err_text})",
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
                    "ES_SYSTEM_REQUIRED) applied."
                )
        except Exception as e:
            self._defer_log(
                "warning",
                f"keep-awake: exception while disabling Power Throttling: {e}",
            )

        # --- ABOVE_NORMAL priority ---
        # Not HIGH, to avoid taking CPU from user tasks, but
        # sufficient for the scheduler not to defer the process behind other
        # background ones. In a separate try/except: if it fails, keep-awake
        # must still be set, otherwise Modern Standby returns.
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

        # --- SetThreadExecutionState: Modern Standby block ---
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
            None  — everything is fine, suspend is blocked;
            str   — description of the problem (what is not masked / systemctl
                    is unavailable / call failed).

        Masking (`systemctl mask sleep.target ...`) is the only way
        from user-space without root to prevent suspend. From the application code
        this cannot be done: privileges are required, so we only check and
        warn once at start.
        """
        import shutil
        import subprocess
        # NOTE: shutil.which('systemctl') — pass str, not PathLike.
        # PyCharm inspector may complain about the PathLike case from Python < 3.12
        # on Windows, but it is not applicable here: the code runs only on Linux.
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
                return f"failed to run systemctl show {t}: {e}"
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
        """Upper bound of |utc − utc_true| inside TTL.

        Error model:
            err(TTL) ≈ σ_ref + δ_rate · TTL
        where:
            σ_ref   — long-term consensus noise (median σ_avg), ns,
            δ_rate  — rate extrapolation error, dimensionless (ns/ns),
                      estimated via σ_recent / sync_interval_ns.

        Returns int in ns.
        """
        sigma_ref = self._consensus._noise_ref_ns or 0
        recent = list(self._consensus._applied_offsets)[:CLOCK_SNAPSHOT_TTL_RECENT_N]
        if len(recent) < 3:
            return max(int(sigma_ref), CLOCK_SNAPSHOT_ACCURACY_DEFAULT_NS)

        sigma_recent = statistics.stdev(recent)
        # sync_interval in seconds → in ns, so that δ_rate is dimensionless.
        sync_interval_ns = self.sync_interval * 1_000_000_000
        delta_rate = sigma_recent / max(sync_interval_ns, 1)
        err = sigma_ref + delta_rate * ttl_ns
        return int(err)

    def _ttl_for_precision_locked(self, precision_ns: int) -> Tuple[int, int]:
        """TTL at which accuracy_ns ≤ precision_ns.

        Returns (ttl_ns, accuracy_ns), both in ns.
        If σ_ref is already > precision_ns — the accuracy is unattainable,
        return the minimal TTL with the actual accuracy_ns = σ_ref.
        """
        sigma_ref = self._consensus._noise_ref_ns or 0
        abs_min_ns = int(CLOCK_SNAPSHOT_TTL_ABS_MIN_SEC * 1_000_000_000)
        abs_max_ns = int(CLOCK_SNAPSHOT_TTL_ABS_MAX_SEC * 1_000_000_000)

        recent = list(self._consensus._applied_offsets)[:CLOCK_SNAPSHOT_TTL_RECENT_N]
        if len(recent) < 3 or sigma_ref >= precision_ns:
            return abs_min_ns, int(sigma_ref)

        sigma_recent = statistics.stdev(recent)
        sync_interval_ns = self.sync_interval * 1_000_000_000
        delta_rate = sigma_recent / max(sync_interval_ns, 1)
        if delta_rate <= 0:
            return abs_max_ns, int(sigma_ref)

        budget = precision_ns - sigma_ref
        ttl_ns = int(budget / delta_rate)
        ttl_ns = max(abs_min_ns, min(abs_max_ns, ttl_ns))
        actual = int(sigma_ref + delta_rate * ttl_ns)
        return ttl_ns, actual

# Global instance
time_sync_service = TimeSyncService.get_instance()