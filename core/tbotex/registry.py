# core/tbotex/registry.py
# (exchange_id, market_type) -> factory.
#
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.
#
#public registry (TBOTEX_PROVIDERS).

from typing import Dict, List, Optional, Tuple
from .protocol import TbotexFactory


# {(exchange.lower(), market_type.lower()): factory}
TBOTEX_PROVIDERS: Dict[Tuple[str, str], TbotexFactory] = {}


def register_provider(exchange: str, market_type: str):
    """Native provider registration decorator.

    Usage:
        @register_provider('binance', 'futures')
        def make_client(config, logger):
            return BinanceUsdmClient(config, logger)

    Direct invocation with 3 arguments (register_provider(ex, mt, factory))
    is NOT supported — it will result in a TypeError. Use only the decorator.
    """
    def deco(factory: TbotexFactory) -> TbotexFactory:
        key = ((exchange or '').lower(), (market_type or 'spot').lower())
        TBOTEX_PROVIDERS[key] = factory
        return factory
    return deco


def get_provider(exchange: str, market_type: str) -> Optional[TbotexFactory]:
    return TBOTEX_PROVIDERS.get(
        ((exchange or '').lower(), (market_type or 'spot').lower())
    )


def has_provider(exchange: str, market_type: str) -> bool:
    return get_provider(exchange, market_type) is not None


def list_available_pairs() -> List[Tuple[str, str]]:
    """For the UI: a list of pairs for which a native provider exists."""
    return sorted(TBOTEX_PROVIDERS.keys())