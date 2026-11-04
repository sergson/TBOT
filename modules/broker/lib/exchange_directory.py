# modules/broker/lib/exchange_directory.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

"""
Shared exchange/market directory for T.B.O.T bots.

Used by both collector and execution modules (and any future bot type
that needs to pick an exchange and a market type). Keeping a single
source of truth avoids the collector and execution dropdowns slowly
drifting apart.

This file contains four kinds of knowledge about exchanges:

  1. EXCHANGES / MARKETS — presentation data for the UI dropdowns.
  2. Parameter translation — build_client_id_params, get_ccxt_market_type,
     resolve_ccxt_exchange_id.
  3. install_exchange_clock — hook that wires a ccxt instance to TBOT UTC
     so signed REST requests do not fail with -1021 InvalidNonce when
     the OS wall clock drifts from the exchange server clock.
  4. EXCHANGE_WORKAROUNDS — per-exchange behavioural overrides applied
     by runtime components (WS streams, order placement, etc.).

Rule: if you ever find yourself writing
    `if exchange_name == 'mexc' and market_type == 'futures':`
somewhere outside this file — stop and put the condition into
EXCHANGE_WORKAROUNDS instead. Runtime code queries it via
get_workaround(exchange, market_type, key, default).

Extending:
  * new exchange in the UI → add to EXCHANGES;
  * new ccxt-parameter difference → add to _PARAM_MAP;
  * new runtime quirk → add to EXCHANGE_WORKAROUNDS with a '_note'
    explaining WHY. When the underlying ccxt/exchange issue is fixed,
    remove the entry — no other code changes required.
"""

from typing import Optional
import ccxt
from core.logger import perf_logger

logger = perf_logger.get_logger('exchange_directory', 'app')


MARKETS = [
    {'label': 'Spot', 'value': 'spot'},
    {'label': 'Futures', 'value': 'futures'},
]


EXCHANGES = [
    {'label': 'Binance', 'value': 'binance', 'default': True,
     'defaults': {'market': 'spot', 'symbol': 'BTC/USDT'}},
    {'label': 'KuCoin', 'value': 'kucoin'},
    {'label': 'MEXC', 'value': 'mexc'},
    {'label': 'OKX', 'value': 'okx'},
    {'label': 'Bybit', 'value': 'bybit'},
]

# ----------------------------------------------------------------------
# ccxt parameter translation
# ----------------------------------------------------------------------
#
# ccxt does not unify the name of the client-order-id parameter across
# operations (create / cancel / fetch) and across exchanges:
#
#     | Exchange | create              | cancel             | fetch              |
#     |----------|---------------------|--------------------|--------------------|
#     | Binance  | newClientOrderId    | origClientOrderId  | origClientOrderId  |
#     | Bybit    | orderLinkId         | orderLinkId        | orderLinkId        |
#     | KuCoin   | clientOid           | clientOid          | clientOid          |
#     | OKX      | clOrdId             | clOrdId            | clOrdId            |
#     | MEXC     | clientOrderId       | clientOrderId      | (order_id only)    |
#     | Gate     | text                | text               | text               |
#
# Passing `clientOrderId` blindly works on some exchanges and silently
# fails on others. OrderManager calls build_client_id_params(exchange,
# operation, cid) and gets back a dict ready to hand to ccxt as `params`.
#
# Extending: add an entry under the exchange's ccxt id (lowercase).
# Use None for an operation to mean "do not pass the cid at all".

DEFAULT_CLIENT_ID_PARAM = "clientOrderId"


# exchange_id (lowercase) -> {operation: param_name | None}
# operation ∈ {'create', 'cancel', 'fetch'}
_PARAM_MAP = {
    # --- Binance ---
    "binance": {
        "create": "newClientOrderId",
        "cancel": "origClientOrderId",
        "fetch":  "origClientOrderId",
    },
    "binanceusdm": {
        "create": "newClientOrderId",
        "cancel": "origClientOrderId",
        "fetch":  "origClientOrderId",
    },
    "binancecoinm": {
        "create": "newClientOrderId",
        "cancel": "origClientOrderId",
        "fetch":  "origClientOrderId",
    },

    # --- Bybit ---
    "bybit": {
        "create": "orderLinkId",
        "cancel": "orderLinkId",
        "fetch":  "orderLinkId",
    },

    # --- KuCoin ---
    "kucoin": {
        "create": "clientOid",
        "cancel": "clientOid",
        "fetch":  "clientOid",
    },
    "kucoinfutures": {
        "create": "clientOid",
        "cancel": "clientOid",
        "fetch":  "clientOid",
    },

    # --- OKX ---
    "okx": {
        "create": "clOrdId",
        "cancel": "clOrdId",
        "fetch":  "clOrdId",
    },

    # --- MEXC ---
    # MEXC spot (v3): create and cancel accept `clientOrderId`.
    # fetch_order by client id is not universally reliable in ccxt,
    # and we anyway use exchange order_id as the primary key —
    # so for fetch we pass nothing and rely on order_id alone.
    "mexc": {
        "create": "clientOrderId",
        "cancel": "clientOrderId",
        "fetch":  None,
    },
    "mexc3": {
        "create": "clientOrderId",
        "cancel": "clientOrderId",
        "fetch":  None,
    },

    # --- Gate ---
    "gate": {
        "create": "text",
        "cancel": "text",
        "fetch":  "text",
    },
    "gateio": {
        "create": "text",
        "cancel": "text",
        "fetch":  "text",
    },

    # --- HTX (ex-Huobi) ---
    "htx": {
        "create": "client-order-id",
        "cancel": "client-order-id",
        "fetch":  "client-order-id",
    },

    # Add more exchanges here as needed.
}


def get_client_id_param(exchange_name: str, operation: str) -> Optional[str]:
    """Return the ccxt parameter name for a client order id.

    Returns None when the operation explicitly opts out of passing the cid
    (see MEXC fetch).
    Unknown exchanges fall back to DEFAULT_CLIENT_ID_PARAM.
    Unknown operations fall back to DEFAULT_CLIENT_ID_PARAM.
    """
    entry = _PARAM_MAP.get((exchange_name or "").lower(), {})
    if operation in entry:
        return entry[operation]
    return DEFAULT_CLIENT_ID_PARAM


def build_client_id_params(exchange_name: str,
                           operation: str,
                           client_order_id: Optional[str]) -> dict:
    """Build the `params` dict for ccxt.

    Returns {} when:
      * client_order_id is None/empty,
      * or the exchange opts out for this operation.
    """
    if not client_order_id:
        return {}
    key = get_client_id_param(exchange_name, operation)
    if key is None:
        return {}
    return {key: client_order_id}


def get_exchange_options():
    """Full option list for the exchange dropdown.

    Merges ccxt.exchanges with the static EXCHANGES entries above.
    On ccxt failure falls back to EXCHANGES only.
    """
    try:
        all_exchange_ids = ccxt.exchanges
    except Exception as e:
        logger.error(f"Failed to get exchange list from ccxt: {e}")
        all_exchange_ids = [item['value'] for item in EXCHANGES]

    custom = {item['value']: item for item in EXCHANGES}

    options = []
    for ex_id in all_exchange_ids:
        entry = custom.get(ex_id)
        if entry:
            options.append({
                'label': entry.get('label', ex_id.capitalize()),
                'value': ex_id,
                'defaults': entry.get('defaults', {}),
            })
        else:
            options.append({
                'label': ex_id.capitalize(),
                'value': ex_id,
                'defaults': {},
            })

    options.sort(key=lambda x: x['label'])
    return options


def get_default_exchange_value():
    """Value of the exchange marked default=True, or first in the list."""
    for item in EXCHANGES:
        if item.get('default'):
            return item['value']
    options = get_exchange_options()
    return options[0]['value'] if options else None

def resolve_ccxt_exchange_id(exchange_name: str, market_type: str) -> str:
    """Return the ccxt exchange id to instantiate.

    ccxt's 'binance' class is a union of spot + USDT-M + COIN-M markets.
    Setting options.defaultType does NOT restrict load_markets() — it only
    affects which endpoint fetch_*/create_order routes to. As a result,
    exchange.markets contains COIN-M perpetual and quarterly symbols,
    and a naive type-based filter can let COIN-M entries through.

    For unified 'futures' we want USDT-M only, so we instantiate the
    dedicated ccxt class 'binanceusdm' instead. It contains USDT-M
    markets exclusively — perpetual ('swap') and quarterly ('future').

    Same logic applies to Kucoin: 'kucoinfutures' is the futures-only
    class, 'kucoin' is the union.

    All other exchanges are returned as-is.
    """
    ex = (exchange_name or '').lower()
    mt = (market_type or 'spot').lower()
    if mt == 'futures':
        if ex == 'binance':
            return 'binanceusdm'
        if ex == 'kucoin':
            return 'kucoinfutures'
    return ex


def get_ccxt_market_type(exchange_name: str, market_type: str) -> str:
    """Translate unified market_type into ccxt's per-exchange type.

    Unified values used in the UI:  'spot' | 'futures'.
    ccxt values vary per exchange:  'spot' | 'future' | 'swap' | 'linear'.

    For 'futures' the default is 'swap' (perpetual), because that is
    what grid/execution bots actually trade. Exchanges that expose
    quarterly/delivery contracts under 'future' are not exposed via
    the UI unless explicitly added.

    Mirrored by every runtime component that instantiates a ccxt client
    (AsyncExchangeFetcher, OrderManager, ExchangeWebSocket) so the symbol
    picker and the trading client see the same market set.
    """
    mt = (market_type or 'spot').lower()
    if mt != 'futures':
        return 'spot'
    ex = (exchange_name or '').lower()
    if ex in ('binance', 'binanceusdm', 'binancecoinm',
              'kucoin', 'kucoinfutures'):
        return 'swap'
    if ex in ('mexc', 'okx'):
        return 'swap'
    if ex == 'bybit':
        return 'linear'
    return 'swap'


# ----------------------------------------------------------------------
# Per-exchange runtime workarounds
# ----------------------------------------------------------------------
#
# Static registry of substitutions that must be applied when talking to a
# specific (exchange, ccxt_market_type) pair, because ccxt or the
# exchange's API behaves in a way that requires special handling.
#
# This is the ONE place in the codebase where such exceptions live.
# Whenever you find yourself adding `if exchange == ...` outside this
# file, move the condition here and query it via get_workaround().
#
# Key:   (exchange_name.lower(), ccxt_market_type)
# Value: dict of overrides. Currently supported keys:
#
#   'price_stream' : 'watch_bids_asks' | 'watch_order_book'
#                  | 'watch_ticker'   | 'rest_poll'
#                    Which feed the price stream uses.
#                    Absent → 'watch_bids_asks' (the default).
#
#   'price_poll_interval_sec' : interval for the 'rest_poll' feed, sec.
#
#   '_note'        : free-form comment; not consumed by code.
#
EXCHANGE_WORKAROUNDS = {
    ('mexc', 'swap'): {
        'price_stream': 'rest_poll',
        'price_poll_interval_sec': 1.0,
        'balance_type': 'swap',
        '_note': (
            "All three ccxt.pro WS feeds on MEXC swap are broken. "
            "Use REST poll. balance_type='swap' for futures wallet."
        ),
    },
    ('binance', 'swap'): {
        'price_stream': 'watch_order_book',
        'balance_type': 'future',
        '_note': (
            "watch_bids_asks broken on Binance USDT-M perp. Use "
            "watch_order_book. fetch_balance requires type='future'."
        ),
    },
    ('bybit', 'linear'): {
        'balance_type': 'swap',
        '_note': "Bybit linear futures: balance_type='swap'.",
    },
    ('okx', 'swap'): {
        'balance_type': 'swap',
        '_note': "OKX swap: balance_type='swap'.",
    },
    ('kucoin', 'swap'): {
        'balance_type': None,
        '_note': (
            "ccxt uses defaultType='swap'; explicit type param breaks "
            "fetch_balance on kucoinfutures."
        ),
    },
}

def get_workaround(exchange_name: str,
                   market_type: str,
                   key: str,
                   default=None):
    """Look up a per-exchange runtime workaround value.

    exchange_name — ccxt id (e.g. 'mexc')
    market_type   — unified market_type ('spot' | 'futures')
    key           — workaround key (e.g. 'price_stream')
    default       — returned when no entry or the key is absent

    Unknown exchanges and unknown keys always yield `default`; there is
    no fallback to a wildcard entry — by design, to keep behaviour
    predictable.
    """
    ccxt_type = get_ccxt_market_type(exchange_name, market_type)
    entry = EXCHANGE_WORKAROUNDS.get(
        ((exchange_name or '').lower(), ccxt_type)
    )
    if not entry:
        return default
    return entry.get(key, default)


def get_price_stream_method(exchange_name: str, market_type: str) -> str:
    """Which ccxt.pro method to use for the price feed.

    Returns 'watch_bids_asks' (default) or 'watch_order_book' when a
    workaround is active for the given (exchange, market_type).
    """
    return get_workaround(
        exchange_name, market_type, 'price_stream', 'watch_bids_asks'
    )