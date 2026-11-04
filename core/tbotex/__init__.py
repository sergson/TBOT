# core/tbotex/__init__.py
#
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.
#
# Native exchange providers — opt-in replacement for ccxt.
# Default remains ccxt; providers register themselves via loader.
from .registry import (
    TBOTEX_PROVIDERS,
    register_provider, get_provider, has_provider,
    list_available_pairs,
)
from .loader import load_tbotex_providers
from .protocol import ExchangeClientProtocol, TbotexFactory

__all__ = [
    'TBOTEX_PROVIDERS',
    'register_provider', 'get_provider', 'has_provider',
    'list_available_pairs',
    'load_tbotex_providers',
    'ExchangeClientProtocol', 'TbotexFactory',
]