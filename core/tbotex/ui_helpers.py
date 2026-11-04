# core/tbotex/ui_helpers.py
#
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.
#
# UI helpers for the exchange_provider dropdown.
# Grid/collector
from dash import Input, Output, State

from .registry import has_provider


PROVIDER_OPTIONS = [
    {'label': 'ccxt (default)', 'value': 'ccxt'},
]

PROVIDER_OPTIONS_NATIVE = [
    {'label': 'ccxt (default)', 'value': 'ccxt'},
    {'label': 'tbotex (native)', 'value': 'tbotex'},
]


def build_provider_field(current_exchange, current_market, current_value):
    """Return (options, value, disabled) for exchange_provider dropdown.

    Pure function — easy to test.
    """
    if not current_exchange or not current_market:
        return PROVIDER_OPTIONS, 'ccxt', True
    if has_provider(current_exchange, current_market):
        value = current_value if current_value in ('ccxt', 'tbotex') else 'ccxt'
        return PROVIDER_OPTIONS_NATIVE, value, False
    return PROVIDER_OPTIONS, 'ccxt', True


def normalize_exchange_provider(value) -> str:
    """Normalization of `exchange_provider` to 'ccxt' | 'tbotex'.

    Used in `_normalize_trading_fields` (execution) and in
    `prepare_config_for_save`/`prepare_new_config` (grid/collector).
    Not explicitly described in "Plan 10" — included as an addition.
    """
    return value if value in ('ccxt', 'tbotex') else 'ccxt'


def register_exchange_provider_callback(app, field_type):
    """Register callback for exchange_provider dropdown options/value/disabled.

    field_type: 'execution-field' (grid/collector — патч G).
    """
    @app.callback(
        Output({'type': field_type, 'field': 'exchange_provider'}, 'options'),
        Output({'type': field_type, 'field': 'exchange_provider'}, 'value'),
        Output({'type': field_type, 'field': 'exchange_provider'}, 'disabled'),
        Input({'type': field_type, 'field': 'exchange'}, 'value'),
        Input({'type': field_type, 'field': 'market_type'}, 'value'),
        State({'type': field_type, 'field': 'exchange_provider'}, 'value'),
        prevent_initial_call=False,
    )
    def _update(exchange, market_type, current):
        return build_provider_field(exchange, market_type, current)

    return _update