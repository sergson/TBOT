# modules/collector/components/utils.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import re
from core.logger import perf_logger
logger = perf_logger.get_logger('collector_utils', 'collector')

def safe_table_name(symbol) -> str:
    """Converts a trading pair symbol into a safe SQLite table name."""
    if not isinstance(symbol, str):
        # Log a warning (optional)
        logger.warning(f"safe_table_name: symbol is not a string: {symbol!r}")
        return "_"  # or any other safe name
    return re.sub(r'[^a-zA-Z0-9_]', '_', symbol)