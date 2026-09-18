# app.py (version 0.4.3)
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import dash
from dash import dcc, html, Input, Output, State, ALL, MATCH, no_update, callback_context
import os
import json
from core.database import DATA_DIR, cleanup_orphan_databases, update_bot_config
import atexit
import signal
import logging
import statistics
from datetime import datetime, timezone

from core import (
    load_modules, init_config_db, add_bot, get_all_bots, get_bot_config,
    update_bot_status, delete_bot, get_setting, save_setting,
    BotManager, bot_registry, perf_logger, colors
)

from core.logger import LOGGER_OBJS, LOGGER_LEVELS, LOG_RETENTION_DAYS_DEFAULT
from core.time_sync import (
    time_sync_service,
    SIGMA_WARMUP_RECORDS,
    SIGMA_WARMUP_TIMEOUT_MULT,
    DOMINANT_STDEV_RATIO,
    SPREAD_HISTORY_MIN,
    REPRODUCTION_SPREAD_MULT,
    HISTORY_VOTE_SHORT_WINDOW,
    HISTORY_VOTE_MIN_SAMPLES,
    DRIFT_MIN_SAMPLES,
    HISTORY_MAX_LEN,
    DOMINANT_MIN_SERVERS,
    WARMUP_MIN_SURVIVORS,
    DOMINANT_MIN_DEVS,
    PER_SERVER_HISTORY
)

class SettingsStorage:
    @staticmethod
    def get_setting(key, default=None):
        return get_setting(key, default)

    @staticmethod
    def save_setting(key, value):
        save_setting(key, value)

os.makedirs(DATA_DIR, exist_ok=True)
init_config_db()
cleanup_orphan_databases()
perf_logger.initialize_with_storage(SettingsStorage)
logger = perf_logger.get_logger('app', 'app')

# ---------------------------------------------------------------------------
# WARNING: sleep control / Modern Standby
# ---------------------------------------------------------------------------
# TimeSyncService.start() with KEEP_AWAKE_ENABLED=1 (default) calls
# Windows API SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED).
# This affects the entire process and prevents the system from entering
# Modern Standby while the service is alive. Without it, all application
# threads — including the asyncio loop of BotManager and trading bots —
# freeze for hours.
#
# OPERATING ENVIRONMENT REQUIREMENTS:
#
#   * Windows (desktop): Modern Standby should be disabled at the OS level
#     to ensure that keep-awake is not ignored by drivers:
#         powercfg -h off
#         powercfg -change -standby-timeout-ac 0
#         powercfg -change -monitor-timeout-ac 0
#         powercfg -change -disk-timeout-ac 0
#     and in the registry HKLM\SYSTEM\CurrentControlSet\Control\Power:
#         PlatformAoAcOverride = 0 (DWORD, reboot required)
#
#   * Laptops: NOT RECOMMENDED. SetThreadExecutionState keeps the system
#     running around the clock — the battery drains in a matter of hours.
#     For a laptop either use TIME_SYNC_KEEP_AWAKE=0 (accepting the
#     consequences in the form of nightly synchronization pauses).
#
#   * Linux / VPS: preferred environment. Modern Standby as a class does
#     not exist, suspend is disabled via systemd (`systemctl mask sleep.target
#     suspend.target hibernate.target hybrid-sleep.target`). keep-awake
#     becomes a no-op.
#
# T.B.O.T FACTORY (plan):
#   When TimeSyncService is moved to a separate process/service on the node
#   (single one for all applications), sleep control should move along with it.
#   This app.py will stop calling SetThreadExecutionState directly —
#   the factory will take over:
#       - keep-awake for the entire node;
#       - healthcheck of the time process;
#       - automatic restart after prolonged pauses;
#       - graceful shutdown with state preservation (bias, applied_offsets,
#         anchor/rate) before restart.
#   Until that moment, app.py remains the sleep-control point for the entire node.
#   Ensure that time_sync_service.start() is called BEFORE starting
#   BotManager and any trading modules, otherwise bots may manage to
#   execute in the window before ES_SYSTEM_REQUIRED is set.
# ---------------------------------------------------------------------------
time_sync_service.start()
if not time_sync_service.wait_for_first_sync(timeout=5):
    logger.error("Failed to restore initial time synchronization.")
else:
    logger.info(f"Precise time synchronization completed ts={time_sync_service.get_utc_ns}.")
    perf_logger.set_clock(time_sync_service.get_utc_ns)

load_modules("modules")

def shutdown_time_sync():
    time_sync_service.stop()
atexit.register(shutdown_time_sync)

bot_manager = BotManager()
bot_manager.start_loop_in_thread()
atexit.register(bot_manager.shutdown)
signal.signal(signal.SIGINT, lambda s, f: bot_manager.shutdown())
signal.signal(signal.SIGTERM, lambda s, f: bot_manager.shutdown())
bot_manager.load_bots()

app = dash.Dash(__name__, title='T.B.O.T', update_title=None)
app.config.suppress_callback_exceptions = True

# Callback registration uses bot_manager.loop
for model_name in bot_registry.list_models():
    if model_name.endswith('.type'):
        meta_cls = bot_registry.get_model(model_name)
        if hasattr(meta_cls, 'register_callbacks'):
            meta_cls.register_callbacks(app, bot_manager, bot_manager.loop)

def build_time_content(n):
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

    def ns_to_str(ns):
        return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)\
                       .strftime('%Y-%m-%d %H:%M:%S.%f')[:-3] + " UTC"

    def ts_to_hms(ts_ns):
        return datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)\
                       .strftime('%H:%M:%S.%f')[:-3]

    def format_ms(value_ns):
        if value_ns is None:
            return "N/A"
        return f"{value_ns / 1e6:.6f} ms"

    sys_str     = ns_to_str(sys_ns)
    precise_str = ns_to_str(precise_ns)

    offset_spread = format_ms(offset_spread_ns)
    ntp_spread    = format_ms(ntp_spread_ns)
    offset_ms     = format_ms(offset_ns)

    if slew_error_ns == 0 and offset_spread_ns is not None:
        slew_error = "compensated"
    elif slew_error_ns == 0:
        slew_error = "N/A"
    else:
        slew_error = format_ms(slew_error_ns)

    diff_threshold_str = (f"±{format_ms(diff_threshold_ns)}"
                          if diff_threshold_ns is not None else "N/A")

    # ---- Render per-server slice from structured tuple ----
    def fmt_servers(servers):
        if not servers:
            return "—"
        items = sorted(servers, key=lambda x: abs(x[2]))
        return ", ".join(
            f"{mark}{name}:{dev / 1e6:+.3f}ms/{delay / 1e6:.1f}ms"
            for name, _proposed, dev, delay, mark in items
        )

    def fmt_threshold(thr_ns):
        return f"thr ±{thr_ns / 1e6:.6f} ms" if thr_ns is not None else "thr N/A"

    def fmt_record(rec):
        inst_mark = (f"[inst {rec.instance_id}]" if rec.instance_id is not None
                     else "[consensus]")
        fav_mark = f" fav={rec.favorite}" if rec.favorite else ""
        if rec.diff_ns is None:
            diff_str = "Δ cold-start"
        else:
            diff_str = f"Δ {rec.diff_ns / 1e6:+.6f} ms"
        return (f"{inst_mark} [{ts_to_hms(rec.timestamp_ns)}] "
                f"{diff_str} ({fmt_threshold(rec.threshold_ns)}){fav_mark} "
                f"{fmt_servers(rec.servers)}")

    def render_history(records, limit=HISTORY_MAX_LEN):
        if not records:
            return "      (empty)"
        return "\n".join("      " + fmt_record(rec) for rec in records[:limit])

    def render_dominant_analysis(inst, current_tick):
        """
        Per-server statistics over ✓ records of the accepted history.
        X = current favorite; others = all other servers of the round.
        M is taken from telemetry (computed by the core), Δ and N are derived.
        """
        hist = inst.get('own_history') or []
        cur_fav = inst.get('favorite')
        M = inst.get('dominant_M')
        sigma_avg = inst.get('sigma_avg_ns')
        thr_ns = inst.get('threshold_ns')
        lock_until = inst.get('armed_lock_until_tick', -10 ** 9)

        lines = []

        # --- Lock ---
        if current_tick < lock_until:
            lines.append(
                f"      armed-lock active: {lock_until - current_tick} ticks left "
                f"(until tick {lock_until})"
            )

        # --- Conditions for M ---
        if thr_ns is None or thr_ns <= 0:
            lines.append("      → not armed: threshold not established yet")
            return "\n".join(lines)
        if sigma_avg is None or sigma_avg <= 0:
            warmup_exc = inst.get('warmup_excluded') or []
            warmup_start = inst.get('warmup_started_tick')
            # Available = everything from the last record, minus excluded ones.
            last = hist[0] if hist else None
            avail = [n for n, *_ in (last.servers or ())] if last else []
            avail = [n for n in avail if n not in warmup_exc]

            grace_left = None
            if warmup_start is not None:
                grace_left = (warmup_start + SIGMA_WARMUP_TIMEOUT_MULT * SIGMA_WARMUP_RECORDS
                              - current_tick)

            # ✓ counters per each available server
            counts: dict = {}
            for rec in hist:
                if rec.is_cold_start or not rec.servers:
                    continue
                for name, _p, _d, _dl, mark in rec.servers:
                    if mark == '✓':
                        counts[name] = counts.get(name, 0) + 1

            lines.append(
                f"      → warming up: σ_avg not captured "
                f"(need {SIGMA_WARMUP_RECORDS} ✓/srv, "
                f"grace left: {grace_left if grace_left is None else max(grace_left, 0)} ticks)"
            )
            if len(avail) < WARMUP_MIN_SURVIVORS:
                lines.append(
                    f"      rule 1: only {len(avail)} available "
                    f"(< {WARMUP_MIN_SURVIVORS}), stay in warmup"
                )
            for name in avail:
                c = counts.get(name, 0)
                mark = "" if c >= SIGMA_WARMUP_RECORDS else f" (need {SIGMA_WARMUP_RECORDS - c} more)"
                lines.append(f"      {name}: {c}/{SIGMA_WARMUP_RECORDS}{mark}")
            if warmup_exc:
                lines.append(f"      permanently excluded: {', '.join(warmup_exc)}")
            return "\n".join(lines)

        if M is None:
            lines.append(f"      → not armed: M undefined (N<{DOMINANT_MIN_SERVERS} or no history)")
            return "\n".join(lines)

        last = hist[0] if hist else None
        N = len(last.servers) if (last and last.servers) else 0
        delta = sigma_avg / thr_ns
        lines.append(
            f"      M={M}  σ_avg={sigma_avg / 1e6:.3f} ms  "
            f"thr=±{thr_ns / 1e6:.3f} ms  Δ={delta:.3f}  N={N}"
        )

        # --- Collect dev_ns per server from ✓ records ---
        per_srv: dict = {}
        for rec in hist:
            if rec.is_cold_start or not rec.servers:
                continue
            for name, _proposed, dev, _delay, mark in rec.servers:
                if mark == '✓':
                    per_srv.setdefault(name, []).append(dev)

        if not per_srv:
            lines.append("      (no ✓ server data yet)")
            return "\n".join(lines)

        # --- Breakdown by server: n, σ ---
        groups = []
        for name, devs in per_srv.items():
            sd = statistics.stdev(devs) if len(devs) >= 2 else None
            groups.append((name, len(devs), sd))
        groups.sort(key=lambda g: (g[0] != cur_fav, -g[1]))

        for name, cnt, sd in groups:
            sd_str = f"σ={sd / 1e6:.3f} ms" if sd is not None else "σ=N/A"
            mark = "  ← current" if name == cur_fav else ""
            if name == cur_fav:
                progress = (f"✓ {cnt}/{M}" if cnt >= M
                            else f"{cnt}/{M} (need {M - cnt} more)")
            else:
                progress = f"{cnt}"
            lines.append(f"      {name}: {progress}, {sd_str}{mark}")

        # --- Verdict ---
        if cur_fav is None or cur_fav not in per_srv:
            lines.append("      → not armed: current favorite absent in ✓ history")
            return "\n".join(lines)

        on_X = per_srv[cur_fav]
        others = [d for n, dl in per_srv.items() if n != cur_fav for d in dl]

        if len(on_X) < M:
            lines.append(f"      → not armed: need {M - len(on_X)} more on X")
        elif len(others) < M:
            lines.append(f"      → not armed: only {len(others)}/{M} on others")
        elif (len(on_X) < DOMINANT_MIN_DEVS or len(others) < DOMINANT_MIN_DEVS):
            lines.append(
                f"      → not armed: insufficient data for stdev "
                f"(need ≥{DOMINANT_MIN_DEVS} on X and others)"
            )
        else:
            sx = statistics.stdev(on_X)
            so = statistics.stdev(others)
            if so == 0:
                lines.append("      → not armed: σ_others=0")
            else:
                ratio = sx / so
                verdict = "ARMED" if ratio < DOMINANT_STDEV_RATIO else "not dominant"
                lines.append(
                    f"      → {verdict}: σ_X/σ_others={ratio:.3f} "
                    f"(need <{DOMINANT_STDEV_RATIO})"
                )
        return "\n".join(lines)

    all_refs = [i.get('reference_offset_ns') for i in instances.values()
                if i.get('reference_offset_ns') is not None]
    median_ref = int(statistics.median(all_refs)) if all_refs else None

    consensus_history_str = render_history(slew_errors_ns)

    # ---- Bias (bias accumulated in TimeSyncService) ----
    if not colony_bias_ns:
        colony_bias_str = "      (empty — history not full yet)"
    else:
        lines = []
        # Sort by ascending absolute bias value
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

    if per_server_delay:
        lines = []
        for srv, st in sorted(per_server_delay.items(),
                              key=lambda kv: (kv[1].get('jitter_ns') or 0)):
            med = st.get('median_ns')
            jit = st.get('jitter_ns') or 0
            n = st.get('n', 0)
            med_str = f"{med / 1e6:7.3f}" if med is not None else "    N/A"
            lines.append(
                f"      {srv:<30s} : med {med_str} ms, "
                f"jitter {jit / 1e6:7.3f} ms (n={n})"
            )
        per_server_delay_str = "\n".join(lines)
    else:
        per_server_delay_str = "      (empty — no successful rounds yet)"

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
            lines.append(
                f"      drift slope                 : "
                f"{slope_us:+.3f} μs/round"
                f"{ppm:+.3f} ppm" if ppm is not None else "ppm N/A"
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
        hist_len = gate.get('history_len', 0)
        hist_min = gate.get('history_min', SPREAD_HISTORY_MIN)
        med_sp = gate.get('median_spread_ns')
        noise_r = gate.get('noise_ref_ns')
        thr_ns = gate.get('threshold_ns')
        ratio = gate.get('ratio')

        if med_sp is None or noise_r is None:
            gate_str = (
                f"reproduction gate                      : allowed (warming up — "
                f"history {hist_len}/{hist_min})"
            )
        else:
            gate_str = (
                f"reproduction gate                    : "
                f"{'allowed' if gate_allowed else 'blocked'}\n"
                f"  median spread (last {hist_len:>2})   : {format_ms(med_sp)}\n"
                f"  noise ref (median σ)                 : {format_ms(noise_r)}\n"
                f"  threshold ({REPRODUCTION_SPREAD_MULT}·σ_ref)               : {format_ms(thr_ns)}\n"
                f"  ratio                                : "
                f"{ratio:.3f}  (need < 1.0)"
            )

        colony_str = (
            f"instances alive           : {population.get('population')}\n"
            f"  current tick              : {population.get('tick')}\n"
            f"  {gate_str}\n"
            f"  offset spread (stdev)     : "
            f"{format_ms(spread_ns) if spread_ns is not None else 'N/A'}\n"
            f"  favorites (per instance)  : "
            f"[{', '.join(str(f) if f else '—' for f in favorites) or '—'}]\n"
            f"  occupied servers          : "
            f"[{', '.join(occupied) or '—'}]"
        )

    # ---- Block per instance ----
    instance_blocks = []
    current_tick = population.get('tick', 0)
    for inst_id in sorted(instances.keys()):
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

        if ref_ns is not None and median_ref is not None:
            ref_str = f"Δ {(ref_ns - median_ref) / 1e6:+.6f} ms vs colony median"
        elif ref_ns is not None:
            ref_str = f"{ref_ns / 1e9:.3f} s (absolute)"
        else:
            ref_str = "N/A (cold start)"

        sigma_avg = inst.get('sigma_avg_ns')
        dominant_M = inst.get('dominant_M')
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

        header = (
            f"Instance {inst_id}\n"
            f"  favorite server           : {inst.get('favorite') or '—'}\n"
            f"{sel_line}"
            f"  reference offset          : {ref_str}\n"
            f"  filter threshold          : "
            f"{'±' + format_ms(thr_ns) if thr_ns is not None else 'N/A'}\n"
            f"  σ_avg (captured)          : "
            f"{format_ms(sigma_avg) if sigma_avg is not None else 'N/A (warming up)'}\n"
            f"  dominant M                : {dominant_M if dominant_M is not None else 'N/A'}\n"
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

    return (
        f"Interval: {n}\n"
        f"Spread of per-server min delay (mixed servers, per-round min-of-5): {ntp_spread}\n"
        f"\nConsensus history vote & drift:\n"
        f"{history_vote_str}\n"
        f"Precise time (T.B.O.T time): {precise_str}\n"
        f"System time (OS time): {sys_str}\n"
        f"Current OS time offset, precise time - system time = {offset_ms}\n"
        f"Current T.B.O.T time offset, precise time - NTP time = {slew_error}\n"
        f"Estimated clock rate vs UTC: "
        f"{f'{rate_ppm:+.3f} ppm' if rate_ppm is not None else 'N/A'}\n"
        f"Last phase error (residual): "
        f"{format_ms(phase_error_ns) if phase_error_ns is not None else 'N/A'}\n"
        f"Standard deviation of T.B.O.T time offsets: {offset_spread}\n"
        f"Median filter threshold across colony: {diff_threshold_str}\n"
        f"\nColony bias (accumulated, applied to raw proposed):\n"
        f"{colony_bias_str}\n"
        f"\nColony noise estimate (robust: 1.4826·MAD(first diffs)/√2, active only):\n"
        f"{colony_noise_str}\n"
        f"\nPer-server min delay (last 10 rounds each, mixed servers):\n"
        f"{per_server_delay_str}\n"
        f"\nColony state:\n"
        f"  {colony_str}\n"
        f"\nConsensus history (applied rounds):\n"
        f"{consensus_history_str}\n"
        f"\nPer-instance telemetry:\n"
        f"{instances_str}"
    )

app.layout = html.Div([
    html.Div([
        html.Details([
            html.Summary([
                html.Span('T.B.O.T', className='bot-title'),
                html.Span('', className='header-indicator'),
            ]),
            html.Div([
                html.Hr(),

                # Buttons left to right
                html.Div([
                    html.Button('➕', id='add-bot-btn', n_clicks=0, style={'marginRight': '10px'}),
                    html.Button('⚙️', id='settings-btn', n_clicks=0, style={'marginRight': '10px'}),
                    html.Button('📋', id='logs-btn', n_clicks=0, style={'marginRight': '10px'}),
                    html.Button('⏱', id='time-btn', n_clicks=0, style={'marginRight': '10px'}),
                ], style={'display': 'flex', 'flexDirection': 'row', 'justifyContent': 'flex-start'}),

                # Add bot form
                html.Div(id='add-bot-form-container', children=[
                    html.Div(id='dynamic-bot-form-content'),
                    html.Button('Save', id='save-bot-btn', style={'marginRight': '10px'}),
                    html.Button('Cancel', id='cancel-add-btn')
                ], style={'display': 'none', 'textAlign': 'left'}),

                # Settings panel
                html.Div(id='settings-panel', children=[
                    html.H3('Settings'),
                    html.Div([
                        html.Label('Debug mode'),
                        dcc.Checklist(id='debug-checkbox', options=[{'label': ' Enable', 'value': 'debug'}],
                                      value=['debug'] if get_setting('debug_mode', 'False') == 'True' else []),
                        html.Div('* Changes will take effect after restart', style={'fontSize': 'small', 'color': colors.GRAY})
                    ], style={'marginBottom': '20px'}),
                    html.H4('Delete Old Logs'),
                    html.Div([
                        html.Label('Delete logs older than (days):'),
                        dcc.Input(id='log-retention-days', type='number', min=0, max=365,
                                  value=int(get_setting('log_retention_days', LOG_RETENTION_DAYS_DEFAULT))),
                        html.Div('0 = disable automatic deletion',
                                 style={'fontSize': 'small', 'color': colors.GRAY, 'marginTop': '5px'}),
                    ], style={'marginBottom': '20px'}),
                    html.H4('Logging levels'),
                    html.Div(id='logging-levels-container', children=[
                        html.Div([
                            html.Label(module),
                            dcc.Dropdown(id={'type': 'log-level-dropdown', 'module': module},
                                         options=[{'label': lvl, 'value': lvl} for lvl in LOGGER_LEVELS],
                                         value=perf_logger.get_level(module))
                        ], style={'marginBottom': '10px'}) for module in LOGGER_OBJS
                    ]),
                    html.Button('Save Settings', id='save-settings-btn'),
                    html.Button('Close', id='close-settings-btn')
                ], style={'display': 'none', 'textAlign': 'left', 'border': f'1px solid {colors.BLACK}', 'padding': '10px', 'margin': '10px 0'}),

                # Logs panel
                html.Div(id='logs-panel', children=[
                    html.H4('Recent Logs', style={'margin': '0 0 5px 0'}),  # reduce margin under heading
                    html.Pre(id='logs-content', children='', style={
                        'maxHeight': '1000px',
                        'overflowY': 'auto',
                        'backgroundColor': '#f8f8f8',
                        'padding': '5px',  # was 10px
                        'fontSize': '12px',
                        'whiteSpace': 'pre-wrap',
                        'lineHeight': '1.2',  # line spacing (default was ~1.4)
                    }),
                ], style={
                    'display': 'none',
                    'textAlign': 'left',
                    'border': f'1px solid {colors.GRAY}',
                    'padding': '5px',  # was 10px
                    'margin': '5px 0',  # top/bottom margins to adjacent blocks 5px
                }),

                # Time info panel
                html.Div(id='time-panel', children=[
                    html.H4('Time Information', style={'margin': '0 0 5px 0'}),
                    html.Pre(id='time-content', children='', style={
                        'maxHeight': '1000px',
                        'overflowY': 'auto',
                        'backgroundColor': '#f8f8f8',
                        'padding': '5px',
                        'fontSize': '12px',
                        'whiteSpace': 'pre-wrap',
                        'lineHeight': '1.2',
                    }),
                ], style={
                    'display': 'none',
                    'textAlign': 'left',
                    'border': f'1px solid {colors.GRAY}',
                    'padding': '5px',
                    'margin': '5px 0',
                }),

                # Stores and Location
                dcc.Store(id='header-mood-command', data=None),   # logo command
                dcc.Store(id='header-mood', data='normal'),
                dcc.Store(id='bots-trigger', data=0),
                dcc.Store(id='relayout-store', data={}),
                dcc.Store(id='editing-bots', data={}),
                dcc.Store(id='prev-edit-clicks', data=[]),
                dcc.Location(id='url', refresh=False),
            ])
        ], id='sticky-header-details', open=True, className='', style={
            'width': '100%',
        }),
    ], id='sticky-header', style={
        'position': 'sticky',
        'top': '0',
        'background': colors.WHITE,
        'zIndex': '1000',
        'padding': '2px 6px',
        'boxShadow': f'0 1px 3px {colors.BLACK_SHADOW}',
    }),

    html.Div(id='bots-container'),
    dcc.Interval(id='tick-1s', interval=1000, n_intervals=0),
    dcc.Interval(id='global-interval', interval=5000, n_intervals=0),
    dcc.Interval(id='header-mood-reset-interval', interval=1000, max_intervals=1, disabled=True),
])

# ------------ header mood and log callbacks --------
@app.callback(
    Output('sticky-header-details', 'className'),
    Input('header-mood', 'data')
)
def set_header_mood_class(mood):
    # Convert Store value to CSS class
    return f'mood-{mood}' if mood and mood != 'normal' else ''

@app.callback(
    [Output('header-mood', 'data', allow_duplicate=True),
     Output('header-mood-reset-interval', 'disabled', allow_duplicate=True)],
    Input('header-mood-reset-interval', 'n_intervals'),
    prevent_initial_call=True
)
def reset_header_mood(n_intervals):
    if n_intervals == 0:
        # not yet a real tick – wait
        return no_update, no_update
    return 'normal', True

@app.callback(
    [Output('header-mood', 'data', allow_duplicate=True),
    Output('header-mood-reset-interval', 'disabled', allow_duplicate=True),
    Output('header-mood-reset-interval', 'n_intervals', allow_duplicate=True),
    Output('header-mood-command', 'data', allow_duplicate=True)],
    Input('header-mood-command', 'data'),
    prevent_initial_call=True
)
def process_mood_command(command):
    if command is None:
        return no_update, no_update, no_update, None
    # Apply command and clear it
    return command, False, 0, None

@app.callback(
    [Output('add-bot-form-container', 'style'),
     Output('dynamic-bot-form-content', 'children'),
     Output('settings-panel', 'style'),
     Output('add-bot-btn', 'n_clicks'),
     Output('settings-btn', 'n_clicks'),
     Output('logs-panel', 'style'),
     Output('logs-content', 'children', allow_duplicate=True),
     Output('logs-btn', 'n_clicks'),
     Output('time-panel', 'style'),
     Output('time-btn', 'n_clicks'),
     Output('time-content', 'children', allow_duplicate=True)],
    [Input('add-bot-btn', 'n_clicks'),
     Input('settings-btn', 'n_clicks'),
     Input('logs-btn', 'n_clicks'),
     Input('time-btn', 'n_clicks'),               # new
     Input('cancel-add-btn', 'n_clicks'),
     Input('close-settings-btn', 'n_clicks'),
     Input('save-bot-btn', 'n_clicks')],
    prevent_initial_call=True
)
def toggle_forms(add_clicks, settings_clicks, logs_clicks, time_clicks,
                 cancel_clicks, close_clicks, save_clicks):
    ctx = callback_context
    if not ctx.triggered:
        return (no_update,) * 11

    triggered_id = ctx.triggered[0]['prop_id'].split('.')[0]

    # Initialize all values
    add_style = {'display': 'none'}
    settings_style = {'display': 'none'}
    logs_style = {'display': 'none'}
    time_style = {'display': 'none'}
    form_content = no_update
    logs_content = no_update
    time_content = no_update
    new_add = add_clicks
    new_settings = settings_clicks
    new_logs = logs_clicks
    new_time = time_clicks

    # Button handling
    if triggered_id == 'add-bot-btn':
        if add_clicks % 2 == 1:
            add_style = {'display': 'block'}
            new_settings = 0
            new_logs = 0
            new_time = 0
            type_options = []
            for model_name in bot_registry.list_models():
                if model_name.endswith('.type'):
                    cls = bot_registry.get_model(model_name)
                    display = getattr(cls, 'display_name', model_name)
                    type_id = model_name.split('.')[0]
                    type_options.append({'label': display, 'value': type_id})
            if not type_options:
                form_content = html.Div("No bot types registered. Check modules.")
            else:
                form_content = html.Div([
                    html.H3('Add Bot'),
                    dcc.Dropdown(id='bot-type-selector', options=type_options, value=type_options[0]['value']),
                    html.Div(id='dynamic-bot-form')
                ])
        else:
            add_style = {'display': 'none'}
        new_add = add_clicks

    elif triggered_id == 'settings-btn':
        if settings_clicks % 2 == 1:
            settings_style = {'display': 'block'}
            new_add = 0
            new_logs = 0
            new_time = 0
        else:
            settings_style = {'display': 'none'}
        new_settings = settings_clicks

    elif triggered_id == 'logs-btn':
        if logs_clicks % 2 == 1:
            logs_style = {'display': 'block'}
            new_add = 0
            new_settings = 0
            new_time = 0
            logs_content = '\n'.join(perf_logger.get_recent_logs('all', 20))
        else:
            logs_style = {'display': 'none'}
        new_logs = logs_clicks

    elif triggered_id == 'time-btn':
        if time_clicks % 2 == 1:
            time_style = {'display': 'block'}
            new_add = 0
            new_settings = 0
            new_logs = 0
            time_content = build_time_content(0)  # fill immediately
        else:
            time_style = {'display': 'none'}
        new_time = time_clicks

    # Closing forms
    elif triggered_id in ['cancel-add-btn', 'save-bot-btn']:
        add_style = {'display': 'none'}
        new_add = 0

    elif triggered_id == 'close-settings-btn':
        settings_style = {'display': 'none'}
        new_settings = 0

    return (add_style, form_content, settings_style, new_add, new_settings,
            logs_style, logs_content, new_logs,
            time_style, new_time, time_content)

@app.callback(
    Output('dynamic-bot-form', 'children'),
    Input('bot-type-selector', 'value')
)
def update_dynamic_form(bot_type):
    if not bot_type:
        return html.Div("Select a bot type")
    meta_cls = bot_registry.get_model(f"{bot_type}.type")
    if not meta_cls or not hasattr(meta_cls, 'form_component'):
        return html.Div(f"Form for type '{bot_type}' not found")
    try:
        return meta_cls.form_component(current_bot_id=None)
    except TypeError:
        return meta_cls.form_component()
    except Exception as e:
        logger.error(f"Error rendering form: {e}")
        return html.Div(f"Error loading form: {e}")

@app.callback(
    [Output('bots-trigger', 'data', allow_duplicate=True),
     Output('editing-bots', 'data', allow_duplicate=True)],
    Input('save-bot-btn', 'n_clicks'),
    [State('bot-type-selector', 'value'),
     State({'type': ALL, 'field': ALL}, 'value'),
     State({'type': ALL, 'field': ALL}, 'id'),
     State('bots-trigger', 'data')],
    prevent_initial_call=True
)
def save_bot(n_clicks, bot_type, field_values, field_ids, trigger):
    if not n_clicks or not bot_type:
        return no_update, no_update

    config = {}
    for val, id_dict in zip(field_values, field_ids):
        field = id_dict.get('field')
        if field:
            config[field] = val

    meta_cls = bot_registry.get_model(f"{bot_type}.type")
    if meta_cls and hasattr(meta_cls, 'prepare_new_config'):
        config = meta_cls.prepare_new_config(config)

    bot_id = add_bot(bot_type, f"{bot_type} bot", config)
    bot_manager.add_bot(bot_id)
    perf_logger.set_mood('happy')
    return trigger + 1, {}

@app.callback(
    Output('bots-container', 'children'),
    [Input('bots-trigger', 'data'),
     Input('editing-bots', 'data'),
     Input('url', 'pathname')],
    [State('relayout-store', 'data')]
)
def render_bots(trigger, editing_bots, pathname, relayout_store):
    bots = get_all_bots()
    if not bots:
        return html.Div('No active bots. Click "+" to add one.')

    editing_bots = editing_bots or {}
    bot_blocks = []
    for bot in bots:
        bot_id = bot['id']
        bot_type = bot['type']
        meta_cls = bot_registry.get_model(f"{bot_type}.type")
        if not meta_cls:
            continue
        config = get_bot_config(bot_id)
        if not config:
            continue
        config['status'] = bot['status']

        if editing_bots.get(str(bot_id)):
            # Edit mode
            if hasattr(meta_cls, 'form_component'):
                try:
                    form = meta_cls.form_component(current_bot_id=bot_id)
                except TypeError:
                    form = meta_cls.form_component()
                except Exception as e:
                    form = html.Div(f"Error loading form: {e}")
            else:
                form = html.Div("Edit form not available for this type.")

            edit_block = html.Div([
                html.H4(f"Editing {config.get('name', f'Bot {bot_id}')}"),
                form,
                html.Button('Save', id={'type': 'edit-save-btn', 'index': bot_id}, style={'marginRight': '10px'}),
                html.Button('Cancel', id={'type': 'edit-cancel-btn', 'index': bot_id})
            ], style={'border': f'1px solid {colors.GRAY}', 'padding': '10px', 'margin': '10px 0'})
            bot_blocks.append(html.Div(edit_block, id={'type': 'bot-card', 'index': bot_id}, key=str(bot_id)))
        else:
            # Normal mode
            if hasattr(meta_cls, 'render_block'):
                block = meta_cls.render_block(bot_id, config, relayout_store)
                bot_blocks.append(html.Div(block, id={'type': 'bot-card', 'index': bot_id}, key=str(bot_id)))
            else:
                bot_blocks.append(html.Div(
                    f"Bot {bot_id} ({bot_type}) - no render_block",
                    id={'type': 'bot-card', 'index': bot_id}, key=str(bot_id)
                ))

    return bot_blocks

@app.callback(
    Output('relayout-store', 'data'),
    Input({'type': 'graph', 'index': ALL}, 'relayoutData'),
    State('relayout-store', 'data'),
    prevent_initial_call=True
)
def save_relayout(relayout_list, stored):
    ctx = callback_context
    if not ctx.triggered:
        return no_update
    triggered = ctx.triggered[0]
    try:
        graph_id_str = triggered['prop_id'].split('.')[0]
        graph_id = json.loads(graph_id_str)
        bot_id = graph_id['index']
        new_relayout = triggered['value']
    except:
        return no_update
    if new_relayout is None or not isinstance(new_relayout, dict):
        return no_update
    stored = stored.copy() if stored else {}
    stored[str(bot_id)] = new_relayout
    return stored

@app.callback(
    Output({'type': 'status-btn', 'index': MATCH}, 'children'),
    [Input({'type': 'status-btn', 'index': MATCH}, 'n_clicks'),
     Input('bots-trigger', 'data')],
    [State({'type': 'status-btn', 'index': MATCH}, 'id')],
    prevent_initial_call=True
)
def toggle_bot(n_clicks, trigger, btn_id):
    bot_id = btn_id['index']
    bots = get_all_bots()
    bot = next((b for b in bots if b['id'] == bot_id), None)
    if not bot:
        return "Start"

    ctx = callback_context
    if not ctx.triggered:
        return "Stop" if bot['status'] == 'running' else "Start"

    prop_id = ctx.triggered[0]['prop_id'].split('.')[0]

    if prop_id == 'bots-trigger':
        return "Stop" if bot['status'] == 'running' else "Start"

    if prop_id == 'status-btn' and (not n_clicks or n_clicks <= 0):
        return "Stop" if bot['status'] == 'running' else "Start"

    # Actual click - perform action
    try:
        if bot['status'] == 'running':
            bot_manager.stop_bot(bot_id)
            update_bot_status(bot_id, 'stopped')
            return "Start"
        else:
            bot_manager.start_bot(bot_id)
            update_bot_status(bot_id, 'running')
            return "Stop"
    except Exception as e:
        logger.error(f"Error toggling bot {bot_id}: {e}")
        # Return current button text and error
        current_text = "Stop" if bot['status'] == 'running' else "Start"
        return current_text

@app.callback(
    Output('bots-trigger', 'data', allow_duplicate=True),
    Input({'type': 'delete', 'index': ALL}, 'n_clicks'),
    [State({'type': 'delete', 'index': ALL}, 'id'),
    State('bots-trigger', 'data')],
    prevent_initial_call=True
)
def delete_bot_callback(n_clicks_list, ids_list, trigger):
    ctx = callback_context
    if not ctx.triggered:
        return no_update

    triggered = ctx.triggered[0]['prop_id'].split('.')[0]
    triggered_id = json.loads(triggered)
    bot_id = triggered_id['index']

    for i, id_dict in enumerate(ids_list):
        if id_dict['index'] == bot_id and n_clicks_list[i]:
            try:
                bot_manager.remove_bot(bot_id)
                delete_bot(bot_id)
                perf_logger.set_mood('happy')
                return trigger + 1
            except Exception as e:
                logger.error(f"Error deleting bot {bot_id}: {e}")
                return no_update

    # If no suitable click found (e.g., n_clicks=0)
    return no_update


@app.callback(
    [Output('editing-bots', 'data', allow_duplicate=True),
     Output('prev-edit-clicks', 'data')],
    Input({'type': 'edit-btn', 'index': ALL}, 'n_clicks'),
    [State('editing-bots', 'data'),
    State('prev-edit-clicks', 'data')],
    prevent_initial_call=True
)
def enter_edit_mode(n_clicks_list, editing, prev_clicks):
    ctx = callback_context
    if not ctx.triggered:
        return no_update, no_update

    if not prev_clicks or len(prev_clicks) != len(n_clicks_list):
        return no_update, n_clicks_list

    for i, (cur, prev) in enumerate(zip(n_clicks_list, prev_clicks)):
        if cur > prev:
            bots = get_all_bots()
            if i < len(bots):
                bot_id = str(bots[i]['id'])
                editing = editing or {}
                editing[bot_id] = True
                return editing, n_clicks_list
    return no_update, n_clicks_list

@app.callback(
    [Output('editing-bots', 'data', allow_duplicate=True),
     Output('bots-trigger', 'data', allow_duplicate=True)],
    Input({'type': 'edit-save-btn', 'index': ALL}, 'n_clicks'),
    [State({'type': 'edit-save-btn', 'index': ALL}, 'id'),
     State({'type': ALL, 'field': ALL}, 'value'),
     State({'type': ALL, 'field': ALL}, 'id'),
     State('editing-bots', 'data'),
     State('bots-trigger', 'data')],
    prevent_initial_call=True
)
def save_editing(n_clicks_list, btn_ids, field_values, field_ids, editing, trigger):
    ctx = callback_context
    if not ctx.triggered:
        return no_update, no_update
    triggered = ctx.triggered[0]
    dict_str = triggered['prop_id'].split('.')[0]
    btn_id = json.loads(dict_str)
    bot_id = btn_id['index']

    idx = None
    for i, id_dict in enumerate(btn_ids):
        if id_dict['index'] == bot_id and n_clicks_list[i]:
            idx = i
            break
    if idx is None:
        return no_update, no_update

    new_fields = {}
    for val, id_dict in zip(field_values, field_ids):
        field = id_dict.get('field')
        if field:
            new_fields[field] = val
    new_fields.pop('data_db_path', None)

    old_config = get_bot_config(bot_id) or {}
    bots = get_all_bots()
    bot = next((b for b in bots if b['id'] == bot_id), None)
    if not bot:
        return no_update, no_update
    bot_type = bot['type']
    meta_cls = bot_registry.get_model(f"{bot_type}.type")

    if meta_cls and hasattr(meta_cls, 'process_edit_save'):
        config = meta_cls.process_edit_save(bot_id, new_fields, old_config)
    else:
        config = old_config.copy()
        config.update(new_fields)

    update_bot_config(bot_id, config)

    if bot_id in bot_manager.bots:
        bot_instance = bot_manager.bots[bot_id]
        bot_instance.config_dirty = True
        if not bot_instance.running:
            bot_instance.config = config

    editing = editing or {}
    editing.pop(str(bot_id), None)
    perf_logger.set_mood('happy')
    return editing, trigger + 1

@app.callback(
    Output('editing-bots', 'data', allow_duplicate=True),
    Input('bots-trigger', 'data'),
    State('editing-bots', 'data'),
    prevent_initial_call=True
)
def cleanup_editing_on_list_change(trigger, editing):
    if not editing:
        return {}
    bots = get_all_bots()
    active_ids = {str(b['id']) for b in bots}
    return {bid: v for bid, v in editing.items() if bid in active_ids}

@app.callback(
    Output('editing-bots', 'data', allow_duplicate=True),
    Input({'type': 'edit-cancel-btn', 'index': ALL}, 'n_clicks'),
    State('editing-bots', 'data'),
    prevent_initial_call=True
)
def cancel_editing(n_clicks_list, editing):
    ctx = callback_context
    if not ctx.triggered:
        return no_update

    triggered = ctx.triggered[0]
    if not triggered.get('value') or triggered['value'] <= 0:
        return no_update

    dict_str = triggered['prop_id'].split('.')[0]
    btn_id = json.loads(dict_str)
    bot_id = str(btn_id['index'])

    editing = editing or {}
    editing.pop(bot_id, None)
    perf_logger.set_mood('cancel')
    return editing

@app.callback(
    [Output('settings-panel', 'style', allow_duplicate=True),
     Output('settings-btn', 'n_clicks', allow_duplicate=True)],
    Input('save-settings-btn', 'n_clicks'),
    [State('debug-checkbox', 'value'),
     State({'type': 'log-level-dropdown', 'module': ALL}, 'value'),
     State({'type': 'log-level-dropdown', 'module': ALL}, 'id'),
     State('log-retention-days', 'value')],
    prevent_initial_call=True
)
def save_settings(n_clicks, debug_val, log_levels, level_ids, retention_days):
    if not n_clicks:
        return no_update, no_update
    try:
        debug_mode = 'True' if debug_val and 'debug' in debug_val else 'False'
        save_setting('debug_mode', debug_mode)
        save_setting('log_retention_days',
                     str(int(retention_days or LOG_RETENTION_DAYS_DEFAULT)))

        settings_update = {}
        for level_val, id_dict in zip(log_levels, level_ids):
            module = id_dict['module']
            settings_update[f'{module}_level'] = level_val
        perf_logger.update_settings(settings_update)
        save_setting('logging_settings', json.dumps(perf_logger.settings))
        perf_logger.cleanup_old_logs(int(retention_days or 0))
    except Exception as e:
        logger.error(f"Error saving settings: {e}")
        return no_update, no_update   # panel stays open

    perf_logger.set_mood('happy')
    # close panel + reset settings button counter,
    # so the next opening works again (toggle via %2)
    return {'display': 'none'}, 0

@app.callback(
    [Output('header-mood-command', 'data', allow_duplicate=True),
     Output('logs-content', 'children', allow_duplicate=True),
     Output('time-content', 'children', allow_duplicate=True)],
    Input('tick-1s', 'n_intervals'),
    [State('header-mood', 'data'),
     State('logs-panel', 'style'),
     State('time-panel', 'style')],
    prevent_initial_call=True
)
def on_tick_1s(n, current_mood, logs_style, time_style):
    # 1) Mood polling — only when the previous "mood" has already played out
    mood_out = no_update
    if current_mood == 'normal':
        mood = perf_logger.get_pending_mood()
        if mood:
            mood_out = mood

    # 2) Logs — only if the panel is open
    logs_out = no_update
    if logs_style and logs_style.get('display') != 'none':
        logs_out = '\n'.join(perf_logger.get_recent_logs('all', 20))

    # 3) Time — only if the panel is open
    time_out = no_update
    if time_style and time_style.get('display') != 'none':
        time_out = build_time_content(n)

    return mood_out, logs_out, time_out

if __name__ == '__main__':
    debug_mode = get_setting('debug_mode', 'False') == 'True'
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    app.run(debug=debug_mode)