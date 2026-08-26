# core/logger.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

"""
Logging module with configurable levels
"""
import logging
import os
from datetime import datetime
import json
import queue
import time

class PerformanceLogger:
    """Performance logger with configurable levels"""

    class MoodLogHandler(logging.Handler):
        def __init__(self, perf_logger_instance):
            super().__init__()
            self.perf_logger_instance = perf_logger_instance
            self.level_mood_map = {
                logging.DEBUG: 'debug',
                logging.INFO: 'info',
                logging.WARNING: 'warning',
                logging.ERROR: 'error',
                logging.CRITICAL: 'critical',
            }

        def emit(self, record):
            mood = self.level_mood_map.get(record.levelno)
            if mood:
                self.perf_logger_instance.set_mood(mood)

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True
        self._loggers = {}
        self._log_dir = "logs"
        self._default_level = logging.DEBUG
        self.mood_queue = queue.Queue()
        self._last_mood = None
        self._last_mood_time = 0
        self.mood_handler = self.MoodLogHandler(self)

        # Create logs directory
        os.makedirs(self._log_dir, exist_ok=True)

        # Default settings – keys correspond to module_type passed to get_logger
        self.settings = {
            'app_level': 'ERROR',
            'collector_level': 'ERROR',
            'fetcher_level': 'ERROR',
            'database_level': 'ERROR',
            'analytics_level': 'ERROR',
            'performance_log': True
        }

    def initialize_with_storage(self, storage):
        """Initialize by loading settings from the database"""
        try:
            saved = storage.get_setting('logging_settings')
            if saved:
                loaded = json.loads(saved) if isinstance(saved, str) else saved
                self.settings.update(loaded)
                print(f"✅ Logging settings loaded from DB: {self.settings}")
            else:
                print(f"⚠ Logging settings not found in DB, using defaults")
        except Exception as e:
            print(f"❌ Error loading logging settings: {e}")
        # Apply levels to all existing loggers
        self._apply_levels_to_existing_loggers()
        return self

    def _apply_levels_to_existing_loggers(self):
        """Updates levels for all previously created loggers."""
        for name, logger in self._loggers.items():
            module_type = 'app'
            for mt in ['app', 'collector', 'fetcher', 'database', 'analytics']:
                if mt in name.lower():
                    module_type = mt
                    break
            level_key = f'{module_type}_level'
            level = self.settings.get(level_key, self._default_level)
            log_level = logging.getLevelName(level)
            logger.setLevel(log_level)
            for handler in logger.handlers:
                handler.setLevel(log_level)
            if self.mood_handler not in logger.handlers:
                logger.addHandler(self.mood_handler)

    def setup_logger(self, name: str, log_file: str, level: str = 'INFO'):
        """Configure a logger"""
        level_map = {
            'DEBUG': logging.DEBUG,
            'INFO': logging.INFO,
            'WARNING': logging.WARNING,
            'ERROR': logging.ERROR,
            'CRITICAL': logging.CRITICAL
        }
        log_level = level_map.get(level.upper(), logging.INFO)

        logger = logging.getLogger(name)
        logger.setLevel(log_level)
        logger.handlers.clear()

        formatter = logging.Formatter(
            '%(asctime)s.%(msecs)03d [%(levelname)-8s] %(name)s - %(message)s',
            datefmt='%H:%M:%S'
        )

        log_path = os.path.join(self._log_dir, log_file)
        file_handler = logging.FileHandler(log_path, encoding='utf-8')
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(log_level)
        console_handler.setFormatter(formatter)

        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
        logger.addHandler(self.mood_handler)

        self._loggers[name] = logger
        return logger

    def get_logger(self, name: str, module_type: str = 'app'):
        """Get a logger with current level settings.
        module_type must be one of: app, collector, fetcher, database, analytics
        """
        log_file = f"{module_type}_{datetime.now().strftime('%Y%m%d')}.log"
        level_key = f'{module_type}_level'
        level = self.settings.get(level_key, self._default_level)

        if name in self._loggers:
            # Update level if changed
            for handler in self._loggers[name].handlers:
                handler.setLevel(logging.getLevelName(level))
            self._loggers[name].setLevel(logging.getLevelName(level))
            return self._loggers[name]

        return self.setup_logger(name, log_file, level)

    def update_settings(self, settings: dict):
        """Update logging settings"""
        self.settings.update(settings)

        # Override levels for existing loggers
        for name, logger in self._loggers.items():
            # Determine module_type from logger name (assume the name contains it)
            module_type = 'app'  # fallback
            for mt in ['app', 'collector', 'fetcher', 'database', 'analytics']:
                if mt in name.lower():
                    module_type = mt
                    break
            level_key = f'{module_type}_level'
            level = self.settings.get(level_key, self._default_level)
            logger.setLevel(logging.getLevelName(level))
            for handler in logger.handlers:
                handler.setLevel(logging.getLevelName(level))

    def save_settings(self, storage):
        """Save settings to the database"""
        try:
            storage.save_setting('logging_settings', json.dumps(self.settings))
        except Exception as e:
            print(f"❌ Error saving logging settings: {e}")

    def load_settings(self, storage):
        """Load settings from the database"""
        try:
            saved = storage.get_setting('logging_settings')
            if saved and isinstance(saved, str):
                loaded = json.loads(saved)
                self.update_settings(loaded)
        except Exception as e:
            print(f"⚠ Error loading logging settings: {e}")

    def set_mood(self, mood: str):
        """Places a mood into the queue, ignoring consecutive duplicates."""
        current_time = time.time()
        # Ignore if the same mood was added less than 1 second ago
        if mood == self._last_mood and (current_time - self._last_mood_time) < 1.0:
            return
        self.mood_queue.put(mood)
        self._last_mood = mood
        self._last_mood_time = current_time

    def get_pending_mood(self) -> str | None:
        """Retrieves one mood from the queue (if any)."""
        try:
            return self.mood_queue.get_nowait()
        except queue.Empty:
            return None

    def get_recent_logs(self, module_type: str = 'app', n_lines: int = 20) -> list:
        """
        Returns the last n_lines lines from the log file(s).
        If module_type == 'all', reads all files *_YYYYMMDD.log in the logs directory.
        """
        if module_type == 'all':
            today_str = datetime.now().strftime('%Y%m%d')
            all_lines = []
            for filename in os.listdir(self._log_dir):
                if filename.endswith(f'_{today_str}.log'):
                    filepath = os.path.join(self._log_dir, filename)
                    try:
                        with open(filepath, 'r', encoding='utf-8') as f:
                            all_lines.extend([line.rstrip('\n') for line in f.readlines()])
                    except Exception:
                        continue
            # sort by time (if needed)
            import re
            def extract_time(line):
                m = re.match(r'(\d{2}:\d{2}:\d{2}\.\d{3})', line)
                return m.group(1) if m else ''

            all_lines.sort(key=extract_time)
            return all_lines[-n_lines:] if all_lines else ["No log entries yet"]
        else:
            log_file = f"{module_type}_{datetime.now().strftime('%Y%m%d')}.log"
            log_path = os.path.join(self._log_dir, log_file)
            try:
                with open(log_path, 'r', encoding='utf-8') as f:
                    lines = [line.rstrip('\n') for line in f.readlines()]
                return lines[-n_lines:] if lines else ["No log entries yet"]
            except FileNotFoundError:
                return [f"Log file {log_file} not found"]

# Singleton instance
perf_logger = PerformanceLogger()