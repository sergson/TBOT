# modules/collector/components.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import hashlib
import sqlite3
import pandas as pd
import plotly.graph_objects as go
from dash import dcc, html
from core import auto_reg, bot_registry
from core.database import get_bot_config
from core.logger import perf_logger

logger = perf_logger.get_logger('collector_module', 'collector')

# ---------- Add form ----------
def collector_form():
    return html.Div([
        dcc.Dropdown(
            id={'type': 'collector-field', 'field': 'exchange'},
            options=[
                {'label': 'Binance', 'value': 'binance'},
                {'label': 'KuCoin', 'value': 'kucoin'},
                {'label': 'MEXC', 'value': 'mexc'},
                {'label': 'OKX', 'value': 'okx'},
                {'label': 'Bybit', 'value': 'bybit'}
            ],
            value='binance'
        ),
        dcc.Dropdown(
            id={'type': 'collector-field', 'field': 'market_type'},
            options=[
                {'label': 'Spot', 'value': 'spot'},
                {'label': 'Futures', 'value': 'futures'}
            ],
            value='spot'
        ),
        dcc.Input(
            id={'type': 'collector-field', 'field': 'symbol'},
            type='text', placeholder='BTC/USDT', value='BTC/USDT'
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
            value='1m'
        ),
        dcc.Dropdown(
            id={'type': 'collector-field', 'field': 'candles_limit'},
            options=[
                {'label': '100 candles', 'value': 100},
                {'label': '500 candles', 'value': 500},
                {'label': '1000 candles', 'value': 1000},
                {'label': '2000 candles', 'value': 2000}
            ],
            value=500
        ),
        dcc.Input(
            id={'type': 'collector-field', 'field': 'data_db_path'},
            type='text', placeholder='data/bot_123.db', value='', disabled=True
        )
    ])

# ---------- Configuration schema ----------
CONFIG_SCHEMA = {
    'exchange': 'TEXT NOT NULL',
    'market_type': 'TEXT NOT NULL',
    'symbol': 'TEXT NOT NULL',
    'timeframe': 'TEXT NOT NULL',
    'candles_limit': 'INTEGER NOT NULL',
    'data_db_path': 'TEXT NOT NULL'
}

# ---------- Graph building function ----------
def build_figure(bot_id: int, config: dict, relayout_store: dict):
    db_path = config['data_db_path']
    table_name = config['symbol'].replace('/', '_').replace('-', '_')
    try:
        with sqlite3.connect(db_path) as conn:
            # Check if table exists
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            )
            if cursor.fetchone() is None:
                logger.info(f"Table {table_name} does not exist yet, returning empty figure")
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
    fig = go.Figure(data=[go.Candlestick(
        x=df['datetime'],
        open=df['open'],
        high=df['high'],
        low=df['low'],
        close=df['close']
    )])
    fig.update_layout(title=f"Bot {bot_id}: {config['exchange']} {config['symbol']} ({config['market_type']})")

    stored = relayout_store.get(str(bot_id))
    if stored and isinstance(stored, dict):
        try:
            fig.update_layout(stored)
        except Exception as e:
            logger.warning(f"Failed to apply relayout for bot {bot_id}: {e}")
    return fig

# ---------- Render bot block in UI ----------
def render_collector_block(bot_id: int, config: dict, relayout_store: dict):
    graph_id = {'type': 'graph', 'index': bot_id}
    logger.debug(f"Render_collector_block: creating graph with id {graph_id}")
    status_button_id = {'type': 'status-btn', 'index': bot_id}
    delete_btn_id = {'type': 'delete', 'index': bot_id}

    fig = build_figure(bot_id, config, relayout_store)

    graph = dcc.Graph(
        id=graph_id,
        figure=fig,
        config={
            'scrollZoom': True,
            'displayModeBar': True,
            'modeBarButtonsToRemove': ['lasso2d', 'select2d']
        },
        style={'height': '400px'}
    )

    return html.Div([
        html.H3(f"{config['exchange']} {config['symbol']} ({config['market_type']})"),
        html.P(f"Timeframe: {config['timeframe']}, Storage: {config['candles_limit']} candles"),
        html.Button("Stop" if config.get('status') == 'running' else "Start",
                    id=status_button_id, n_clicks=0),
        html.Button("Delete", id=delete_btn_id, n_clicks=0),
        html.Hr(),
        graph
    ], id=f"bot-{bot_id}", style={'border': '1px solid black', 'padding': '10px', 'margin': '10px'})

# ---------- Register bot type in registry (not in bot_registry, but in separate metadata storage) ----------
# We can store type metadata in the class itself, but for Dash it's convenient to have a separate structure.

logger.debug(f"Functions defined, about to define CollectorTypeMeta")


@auto_reg
class CollectorTypeMeta:
    """Stub class for storing metadata of type 'collector'."""
    _name = "collector.type"
    display_name = "Data Collector"
    form_component = staticmethod(collector_form)
    config_schema = CONFIG_SCHEMA
    bot_model = "collector.bot"  # reference to the bot model name
    render_block = staticmethod(render_collector_block)

logger.debug(f"CollectorTypeMeta defined")