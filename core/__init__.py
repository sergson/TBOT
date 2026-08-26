# core/__init__.py
from .registry import bot_registry, auto_reg
from .base_bot import BaseBot
from .loader import load_modules
from .bot_manager import BotManager
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