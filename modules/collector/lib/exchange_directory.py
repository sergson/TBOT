# modules/collector/lib/exchange_directory.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.
"""
T.B.O.T project collector bot exchange setting directory.
"""

import ccxt


# Default markets list
MARKETS = [
                {'label': 'Spot', 'value': 'spot'},
                {'label': 'Futures', 'value': 'futures'}
            ]

#Default exchanges and default exchange
EXCHANGES = [
    {'label': 'Binance', 'value': 'binance', 'default':True,
     'defaults':{'market':'spot',
                'symbol':'BTC/USDT'},
     },
    {'label': 'KuCoin', 'value': 'kucoin'},
    {'label': 'MEXC', 'value': 'mexc'},
    {'label': 'OKX', 'value': 'okx','markets':MARKETS},
    {'label': 'Bybit', 'value': 'bybit'}
]


def get_exchange_options():
    """
    Returns the complete list of options for the exchange selection dropdown.
    Combines ccxt.exchanges with the static settings from EXCHANGES.
    """
    # Get all exchanges from ccxt
    try:
        all_exchange_ids = ccxt.exchanges  # list of strings
    except Exception:
        # If ccxt is unavailable, use only the static list
        all_exchange_ids = [item['value'] for item in EXCHANGES]

    # Create a dictionary of custom settings by value for quick lookup
    custom = {item['value']: item for item in EXCHANGES}

    options = []
    for ex_id in all_exchange_ids:
        if ex_id in custom:
            # There is a custom entry — take the label and all additional fields
            entry = custom[ex_id]
            options.append({
                'label': entry.get('label', ex_id.capitalize()),
                'value': ex_id,
                'defaults': entry.get('defaults', {}),
                'markets': entry.get('markets', None),
                # any other fields can be added if needed
            })
        else:
            # No custom entry — generate the label automatically
            options.append({
                'label': ex_id.capitalize(),
                'value': ex_id,
                'defaults': {},
                'markets': None,
            })

    # Sort by label for convenience (optional)
    options.sort(key=lambda x: x['label'])

    return options

def get_default_exchange_value():
    """Returns the default exchange value (from static EXCHANGES or the first from ccxt)."""
    # Look for an exchange with default=True in the static list
    for item in EXCHANGES:
        if item.get('default'):
            return item['value']
    # Otherwise return the first available one
    options = get_exchange_options()
    return options[0]['value'] if options else None