# modules/broker/lib/order_updates.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

"""Per-owner order update queue.

WS orders worker in broker_service pushes; executor polls via
get_order_updates(owner_bot_id, since_id, timeout_sec).

Contract (plan v8 §3.3):
  * bounded 10000 per owner
  * on overflow: evict oldest + throttled warning (60s per owner)
  * monotonic id per owner, since_id is exclusive
  * event-based wakeup + timeout fallback
"""

import asyncio
import time
from collections import deque


class OrderUpdateRecord:
    __slots__ = ('id', 'cid', 'update', 'ts_ns')

    def __init__(self, _id: int, cid: str, update: dict, ts_ns: int):
        self.id = _id
        self.cid = cid
        self.update = update
        self.ts_ns = ts_ns


class OrderUpdateQueue:
    MAXLEN = 10_000
    MAX_BATCH = 100
    OVERFLOW_LOG_INTERVAL_NS = 60 * 1_000_000_000

    def __init__(self, logger):
        self.logger = logger
        self._deques: dict = {}
        self._next_ids: dict = {}
        self._events: dict = {}
        self._last_overflow_log: dict = {}
        self._lock = asyncio.Lock()

    async def push(self, owner_bot_id: int, cid: str, update: dict) -> None:
        async with self._lock:
            dq = self._deques.get(owner_bot_id)
            if dq is None:
                dq = deque(maxlen=self.MAXLEN)
                self._deques[owner_bot_id] = dq
            if len(dq) == self.MAXLEN:
                now_ns = time.monotonic_ns()
                last = self._last_overflow_log.get(owner_bot_id, 0)
                if now_ns - last > self.OVERFLOW_LOG_INTERVAL_NS:
                    self.logger.warning(
                        f"OrderUpdateQueue[{owner_bot_id}] overflow "
                        f"at {self.MAXLEN}")
                    self._last_overflow_log[owner_bot_id] = now_ns
            nid = self._next_ids.get(owner_bot_id, 0) + 1
            self._next_ids[owner_bot_id] = nid
            dq.append(OrderUpdateRecord(
                nid, cid, update, time.monotonic_ns()))
            ev = self._events.get(owner_bot_id)
            if ev is not None:
                ev.set()

    async def poll(self, owner_bot_id: int, since_id: int,
                   timeout_sec: float) -> list:
        async with self._lock:
            dq = self._deques.get(owner_bot_id)
            if dq:
                ready = [r for r in dq if r.id > since_id]
                if ready:
                    return ready[:self.MAX_BATCH]
            ev = self._events.get(owner_bot_id)
            if ev is None:
                ev = asyncio.Event()
                self._events[owner_bot_id] = ev
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            return []
        async with self._lock:
            ev.clear()
            dq = self._deques.get(owner_bot_id)
            if not dq:
                return []
            return [r for r in dq if r.id > since_id][:self.MAX_BATCH]

    async def cleanup(self, owner_bot_id: int) -> None:
        async with self._lock:
            self._deques.pop(owner_bot_id, None)
            self._next_ids.pop(owner_bot_id, None)
            self._events.pop(owner_bot_id, None)
            self._last_overflow_log.pop(owner_bot_id, None)

    def stats(self) -> dict:
        return {
            'owners': len(self._deques),
            'total_records': sum(len(d) for d in self._deques.values()),
        }