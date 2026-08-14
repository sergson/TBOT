# modules/grid/components.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import json
import sqlite3
import os
import pandas as pd
import plotly.graph_objects as go
import asyncio
from dash import dcc, html, dash_table, Output, Input, State, MATCH, ALL, no_update, callback_context
from core import auto_reg
from core.database import get_all_bots, get_bot_config
from core.logger import perf_logger

logger = perf_logger.get_logger("grid_bot_ui", "analytics")

# ----------------------------------------------------------------------
# Helper: get list of tables from collector database
# ----------------------------------------------------------------------
def get_collector_tables(collector_bot_id: int) -> list:
    try:
        config = get_bot_config(collector_bot_id)
        if not config or config.get('bot_type') != 'collector':
            return []
        db_path = config.get('data_db_path')
        if not db_path or not os.path.exists(db_path):
            return []
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        tables = [row[0] for row in cursor.fetchall()]
        conn.close()
        return [{"label": t, "value": t} for t in tables]
    except Exception as e:
        logger.error(f"Error getting collector tables: {e}")
        return []

# ----------------------------------------------------------------------
# Normalize configuration before saving (call in app.py)
# ----------------------------------------------------------------------
def prepare_config_for_save(raw_config: dict) -> dict:
    cleaned = raw_config.copy()
    if 'use_analyst_close' in cleaned:
        val = cleaned['use_analyst_close']
        cleaned['use_analyst_close'] = 1 if (isinstance(val, list) and 1 in val) else 0
    numeric_fields = ['leverage', 'max_averaging_count', 'smart_averaging_count',
                      'poll_interval_sec', 'execution_timeout_sec', 'execution_reserve_sec']
    # deals_display_count removed
    for f in numeric_fields:
        if f in cleaned and cleaned[f] is not None:
            cleaned[f] = int(cleaned[f])
    float_fields = ['averaging_threshold_pnl', 'breakeven_pnl', 'close_pnl', 'liquidation_pnl']
    for f in float_fields:
        if f in cleaned and cleaned[f] is not None:
            cleaned[f] = float(cleaned[f])
    cleaned.pop('bot_type', None)
    return cleaned

# ----------------------------------------------------------------------
# Create / edit form (unchanged except deals_display_count removed)
# ----------------------------------------------------------------------
def form_component(current_bot_id=None):
    all_bots = get_all_bots()
    collector_options = [
        {"label": f"{b['name']} (ID {b['id']})", "value": b["id"]}
        for b in all_bots if b["type"] == "collector"
    ]
    analyst_options = [
        {"label": f"{b['name']} (ID {b['id']})", "value": b["id"]}
        for b in all_bots if b["type"] == "analyst"
    ] or [{"label": "None", "value": None}]

    collector_bot_id = None
    collector_table = None
    analyst_bot_id = None
    use_analyst_close = 1
    leverage = 200
    averaging_strategy = "sum_prev"
    averaging_threshold_pnl = 20
    max_averaging_count = 7
    smart_averaging_count = 2
    breakeven_pnl = 4.0
    close_pnl = 50.0
    liquidation_pnl = 90
    poll_interval_sec = 30
    execution_timeout_sec = 0
    recalc_strategy = "recalc_grid"
    execution_reserve_sec = 30

    editing = current_bot_id is not None
    cfg = {}
    if editing:
        cfg = get_bot_config(current_bot_id)
        if cfg:
            collector_bot_id = cfg.get('collector_bot_id', collector_bot_id)
            collector_table = cfg.get('collector_table', collector_table)
            analyst_bot_id = cfg.get('analyst_bot_id', analyst_bot_id)
            use_analyst_close = cfg.get('use_analyst_close', use_analyst_close)
            leverage = cfg.get('leverage', leverage)
            averaging_strategy = cfg.get('averaging_strategy', averaging_strategy)
            averaging_threshold_pnl = cfg.get('averaging_threshold_pnl', averaging_threshold_pnl)
            max_averaging_count = cfg.get('max_averaging_count', max_averaging_count)
            smart_averaging_count = cfg.get('smart_averaging_count', smart_averaging_count)
            breakeven_pnl = cfg.get('breakeven_pnl', breakeven_pnl)
            close_pnl = cfg.get('close_pnl', close_pnl)
            liquidation_pnl = cfg.get('liquidation_pnl', liquidation_pnl)
            poll_interval_sec = cfg.get('poll_interval_sec', poll_interval_sec)
            execution_timeout_sec = cfg.get('execution_timeout_sec', execution_timeout_sec)
            recalc_strategy = cfg.get('recalc_strategy', recalc_strategy)
            execution_reserve_sec = cfg.get('execution_reserve_sec', execution_reserve_sec)

    if editing:
        liquidation_order_type = cfg.get('liquidation_order_type', 'market')
    else:
        liquidation_order_type = 'market'

    return html.Div([
        html.Label("Collector Bot"),
        dcc.Dropdown(
            id={"type": "grid-field", "field": "collector_bot_id"},
            options=collector_options,
            value=collector_bot_id,
            placeholder="Select collector",
            disabled=editing
        ),
        html.Label("Collector Table Name (optional)"),
        dcc.Dropdown(
            id={"type": "grid-field", "field": "collector_table"},
            options=[],
            value=collector_table,
            placeholder="Auto-select first table",
        ),
        html.Label("Analyst Bot (optional)"),
        dcc.Dropdown(
            id={"type": "grid-field", "field": "analyst_bot_id"},
            options=analyst_options,
            value=analyst_bot_id,
            placeholder="Select analyst",
            disabled=editing
        ),
        html.Label("Use analyst signal for closing"),
        dcc.RadioItems(
            id={"type": "grid-field", "field": "use_analyst_close"},
            options=[{"label": "Yes", "value": 1}, {"label": "No", "value": 0}],
            value=use_analyst_close
        ),
        html.Label("Leverage"),
        dcc.Input(id={"type": "grid-field", "field": "leverage"}, type="number", min=1, max=500, value=leverage),
        html.Label("Averaging Strategy"),
        dcc.Dropdown(
            id={"type": "grid-field", "field": "averaging_strategy"},
            options=[
                {"label": "Equal volumes [1,1,1,...]", "value": "equal"},
                {"label": "Sum previous [1,2,4,8,16,32,...]", "value": "sum_prev"},
                {"label": "Double [2,4,8,16,32,64...]", "value": "double"},
                {"label": "Triple [1,3,9,27,81,...]", "value": "triple"},
            ],
            value=averaging_strategy,
        ),
        html.Label("Averaging Threshold PNL (%)"),
        dcc.Input(id={"type": "grid-field", "field": "averaging_threshold_pnl"}, type="number", value=averaging_threshold_pnl, min=1, max=90, step=1),
        html.Label("Max Averaging Count"),
        dcc.Input(id={"type": "grid-field", "field": "max_averaging_count"}, type="number", value=max_averaging_count, min=1, max=50000),
        html.Label("Smart Averaging Count"),
        dcc.Input(id={"type": "grid-field", "field": "smart_averaging_count"}, type="number", value=smart_averaging_count, min=0, max=50000),
        html.Label("PNL at which price stop-loss stops moving into loss (Break‑even PNL %)"),
        dcc.Input(id={"type": "grid-field", "field": "breakeven_pnl"}, type="number", value=breakeven_pnl, min=0, max=50, step=0.01),
        html.Label("Target close PNL (must be ≥ Break‑even) %"),
        dcc.Input(id={"type": "grid-field", "field": "close_pnl"}, type="number", value=close_pnl, min=0.01, step=0.01),
        html.Label("Liquidation PNL (%)"),
        dcc.Input(id={"type": "grid-field", "field": "liquidation_pnl"}, type="number", value=liquidation_pnl, min=20, max=100, step=1),
        html.Label("Liquidation Order Type"),
        dcc.Dropdown(
            id={"type": "grid-field", "field": "liquidation_order_type"},
            options=[
                {"label": "Market", "value": "market"},
                {"label": "Limit", "value": "limit"},
            ],
            value=liquidation_order_type,
        ),
        html.Label("Interval between signal condition checks (seconds)"),
        dcc.Input(id={"type": "grid-field", "field": "poll_interval_sec"}, type="number", value=poll_interval_sec, min=10, max=3600),
        html.Label("Signal lifetime; after this time unexecuted signal triggers recalculation strategy (seconds)"),
        dcc.Input(id={"type": "grid-field", "field": "execution_timeout_sec"}, type="number", value=execution_timeout_sec, min=0, max=3600),
        html.Label("Signal Recalc Strategy"),
        dcc.Dropdown(
            id={"type": "grid-field", "field": "recalc_strategy"},
            options=[
                {"label": "No recalc", "value": "none"},
                {"label": "Recalc grid", "value": "recalc_grid"},
                {"label": "Market order nearest", "value": "market_order"},
            ],
            value=recalc_strategy,
        ),
        html.Label("Seconds to anticipate the level crossing (price extrapolation)"),
        dcc.Input(id={"type": "grid-field", "field": "execution_reserve_sec"}, type="number", value=execution_reserve_sec, min=0, max=3600),
        # deals_display_count field removed
    ])

# ----------------------------------------------------------------------
# Build current position chart (main) – unchanged
# ----------------------------------------------------------------------
def build_figure(bot_id: int, config: dict, relayout_store: dict) -> go.Figure:
    db_path = f"data/bot_{bot_id}.db"
    fig = go.Figure()
    price_df = pd.DataFrame()

    try:
        conn = sqlite3.connect(db_path)
        price_df = pd.read_sql_query(
            "SELECT timestamp, close FROM price_history ORDER BY timestamp ASC", conn
        )
        conn.close()
        if not price_df.empty:
            price_df['datetime'] = pd.to_datetime(price_df['timestamp'], unit='s', utc=True)
            fig.add_trace(go.Scatter(
                x=price_df['datetime'], y=price_df['close'],
                mode='lines', name='Price',
                line=dict(color='black', width=2),
                hovertemplate='Price: %{y:.2f}<extra></extra>'
            ))
    except Exception as e:
        logger.error(f"Error loading price history: {e}")

    pos_id = get_current_position_id_from_db(bot_id, db_path)
    logger.debug(f"build_figure for bot {bot_id}: pos_id={pos_id}")
    if not pos_id:
        return fig

    try:
        conn = sqlite3.connect(db_path)
        if not price_df.empty:
            x_min, x_max = price_df['datetime'].min(), price_df['datetime'].max()
        else:
            x_min = x_max = None

        logger.debug(f"[build_figure] bot={bot_id}, pos_id={pos_id}, x_min={x_min}, x_max={x_max}")

        # Liquidation
        liq = conn.execute(
            "SELECT signal_price FROM levels_signals "
            "WHERE position_id=? AND signal_number=-1 LIMIT 1",
            (pos_id,)
        ).fetchone()
        if liq and x_min:
            fig.add_trace(go.Scatter(
                x=[x_min, x_max], y=[liq[0], liq[0]],
                mode='lines', name=f'Liquidation: {liq[0]:.2f}',
                line=dict(color='red', width=2, dash='solid')
            ))

        # Average price and break-even/close levels
        avg_row = conn.execute(
            "SELECT real_avg_entry_price, avg_entry_price, signal_direction "
            "FROM levels_signals WHERE position_id=? AND (real_flag=1 OR signal_number=0) "
            "ORDER BY signal_number DESC LIMIT 1",
            (pos_id,)
        ).fetchone()
        if avg_row and x_min:
            effective_avg = avg_row[0] if avg_row[0] is not None else avg_row[1]
            direction = avg_row[2]
            sign = 1 if direction == 'buy' else -1
            leverage = config.get('leverage', 1)
            breakeven_pnl = config.get('breakeven_pnl', 0)
            close_pnl = config.get('close_pnl', 0)

            be_price = effective_avg * (1 + sign * breakeven_pnl / 100.0 / leverage)
            close_price = effective_avg * (1 + sign * close_pnl / 100.0 / leverage)

            fig.add_trace(go.Scatter(
                x=[x_min, x_max], y=[be_price, be_price],
                mode='lines', name=f'Break-even: {be_price:.2f}',
                line=dict(color='green', width=2, dash='dash')
            ))
            fig.add_trace(go.Scatter(
                x=[x_min, x_max], y=[close_price, close_price],
                mode='lines', name=f'Close PNL: {close_price:.2f}',
                line=dict(color='green', width=2, dash='solid')
            ))

        # Select all level records (signal_number >= 1)
        levels_all = conn.execute(
            "SELECT signal_price, signal_flag FROM levels_signals "
            "WHERE position_id=? AND signal_number>=1 "
            "ORDER BY signal_price",
            (pos_id,)
        ).fetchall()
        # Keep only unexecuted (signal_flag == 0)
        levels = [row[0] for row in levels_all if row[1] == 0]
        logger.debug(f"Found {len(levels)} levels out of {len(levels_all)} rows with signal_number>=1")

        if levels and x_min:
            level_prices = levels
            min_lev = min(level_prices)
            max_lev = max(level_prices)
            first = True
            for price in level_prices:
                if first:
                    fig.add_trace(go.Scatter(
                        x=[x_min, x_max], y=[price, price],
                        mode='lines', name=f'Averaging levels: {min_lev:.2f} – {max_lev:.2f}',
                        line=dict(color='blue', width=1, dash='dash'),
                        showlegend=True
                    ))
                    first = False
                else:
                    fig.add_trace(go.Scatter(
                        x=[x_min, x_max], y=[price, price],
                        mode='lines', line=dict(color='blue', width=1, dash='dash'),
                        showlegend=False
                    ))
            logger.debug(f"Added {len(level_prices)} level lines")

        conn.close()
    except Exception as e:
        logger.error(f"Error loading levels: {e}")

    try:
        conn = sqlite3.connect(db_path)
        signals_df = pd.read_sql_query(
            "SELECT * FROM levels_signals WHERE position_id=? AND signal_flag=1",
            conn, params=(pos_id,)
        )
        real_df = pd.read_sql_query(
            "SELECT * FROM levels_signals WHERE position_id=? AND real_flag=1",
            conn, params=(pos_id,)
        )
        conn.close()

        if not signals_df.empty:
            signals_df['datetime'] = pd.to_datetime(signals_df['signal_timestamp'], unit='s', utc=True)
            buy_signals = signals_df[signals_df['signal_direction'] == 'buy']
            sell_signals = signals_df[signals_df['signal_direction'] == 'sell']
            close_signals = signals_df[signals_df['signal_number'] == -2]
            liq_signals = signals_df[signals_df['signal_number'] == -1]

            entry = signals_df[signals_df['signal_number'] == 0]
            if not entry.empty:
                row = entry.iloc[0]
                direction = row['signal_direction']
                color = 'green' if direction == 'buy' else 'red'
                symbol = 'triangle-up' if direction == 'buy' else 'triangle-down'
                name = 'Buy entry' if direction == 'buy' else 'Sell entry'
                fig.add_trace(go.Scatter(
                    x=[row['datetime']], y=[row['signal_price']],
                    mode='markers', name=name,
                    marker=dict(color=color, symbol=symbol, size=12),
                    hovertemplate='Entry price: %{y:.2f}<extra></extra>'
                ))

            if not buy_signals.empty:
                fig.add_trace(go.Scatter(
                    x=buy_signals['datetime'], y=buy_signals['signal_price'],
                    mode='markers', name='Buy signal',
                    marker=dict(color='lightgreen', symbol='triangle-up', size=10),
                    hovertemplate='Buy signal: %{y:.2f}<extra></extra>'
                ))
            if not sell_signals.empty:
                fig.add_trace(go.Scatter(
                    x=sell_signals['datetime'], y=sell_signals['signal_price'],
                    mode='markers', name='Sell signal',
                    marker=dict(color='lightcoral', symbol='triangle-down', size=10),
                    hovertemplate='Sell signal: %{y:.2f}<extra></extra>'
                ))
            if not close_signals.empty:
                fig.add_trace(go.Scatter(
                    x=close_signals['datetime'], y=close_signals['signal_price'],
                    mode='markers', name='Close signal',
                    marker=dict(color='blue', symbol='x', size=10),
                    customdata=close_signals[['signal_pnl', 'real_pnl']],
                    hovertemplate='Close PNL: %{customdata[0]:.2f}%<br>Real: %{customdata[1]:.2f}%<extra></extra>'
                ))
            if not liq_signals.empty:
                fig.add_trace(go.Scatter(
                    x=liq_signals['datetime'], y=liq_signals['signal_price'],
                    mode='markers', name='Liquidation signal',
                    marker=dict(color='black', symbol='star', size=10),
                    customdata=liq_signals[['signal_pnl', 'real_pnl']],
                    hovertemplate='Liq PNL: %{customdata[0]:.2f}%<br>Real: %{customdata[1]:.2f}%<extra></extra>'
                ))

        if not real_df.empty:
            real_df['datetime'] = pd.to_datetime(real_df['real_timestamp'], unit='s', utc=True)
            buy_real = real_df[real_df['signal_direction'] == 'buy']
            sell_real = real_df[real_df['signal_direction'] == 'sell']
            if not buy_real.empty:
                fig.add_trace(go.Scatter(
                    x=buy_real['datetime'], y=buy_real['real_price'],
                    mode='markers', name='Real buy',
                    marker=dict(color='darkgreen', symbol='triangle-up', size=10),
                    hovertemplate='Real buy: %{y:.2f}<extra></extra>'
                ))
            if not sell_real.empty:
                fig.add_trace(go.Scatter(
                    x=sell_real['datetime'], y=sell_real['real_price'],
                    mode='markers', name='Real sell',
                    marker=dict(color='darkred', symbol='triangle-down', size=10),
                    hovertemplate='Real sell: %{y:.2f}<extra></extra>'
                ))

    except Exception as e:
        logger.error(f"Error plotting points: {e}")

    stored = relayout_store.get(str(bot_id))
    if stored:
        try:
            fig.update_layout(stored)
        except:
            pass

    fig.update_layout(
        title=f"Grid Bot {bot_id}",
        xaxis_title="Time",
        yaxis_title="Price",
        legend=dict(x=0, y=1, bgcolor='rgba(255,255,255,0.8)', bordercolor='black', borderwidth=1),
        uirevision='grid',  # preserves zoom / pan
        xaxis_rangeslider_visible=False  # remove bottom slider (optional)
    )
    logger.debug(f"Total traces in figure: {len(fig.data)}")
    return fig

def get_current_position_id_from_db(bot_id, db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.execute("SELECT value FROM bot_settings WHERE key='current_position_id'")
    row = cur.fetchone()
    conn.close()
    if row:
        return row[0].strip("'\"")   # remove possible quotes
    return None

# ----------------------------------------------------------------------
# Build history of deals chart – unchanged
# ----------------------------------------------------------------------
def build_history_figure(bot_id: int, config: dict, relayout_store: dict) -> go.Figure:
    db_path = f"data/bot_{bot_id}.db"
    fig = go.Figure()
    price_df = pd.DataFrame()

    try:
        conn = sqlite3.connect(db_path)
        price_df = pd.read_sql_query("SELECT timestamp, close FROM price_history ORDER BY timestamp ASC", conn)
        conn.close()
        if not price_df.empty:
            price_df['datetime'] = pd.to_datetime(price_df['timestamp'], unit='s', utc=True)
            fig.add_trace(go.Scatter(x=price_df['datetime'], y=price_df['close'], mode='lines', name='Price',
                                     line=dict(color='gray', width=1), hoverinfo='y'))
    except:
        pass

    try:
        conn = sqlite3.connect(db_path)
        history = pd.read_sql_query("SELECT * FROM deals_history", conn)
        conn.close()
        if not history.empty:
            history['datetime'] = pd.to_datetime(history['signal_timestamp'], unit='s', utc=True)

            buy_sig = history[history['signal_direction'] == 'buy']
            sell_sig = history[history['signal_direction'] == 'sell']
            close_sig = history[history['signal_number'] == -2]
            liq_sig = history[history['signal_number'] == -1]

            if not buy_sig.empty:
                fig.add_trace(go.Scatter(
                    x=buy_sig['datetime'], y=buy_sig['signal_price'],
                    mode='markers', name='Buy signal (history)',
                    marker=dict(color='lightgreen', symbol='triangle-up', size=8),
                    hovertemplate='Buy: %{y:.2f}<extra></extra>'
                ))
            if not sell_sig.empty:
                fig.add_trace(go.Scatter(
                    x=sell_sig['datetime'], y=sell_sig['signal_price'],
                    mode='markers', name='Sell signal (history)',
                    marker=dict(color='lightcoral', symbol='triangle-down', size=8),
                    hovertemplate='Sell: %{y:.2f}<extra></extra>'
                ))
            if not close_sig.empty:
                fig.add_trace(go.Scatter(
                    x=close_sig['datetime'], y=close_sig['signal_price'],
                    mode='markers', name='Close signal (history)',
                    marker=dict(color='lightskyblue', symbol='x', size=8),
                    hovertemplate='Close: %{y:.2f}<extra></extra>'
                ))
            if not liq_sig.empty:
                fig.add_trace(go.Scatter(
                    x=liq_sig['datetime'], y=liq_sig['signal_price'],
                    mode='markers', name='Liquidation signal (history)',
                    marker=dict(color='lightgray', symbol='star', size=8),
                    hovertemplate='Liquidation: %{y:.2f}<extra></extra>'
                ))

            real = history[history['real_price'].notna()]
            if not real.empty:
                buy_real = real[real['signal_direction'] == 'buy']
                sell_real = real[real['signal_direction'] == 'sell']
                close_real = real[real['signal_number'] == -2]
                liq_real = real[real['signal_number'] == -1]

                if not buy_real.empty:
                    fig.add_trace(go.Scatter(
                        x=buy_real['datetime'], y=buy_real['real_price'],
                        mode='markers', name='Real buy (history)',
                        marker=dict(color='darkgreen', symbol='triangle-up', size=8),
                        hovertemplate='Real buy: %{y:.2f}<extra></extra>'
                    ))
                if not sell_real.empty:
                    fig.add_trace(go.Scatter(
                        x=sell_real['datetime'], y=sell_real['real_price'],
                        mode='markers', name='Real sell (history)',
                        marker=dict(color='darkred', symbol='triangle-down', size=8),
                        hovertemplate='Real sell: %{y:.2f}<extra></extra>'
                    ))
                if not close_real.empty:
                    fig.add_trace(go.Scatter(
                        x=close_real['datetime'], y=close_real['real_price'],
                        mode='markers', name='Real close (history)',
                        marker=dict(color='darkblue', symbol='x', size=8),
                        hovertemplate='Real close: %{y:.2f}<extra></extra>'
                    ))
                if not liq_real.empty:
                    fig.add_trace(go.Scatter(
                        x=liq_real['datetime'], y=liq_real['real_price'],
                        mode='markers', name='Real liquidation (history)',
                        marker=dict(color='black', symbol='star', size=8),
                        hovertemplate='Real liq: %{y:.2f}<extra></extra>'
                    ))
    except Exception as e:
        logger.error(f"Error building history figure: {e}")

    fig.update_layout(
        title="Historical Deals",
        xaxis_title="Time",
        yaxis_title="Price",
        legend=dict(x=0, y=1, bgcolor='rgba(255,255,255,0.8)'),
        uirevision='grid_history',  # preserves zoom between redraws
        xaxis_rangeslider_visible=False  # removes bottom slider (optional)
    )
    return fig
# ----------------------------------------------------------------------
# Statistics table – now integrated into callback
# ----------------------------------------------------------------------
def render_grid_block(bot_id: int, config: dict, relayout_store: dict):
    graph_id = {"type": "grid-graph", "index": bot_id}
    history_graph_id = {"type": "history-graph", "index": bot_id}
    status_btn_id = {"type": "status-btn", "index": bot_id}
    delete_btn_id = {"type": "delete", "index": bot_id}
    close_btn_id = {"type": "grid-close-btn", "index": bot_id}
    edit_btn_id = {"type": "edit-btn", "index": bot_id}

    fig = build_figure(bot_id, config, relayout_store)
    hist_fig = build_history_figure(bot_id, config, relayout_store)

    graph = dcc.Graph(id=graph_id, figure=fig, config={"scrollZoom": True, "displayModeBar": True},
                      style={"height": "500px"})
    history_graph = dcc.Graph(id=history_graph_id, figure=hist_fig,
                              config={"scrollZoom": True, "displayModeBar": True},
                              style={"height": "350px", "marginTop": "10px"})

    # Navigation and deals table container
    prev_btn = html.Button("◀ Prev", id={'type': 'deals-prev-btn', 'index': bot_id},
                           disabled=True, style={'marginRight': '5px'})
    next_btn = html.Button("Next ▶", id={'type': 'deals-next-btn', 'index': bot_id},
                           disabled=True, style={'marginLeft': '5px'})
    pos_indicator = html.Span("Position 0/0", id={'type': 'deals-pos-indicator', 'index': bot_id})
    nav_bar = html.Div([prev_btn, pos_indicator, next_btn],
                       style={'marginBottom': '10px', 'display': 'flex', 'alignItems': 'center'})

    deals_container = html.Div(id={'type': 'deals-container', 'index': bot_id})
    pos_store = dcc.Store(id={'type': 'deals-pos-store', 'index': bot_id}, data={'position_id': None})

    settings_preview = html.Div([
        html.P(f"Collector: {config.get('collector_bot_id')} (table: {config.get('collector_table', 'auto')})"),
        html.P(f"Analyst: {config.get('analyst_bot_id')} | Use for close: {bool(config.get('use_analyst_close',1))}"),
        html.P(f"Leverage: {config.get('leverage')} | Strategy: {config.get('averaging_strategy')} | Threshold: {config.get('averaging_threshold_pnl')}% | Max avg: {config.get('max_averaging_count')} | Smart avg: {config.get('smart_averaging_count')}"),
        html.P(f"Break-even: {config.get('breakeven_pnl')}% | Close: {config.get('close_pnl')}% | Liq: {config.get('liquidation_pnl')}%"),
        html.P(f"Poll: {config.get('poll_interval_sec')}s | Timeout: {config.get('execution_timeout_sec')}s | Recalc: {config.get('recalc_strategy')} | Reserve: {config.get('execution_reserve_sec')}s"),
    ], style={"fontSize": "small", "backgroundColor": "#f0f0f0", "padding": "5px", "borderRadius": "5px"})

    return html.Div([
        html.Div([
            html.H3(f"Grid Bot {bot_id}", style={"display": "inline-block", "marginRight": "20px"}),
            html.Button("Stop" if config.get("status") == "running" else "Start", id=status_btn_id, n_clicks=0),
            html.Button("Close Position", id=close_btn_id, n_clicks=0, style={"marginLeft": "10px", "backgroundColor": "#ffaaaa"}),
            html.Button("Edit", id=edit_btn_id, n_clicks=0, style={"marginLeft": "10px"}),
            html.Button("Delete", id=delete_btn_id, n_clicks=0, style={"marginLeft": "10px"}),
        ]),
        settings_preview,
        html.Hr(),
        graph,
        history_graph,
        nav_bar,
        deals_container,
        pos_store
    ], id=f"bot-{bot_id}", style={"border": "1px solid black", "padding": "10px", "margin": "10px"})
# ----------------------------------------------------------------------
# Type registration
# ----------------------------------------------------------------------
@auto_reg
class GridTypeMeta:
    _name = "grid.type"
    display_name = "Grid Bot"
    form_component = staticmethod(form_component)
    bot_model = "grid.bot"
    render_block = staticmethod(render_grid_block)
    build_history_figure = staticmethod(build_history_figure)

    @staticmethod
    def prepare_new_config(raw_config: dict) -> dict:
        return prepare_config_for_save(raw_config)

    @staticmethod
    def process_edit_save(bot_id, new_fields, old_config):
        new_fields.pop('data_db_path', None)
        config = old_config.copy()
        config.update(new_fields)
        if 'data_db_path' not in config:
            config['data_db_path'] = f"data/bot_{bot_id}.db"
        return prepare_config_for_save(config)

    @staticmethod
    def register_callbacks(app, bot_manager, loop):
        # ------------------ Main chart update -----------------
        @app.callback(
            Output({'type': 'grid-graph', 'index': MATCH}, 'figure'),
            Input('global-interval', 'n_intervals'),
            State({'type': 'grid-graph', 'index': MATCH}, 'id'),
            State('relayout-store', 'data')
        )
        def update_grid_graph(n, graph_id, relayout_store):
            bot_id = graph_id['index']
            logger.debug(f"update_grid_graph called for bot_id={bot_id}, n_intervals={n}")
            bots = get_all_bots()
            bot = next((b for b in bots if b['id'] == bot_id), None)
            if not bot or bot['status'] != 'running':
                logger.debug(f"Bot {bot_id} not found or not running, status: {bot['status'] if bot else 'no bot'}")
                return no_update
            config = get_bot_config(bot_id)
            if not config:
                logger.debug(f"Config for {bot_id} not found")
                return no_update
            logger.debug(f"Building figure for bot {bot_id}")
            return build_figure(bot_id, config, relayout_store)

        # ------------------ Historical chart update -----------------
        @app.callback(
            Output({'type': 'history-graph', 'index': MATCH}, 'figure'),
            Input('global-interval', 'n_intervals'),
            State({'type': 'history-graph', 'index': MATCH}, 'id'),
            State('relayout-store', 'data')
        )
        def update_history_graph(n, graph_id, relayout_store):
            bot_id = graph_id['index']
            config = get_bot_config(bot_id)
            if not config:
                return no_update
            return build_history_figure(bot_id, config, relayout_store)

        # ------------------ Close Position -----------------
        @app.callback(
            Output('bots-trigger', 'data', allow_duplicate=True),
            Input({'type': 'grid-close-btn', 'index': ALL}, 'n_clicks'),
            State({'type': 'grid-close-btn', 'index': ALL}, 'id'),
            prevent_initial_call=True
        )
        def close_position(n_clicks_list, ids_list):
            ctx = callback_context
            if not ctx.triggered:
                return no_update
            triggered = ctx.triggered[0]
            dict_str = triggered['prop_id'].split('.')[0]
            btn_id = json.loads(dict_str)
            bot_id = btn_id['index']
            for i, id_dict in enumerate(ids_list):
                if (id_dict['index'] == bot_id and
                        n_clicks_list[i] and n_clicks_list[i] > 0):
                    if bot_id in bot_manager.bots:
                        bot = bot_manager.bots[bot_id]
                        if hasattr(bot, 'request_close_position'):
                            future = asyncio.run_coroutine_threadsafe(
                                bot.request_close_position(), loop
                            )
                            try:
                                future.result(timeout=5)
                            except Exception as e:
                                logger.error(f"Error in request_close_position for bot {bot_id}: {e}")
                    break
            return no_update

        # ------------------ Deals table + navigation (single callback) -----------------
        @app.callback(
            Output({'type': 'deals-container', 'index': MATCH}, 'children'),
            Output({'type': 'deals-pos-store', 'index': MATCH}, 'data'),
            Output({'type': 'deals-prev-btn', 'index': MATCH}, 'disabled'),
            Output({'type': 'deals-next-btn', 'index': MATCH}, 'disabled'),
            Output({'type': 'deals-pos-indicator', 'index': MATCH}, 'children'),
            Input('global-interval', 'n_intervals'),
            Input({'type': 'deals-prev-btn', 'index': MATCH}, 'n_clicks'),
            Input({'type': 'deals-next-btn', 'index': MATCH}, 'n_clicks'),
            State({'type': 'deals-pos-store', 'index': MATCH}, 'data'),
            State({'type': 'deals-container', 'index': MATCH}, 'id')
        )
        def manage_deals(n_intervals, prev_clicks, next_clicks, pos_store_data, container_id):
            ctx = callback_context
            if not ctx.triggered:
                return no_update, no_update, no_update, no_update, no_update

            bot_id = container_id['index']
            db_path = f"data/bot_{bot_id}.db"
            triggered_prop = ctx.triggered[0]['prop_id'].split('.')[0]

            try:
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                positions = conn.execute(
                    "SELECT position_id, MAX(moved_to_history_at) as last_move FROM deals_history "
                    "GROUP BY position_id ORDER BY last_move DESC"
                ).fetchall()
                conn.close()
                position_ids = [row['position_id'] for row in positions]
                total = len(position_ids)
                if total == 0:
                    return html.Div("No deals yet"), no_update, True, True, "No deals"

                # Determine current index
                current_pos = None
                if pos_store_data and isinstance(pos_store_data, dict):
                    current_pos = pos_store_data.get('position_id')
                if not current_pos or current_pos not in position_ids:
                    current_pos = position_ids[0]

                current_idx = position_ids.index(current_pos)

                # Handle button clicks
                if 'deals-prev-btn' in triggered_prop:
                    if current_idx > 0:
                        current_idx -= 1
                        current_pos = position_ids[current_idx]
                elif 'deals-next-btn' in triggered_prop:
                    if current_idx < total - 1:
                        current_idx += 1
                        current_pos = position_ids[current_idx]

                # Load table for current position
                conn = sqlite3.connect(db_path)
                df = pd.read_sql_query(
                    "SELECT history_id, signal_number, signal_timestamp, real_timestamp, signal_direction, "
                    "signal_price, real_price, signal_vol_rate, real_vol_rate, "
                    "signal_pnl, real_pnl, signal_position_volume, real_position_volume "
                    "FROM deals_history WHERE position_id=? ORDER BY history_id DESC",
                    conn, params=(current_pos,)
                )
                conn.close()
                if df.empty:
                    table = html.Div("No data for this position")
                else:
                    for col in ['signal_timestamp', 'real_timestamp']:
                        if col in df.columns:
                            df[col] = pd.to_datetime(df[col], unit='s', utc=True).dt.strftime('%Y-%m-%d %H:%M:%S')

                    columns = [
                        {"name": "History ID", "id": "history_id"},
                        {"name": "#", "id": "signal_number"},
                        {"name": "Signal Time", "id": "signal_timestamp"},
                        {"name": "Real Time", "id": "real_timestamp"},
                        {"name": "Dir", "id": "signal_direction"},
                        {"name": "S Price", "id": "signal_price"},
                        {"name": "R Price", "id": "real_price"},
                        {"name": "S Vol", "id": "signal_vol_rate"},
                        {"name": "R Vol", "id": "real_vol_rate"},
                        {"name": "S PNL%", "id": "signal_pnl"},
                        {"name": "R PNL%", "id": "real_pnl"},
                        {"name": "S Pos Vol", "id": "signal_position_volume"},
                        {"name": "R Pos Vol", "id": "real_position_volume"},
                    ]
                    table = dash_table.DataTable(
                        data=df.to_dict("records"), columns=columns,
                        style_table={"overflowX": "auto"},
                        style_cell={"textAlign": "center", "padding": "5px"},
                        page_size=len(df)
                    )

                # Button states
                prev_disabled = (current_idx == 0)
                next_disabled = (current_idx == total - 1)
                indicator_text = f"Position {current_idx + 1} of {total}"

                new_store_data = {'position_id': current_pos}
                return html.Div([html.H4(f"Deals History – {current_pos}"), table]), new_store_data, \
                    prev_disabled, next_disabled, indicator_text

            except Exception as e:
                logger.error(f"Error in manage_deals: {e}")
                return html.Div("Error loading deals history"), no_update, True, True, "Error"

        # ------------------ Update collector table list -----------------
        @app.callback(
            Output({'type': 'grid-field', 'field': 'collector_table'}, 'options'),
            Input({'type': 'grid-field', 'field': 'collector_bot_id'}, 'value')
        )
        def update_collector_tables(collector_id):
            if not collector_id:
                return []
            from .components import get_collector_tables
            return get_collector_tables(collector_id)