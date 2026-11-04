# modules/broker/lib/clock.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

"""TBOT-clock override for ccxt / ccxt.pro clients.

All timestamps inside T.B.O.T are ns, TBOT UTC (time_sync_service).
Wiring that clock into ccxt ensures signed REST calls do not fail with
-1021 InvalidNonce when the OS wall clock drifts from the exchange
server clock.
"""

import time

def install_exchange_clock(exchange, logger=None, monotonic=False):
    """Wire a ccxt exchange instance to TBOT UTC.

    By default ccxt.milliseconds() returns time.time() * 1000 — the OS
    wall clock. That clock can drift from the exchange's server clock
    by more than 1 s, causing -1021 InvalidNonce on every signed REST
    request (including ccxt.pro's internal watch_orders path). TBOT
    keeps its own NTP-synced time in time_sync_service; feed that into
    ccxt so every signed call uses the same clock we use everywhere else.

    Idempotent: safe to call more than once on the same instance.
    Uses a lazy import so importing this module for UI helpers does not
    pull in the time service.
    """
    try:
        from core.time_sync import time_sync_service
    except Exception as e:
        # If the time service is unavailable, leave ccxt on its
        # default clock — better than crashing.
        if logger:
            logger.warning(
                f"install_exchange_clock: time_sync unavailable ({e}); "
                f"ccxt stays on OS wall clock"
            )
        return

    def _tbot_milliseconds() -> int:
       # 1. Precision-aware path: respects TTL and the consistency of 5 fields.
        try:
            if monotonic:
                return time_sync_service.get_utc_ns_monotonic() // 1_000_000
            snap = time_sync_service.get_clock_snapshot()
            if snap is not None:
                utc_ns = time_sync_service.utc_from_snapshot(
                    snap, time.monotonic_ns()
                )
                if utc_ns is not None:
                    return utc_ns // 1_000_000
        except Exception as _e:
            if logger:
                logger.debug(f"tbot clock snapshot failed: {_e}")

        # 2. Raw model time (fallback — if the snapshot has become stale,
        #    but the service is still "synced" — get_utc_ns will return an extrapolation of the model).
        try:
            return time_sync_service.get_utc_ns() // 1_000_000
        except Exception:
            pass

        # 3. Hard fallback: system time.
        return time.time_ns() // 1_000_000

    exchange.milliseconds = _tbot_milliseconds

    # IMPORTANT: modify `options`, not `exchange.timeDifference`.
    # `load_time_difference()` will eventually overwrite this value,
    # but `nonce()` must be meaningful before that function is called.
    try:
        exchange.options['timeDifference'] = 0
    except Exception:
        pass