# core/bot_manager.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import asyncio
import concurrent.futures
import os
import threading
import time
from .database import (
    get_all_bots, get_bot_config, DATA_DIR, add_bot as db_add_bot,
    cleanup_orphan_bots, cleanup_orphan_databases,
    delete_bot, update_bot_status,
)
from .logger import perf_logger
from .registry import bot_registry
from .exchange import ExchangeHandle

logger = perf_logger.get_logger('bot_manager', 'app')

TRUSTED_RESTART_TYPES = ('collector.bot', 'execution.bot')
FORCE_RESTART_MIN_INTERVAL_NS = 10 * 1_000_000_000


class BotManager:
    def __init__(self):
        self.bots = {}
        self.loop = None
        self._loop_thread = None
        self._shutdown_event = threading.Event()
        self.graph_hashes = {}
        self._status_warned: set = set()

        self._protected_bots: set = set()
        self._last_force_restart_ns: dict = {}
        self._broker_id: int | None = None

    # ---------- loop thread ----------

    def start_loop_in_thread(self):
        self.loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, name="BotEventLoop", daemon=False)
        self._loop_thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        if pending:
            self.loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    # ---------- load ----------

    def load_bots(self):
        try:
            removed = cleanup_orphan_bots()
            if removed:
                logger.info(
                    f"load_bots: removed {len(removed)} orphan bot "
                    f"record(s) from config.db: {removed}")
        except Exception as e:
            logger.error(
                f"load_bots: cleanup_orphan_bots failed: {e}", exc_info=True)
        try:
            cleanup_orphan_databases()
        except Exception as e:
            logger.error(
                f"load_bots: cleanup_orphan_databases failed: {e}",
                exc_info=True)

        # Auto-create broker (first) if absent.
        bots_data = get_all_bots()
        has_broker = any(b['type'] == 'broker' for b in bots_data)
        if not has_broker:
            try:
                new_id = db_add_bot(
                    'broker', 'Broker', {'default_provider': 'ccxt'})
                logger.info(f"Auto-created broker bot id={new_id}")
                bots_data = get_all_bots()
            except Exception as e:
                logger.error(
                    f"load_bots: auto-create broker failed: {e}",
                    exc_info=True)

        for bot in bots_data:
            if bot['type'] == 'broker':
                self._broker_id = bot['id']
                self._protected_bots.add(bot['id'])

        # N-6: broker first — consumers depend on it.
        bots_data.sort(key=lambda b: (
            0 if b['type'] == 'broker' else 1,
            b.get('position', 0),
            b['id'],
        ))

        for bot in bots_data:
            if bot['id'] not in self.bots:
                self._create_instance(bot['id'])
            if bot['type'] == 'broker':
                # N-6: broker is infrastructure — always start.
                # Consumers probe via _ensure_broker, but auto-start
                # removes the 5–7s first-start delay.
                self.start_bot(bot['id'])
            elif bot['status'] in ('running', 'paused'):
                self.start_bot(bot['id'])

    def _create_instance(self, bot_id: int):
        config = get_bot_config(bot_id)
        if not config:
            db_path = os.path.join(DATA_DIR, f'bot_{bot_id}.db')
            logger.error(
                f"Bot {bot_id}: config not found at {db_path} — skipping")
            return
        bot_type = config.get('bot_type')
        if not bot_type:
            logger.error(
                f"Bot {bot_id}: 'bot_type' missing in config — skipping")
            return
        bot_class = bot_registry.get_model(f"{bot_type}.bot")
        if not bot_class:
            logger.error(f"Unknown bot model: {bot_type}.bot")
            return
        self.bots[bot_id] = bot_class(bot_id, manager=self)
        if bot_type == 'broker':
            self._protected_bots.add(bot_id)
            self._broker_id = bot_id

    # ---------- status / create ----------

    def _on_bot_start_done(self, bot_id: int, future):
        try:
            future.result()
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(
                f"Bot {bot_id} start failed: {e}", exc_info=True)
            bot = self.bots.get(bot_id)
            if bot is not None:
                bot.running = False
            try:
                update_bot_status(bot_id, 'stopped')
            except Exception as ee:
                logger.warning(
                    f"Bot {bot_id}: failed to reset status: {ee}")

    def get_bot_status(self, bot_id: int) -> str:
        db_path = os.path.join(DATA_DIR, f'bot_{bot_id}.db')
        if not os.path.exists(db_path):
            if bot_id not in self._status_warned:
                logger.warning(
                    f"get_bot_status({bot_id}): config DB {db_path} missing")
                self._status_warned.add(bot_id)
            return 'unknown'
        try:
            cfg = get_bot_config(bot_id, include_status=True)
        except Exception as e:
            logger.warning(f"get_bot_status({bot_id}) failed: {e}")
            return 'unknown'
        if not cfg:
            logger.warning(f"get_bot_status({bot_id}): no config")
            return 'unknown'
        status = cfg.get('status')
        if not status:
            logger.warning(f"get_bot_status({bot_id}): empty status")
            return 'unknown'
        self._status_warned.discard(bot_id)
        return status

    def add_bot(self, bot_id: int):
        """Create instance without starting. Validate broker for dependent
        types.

        Raises ValueError if collector/execution is added without broker.
        """
        config = get_bot_config(bot_id) or {}
        bot_type = config.get('bot_type')
        if bot_type in ('collector', 'execution'):
            if self._broker_id is None:
                raise ValueError(
                    f"Broker bot not found; create broker first "
                    f"(cannot add {bot_type} #{bot_id})")
        if bot_type == 'broker':
            if self._broker_id is not None and self._broker_id != bot_id:
                raise ValueError(
                    f"Broker bot #{self._broker_id} already exists; "
                    f"only one broker is supported "
                    f"(cannot add broker #{bot_id})")
        if bot_id in self.bots:
            logger.debug(f"add_bot({bot_id}): already instantiated, skip")
            return
        self._create_instance(bot_id)

    def remove_bot(self, bot_id: int):
        if bot_id in self._protected_bots:
            logger.warning(
                f"remove_bot({bot_id}): bot is protected — refusing")
            return
        if bot_id in self.bots:
            self.stop_bot(bot_id)
            bot = self.bots[bot_id]
            if bot.running:
                logger.warning(
                    f"Bot {bot_id} did not stop cleanly, "
                    f"force cancelling tasks")
                if self.loop and self.loop.is_running():
                    for task in asyncio.all_tasks(self.loop):
                        if task.get_name().startswith(f"execution_{bot_id}") \
                                or getattr(task, 'bot_id', None) == bot_id:
                            task.cancel()
                    time.sleep(3)
            if hasattr(bot, '_close_db'):
                try:
                    bot._close_db()
                except Exception as e:
                    logger.warning(
                        f"remove_bot({bot_id}): _close_db failed: {e}")
            del self.bots[bot_id]

        for req_bot in self.bots.values():
            req_bot.dynamics.pop(bot_id, None)
        self.graph_hashes.pop(bot_id, None)
        self._status_warned.discard(bot_id)
        self._last_force_restart_ns.pop(bot_id, None)

        try:
            delete_bot(bot_id)
        except Exception as e:
            logger.error(
                f"remove_bot({bot_id}): delete_bot failed: {e}",
                exc_info=True)

    # ---------- exchange ----------

    async def request_exchange(self, requester_id: int, target_id: int,
                               mapping: dict) -> ExchangeHandle:
        requester = self.bots.get(requester_id)
        target = self.bots.get(target_id)
        if not requester:
            raise KeyError(f"Requester bot {requester_id} not found")
        if not target:
            raise KeyError(f"Target bot {target_id} not found")

        caps = target.get_capabilities()
        if asyncio.iscoroutinefunction(
                getattr(target, 'get_capabilities', None)):
            caps = await caps

        existing = requester.dynamics.get(target_id)
        if isinstance(existing, ExchangeHandle):
            handle = existing
        else:
            handle = ExchangeHandle(target_id)

        if not handle.owner_label:
            _raw = getattr(requester, '_name', '') or 'bot.bot'
            _type = _raw.split('.')[0]
            handle.owner_label = f"{_type} #{requester_id}"

        for local_name, keywords in mapping.items():
            matched = None
            for internal_key, cap in caps.items():
                if any(kw in cap["keywords"] for kw in keywords):
                    matched = internal_key
                    break
            if not matched:
                raise ValueError(
                    f"No capability matching keywords {keywords} in bot "
                    f"{target_id}")
            cap = caps[matched]
            handle.add_data_access(
                local_name, cap.get("getter"), cap.get("setter"))

        requester.dynamics[target_id] = handle
        return handle

    # ---------- start / stop ----------

    def start_bot(self, bot_id: int):
        bot = self.bots.get(bot_id)
        if bot is None:
            logger.warning(f"start_bot({bot_id}): bot not registered")
            return
        if self.loop is None:
            logger.warning(f"start_bot({bot_id}): event loop not started yet")
            return
        if bot.running:
            logger.debug(f"start_bot({bot_id}): already running, skip")
            return
        future = asyncio.run_coroutine_threadsafe(bot.start(), self.loop)
        future.add_done_callback(
            lambda f, bid=bot_id: self._on_bot_start_done(bid, f))
        perf_logger.set_mood('happy')

    def stop_bot(self, bot_id: int):
        if bot_id in self._protected_bots:
            logger.warning(
                f"stop_bot({bot_id}): bot is protected — refusing")
            return
        bot = self.bots.get(bot_id)
        if bot is None or self.loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(bot.stop(), self.loop)
        try:
            future.result(timeout=5)
            perf_logger.set_mood('happy')
        except concurrent.futures.TimeoutError:
            logger.warning(
                f"stop_bot({bot_id}): stop() > 5s, continuing in background")
        except Exception as e:
            logger.error(
                f"Error stopping bot {bot_id}: {e}", exc_info=True)

    # ---------- restart (D5) ----------

    def _is_trusted_caller(self, caller_id: int) -> bool:
        caller = self.bots.get(caller_id)
        if caller is None:
            return False
        return getattr(caller, '_name', '') in TRUSTED_RESTART_TYPES

    def restart_bot(self, caller_id: int, target_id: int) -> bool:
        """Simple restart: only if target is protected and NOT running.

        Called by _ensure_broker when status='stopped'/'paused'. Does not
        block — start_bot() schedules the start task on the shared loop.
        """
        if not self._is_trusted_caller(caller_id):
            logger.warning(
                f"restart_bot: caller {caller_id} is not trusted "
                f"(bot_type not in {TRUSTED_RESTART_TYPES})")
            return False
        if target_id not in self._protected_bots:
            logger.warning(
                f"restart_bot: target {target_id} is not protected")
            return False
        target = self.bots.get(target_id)
        if target is None:
            return False
        if target.running:
            return False
        logger.info(
            f"restart_bot: caller {caller_id} restarting {target_id}")
        self.start_bot(target_id)
        return True

    def force_restart_bot(self, caller_id: int, target_id: int) -> bool:
        """Force restart: stop target (if running) then start.

        Rate-limited to 1 per 10s per target. Trusted caller + protected
        target required. Non-blocking — schedules stop, then start via
        callback.
        """
        if not self._is_trusted_caller(caller_id):
            logger.warning(
                f"force_restart_bot: caller {caller_id} is not trusted")
            return False
        if target_id not in self._protected_bots:
            logger.warning(
                f"force_restart_bot: target {target_id} is not protected")
            return False
        if self.loop is None:
            return False
        now_ns = time.time_ns()
        last = self._last_force_restart_ns.get(target_id, 0)
        if now_ns - last < FORCE_RESTART_MIN_INTERVAL_NS:
            logger.debug(
                f"force_restart_bot({target_id}): rate-limited "
                f"({(now_ns - last) / 1e9:.1f}s < 10s)")
            return False
        self._last_force_restart_ns[target_id] = now_ns

        target = self.bots.get(target_id)
        if target is None:
            return False

        logger.warning(
            f"force_restart_bot: caller {caller_id} force-restarting "
            f"{target_id}")
        if target.running:
            self._force_stop_bot(
                target_id,
                on_done=lambda: self.start_bot(target_id))
        else:
            self.start_bot(target_id)
        return True

    def _force_stop_bot(self, bot_id: int, on_done=None):
        """Internal: stop bot bypassing _protected_bots.

        Non-blocking. on_done is called on the loop thread after stop
        completes (or on failure)."""
        bot = self.bots.get(bot_id)
        if bot is None or self.loop is None:
            if on_done:
                try:
                    on_done()
                except Exception:
                    pass
            return
        future = asyncio.run_coroutine_threadsafe(bot.stop(), self.loop)

        def _cb(f):
            try:
                f.result()
            except Exception as e:
                logger.warning(
                    f"_force_stop_bot({bot_id}) failed: {e}")
            if on_done:
                try:
                    self.loop.call_soon_threadsafe(on_done)
                except Exception as e:
                    logger.error(
                        f"_force_stop_bot({bot_id}) on_done error: {e}")

        future.add_done_callback(_cb)

    # ---------- shutdown ----------

    def shutdown(self):
        self._shutdown_event.set()

        # N-5: stop consumers first, then broker (protected).
        protected_ids = [bid for bid in self.bots
                         if bid in self._protected_bots]
        regular_ids = [bid for bid in self.bots
                       if bid not in self._protected_bots]
        ordered_ids = regular_ids + protected_ids

        for bot_id in ordered_ids:
            bot = self.bots.get(bot_id)
            if bot is None:
                continue
            if self.loop is None or not self.loop.is_running():
                logger.warning(
                    f"shutdown: bot {bot_id} skipped (loop not running)")
                continue
            try:
                future = asyncio.run_coroutine_threadsafe(
                    bot.stop(), self.loop)
                future.result(timeout=5)
            except concurrent.futures.TimeoutError:
                logger.warning(
                    f"shutdown: bot {bot_id} stop() > 5s")
            except Exception as e:
                logger.error(
                    f"shutdown: bot {bot_id} stop failed: {e}",
                    exc_info=True)

        for bot in self.bots.values():
            if hasattr(bot, '_close_db'):
                try:
                    bot._close_db()
                except Exception as e:
                    logger.warning(f"shutdown: _close_db failed: {e}")
        self.bots.clear()

        self.bots.clear()
        self._protected_bots.clear()
        self._broker_id = None
        self._last_force_restart_ns.clear()
        self._status_warned.clear()
        self.graph_hashes.clear()
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._loop_thread.join(timeout=10)
            if self._loop_thread.is_alive():
                logger.error(
                    "shutdown: BotEventLoop thread did not exit in 10s — "
                    "check for tasks that ignore cancellation")