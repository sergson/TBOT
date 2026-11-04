# core/tbotex/loader.py
# Imports every module under core/tbotex/providers/ for its side effect:
# the module calls @register_provider(...) at import time.
#
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.
#
import importlib
import os
from core.logger import perf_logger

logger = perf_logger.get_logger('tbotex_loader', 'app')


def load_tbotex_providers() -> list:
    """Imports all modules located in core/tbotex/providers/.
    Upon import, each module is decorated with @register_provider — this
    constitutes the registration process. Returns a list of the names of the loaded modules.
    """
    providers_dir = os.path.join(os.path.dirname(__file__), 'providers')
    if not os.path.isdir(providers_dir):
        return []
    loaded = []
    for entry in sorted(os.listdir(providers_dir)):
        if entry.startswith('_'):
            continue
        mod_name = entry[:-3] if entry.endswith('.py') else entry
        if not entry.endswith('.py') and not os.path.isdir(
                os.path.join(providers_dir, entry)):
            continue
        pkg = f"core.tbotex.providers.{mod_name}"
        try:
            importlib.import_module(pkg)
            loaded.append(mod_name)
            logger.debug(f"tbotex provider module loaded: {mod_name}")
        except Exception as e:
            logger.error(f"tbotex provider '{mod_name}' failed: {e}")
    return loaded