# core/__init__.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

from .registry import bot_registry, auto_reg
from .base_bot import BaseBot
from .loader import load_modules
from .bot_manager import BotManager
from .tbotex import load_tbotex_providers
from .database import (
    Field, Char, Text, Integer, Boolean, Float, Selection, Json,
    Model, MODEL_REGISTRY, DBSQLite3, Record, Recordset,
    init_config_db, add_bot, update_bot_config, get_bot_config,
    get_all_bots, update_bot_status, delete_bot,
    get_setting, save_setting, cleanup_orphan_databases
)
from .logger import perf_logger
from . import colors
from . import styles
from . import graphics