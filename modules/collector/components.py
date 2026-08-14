# modules/collector/components.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import sqlite3
import pandas as pd
import plotly.graph_objects as go
from core import auto_reg, bot_registry
from core.database import get_bot_config, get_all_bots
from core.logger import perf_logger
from dash import dcc, html, Output, Input, State, MATCH, ALL, no_update, callback_context
from plotly.subplots import make_subplots

logger = perf_logger.get_logger('collector_module', 'collector')

COLLECTOR_FIELDS = ['exchange', 'market_type', 'symbol', 'timeframe', 'candles_limit']

# ---------- Add/Edit form ----------
def collector_form(current_bot_id=None):
    # default values
    exchange = 'binance'
    market_type = 'spot'
    symbol = 'BTC/USDT'
    timeframe = '1m'
    candles_limit = 100
    editing = current_bot_id is not None

    if editing:
        cfg = get_bot_config(current_bot_id)
        if cfg:
            exchange = cfg.get('exchange', exchange)
            market_type = cfg.get('market_type', market_type)
            symbol = cfg.get('symbol', symbol)
            timeframe = cfg.get('timeframe', timeframe)
            candles_limit = cfg.get('candles_limit', candles_limit)

    fields = [
        dcc.Dropdown(
            id={'type': 'collector-field', 'field': 'exchange'},
            options=[
                {'label': 'Binance', 'value': 'binance'},
                {'label': 'KuCoin', 'value': 'kucoin'},
                {'label': 'MEXC', 'value': 'mexc'},
                {'label': 'OKX', 'value': 'okx'},
                {'label': 'Bybit', 'value': 'bybit'}
            ],
            value=exchange
        ),
        dcc.Dropdown(
            id={'type': 'collector-field', 'field': 'market_type'},
            options=[
                {'label': 'Spot', 'value': 'spot'},
                {'label': 'Futures', 'value': 'futures'}
            ],
            value=market_type
        ),
        dcc.Input(
            id={'type': 'collector-field', 'field': 'symbol'},
            type='text', placeholder='BTC/USDT', value=symbol
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
    table_name = config['symbol'].replace('/', '_').replace('-', '_')
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            )
            if cursor.fetchone() is None:
                return go.Figure()

            df = pd.read_sql_query(f'''
                SELECT timestamp, open, high, low, close, volume
                FROM {table_name}
                ORDER BY timestamp ASC
            ''', conn)
    except Exception as e:
        logger.error(f"build_figure: error reading DB for bot {bot_id}: {e}")
        return go.Figure()

    if df.empty:
        return go.Figure()

    df['datetime'] = pd.to_datetime(df['timestamp'], unit='s', utc=True)

    # Subplots: 2 rows, shared x-axis, top 70% height, bottom 30%
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        vertical_spacing=0.02,
                        row_heights=[0.7, 0.3])

    # Candlestick (row 1)
    fig.add_trace(go.Candlestick(
        x=df['datetime'],
        open=df['open'], high=df['high'], low=df['low'], close=df['close'],
        name='Price'
    ), row=1, col=1)

    # Volume bars (row 2)
    fig.add_trace(go.Bar(
        x=df['datetime'], y=df['volume'],
        name='Volume',
        marker_color='lightblue'
    ), row=2, col=1)

    # Layout – remove range slider, enable uirevision to preserve zoom
    fig.update_layout(
        title=f"Bot {bot_id}: {config['exchange']} {config['symbol']} ({config['market_type']})",
        xaxis_rangeslider_visible=False,
        uirevision='collector',          # key for preserving UI state
        hovermode='x unified'
    )

    # Axis labels
    fig.update_xaxes(title_text="", row=1, col=1)        # hide on upper plot
    fig.update_xaxes(title_text="Time", row=2, col=1)
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)

    # Restore saved user changes (ranges, sizes)
    stored = relayout_store.get(str(bot_id))
    if stored and isinstance(stored, dict):
        try:
            fig.update_layout(stored)
        except Exception as e:
            logger.warning(f"Failed to apply relayout for bot {bot_id}: {e}")

    return fig

# ---------- Render bot block in UI ----------
def render_collector_block(bot_id: int, config: dict, relayout_store: dict):
    graph_id = {'type': 'collector-graph', 'index': bot_id}   # unique type
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

    return html.Div([
        html.Div([
            html.H3(f"{config['exchange']} {config['symbol']} ({config['market_type']})"),
            html.P(f"Timeframe: {config['timeframe']}, Storage: {config['candles_limit']} candles"),
            html.Button("Stop" if config.get('status') == 'running' else "Start",
                        id=status_button_id, n_clicks=0),
            html.Button("Edit", id=edit_btn_id, n_clicks=0),
            html.Button("Delete", id=delete_btn_id, n_clicks=0),
        ]),
        html.Hr(),
        graph
    ], id=f"bot-{bot_id}", style={'border': '1px solid black', 'padding': '10px', 'margin': '10px'})

# ---------- Register bot type in registry (not in bot_registry, but in separate metadata storage) ----------
# We can store type metadata in the class itself, but for Dash it's convenient to have a separate structure.

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
        return raw_config

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

    @staticmethod
    def process_edit_save(bot_id, new_fields, old_config):
        # Protection against path change
        new_fields.pop('data_db_path', None)
        # Take only valid collector fields
        config = {k: v for k, v in old_config.items() if k in COLLECTOR_FIELDS}
        config.update(new_fields)
        # Ensure data_db_path is present
        if 'data_db_path' not in config:
            config['data_db_path'] = f"data/bot_{bot_id}.db"
        config.pop('bot_type', None)
        return config

logger.debug(f"CollectorTypeMeta defined")