# modules/collector/components.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import pandas as pd
from dash import dcc, html, Output, Input, State, MATCH, no_update
from plotly.subplots import make_subplots
import plotly.graph_objects as go
from core import auto_reg, colors, styles, graphics
from core import styles as ui_compact
from core.styles import dropdown_field
from core.database import get_bot_config, get_all_bots, DBSQLite3
from core.logger import perf_logger
from modules.broker.lib.exchange_directory import (
    EXCHANGES, MARKETS, get_exchange_options, get_default_exchange_value,
)
from .lib.utils import safe_table_name
from .models import (
    FAST_MODE_AVAILABLE, FAST_TIMEFRAMES, SLOW_TIMEFRAMES,
)

logger = perf_logger.get_logger('collector_module', 'collector')

COLLECTOR_FIELDS = ['exchange', 'market_type', 'symbol', 'timeframe',
                    'candles_limit']

# TF < 1s excluded (write amplification on SQLite is unsustainable).
_TF_LABELS = {
    '1s': '1 sec',
    '5s': '5 sec',
    '15s': '15 sec',
    '30s': '30 sec',
    '1m': '1 minute',
    '5m': '5 minutes',
    '15m': '15 minutes',
    '30m': '30 minutes',
    '1h': '1 hour',
    '2h': '2 hours',
    '4h': '4 hours',
    '1d': '1 day',
}


def _timeframe_options():
    """Build the timeframe dropdown options respecting
    FAST_MODE_AVAILABLE.

    No callback is needed: the constant is read once at form render.
    """
    seq = list(FAST_TIMEFRAMES) if FAST_MODE_AVAILABLE else []
    seq += list(SLOW_TIMEFRAMES)
    seen, out = set(), []
    for v in seq:
        if v in seen:
            continue
        seen.add(v)
        out.append({'label': _TF_LABELS.get(v, v), 'value': v})
    return out


# ---------- Add/Edit form ----------
def collector_form(current_bot_id=None):
    # Retrieve a dynamic list of exchanges (ccxt + static settings)
    exchange_options = get_exchange_options()
    # Default value from the static EXCHANGES, or the first one available
    default_exchange = get_default_exchange_value()

    # Defining initial values
    exchange = default_exchange
    market_type = None
    symbol = None
    timeframe = '1m'
    candles_limit = 100

    # If we are editing an existing bot, we take its configuration
    cfg = {}
    if current_bot_id is not None:
        cfg = get_bot_config(current_bot_id) or {}
        if cfg:
            exchange = cfg.get('exchange', exchange)
            market_type = cfg.get('market_type', market_type)
            symbol = cfg.get('symbol', symbol)
            timeframe = cfg.get('timeframe', timeframe)
            candles_limit = cfg.get('candles_limit', candles_limit)

    # If an exchange from the config is missing from the list (for
    # example, it was removed from ccxt), use the default value
    if exchange not in [opt['value'] for opt in exchange_options]:
        exchange = default_exchange

    # Default values for market_type and symbol are taken from the
    # selected exchange's static record, if available
    exchange_entry = next(
        (item for item in EXCHANGES if item.get('value') == exchange), {})
    defaults = exchange_entry.get('defaults') or {}
    if market_type is None:
        market_type = defaults.get('market')
    if symbol is None:
        symbol = defaults.get('symbol')
    if not symbol:
        symbol = 'BTC/USDT'

    fields = [
        dropdown_field(
            {'type': 'collector-field', 'field': 'exchange'},
            "Exchange",
            options=exchange_options,
            value=exchange, inline=True,
        ),
        dropdown_field(
            {'type': 'collector-field', 'field': 'market_type'},
            "Market type",
            # Static list; updated via a callback when the exchange changes
            options=MARKETS,
            value=market_type, inline=True,
        ),
        dropdown_field(
            {"type": "collector-field", "field": "symbol"},
            "Pair",
            options=[],  # populated via callback
            value=symbol,
            disabled=current_bot_id is not None,
            inline=True,
        ),
        dropdown_field(
            {'type': 'collector-field', 'field': 'timeframe'},
            "Timeframe",
            options=_timeframe_options(),
            value=timeframe,
            inline=True,
        ),
        dropdown_field(
            {'type': 'collector-field', 'field': 'candles_limit'},
            "Storage",
            options=[
                {'label': '100 candles', 'value': 100},
                {'label': '500 candles', 'value': 500},
                {'label': '1000 candles', 'value': 1000},
                {'label': '2000 candles', 'value': 2000}
            ],
            value=candles_limit,
            inline=True,
        ),
    ]

    return html.Div(fields, style=ui_compact.STYLE_FORM)


# ---------- Graph building function ----------
def build_figure(bot_id: int, config: dict, relayout_store: dict):
    db_path = config['data_db_path']
    table_name = safe_table_name(config['symbol'])

    env = DBSQLite3(db_path)
    try:
        candle_manager = env.get_model_manager('collector.candle', table_name)
        candles_limit = int(config.get('candles_limit', 2000) or 2000)
        # ASC+LIMIT returns the OLDEST N — misses newest candles until
        # a prune collapses the table. Use DESC to grab the freshest N,
        # then sort back to ASC for plotly.
        records = candle_manager.search(
            [], order='timestamp DESC', limit=candles_limit)
        if not records:
            return go.Figure()
        data = records.read()
        df = pd.DataFrame(data)
        if not df.empty:
            df = df.sort_values('timestamp').reset_index(drop=True)
    except Exception as e:
        logger.error(f"build_figure: error reading DB for bot {bot_id}: {e}")
        return go.Figure()
    finally:
        env.close()

    if df.empty:
        return go.Figure()

    df['datetime'] = pd.to_datetime(df['timestamp'], unit='ns', utc=True)

    # Subplots: 2 rows, shared x-axis
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        vertical_spacing=0.005,
                        row_heights=[0.75, 0.25])

    # Candlestick (row 1)
    fig.add_trace(graphics.candlestick_trace(df), row=1, col=1)

    # Volume bars (row 2)
    fig.add_trace(graphics.volume_bar_trace(df, color=colors.BLUE),
                  row=2, col=1)

    # Apply universal compact layout
    graphics.apply_layout(
        fig,
        uirevision='collector',
        hovermode='x unified',
        compact=True,
    )

    # Explicitly clear secondary axes (compact=True does not reset the
    # title for subplots)
    fig.update_xaxes(title_text="", row=1, col=1)
    fig.update_xaxes(title_text="", row=2, col=1)
    fig.update_yaxes(title_text="", row=1, col=1)
    fig.update_yaxes(title_text="", row=2, col=1)

    # Restore saved user changes
    stored = relayout_store.get(f"{bot_id}:collector-graph")
    if stored and isinstance(stored, dict):
        try:
            normalized = graphics.normalize_dash_relayout(stored)
            if normalized:
                fig.update_layout(normalized)
        except Exception as e:
            logger.warning(
                f"Failed to apply relayout for bot {bot_id}: "
                f"{e}; stored={stored!r}")

    return fig


# ---------- Render bot block in UI ----------
def _format_candles_age(bot_id):
    """Show how many seconds ago the collector recorded the latest candle."""
    from core.time_sync import time_sync_service
    try:
        cfg = get_bot_config(bot_id) or {}
        db_path = cfg.get('data_db_path')
        if not db_path:
            return "—"
        from .lib.utils import safe_table_name
        table = safe_table_name(cfg.get('symbol', ''))
        env = DBSQLite3(db_path)
        try:
            mgr = env.get_model_manager('collector.candle', table)
            rows = mgr.search([], order='tbot_utc_stamp_ns DESC', limit=1)
            if not rows:
                return "no data"
            last_ns = rows[0].tbot_utc_stamp_ns or 0
        finally:
            env.close()
    except Exception:
        return "—"
    now_ns = (time_sync_service.get_utc_ns()
              if time_sync_service.is_synced_event.is_set()
              else __import__('time').time_ns())
    age_sec = max(0, (now_ns - last_ns) // 1_000_000_000) if last_ns else 0
    if age_sec < 20:
        return f"fresh ({age_sec}s)"
    if age_sec < 90:
        return f"{age_sec}s ago"
    return f"⚠ stale ({age_sec}s)"


def _render_collector_info_panel(bot_id, config):
    return graphics.info_panel([
        ("Exchange",
         f"{config['exchange']} {config['symbol']} "
         f"({config['market_type']})"),
        ("Feed",
         f"timeframe {config['timeframe']} | "
         f"retention {config['candles_limit']} candles | "
         f"last write: {_format_candles_age(bot_id)}"),
    ])


def render_collector_block(bot_id: int, config: dict,
                            relayout_store: dict):
    graph_id = {'type': 'collector-graph', 'index': bot_id}
    status_button_id = {'type': 'status-btn', 'index': bot_id}
    delete_btn_id = {'type': 'delete', 'index': bot_id}
    edit_btn_id = {'type': 'edit-btn', 'index': bot_id}

    fig = build_figure(bot_id, config, relayout_store)
    graph = dcc.Graph(
        id=graph_id,
        figure=fig,
        config={'scrollZoom': True, 'displayModeBar': True,
                'modeBarButtonsToRemove': ['lasso2d', 'select2d']},
        style={'height': '300px', "marginTop": styles.MARGIN_ZERO}
    )

    title_text = (f"#{bot_id} Collector Bot  ←  "
                  f"{config['exchange']} {config['symbol']} "
                  f"({config['market_type']})")

    return html.Details([
        html.Summary(
            title_text,
            id={'type': 'collector-bot-header', 'index': bot_id},
            style=styles.bot_card_header_style(config.get('status'))),
        html.Div([
            html.Div(
                id={'type': 'collector-info-panel', 'index': bot_id},
                children=_render_collector_info_panel(bot_id, config),
            ),
            html.Div([
                html.Button(
                    "Stop" if config.get('status') == 'running' else "Start",
                    id=status_button_id, n_clicks=0),
                html.Button("Edit", id=edit_btn_id, n_clicks=0),
                html.Button("Delete", id=delete_btn_id, n_clicks=0),
            ], style=styles.STYLE_BUTTON_ROW),
            html.Hr(style={"margin": "1px 0"}),
            graph
        ], style=styles.STYLE_BOTCARD_BODY)
    ], open=True, style=styles.STYLE_BOTCARD)


# ---------- Register bot type in registry ----------
logger.debug(f"Functions defined, about to define CollectorTypeMeta")


def collector_prepare_new_config(raw_config: dict) -> dict:
    """Normalization of the collector config upon creation/edit."""
    config = raw_config.copy()
    if not config.get('symbol'):
        config['symbol'] = 'BTC/USDT'
    try:
        v = int(config.get('candles_limit') or 100)
    except (TypeError, ValueError):
        v = 100
    if v not in (100, 500, 1000, 2000):
        v = min((100, 500, 1000, 2000), key=lambda x: abs(x - v))
    config['candles_limit'] = v

    # Timeframe validation — respects FAST_MODE_AVAILABLE.
    tf = config.get('timeframe')
    valid = set(SLOW_TIMEFRAMES)
    if FAST_MODE_AVAILABLE:
        valid |= set(FAST_TIMEFRAMES)
    if tf not in valid:
        config['timeframe'] = '1m'

    mt = config.get('market_type')
    if mt not in ('spot', 'futures'):
        config['market_type'] = 'spot'
    ex = config.get('exchange')
    if not ex:
        config['exchange'] = 'binance'
    return config


@auto_reg
class CollectorTypeMeta:
    _name = "collector.type"
    display_name = "Data Collector"
    form_component = staticmethod(collector_form)
    bot_model = "collector.bot"
    render_block = staticmethod(render_collector_block)

    @staticmethod
    def prepare_new_config(raw_config: dict) -> dict:
        return collector_prepare_new_config(raw_config)

    @staticmethod
    def register_callbacks(app, bot_manager, loop):  # noqa: ARG003
        @app.callback(
            Output({'type': 'collector-graph', 'index': MATCH}, 'figure'),
            Input('global-interval', 'n_intervals'),
            State({'type': 'collector-graph', 'index': MATCH}, 'id'),
            State('relayout-store', 'data')
        )
        def update_collector_graph(n, graph_id, relayout_store):  # noqa: ARG001
            bot_id = graph_id['index']
            bots = get_all_bots()
            bot = next((b for b in bots if b['id'] == bot_id), None)
            if not bot or bot['status'] != 'running':
                return no_update
            config = get_bot_config(bot_id)
            if not config:
                return no_update
            return build_figure(bot_id, config, relayout_store)

        @app.callback(
            Output({'type': 'collector-bot-header', 'index': MATCH}, 'style'),
            Input('global-interval', 'n_intervals'),
            State({'type': 'collector-bot-header', 'index': MATCH}, 'id')
        )
        def update_header_style(n, header_id):  # noqa: ARG001
            bot_id = header_id['index']
            bots = get_all_bots()
            bot = next((b for b in bots if b['id'] == bot_id), None)
            if not bot:
                return no_update
            status = bot['status']
            return styles.bot_card_header_style(status)

        @app.callback(
            Output({'type': 'collector-info-panel', 'index': MATCH},
                   'children'),
            Input('global-interval', 'n_intervals'),
            State({'type': 'collector-info-panel', 'index': MATCH}, 'id'),
        )
        def update_collector_info_panel(n, panel_id):  # noqa: ARG001
            bot_id = panel_id['index']
            config = get_bot_config(bot_id)
            if not config:
                return no_update
            return _render_collector_info_panel(bot_id, config)

        @app.callback(
            Output({'type': 'collector-field', 'field': 'market_type'},
                   'options'),
            Output({'type': 'collector-field', 'field': 'market_type'},
                   'value'),
            Input({'type': 'collector-field', 'field': 'exchange'}, 'value'),
            prevent_initial_call=True
        )
        def update_market_types(exchange_id):
            if not exchange_id:
                return [], None
            broker = _get_broker_instance(bot_manager)
            if broker is None:
                return MARKETS, 'spot'
            try:
                types = _run_broker_cap(
                    bot_manager, broker,
                    'get_supported_market_types', exchange_id,
                    owner="ui:collector_market_type")
                options = [{'label': t.capitalize(), 'value': t}
                           for t in (types or [])]
                default_value = ('spot' if 'spot' in (types or [])
                                 else (types[0] if types else None))
                return options, default_value
            except Exception as e:
                logger.error(f"market_types via broker failed: {e}")
                return MARKETS, 'spot'

        @app.callback(
            Output({'type': 'collector-field', 'field': 'symbol'}, 'options'),
            Input({'type': 'collector-field', 'field': 'exchange'}, 'value'),
            Input({'type': 'collector-field', 'field': 'market_type'}, 'value')
        )
        def update_symbols(exchange_id, market_type):
            if not exchange_id or not market_type:
                return []
            broker = _get_broker_instance(bot_manager)
            if broker is None:
                return []
            try:
                symbols = _run_broker_cap(
                    bot_manager, broker, 'get_symbols',
                    exchange_id, market_type,
                    owner="ui:collector_symbols")
                return [{'label': s, 'value': s} for s in (symbols or [])]
            except Exception as e:
                logger.error(f"symbols via broker failed: {e}")
                return []

        def _get_broker_instance(_bot_manager):
            for _bid, b in _bot_manager.bots.items():
                if getattr(b, '_name', '') == 'broker.bot':
                    return b
            return None

        def _run_broker_cap(_bot_manager, broker, cap_name, *args,
                            owner="ui:collector_form"):
            import asyncio
            from core.exchange import _owner_ctx
            caps = broker.get_capabilities()
            for key, cap in caps.items():
                if key == cap_name:
                    getter = cap.get('getter')
                    if getter is None:
                        raise ValueError(
                            f"capability '{cap_name}' has no getter")
                    _loop = _bot_manager.loop
                    if not _loop or not _loop.is_running():
                        raise RuntimeError("bot event loop not running")
                    token = _owner_ctx.set(owner)
                    try:
                        fut = asyncio.run_coroutine_threadsafe(
                            getter(*args), _loop)
                        return fut.result(timeout=15)
                    finally:
                        _owner_ctx.reset(token)
            raise ValueError(
                f"Broker does not expose '{cap_name}'")

    @staticmethod
    def process_edit_save(bot_id, new_fields, old_config):
        new_fields.pop('data_db_path', None)
        config = {k: v for k, v in old_config.items()
                  if k in COLLECTOR_FIELDS}
        config.update(new_fields)
        if not config.get('symbol'):
            config['symbol'] = 'BTC/USDT'
        if 'data_db_path' not in config:
            config['data_db_path'] = f"data/bot_{bot_id}.db"
        config.pop('bot_type', None)
        return collector_prepare_new_config(config)


logger.debug(f"CollectorTypeMeta defined")