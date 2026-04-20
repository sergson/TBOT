# modules/logger.py
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
from typing import Optional
import json


class PerformanceLogger:
    """Performance logger with configurable levels"""

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
        return self

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


# Singleton instance
perf_logger = PerformanceLogger()