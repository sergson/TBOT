# core/loader.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import os
import sys
import importlib
from typing import List
from .logger import perf_logger

logger = perf_logger.get_logger('loader', 'app')

def load_modules(modules_path: str = "modules") -> List[str]:
    """
    Dynamically loads all modules from the specified folder.
    Returns a list of successfully loaded module names.
    """
    abs_modules_path = os.path.abspath(modules_path)
    logger.debug(f"Loading modules from {abs_modules_path}")

    if not os.path.exists(abs_modules_path):
        logger.warning(f"Modules folder '{abs_modules_path}' not found")
        return []

    parent_dir = os.path.dirname(abs_modules_path)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
        logger.debug(f"Added {parent_dir} to sys.path")

    loaded = []
    for item in sorted(os.listdir(abs_modules_path)):
        module_dir = os.path.join(abs_modules_path, item)
        if not os.path.isdir(module_dir):
            continue
        init_file = os.path.join(module_dir, '__init__.py')
        if not os.path.exists(init_file):
            continue

        try:
            pkg_name = f"{os.path.basename(modules_path)}.{item}"
            logger.debug(f"Importing {pkg_name}...")
            importlib.import_module(pkg_name)
            loaded.append(item)
            logger.debug(f"Module '{item}' successfully loaded")
        except Exception as e:
            logger.error(f"Error loading module '{item}': {e}")
            import traceback
            traceback.print_exc()

    logger.debug(f"Loaded modules: {loaded}")
    return loaded