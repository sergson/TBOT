# modules/collector_module.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import hashlib
import sqlite3
import pandas as pd
import plotly.graph_objects as go
from dash import dcc, html

from .bot_registry import bot_registry
from .collector_bot import CollectorBot
from .database import get_bot_config
from .logger import perf_logger

logger = perf_logger.get_logger('collector_module', 'collector')

# ----- Add form definition -----
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

# ----- Configuration table schema (will be created as config_collector) -----
CONFIG_SCHEMA = {
    'exchange': 'TEXT NOT NULL',
    'market_type': 'TEXT NOT NULL',
    'symbol': 'TEXT NOT NULL',
    'timeframe': 'TEXT NOT NULL',
    'candles_limit': 'INTEGER NOT NULL',
    'data_db_path': 'TEXT NOT NULL'
}

# ----- Bot block rendering function in UI -----
def render_collector_block(bot_id: int, config: dict, relayout_store: dict):
    """Returns a Dash component to display the collector bot."""
    graph_id = {'type': 'graph', 'index': bot_id}
    status_button_id = {'type': 'status-btn', 'index': bot_id}
    delete_btn_id = {'type': 'delete', 'index': bot_id}

    # Build figure
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

def build_figure(bot_id: int, config: dict, relayout_store: dict):
    """Builds a candlestick chart from the bot's database."""
    db_path = config['data_db_path']
    table_name = config['symbol'].replace('/', '_').replace('-', '_')
    try:
        with sqlite3.connect(db_path) as conn:
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

# ----- Register the type in the registry -----
bot_registry.register(
    type_id='collector',
    display_name='Data Collector',
    form_component=collector_form,
    config_schema=CONFIG_SCHEMA,
    bot_class=CollectorBot,
    render_block=render_collector_block
)