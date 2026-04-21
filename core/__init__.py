# core/__init__.py
from .registry import bot_registry, auto_reg
from .base_bot import BaseBot
from .loader import load_modules
from .bot_manager import BotManager
from .database import (
    init_config_db, add_bot, get_all_bots, get_bot_config,
    update_bot_status, delete_bot, get_setting, save_setting
)
from .logger import perf_logger