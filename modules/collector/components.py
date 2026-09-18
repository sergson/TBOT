# modules/collector/components.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from core import auto_reg, colors, styles, graphics
from core.database import get_bot_config, get_all_bots, DBSQLite3
from core.logger import perf_logger
from dash import dcc, html, Output, Input, State, MATCH, ALL, no_update, callback_context
from .lib.exchange_directory import EXCHANGES, MARKETS, get_exchange_options, get_default_exchange_value
from .lib.fetcher import AsyncExchangeFetcher
from .lib.utils import safe_table_name

logger = perf_logger.get_logger('collector_module', 'collector')

COLLECTOR_FIELDS = ['exchange', 'market_type', 'symbol', 'timeframe', 'candles_limit']

# ---------- Add/Edit form ----------
def collector_form(current_bot_id=None):
    # Get the dynamic list of exchanges (ccxt + static settings)
    exchange_options = get_exchange_options()
    # Default value from the static EXCHANGES or the first available
    default_exchange = get_default_exchange_value()

    # Determine initial values
    exchange = default_exchange
    market_type = None
    symbol = None
    timeframe = '1m'
    candles_limit = 100

    # If editing an existing bot, take its configuration
    if current_bot_id is not None:
        cfg = get_bot_config(current_bot_id)
        if cfg:
            exchange = cfg.get('exchange', exchange)
            market_type = cfg.get('market_type', market_type)
            symbol = cfg.get('symbol', symbol)
            timeframe = cfg.get('timeframe', timeframe)
            candles_limit = cfg.get('candles_limit', candles_limit)

    # If the exchange from the config is missing in the list (e.g., removed from ccxt), take the default value
    if exchange not in [opt['value'] for opt in exchange_options]:
        exchange = default_exchange

    # Default values for market_type and symbol are taken from the static record of the selected exchange, if present
    exchange_entry = next((item for item in EXCHANGES if item.get('value') == exchange), {})
    defaults = exchange_entry.get('defaults') or {}
    if market_type is None:
        market_type = defaults.get('market')
    if symbol is None:
        symbol = defaults.get('symbol')
    if not symbol:
        symbol = 'BTC/USDT'

    fields = [
        dcc.Dropdown(
            id={'type': 'collector-field', 'field': 'exchange'},
            options=exchange_options,
            value=exchange
        ),
        dcc.Dropdown(
            id={'type': 'collector-field', 'field': 'market_type'},
            options=MARKETS,  # static list, will be updated by a callback on exchange change
            value=market_type
        ),
        html.Label("Pair"),
        dcc.Dropdown(
            id={"type": "collector-field", "field": "symbol"},
            options=[],  # filled by a callback
            value=symbol,
            placeholder="BTC/USDT",
            disabled=current_bot_id is not None  # when editing, the symbol is not changed
        ),
        dcc.Dropdown(
            id={'type': 'collector-field', 'field': 'timeframe'},
            options=[
                {'label': '1 minute', 'value': '1m'},
                {'label': '5 minutes', 'value': '5m'},
                {'label': '15 minutes', 'value': '15m'},
                {'label': '30 minutes', 'value': '30m'},
                {'label': '1 hour', 'value': '1h'},
                {'label': '2 hours', 'value': '2h'},
                {'label': '4 hours', 'value': '4h'},
                {'label': '1 day', 'value': '1d'}
            ],
            value=timeframe
        ),
        dcc.Dropdown(
            id={'type': 'collector-field', 'field': 'candles_limit'},
            options=[
                {'label': '100 candles', 'value': 100},
                {'label': '500 candles', 'value': 500},
                {'label': '1000 candles', 'value': 1000},
                {'label': '2000 candles', 'value': 2000}
            ],
            value=candles_limit
        )
    ]

    return html.Div(fields)


# ---------- Graph building function ----------
def build_figure(bot_id: int, config: dict, relayout_store: dict):
    db_path = config['data_db_path']
    table_name = safe_table_name(config['symbol'])

    env = DBSQLite3(db_path)
    try:
        candle_manager = env.get_model_manager('collector.candle', table_name)
        records = candle_manager.search([], order='timestamp ASC')
        if not records:
            return go.Figure()
        data = records.read()
        df = pd.DataFrame(data)
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
                        vertical_spacing=0.02,
                        row_heights=[0.7, 0.3])

    # Candlestick (row 1)
    fig.add_trace(graphics.candlestick_trace(df), row=1, col=1)

    # Volume bars (row 2)
    fig.add_trace(graphics.volume_bar_trace(df, color=colors.BLUE), row=2, col=1)

    # Apply universal layout
    graphics.apply_layout(
        fig,
        title=f"Bot {bot_id}: {config['exchange']} {config['symbol']} ({config['market_type']})",
        uirevision='collector',
        show_rangeslider=False,
        hovermode='x unified',
        legend_bordercolor=None,
        title_font_size=styles.FONT_SIZE_BOTHEADER
    )

    # Override axis labels for subplots
    fig.update_xaxes(title_text="", row=1, col=1)
    fig.update_xaxes(title_text="Time", row=2, col=1)
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)

    # Restore saved user changes
    stored = relayout_store.get(str(bot_id))
    if stored and isinstance(stored, dict):
        try:
            fig.update_layout(stored)
        except Exception as e:
            logger.warning(f"Failed to apply relayout for bot {bot_id}: {e}")

    return fig


# ---------- Render bot block in UI ----------
def render_collector_block(bot_id: int, config: dict, relayout_store: dict):
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
        style={'height': '400px'}
    )

    title_text = f"#{bot_id} Collector Bot  ←  {config['exchange']} {config['symbol']} ({config['market_type']})"

    return html.Details([
        html.Summary(title_text,
                     id={'type': 'collector-bot-header', 'index': bot_id},
                     style=styles.bot_card_header_style(config.get('status'))),
        html.Div([
            html.P(f"Timeframe: {config['timeframe']}, Storage: {config['candles_limit']} candles"),
            html.Button("Stop" if config.get('status') == 'running' else "Start",
                        id=status_button_id, n_clicks=0),
            html.Button("Edit", id=edit_btn_id, n_clicks=0),
            html.Button("Delete", id=delete_btn_id, n_clicks=0),
            html.Hr(),
            graph
        ])
    ], open=True, style=styles.STYLE_BOTCARD)


# ---------- Register bot type in registry ----------
logger.debug(f"Functions defined, about to define CollectorTypeMeta")

@auto_reg
class CollectorTypeMeta:
    _name = "collector.type"
    display_name = "Data Collector"
    form_component = staticmethod(collector_form)
    bot_model = "collector.bot"
    render_block = staticmethod(render_collector_block)

    @staticmethod
    def prepare_new_config(raw_config: dict) -> dict:
        config = raw_config.copy()
        if not config.get('symbol'):
            config['symbol'] = 'BTC/USDT'  # default value
        return config

    @staticmethod
    def register_callbacks(app, bot_manager, loop):
        @app.callback(
            Output({'type': 'collector-graph', 'index': MATCH}, 'figure'),
            Input('global-interval', 'n_intervals'),
            State({'type': 'collector-graph', 'index': MATCH}, 'id'),
            State('relayout-store', 'data')
        )
        def update_collector_graph(n, graph_id, relayout_store):
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
        def update_header_style(n, header_id):
            bot_id = header_id['index']
            bots = get_all_bots()
            bot = next((b for b in bots if b['id'] == bot_id), None)
            if not bot:
                return no_update
            status = bot['status']
            return styles.bot_card_header_style(status)

        @app.callback(
            Output({'type': 'collector-field', 'field': 'market_type'}, 'options'),
            Output({'type': 'collector-field', 'field': 'market_type'}, 'value'),
            Input({'type': 'collector-field', 'field': 'exchange'}, 'value'),
            prevent_initial_call=True
        )
        async def update_market_types(exchange_id):
            if not exchange_id:
                return [], None
            try:
                async with AsyncExchangeFetcher(exchange_id,
                                                'spot') as fetcher:  # market_type does not matter for loading markets
                    types = await fetcher.get_supported_market_types()
                options = [{'label': t.capitalize(), 'value': t} for t in types]
                # If spot is present, select it by default, otherwise the first one
                default_value = 'spot' if 'spot' in types else (types[0] if types else None)
                return options, default_value
            except Exception as e:
                logger.error(f"Error fetching market types for {exchange_id}: {e}")
                # Return the standard options
                return MARKETS, 'spot'

        @app.callback(
            Output({'type': 'collector-field', 'field': 'symbol'}, 'options'),
            Input({'type': 'collector-field', 'field': 'exchange'}, 'value'),
            Input({'type': 'collector-field', 'field': 'market_type'}, 'value')
        )
        async def update_symbols(exchange_id, market_type):
            if not exchange_id or not market_type:
                return []
            try:
                async with AsyncExchangeFetcher(exchange_id, market_type) as fetcher:
                    symbols = await fetcher.get_symbols()
                return [{'label': s, 'value': s} for s in symbols]
            except Exception as e:
                logger.error(f"Error loading the list of pairs for {exchange_id} ({market_type}): {e}")
                return []

    @staticmethod
    def process_edit_save(bot_id, new_fields, old_config):
        new_fields.pop('data_db_path', None)
        config = {k: v for k, v in old_config.items() if k in COLLECTOR_FIELDS}
        config.update(new_fields)
        if not config.get('symbol'):
            config['symbol'] = 'BTC/USDT'
        if 'data_db_path' not in config:
            config['data_db_path'] = f"data/bot_{bot_id}.db"
        config.pop('bot_type', None)
        return config

logger.debug(f"CollectorTypeMeta defined")