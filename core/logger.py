# core/logger.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

"""
Thread-safe logger without blocking in producers.

Architecture (QueueHandler + QueueListener + Watchdog):

    Producer (any thread: sync, watchdog, consensus, UI-callback, ...)
        logger.info(...)
          → Logger.filter: TimeSyncFilter sets record.ts_utc_ns
          → QueueHandler.emit → queue.put_nowait (drop-oldest on Full)
        ← immediate return, no I/O

    Listener (one daemon thread per module_type)
        queue.get()
          → FileHandler.emit (write + flush)
          → StreamHandler.emit (stderr)
          → MoodLogHandler.emit → set_mood → deque.append

    Watchdog (one daemon thread per PerformanceLogger, interval 30s)
        checks: is listener alive? is queue not full? drops during interval?
        on problem — writes directly to sys.stderr, bypassing logging
        on listener death — restarts it

Guarantees:
    * No logger.xxx() call blocks on I/O or on a full queue.
      Producer holds handler-lock only for put_nowait (microseconds).
    * Listener death/hang does not stall producers; watchdog restarts it.
    * Queue overflow → drop-oldest + counter; watchdog complains to stderr
      if drops are systematic.
    * TimeSyncFilter is called in Logger.handle BEFORE acquire(handler.lock),
      so cross-module lock with TimeSyncService is impossible.
    * SyncFormatter.formatTime does NOT call clock — reads only
      record.ts_utc_ns. No access to external locks under handler-lock.
    * get_recent_logs reads file tail (not whole file), with cache.
    * File write errors (disk full, PermissionError) are caught
      in listener; producer is unaffected.
"""

import atexit
import json
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

# Default log retention days
LOG_RETENTION_DAYS_DEFAULT = 2

# Logging objects — topics
LOGGER_OBJS = ('app', 'time', 'collector', 'fetcher', 'database', 'analytics', 'execution')

LOGGER_LEVEL_MAP = {
    'DEBUG': logging.DEBUG,
    'INFO': logging.INFO,
    'WARNING': logging.WARNING,
    'ERROR': logging.ERROR,
    'CRITICAL': logging.CRITICAL,
}

# Logging levels
LOGGER_LEVELS = tuple(LOGGER_LEVEL_MAP.keys())

# Default level for each object
DEFAULT_LOG_LEVEL = 'ERROR'

DEFAULT_LOGGER_SETTINGS = {
    **{f'{obj}_level': DEFAULT_LOG_LEVEL for obj in LOGGER_OBJS},
}

# --- Tuning via env ---

# Queue size per module_type. 10000 records ~ 5 MB for a typical record.
# Total ~35 MB for 7 module_types — acceptable.
QUEUE_MAX_SIZE = int(os.environ.get('TBOT_LOG_QUEUE_SIZE', '10000'))

# Watchdog interval (sec)
WATCHDOG_INTERVAL_SEC = 30

# Drops threshold per interval — above → noise to stderr
DROP_ALERT_THRESHOLD = int(os.environ.get('TBOT_LOG_DROP_ALERT', '100'))

# Cache TTL for get_recent_logs (sec)
RECENT_LOGS_CACHE_TTL_SEC = 2.0

# =====================================================================
# Formatter / Filter
# =====================================================================

class SyncFormatter(logging.Formatter):
    """Formatter that takes time from record.ts_utc_ns (set by filter).
    First column: HH:MM:SS.SSSSSSSSS+00:00 (UTC).

    IMPORTANT: formatTime does NOT call external clock functions. This guarantees
    that under handler-lock (in listener thread) there will be no access to
    TimeSyncService.lock and other cross-module resources.
    """

    def formatTime(self, record, datefmt=None):
        ns = getattr(record, 'ts_utc_ns', None)
        if ns is None:
            # Do not block and do not fail — safe default.
            return "0000-00-00 00:00:00.000000000+00:00"
        secs, frac = divmod(int(ns), 1_000_000_000)
        dt = datetime.fromtimestamp(secs, tz=timezone.utc)
        return f"{dt.strftime('%H:%M:%S')}.{frac:09d}+00:00"

class TimeSyncFilter(logging.Filter):
    """Sets record.ts_utc_ns at the moment the record is formed.

    Called in Logger.handle — before acquire(handler.lock). Therefore,
    cross-module deadlock with TimeSyncService is impossible: clock() takes
    time_sync.lock and releases it before the producer blocks on
    logging handler-lock.

    clock_getter — callable that reads the current clock from perf_logger
    (via bound method, so that set_clock() after loggers are created
    works without rebuilding filters).
    """

    def __init__(self, clock_getter: Callable[[], int]):
        super().__init__()
        self._clock_getter = clock_getter

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.ts_utc_ns = self._clock_getter()
        except Exception:
            # clock may fail (e.g., time_sync not yet initialized).
            # Do not block, do not fail — leave ts_utc_ns = None.
            record.ts_utc_ns = None
        return True

# =====================================================================
# QueueHandler with drop-oldest
# =====================================================================

class DropOldestQueueHandler(logging.Handler):
    """Puts record into queue without blocking producer.

    put_nowait never blocks. On Full — evict one old record, put new one.
    On repeated Full — lose new one, increment counter.
    Critical section under handler-lock — only put_nowait (microseconds).
    """

    def __init__(self, q: queue.Queue):
        super().__init__()
        self._q = q
        self.drops = 0
        self._drop_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._q.put_nowait(record)
            return
        except queue.Full:
            pass
        # Try to evict one old record
        try:
            self._q.get_nowait()
        except queue.Empty:
            pass
        try:
            self._q.put_nowait(record)
            with self._drop_lock:
                self.drops += 1  # lost old
        except queue.Full:
            with self._drop_lock:
                self.drops += 1  # lost new

    def reset_drops(self) -> int:
        with self._drop_lock:
            n = self.drops
            self.drops = 0
            return n

# =====================================================================
# MoodLogHandler — runs in listener, not in producer
# =====================================================================

class MoodLogHandler(logging.Handler):
    """Converts record level to mood for UI.
    Runs in listener thread; deque.append does not block.
    """

    _LEVEL_TO_MOOD = {
        logging.DEBUG: 'debug',
        logging.INFO: 'info',
        logging.WARNING: 'warning',
        logging.ERROR: 'error',
        logging.CRITICAL: 'critical',
    }

    def __init__(self, perf_logger_instance, module_type: str):
        super().__init__()
        self._pl = perf_logger_instance
        self._module_type = module_type

    def emit(self, record: logging.LogRecord) -> None:
        mood = self._LEVEL_TO_MOOD.get(record.levelno)
        if not mood:
            return
        try:
            self._pl.set_mood(mood, module_type=self._module_type)
        except Exception:
            # mood is cosmetic, not a reason to fail
            pass

# =====================================================================
# Dispatcher — runs in listener thread, distributes record to handlers
# =====================================================================

class _DispatcherHandler(logging.Handler):
    """Runs in listener thread. Receives record from queue and passes
    it to real handlers (file, console, mood). Errors in one
    handler do not kill others and do not kill listener.
    """

    def __init__(self, runtime: '_ModuleRuntime'):
        super().__init__()
        self._rt = runtime

    def emit(self, record: logging.LogRecord) -> None:
        rt = self._rt

        # File handler — main path; open/recreate by date
        try:
            fh = rt.ensure_file_handler()
            if record.levelno >= fh.level:
                fh.handle(record)
        except Exception as e:
            self._report_internal(f"file handler ({e})", record)

        # Console — best-effort; may hang on pipe,
        # but this is only listener, watchdog will restart it
        try:
            if record.levelno >= rt.console_handler.level:
                rt.console_handler.handle(record)
        except Exception as e:
            self._report_internal(f"console handler ({e})", record)

        # Mood
        try:
            if record.levelno >= rt.mood_handler.level:
                rt.mood_handler.handle(record)
        except Exception as e:
            self._report_internal(f"mood handler ({e})", record)

    @staticmethod
    def _report_internal(where: str, record: logging.LogRecord) -> None:
        try:
            sys.stderr.write(
                f"[logger-internal] error in {where}: "
                f"level={record.levelname} name={record.name}\n"
            )
            sys.stderr.flush()
        except Exception:
            pass

# =====================================================================
# _ModuleRuntime — queue + handlers + listener for one module_type
# =====================================================================

class _ModuleRuntime:
    """Runtime for one module_type (app/time/collector/...).

    Holds queue, set of handlers, listener thread. Recreating
    listener on death — method restart_listener().
    """

    def __init__(self, module_type: str, log_dir: str, perf_logger_instance):
        self.module_type = module_type
        self.log_dir = log_dir
        self._pl = perf_logger_instance

        self.queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAX_SIZE)
        self.qhandler = DropOldestQueueHandler(self.queue)

        self.formatter = SyncFormatter(
            '%(asctime)s [%(levelname)-8s] %(name)s - %(message)s'
        )

        # FileHandler is created lazily, on first write after start
        # or date change. Protected by _file_lock, but in fact writer is single —
        # listener.
        self._file_handler: Optional[logging.FileHandler] = None
        self._file_date: Optional[str] = None
        self._file_lock = threading.Lock()

        # Console handler: stderr, not stdout. If stdout is a pipe
        # without reader, blocking in listener; producers are not affected.
        self.console_handler = logging.StreamHandler(stream=sys.stderr)
        self.console_handler.setFormatter(self.formatter)

        # Mood — in listener
        self.mood_handler = MoodLogHandler(perf_logger_instance, module_type)
        self.mood_handler.setFormatter(self.formatter)

        self._listener: Optional[logging.handlers.QueueListener] = None
        self._listener_lock = threading.Lock()
        self._start_listener()

    # ---------- file / rotation by date ----------

    def _current_log_path(self) -> str:
        date_str = datetime.now().strftime('%Y%m%d')
        return os.path.join(self.log_dir, f"{self.module_type}_{date_str}.log")

    def ensure_file_handler(self) -> logging.FileHandler:
        """Opens FileHandler for current date. If date changed —
        closes old and opens new. Called from listener.
        """
        with self._file_lock:
            date_str = datetime.now().strftime('%Y%m%d')
            if self._file_handler is not None and self._file_date == date_str:
                return self._file_handler
            if self._file_handler is not None:
                try:
                    self._file_handler.close()
                except Exception:
                    pass
            path = self._current_log_path()
            fh = logging.FileHandler(path, encoding='utf-8', delay=False)
            fh.setFormatter(self.formatter)
            self._file_handler = fh
            self._file_date = date_str
            return fh

    # ---------- listener ----------

    def _start_listener(self) -> None:
        dispatcher = _DispatcherHandler(self)
        self._listener = logging.handlers.QueueListener(
            self.queue,
            dispatcher,
            respect_handler_level=True,
        )
        self._listener.start()

    def restart_listener(self) -> bool:
        with self._listener_lock:
            old = self._listener
            if old is not None:
                try:
                    old.stop()
                except Exception:
                    pass
            try:
                self._start_listener()
                return True
            except Exception:
                self._listener = None
                return False

    def listener_alive(self) -> bool:
        t = getattr(self._listener, '_thread', None)
        return t is not None and t.is_alive()

    def set_level(self, level: int) -> None:
        self.qhandler.setLevel(level)
        self.console_handler.setLevel(level)
        self.mood_handler.setLevel(level)
        with self._file_lock:
            if self._file_handler is not None:
                self._file_handler.setLevel(level)

    def stop(self) -> None:
        with self._listener_lock:
            if self._listener is not None:
                try:
                    self._listener.stop()
                except Exception:
                    pass
                self._listener = None
        with self._file_lock:
            if self._file_handler is not None:
                try:
                    self._file_handler.close()
                except Exception:
                    pass
                self._file_handler = None

# =====================================================================
# PerformanceLogger — Singleton
# =====================================================================

class PerformanceLogger:
    """Thread-safe singleton logger.

    Entry point — module-level variable perf_logger.
    """

    _instance: Optional['PerformanceLogger'] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, '_initialized', False):
            return
        self._initialized = True

        self._loggers: dict[str, logging.Logger] = {}
        self._runtimes: dict[str, _ModuleRuntime] = {}
        self._log_dir = "logs"
        self._default_level = DEFAULT_LOG_LEVEL

        # Mood: deque with maxlen — append/popleft never block
        self.mood_queue: deque = deque(maxlen=5)
        self._last_mood: Optional[Tuple[str, str]] = None
        self._last_mood_time: float = 0.0

        # Clock (epoch-ns). Default time.time_ns, after set_clock —
        # bound method from TimeSyncService.
        self._clock_ns: Callable[[], int] = time.time_ns

        # Common filter for all loggers
        self._time_filter = TimeSyncFilter(self.get_clock_ns)

        # Lock for settings — to keep update_settings consistent
        self._settings_lock = threading.RLock()

        # Cache for get_recent_logs
        self._recent_cache: dict = {}
        self._recent_cache_lock = threading.Lock()

        # Settings
        self.settings = dict(DEFAULT_LOGGER_SETTINGS)

        os.makedirs(self._log_dir, exist_ok=True)

        # Watchdog
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="LoggerWatchdog",
            daemon=True,
        )
        self._watchdog_thread.start()

        atexit.register(self._shutdown)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def initialize_with_storage(self, storage) -> 'PerformanceLogger':
        try:
            saved = storage.get_setting('logging_settings')
            if saved:
                loaded = json.loads(saved) if isinstance(saved, str) else saved
                with self._settings_lock:
                    self.settings.update(loaded)
                print(f"✅ Logging settings loaded from DB: {self.settings}")
            else:
                print("⚠ Logging settings not found in DB, using defaults")
        except Exception as e:
            print(f"❌ Error loading settings: {e}")

        self._apply_levels_to_existing_loggers()

        try:
            retention_days = int(storage.get_setting('log_retention_days', '0'))
        except Exception as e:
            retention_days = 0
            print(f"❌ Error loading retention setting: {e}")

        self.cleanup_old_logs(retention_days)
        return self

    def get_logger(self, name: str, module_type: str = 'app') -> logging.Logger:
        """Returns a logger. module_type ∈ LOGGER_OBJS."""
        if module_type not in LOGGER_OBJS:
            module_type = 'app'

        with self._settings_lock:
            level_name = self.settings.get(f'{module_type}_level',
                                           self._default_level)
        level = LOGGER_LEVEL_MAP.get(level_name.upper(), logging.ERROR)

        existing = self._loggers.get(name)
        if existing is not None:
            existing.setLevel(level)
            rt = self._runtimes.get(module_type)
            if rt is not None:
                rt.set_level(level)
            return existing

        return self._setup_logger(name, module_type, level)

    def _setup_logger(self, name: str, module_type: str,
                      level: int) -> logging.Logger:
        rt = self._runtimes.get(module_type)
        if rt is None:
            rt = _ModuleRuntime(module_type, self._log_dir, self)
            self._runtimes[module_type] = rt

        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.propagate = False

        for h in list(logger.handlers):
            logger.removeHandler(h)

        if self._time_filter not in logger.filters:
            logger.addFilter(self._time_filter)

        # The only handler on the logger is the queue handler.
        # All side effects (file, console, mood) are in the listener.
        rt.set_level(level)
        logger.addHandler(rt.qhandler)

        self._loggers[name] = logger
        return logger

    def _apply_levels_to_existing_loggers(self) -> None:
        with self._settings_lock:
            items = list(self._loggers.items())
            settings_copy = dict(self.settings)

        for name, lg in items:
            module_type = self._detect_module_type(name)
            level_name = settings_copy.get(f'{module_type}_level',
                                           self._default_level)
            level = LOGGER_LEVEL_MAP.get(level_name.upper(), logging.ERROR)
            lg.setLevel(level)
            rt = self._runtimes.get(module_type)
            if rt is not None:
                rt.set_level(level)

    def _detect_module_type(self, logger_name: str) -> str:
        n = logger_name.lower()
        for mt in LOGGER_OBJS:
            if mt in n:
                return mt
        return 'app'

    def get_level(self, obj: str) -> str:
        with self._settings_lock:
            return self.settings.get(f'{obj}_level', DEFAULT_LOG_LEVEL)

    def update_settings(self, settings: dict) -> None:
        with self._settings_lock:
            self.settings.update(settings)
        self._apply_levels_to_existing_loggers()

    def save_settings(self, storage) -> None:
        try:
            with self._settings_lock:
                blob = json.dumps(self.settings)
            storage.save_setting('logging_settings', blob)
        except Exception as e:
            print(f"❌ Error saving logging settings: {e}")

    def load_settings(self, storage) -> None:
        try:
            saved = storage.get_setting('logging_settings')
            if saved and isinstance(saved, str):
                loaded = json.loads(saved)
                self.update_settings(loaded)
        except Exception as e:
            print(f"⚠ Error loading logging settings: {e}")

    def is_enabled(self, module_type: str, record_level) -> bool:
        with self._settings_lock:
            threshold_name = self.settings.get(f'{module_type}_level',
                                               DEFAULT_LOG_LEVEL)
        threshold = LOGGER_LEVEL_MAP.get(threshold_name.upper(), logging.ERROR)
        if isinstance(record_level, str):
            rl = LOGGER_LEVEL_MAP.get(record_level.upper())
            if rl is None:
                return False
            record_level = rl
        return record_level >= threshold

    # ------------------------------------------------------------------
    # Mood
    # ------------------------------------------------------------------

    def set_mood(self, mood: str, module_type: str = 'app') -> None:
        """Puts mood into deque. Never blocks.
        Throttling: same pair (module_type, mood) not more than once per second.
        """
        if not self.is_enabled(module_type, mood):
            return
        now = time.time()
        key = (module_type, mood)
        if key == self._last_mood and (now - self._last_mood_time) < 1.0:
            return
        # deque.append with maxlen does not block
        self.mood_queue.append(mood)
        self._last_mood = key
        self._last_mood_time = now

    def get_pending_mood(self) -> Optional[str]:
        try:
            return self.mood_queue.popleft()
        except IndexError:
            return None

    # ------------------------------------------------------------------
    # Recent logs (read tail, cache)
    # ------------------------------------------------------------------

    def get_recent_logs(self, module_type: str = 'app',
                        n_lines: int = 20) -> List[str]:
        cache_key = (module_type, n_lines)
        now = time.monotonic()
        with self._recent_cache_lock:
            cached = self._recent_cache.get(cache_key)
            if cached is not None:
                ts, val = cached
                if now - ts < RECENT_LOGS_CACHE_TTL_SEC:
                    return val

        try:
            if module_type == 'all':
                result = self._read_all_tails(n_lines)
            else:
                today = datetime.now().strftime('%Y%m%d')
                result = self._read_file_tail(
                    os.path.join(self._log_dir, f"{module_type}_{today}.log"),
                    n_lines,
                )
        except Exception as e:
            result = [f"[logger] failed to read logs: {e}"]

        with self._recent_cache_lock:
            self._recent_cache[cache_key] = (now, result)
        return result

    @staticmethod
    def _read_file_tail(path: str, n_lines: int) -> List[str]:
        """Reads the last n_lines from file without loading it entirely."""
        if not os.path.exists(path):
            return [f"Log file {os.path.basename(path)} not found"]

        size = os.path.getsize(path)
        if size <= 1024 * 1024:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                tail = deque(f, maxlen=n_lines)
            return [ln.rstrip('\n') for ln in tail] or ["No log entries yet"]

        # Large file — read in blocks from the end
        block_size = 8192
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            buf = b''
            while pos > 0 and buf.count(b'\n') <= n_lines:
                step = min(block_size, pos)
                pos -= step
                f.seek(pos)
                chunk = f.read(step)
                buf = chunk + buf
        text = buf.decode('utf-8', errors='replace')
        lines = text.splitlines()
        return lines[-n_lines:] if lines else ["No log entries yet"]

    def _read_all_tails(self, n_lines: int) -> List[str]:
        today = datetime.now().strftime('%Y%m%d')
        all_lines: List[str] = []
        try:
            entries = os.listdir(self._log_dir)
        except Exception:
            return ["No log entries yet"]
        for filename in entries:
            if filename.endswith(f'_{today}.log'):
                try:
                    all_lines.extend(self._read_file_tail(
                        os.path.join(self._log_dir, filename), n_lines))
                except Exception:
                    continue
        if not all_lines:
            return ["No log entries yet"]
        all_lines.sort()
        return all_lines[-n_lines:]

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup_old_logs(self, retention_days: int = 0) -> None:
        if retention_days <= 0:
            return
        cutoff = time.time() - retention_days * 86400
        try:
            entries = os.listdir(self._log_dir)
        except Exception:
            return
        for filename in entries:
            if not filename.endswith('.log'):
                continue
            filepath = os.path.join(self._log_dir, filename)
            try:
                if os.path.getmtime(filepath) < cutoff:
                    os.remove(filepath)
                    print(f"🗑 Deleted old log: {filename}")
            except Exception as e:
                print(f"⚠ Could not delete {filename}: {e}")

    # ------------------------------------------------------------------
    # Clock
    # ------------------------------------------------------------------

    def get_clock_ns(self) -> int:
        """Called from TimeSyncFilter in producers, BEFORE handler-lock.
        Exception will propagate to filter, which will leave ts_utc_ns = None.
        """
        return self._clock_ns()

    def set_clock(self, clock_fn: Callable[[], int]) -> None:
        """Replaces time source. Example:
        perf_logger.set_clock(time_sync_service.get_utc_ns)
        """
        self._clock_ns = clock_fn

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(timeout=WATCHDOG_INTERVAL_SEC):
            try:
                self._watchdog_tick()
            except Exception as e:
                self._warn_stderr(f"[logger] watchdog tick failed: {e}")

    def _watchdog_tick(self) -> None:
        for module_type, rt in list(self._runtimes.items()):
            # 1. Listener liveness
            if not rt.listener_alive():
                self._warn_stderr(
                    f"[logger] listener for '{module_type}' is dead, restarting"
                )
                if not rt.restart_listener():
                    self._warn_stderr(
                        f"[logger] failed to restart listener for '{module_type}'"
                    )
                continue

            # 2. Queue overflow
            qsize = rt.queue.qsize()
            if qsize > int(QUEUE_MAX_SIZE * 0.8):
                self._warn_stderr(
                    f"[logger] queue for '{module_type}' is "
                    f"{qsize}/{QUEUE_MAX_SIZE} (>80%), listener slow?"
                )

            # 3. Drops
            drops = rt.qhandler.reset_drops()
            if drops >= DROP_ALERT_THRESHOLD:
                self._warn_stderr(
                    f"[logger] dropped {drops} records for '{module_type}' "
                    f"in last {WATCHDOG_INTERVAL_SEC}s "
                    f"(queue full — consumer can't keep up)"
                )

    @staticmethod
    def _warn_stderr(msg: str) -> None:
        """The only path independent of logging and its locks."""
        try:
            sys.stderr.write(msg + "\n")
            sys.stderr.flush()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _shutdown(self) -> None:
        self._watchdog_stop.set()
        for rt in self._runtimes.values():
            rt.stop()

# Singleton
perf_logger = PerformanceLogger()