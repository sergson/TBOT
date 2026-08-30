# modules/grid/components.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import json
import os
import pandas as pd
import plotly.graph_objects as go
import asyncio
from dash import dcc, html, dash_table, Output, Input, State, MATCH, ALL, no_update, callback_context
from core import auto_reg, colors, styles, graphics
from core.database import get_all_bots, get_bot_config, DBSQLite3
from core.logger import perf_logger

logger = perf_logger.get_logger("grid_bot_ui", "analytics")

# ----------------------------------------------------------------------
# Helper: get tables from collector database using ORM
# ----------------------------------------------------------------------
def get_collector_tables(collector_bot_id: int) -> list:
    """Return list of table names (as dropdown options) from collector's DB."""
    try:
        config = get_bot_config(collector_bot_id)
        if not config or config.get('bot_type') != 'collector':
            return []
        db_path = config.get('data_db_path')
        if not db_path or not os.path.exists(db_path):
            return []
        env = DBSQLite3(db_path)
        try:
            tables = env.list_tables()
        finally:
            env.close()
        # filter out internal tables if any
        return [{"label": t, "value": t} for t in tables if not t.startswith('bot_')]
    except Exception as e:
        logger.error(f"Error getting collector tables: {e}")
        return []

# ----------------------------------------------------------------------
# Normalize configuration before saving
# ----------------------------------------------------------------------
def prepare_config_for_save(raw_config: dict) -> dict:
    cleaned = raw_config.copy()
    if 'use_analyst_close' in cleaned:
        val = cleaned['use_analyst_close']
        cleaned['use_analyst_close'] = 1 if (isinstance(val, list) and 1 in val) else 0
    numeric_fields = ['leverage', 'max_averaging_count', 'smart_averaging_count',
                      'poll_interval_sec', 'execution_timeout_sec', 'execution_reserve_sec']
    for f in numeric_fields:
        if f in cleaned and cleaned[f] is not None:
            cleaned[f] = int(cleaned[f])
    if 'smart_averaging_count' in cleaned and 'max_averaging_count' in cleaned:
        if cleaned['smart_averaging_count'] > cleaned['max_averaging_count']:
            cleaned['smart_averaging_count'] = cleaned['max_averaging_count']
    float_fields = {
        'averaging_threshold_pnl': 20.0,
        'breakeven_pnl': 4.0,
        'close_pnl': 50.0,
        'liquidation_pnl': 90.0,
    }
    for f, default in float_fields.items():
        if cleaned.get(f) is None:
            cleaned[f] = default
        else:
            cleaned[f] = float(cleaned[f])

    if cleaned.get('smart_averaging_count') > cleaned.get('max_averaging_count'):
        cleaned['smart_averaging_count'] = cleaned['max_averaging_count']

    cleaned.pop('bot_type', None)
    return cleaned

# ----------------------------------------------------------------------
# Create / edit form
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
        dcc.Input(id={"type": "grid-field", "field": "breakeven_pnl"}, type="number", value=breakeven_pnl, min=0, step=0.01),
        html.Label("Target close PNL (must be ≥ Break‑even) %"),
        dcc.Input(id={"type": "grid-field", "field": "close_pnl"}, type="number", value=close_pnl, min=0.01, step=0.01),
        html.Label("Liquidation PNL (%)"),
        dcc.Input(id={"type": "grid-field", "field": "liquidation_pnl"}, type="number", value=liquidation_pnl, min=1, step=1),
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
    ])

# ----------------------------------------------------------------------
# Build position charts
# ----------------------------------------------------------------------
def build_figure(bot_id: int, config: dict, relayout_store: dict) -> go.Figure:
    db_path = f"data/bot_{bot_id}.db"
    fig = go.Figure()
    price_df = pd.DataFrame()
    y_values = []

    # Valid timestamp threshold (nanoseconds, approx year 2001)
    min_valid_ts = 1_000_000_000_000_000  # 1e15

    env = DBSQLite3(db_path)
    try:
        # Get price history
        price_manager = env['price.history']
        price_recs = price_manager.search([], order='timestamp ASC')
        if price_recs:
            data = price_recs.read()
            price_df = pd.DataFrame(data)
            if not price_df.empty:
                price_df = price_df[price_df['timestamp'] >= min_valid_ts].copy()
                if not price_df.empty:
                    price_df['datetime'] = pd.to_datetime(price_df['timestamp'], unit='ns', utc=True)
                    fig.add_trace(graphics.price_line(
                        price_df['datetime'], price_df['high'],
                        'Price high',
                        color=colors.BLACK,
                        width=1,
                        hovertemplate='Price high: %{y:.2f}<extra></extra>'
                    ))
                    fig.add_trace(graphics.price_line(
                        price_df['datetime'], price_df['low'],
                        'Price low',
                        color=colors.BLACK,
                        width=1,
                        hovertemplate='Price low: %{y:.2f}<extra></extra>'
                    ))

        # Current position
        settings_manager = env['bot.settings']
        pos_rec = settings_manager.search([('key', '=', 'current_position_id')], limit=1)
        pos_id = pos_rec[0].value.strip("'\"") if pos_rec else None
        if not pos_id:
            return fig

        levels_manager = env['levels.signals']

        # Position direction from entry
        entry_recs = levels_manager.search([
            ('position_id', '=', pos_id),
            ('signal_number', '=', 0)
        ], limit=1)
        if not entry_recs:
            return fig
        entry_rec = entry_recs[0]
        sign = 1 if entry_rec.signal_direction == 'buy' else -1
        entry_ts = entry_rec.signal_timestamp

        # Determine starting point (10 candles before entry)
        start_ts = None
        if entry_ts is not None and not price_df.empty:
            before_entry = price_df[price_df['timestamp'] <= entry_ts]
            if not before_entry.empty:
                start_ts = before_entry['timestamp'].tail(10).min()

        x_min = price_df['datetime'].min() if not price_df.empty else None
        x_max = price_df['datetime'].max() if not price_df.empty else None

        # Liquidation
        liq_recs = levels_manager.search([
            ('position_id', '=', pos_id),
            ('signal_number', '=', -1)
        ], order='id ASC', limit=1)
        if liq_recs and x_min is not None:
            liq_price = liq_recs[0].signal_price
            if liq_price is not None:
                y_values.append(liq_price)
                fig.add_trace(graphics.hline(
                    x_min, x_max, liq_price,
                    f'Liquidation: {liq_price:.2f}',
                    colors.RED, width=2, dash='solid'
                ))

        # Last executed trade (as in old code)
        real_records = levels_manager.search([
            ('position_id', '=', pos_id),
            ('real_flag', '=', True)
        ], order='id DESC')
        last_real = None
        for rec in real_records:
            vol = rec.real_position_volume if rec.real_position_volume is not None else rec.signal_position_volume
            if vol is None or vol > 0:
                last_real = rec
                break

        if last_real and x_min is not None:
            effective_avg = last_real.real_avg_entry_price if last_real.real_avg_entry_price is not None else last_real.avg_entry_price
            if effective_avg is not None and effective_avg > 0:
                leverage = config.get('leverage', 1) or 1
                breakeven_pnl = config.get('breakeven_pnl', 0) or 0
                close_pnl = config.get('close_pnl', 0) or 0

                be_price = effective_avg * (1 + sign * breakeven_pnl / 100.0 / leverage)
                close_price = effective_avg * (1 + sign * close_pnl / 100.0 / leverage)

                y_values.extend([be_price, close_price])

                fig.add_trace(graphics.hline(
                    x_min, x_max, be_price,
                    f'Break-even: {be_price:.2f}',
                    colors.GREEN, width=1, dash='dash'
                ))
                fig.add_trace(graphics.hline(
                    x_min, x_max, close_price,
                    f'Close PNL: {close_price:.2f}',
                    colors.GREEN, width=2, dash='solid'
                ))

        # Averaging levels (unexecuted, signal_flag=0)
        level_recs = levels_manager.search([
            ('position_id', '=', pos_id),
            ('signal_number', '>=', 1),
            ('signal_flag', '=', 0)
        ], order='signal_price ASC')
        if level_recs and x_min is not None:
            level_prices = [r.signal_price for r in level_recs if r.signal_price is not None]
            if level_prices:
                y_values.extend(level_prices)
                min_lev = min(level_prices)
                max_lev = max(level_prices)
                first = True
                for price in level_prices:
                    trace = graphics.hline(
                        x_min, x_max, price,
                        'Averaging levels',
                        colors.BLUE, width=1, dash='dash',
                    )
                    # Group lines into one legend and show only first
                    trace.update(legendgroup='averaging_levels', showlegend=first)
                    fig.add_trace(trace)
                    first = False

        # Signal markers (signal_flag=1)
        signal_recs = levels_manager.search([
            ('position_id', '=', pos_id),
            ('signal_flag', '=', True)
        ])
        if signal_recs:
            df_sig = pd.DataFrame(signal_recs.read())
            if not df_sig.empty:
                df_sig = df_sig[df_sig['signal_timestamp'] >= min_valid_ts]
                if not df_sig.empty:
                    df_sig['datetime'] = pd.to_datetime(df_sig['signal_timestamp'], unit='ns', utc=True, errors='coerce')
                    df_sig = df_sig.dropna(subset=['datetime'])
                    if not df_sig.empty:
                        entry_df = df_sig[df_sig['signal_number'] == 0]
                        if not entry_df.empty:
                            row = entry_df.iloc[0]
                            fig.add_trace(graphics.marker(
                                row['datetime'], row['signal_price'],
                                'Buy entry' if row['signal_direction'] == 'buy' else 'Sell entry',
                                colors.GREEN if row['signal_direction'] == 'buy' else colors.RED,
                                'triangle-up' if row['signal_direction'] == 'buy' else 'triangle-down',
                                size=12,
                                hovertemplate='Entry price: %{y:.2f}<extra></extra>'
                            ))

                        buy_signals = df_sig[(df_sig['signal_direction'] == 'buy') & (df_sig['signal_number'] != 0)]
                        sell_signals = df_sig[df_sig['signal_direction'] == 'sell']
                        close_signals = df_sig[df_sig['signal_number'] == -2]
                        liq_signals = df_sig[df_sig['signal_number'] == -1]

                        if not buy_signals.empty:
                            fig.add_trace(graphics.markers_trace(
                                buy_signals['datetime'], buy_signals['signal_price'],
                                'Buy signal',
                                colors.MEDIUM_GREEN, 'triangle-up', 4,
                                hovertemplate='Buy signal: %{y:.2f}<extra></extra>'
                            ))
                        if not sell_signals.empty:
                            fig.add_trace(graphics.markers_trace(
                                sell_signals['datetime'], sell_signals['signal_price'],
                                'Sell signal',
                                colors.MEDIUM_RED, 'triangle-down', 4,
                                hovertemplate='Sell signal: %{y:.2f}<extra></extra>'
                            ))

                        # Close and Liq signals — usually single, add one by one
                        if not close_signals.empty:
                            fig.add_trace(graphics.markers_trace(
                                close_signals['datetime'],
                                close_signals['signal_price'],
                                'Close signal',
                                colors.BLUE, 'x', 4,
                                hovertemplate='Close PNL: %{customdata[0]:.2f}%<br>Real: %{customdata[1]:.2f}%<extra></extra>',
                                customdata=[[row.signal_pnl, row.real_pnl] for _, row in close_signals.iterrows()]
                            ))
                        if not liq_signals.empty:
                            fig.add_trace(graphics.markers_trace(
                                liq_signals['datetime'],
                                liq_signals['signal_price'],
                                'Liquidation signal',
                                colors.BLACK, 'star', 4,
                                hovertemplate='Liq PNL: %{customdata[0]:.2f}%<br>Real: %{customdata[1]:.2f}%<extra></extra>',
                                customdata=[[row.signal_pnl, row.real_pnl] for _, row in liq_signals.iterrows()]
                            ))

        # Real execution markers (real_flag=1)
        real_exec_recs = levels_manager.search([
            ('position_id', '=', pos_id),
            ('real_flag', '=', True)
        ])
        if real_exec_recs:
            df_real = pd.DataFrame(real_exec_recs.read())
            if not df_real.empty and 'real_timestamp' in df_real.columns:
                df_real = df_real[df_real['real_timestamp'].notna()]
                if not df_real.empty:
                    df_real = df_real[df_real['real_timestamp'] >= min_valid_ts]
                    if not df_real.empty:
                        df_real['datetime'] = pd.to_datetime(df_real['real_timestamp'], unit='ns', utc=True, errors='coerce')
                        df_real = df_real.dropna(subset=['datetime'])
                        if not df_real.empty:
                            buy_real = df_real[df_real['signal_direction'] == 'buy']
                            sell_real = df_real[df_real['signal_direction'] == 'sell']
                            if not buy_real.empty:
                                fig.add_trace(graphics.markers_trace(
                                    buy_real['datetime'], buy_real['real_price'],
                                    'Real buy',
                                    colors.DARK_GREEN, 'triangle-up', 6,
                                    hovertemplate='Real buy: %{y:.2f}<extra></extra>'
                                ))
                            if not sell_real.empty:
                                fig.add_trace(graphics.markers_trace(
                                    sell_real['datetime'], sell_real['real_price'],
                                    'Real sell',
                                    colors.DARK_RED, 'triangle-down', 6,
                                    hovertemplate='Real sell: %{y:.2f}<extra></extra>'
                                ))

    except Exception as e:
        logger.error(f"Error building figure for bot {bot_id}: {e}", exc_info=True)
    finally:
        env.close()

    # Apply saved relayout
    stored = relayout_store.get(str(bot_id))
    if stored:
        try:
            fig.update_layout(stored)
        except Exception as e:
            logger.warning(f"Failed to apply relayout: {e}")

    # X range
    start_dt = None
    end_dt = None
    if start_ts is not None and not price_df.empty:
        start_dt = pd.to_datetime(start_ts, unit='ns', utc=True)
        end_dt = price_df['datetime'].max()

    # Y range
    if start_ts is not None and not price_df.empty:
        visible_mask = (price_df['timestamp'] >= start_ts) & (price_df['timestamp'] <= price_df['timestamp'].max())
        visible_df = price_df[visible_mask]
    else:
        visible_df = price_df

    if not visible_df.empty:
        y_values.append(visible_df['low'].min())
        y_values.append(visible_df['high'].max())

    y_clean = [v for v in y_values if v is not None]
    if y_clean:
        min_y = min(y_clean)
        max_y = max(y_clean)
        diff = max_y - min_y
        pad = diff * 0.10 if diff > 0 else (abs(min_y) * 0.01 if min_y != 0 else 1.0)
        yaxis_range = [min_y - pad, max_y + pad]
    else:
        yaxis_range = None

    graphics.apply_layout(
        fig,
        title=f"Grid Bot {bot_id}",
        uirevision='grid',
        show_rangeslider=False,
        hovermode='x unified',
        xaxis_range=[start_dt, end_dt] if start_dt is not None and end_dt is not None else None,
        yaxis_range=yaxis_range,
        legend_bordercolor=colors.BLACK,
        title_font_size=styles.FONT_SIZE_BOTHEADER
    )
    return fig

# ----------------------------------------------------------------------
# Build history of deals chart
# ----------------------------------------------------------------------
def build_history_figure(bot_id: int, config: dict, relayout_store: dict) -> go.Figure:
    db_path = f"data/bot_{bot_id}.db"
    fig = go.Figure()
    env = DBSQLite3(db_path)

    min_valid_ts = 1_000_000_000_000_000
    x_min_range = None
    x_max_range = None

    try:
        # Price history
        price_manager = env['price.history']
        recs = price_manager.search([], order='timestamp ASC')
        if recs:
            df_price = pd.DataFrame(recs.read())
            if not df_price.empty:
                df_price = df_price[df_price['timestamp'] >= min_valid_ts].copy()
                if not df_price.empty:
                    df_price['datetime'] = pd.to_datetime(df_price['timestamp'], unit='ns', utc=True)
                    fig.add_trace(graphics.price_line(
                        df_price['datetime'], df_price['high'],
                        'Price high',
                        color=colors.BLACK, width=1,
                        hoverinfo='y'
                    ))
                    fig.add_trace(graphics.price_line(
                        df_price['datetime'], df_price['low'],
                        'Price low',
                        color=colors.BLACK, width=1,
                        hoverinfo='y'
                    ))

        # Deals history
        deals_manager = env['deals.history']
        deals = deals_manager.search([], order='history_id DESC')
        if deals:
            df = pd.DataFrame(deals.read())
            df = df[df['signal_timestamp'] >= min_valid_ts].copy()
            if 'real_timestamp' in df.columns:
                df['real_timestamp'] = pd.to_numeric(df['real_timestamp'], errors='coerce')
                df = df[(df['real_timestamp'].isna()) | (df['real_timestamp'] >= min_valid_ts)]

            if not df.empty:
                df['datetime'] = pd.to_datetime(df['signal_timestamp'], unit='ns', utc=True, errors='coerce')

                entry = df[df['signal_number'] == 0]
                buy_sig = df[(df['signal_direction'] == 'buy') & (df['signal_number'] != 0)]
                sell_sig = df[(df['signal_direction'] == 'sell') & (df['signal_number'] != 0)]
                close_sig = df[df['signal_number'] == -2]
                liq_sig = df[df['signal_number'] == -1]

                if not buy_sig.empty:
                    fig.add_trace(graphics.markers_trace(
                        buy_sig['datetime'], buy_sig['signal_price'],
                        'Buy signal', colors.MEDIUM_GREEN, 'triangle-up', 4,
                        hovertemplate='Buy: %{y:.2f}<extra></extra>'
                    ))
                if not sell_sig.empty:
                    fig.add_trace(graphics.markers_trace(
                        sell_sig['datetime'], sell_sig['signal_price'],
                        'Sell signal', colors.MEDIUM_RED, 'triangle-down', 4,
                        hovertemplate='Sell: %{y:.2f}<extra></extra>'
                    ))

                if not entry.empty:
                    buy_entries = entry[entry['signal_direction'] == 'buy']
                    sell_entries = entry[entry['signal_direction'] == 'sell']
                    if not buy_entries.empty:
                        fig.add_trace(graphics.markers_trace(
                            buy_entries['datetime'], buy_entries['signal_price'],
                            'Buy entry', colors.GREEN, 'triangle-up', 12,
                            hovertemplate='Entry price: %{y:.2f}<extra></extra>'
                        ))
                    if not sell_entries.empty:
                        fig.add_trace(graphics.markers_trace(
                            sell_entries['datetime'], sell_entries['signal_price'],
                            'Sell entry', colors.RED, 'triangle-down', 12,
                            hovertemplate='Entry price: %{y:.2f}<extra></extra>'
                        ))

                # Close and Liq in history — single markers
                if not close_sig.empty:
                    fig.add_trace(graphics.markers_trace(
                        close_sig['datetime'], close_sig['signal_price'],
                        'Close signal', colors.MEDIUM_BLUE, 'x', 4,
                        hovertemplate='Close: %{y:.2f}<extra></extra>'
                    ))
                if not liq_sig.empty:
                    fig.add_trace(graphics.markers_trace(
                        liq_sig['datetime'], liq_sig['signal_price'],
                        'Liquidation signal', colors.MEDIUM_GRAY, 'star', 4,
                        hovertemplate='Liquidation: %{y:.2f}<extra></extra>'
                    ))

                # Real executions
                real = df[df['real_price'].notna()]
                if not real.empty:
                    real = real[real['real_timestamp'].notna()]
                    real = real[real['real_timestamp'] >= min_valid_ts] if not real.empty else real
                    if not real.empty:
                        buy_real = real[real['signal_direction'] == 'buy']
                        sell_real = real[real['signal_direction'] == 'sell']
                        close_real = real[real['signal_number'] == -2]
                        liq_real = real[real['signal_number'] == -1]

                        if not buy_real.empty:
                            fig.add_trace(graphics.markers_trace(
                                buy_real['datetime'], buy_real['real_price'],
                                'Real buy', colors.DARK_GREEN, 'triangle-up', 6,
                                hovertemplate='Real buy: %{y:.2f}<extra></extra>'
                            ))
                        if not sell_real.empty:
                            fig.add_trace(graphics.markers_trace(
                                sell_real['datetime'], sell_real['real_price'],
                                'Real sell', colors.DARK_RED, 'triangle-down', 6,
                                hovertemplate='Real sell: %{y:.2f}<extra></extra>'
                            ))
                        if not close_real.empty:
                            fig.add_trace(graphics.markers_trace(
                                close_real['datetime'], close_real['real_price'],
                                'Real close', colors.DARK_BLUE, 'x', 6,
                                hovertemplate='Real close: %{y:.2f}<extra></extra>'
                            ))
                        if not liq_real.empty:
                            fig.add_trace(graphics.markers_trace(
                                liq_real['datetime'], liq_real['real_price'],
                                'Real liquidation', colors.BLACK, 'star', 6,
                                hovertemplate='Real liq: %{y:.2f}<extra></extra>'
                            ))

                # Compute X range
                timestamps = []
                if 'signal_timestamp' in df.columns:
                    timestamps.append(df['signal_timestamp'])
                if 'real_timestamp' in df.columns:
                    real_ts = df['real_timestamp'].dropna()
                    if not real_ts.empty:
                        timestamps.append(real_ts)

                if timestamps:
                    all_ts = pd.concat(timestamps)
                    all_ts = all_ts[all_ts >= min_valid_ts]
                    if not all_ts.empty:
                        min_ts = all_ts.min()
                        max_ts = all_ts.max()
                        x_min_range = pd.to_datetime(min_ts, unit='ns', utc=True)
                        x_max_range = pd.to_datetime(max_ts, unit='ns', utc=True)
                        padding = pd.Timedelta(minutes=30)
                        x_min_range -= padding
                        x_max_range += padding

    except Exception as e:
        logger.error(f"Error building history figure for bot {bot_id}: {e}")
    finally:
        env.close()

    graphics.apply_layout(
        fig,
        title="Historical Deals",
        uirevision='grid_history',
        hovermode=None,
        xaxis_range=[x_min_range, x_max_range] if x_min_range is not None and x_max_range is not None else None,
        legend_bordercolor=None,
        title_font_size=styles.FONT_SIZE_BOTHEADER
    )

    return fig

# ----------------------------------------------------------------------
# Render bot block in UI
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
                      style={"height": "450px"})
    history_graph = dcc.Graph(id=history_graph_id, figure=hist_fig,
                              config={"scrollZoom": True, "displayModeBar": True},
                              style={"height": "400px", "marginTop": styles.MARGIN_NORMAL})

    prev_btn = html.Button("◀ Prev", id={'type': 'deals-prev-btn', 'index': bot_id},
                           disabled=True, style={'marginRight': styles.MARGIN_NORMAL})
    next_btn = html.Button("Next ▶", id={'type': 'deals-next-btn', 'index': bot_id},
                           disabled=True, style={'marginLeft': styles.MARGIN_NORMAL})
    pos_indicator = html.Span("Position 0/0", id={'type': 'deals-pos-indicator', 'index': bot_id})
    nav_bar = html.Div([prev_btn, pos_indicator, next_btn],
                       style={'marginBottom': styles.MARGIN_NORMAL, 'display': 'flex', 'alignItems': 'center'})

    deals_container = html.Div(id={'type': 'deals-container', 'index': bot_id})
    pos_store = dcc.Store(id={'type': 'deals-pos-store', 'index': bot_id}, data={'position_id': None})

    settings_preview = html.Div([
        html.P(f"Collector: {config.get('collector_bot_id')} (table: {config.get('collector_table', 'auto')})"),
        html.P(f"Analyst: {config.get('analyst_bot_id')} | Use for close: {bool(config.get('use_analyst_close',1))}"),
        html.P(f"Leverage: {config.get('leverage')} | Strategy: {config.get('averaging_strategy')} | Threshold: {config.get('averaging_threshold_pnl')}% | Max avg: {config.get('max_averaging_count')} | Smart avg: {config.get('smart_averaging_count')}"),
        html.P(f"Break-even: {config.get('breakeven_pnl')}% | Close: {config.get('close_pnl')}% | Liq: {config.get('liquidation_pnl')}%"),
        html.P(f"Poll: {config.get('poll_interval_sec')}s | Timeout: {config.get('execution_timeout_sec')}s | Recalc: {config.get('recalc_strategy')} | Reserve: {config.get('execution_reserve_sec')}s"),
    ], style={
            "fontSize": "small",
            "lineHeight": "1",
            "padding": styles.PADDING_ZERO,
            "backgroundColor": colors.LIGHT_BG
            })

    title_text = f"#{bot_id} Grid Bot  ←  Collector Bot #{config.get('collector_bot_id')}"

    return html.Details([
        html.Summary(
            title_text,
            id={'type': f'grid-bot-header', 'index': bot_id},
            style=styles.bot_card_header_style(config.get('status'))
        ),
        html.Div([
            settings_preview,
            html.Div([
                html.Button("Stop" if config.get("status") == "running" else "Start",
                            id=status_btn_id, n_clicks=0),
                html.Button("Close Position", id=close_btn_id, n_clicks=0,
                            style={"marginLeft": styles.MARGIN_NORMAL, "backgroundColor": colors.LIGHT_RED}),
                html.Button("Edit", id=edit_btn_id, n_clicks=0, style={"marginLeft": styles.MARGIN_NORMAL}),
                html.Button("Delete", id=delete_btn_id, n_clicks=0, style={"marginLeft": styles.MARGIN_NORMAL}),
            ], style={"marginBottom": styles.MARGIN_NORMAL}),
            html.Hr(),
            graph,
            history_graph,
            nav_bar,
            deals_container,
            pos_store
        ])
    ], open=True, style=styles.STYLE_BOTCARD)

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

    @staticmethod
    def prepare_new_config(raw_config: dict) -> dict:
        config = prepare_config_for_save(raw_config)
        config['max_averaging_count_initial'] = config['max_averaging_count']
        return config

    @staticmethod
    def process_edit_save(bot_id, new_fields, old_config):
        new_fields.pop('data_db_path', None)
        config = old_config.copy()
        config.update(new_fields)
        if 'data_db_path' not in config:
            config['data_db_path'] = f"data/bot_{bot_id}.db"
        if 'max_averaging_count' in new_fields and new_fields['max_averaging_count'] != old_config.get('max_averaging_count'):
            config['max_averaging_count_initial'] = new_fields['max_averaging_count']
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
            bots = get_all_bots()
            bot = next((b for b in bots if b['id'] == bot_id), None)
            if not bot or bot['status'] != 'running':
                return no_update
            config = get_bot_config(bot_id)
            if not config:
                return no_update
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

            # Find the correct bot and send request
            for i, id_dict in enumerate(ids_list):
                if (id_dict['index'] == bot_id and
                        n_clicks_list[i] and n_clicks_list[i] > 0):
                    try:
                        if bot_id in bot_manager.bots:
                            bot = bot_manager.bots[bot_id]
                            if hasattr(bot, 'request_close_position'):
                                future = asyncio.run_coroutine_threadsafe(
                                    bot.request_close_position(), loop
                                )
                                future.result(timeout=5)
                        mood = 'happy'  # success
                    except Exception as e:
                        logger.error(f"Error in request_close_position for bot {bot_id}: {e}")
                    break
            else:
                # If loop didn't find a match (e.g., click but n_clicks==0)
                return no_update
            perf_logger.set_mood('happy')
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
                env = DBSQLite3(db_path)
                try:
                    deals_manager = env['deals.history']
                    all_records = deals_manager.search([], order='history_id DESC')
                    if not all_records:
                        return html.Div("No deals yet"), no_update, True, True, "No deals"
                    df_all = pd.DataFrame(all_records.read())
                    if 'moved_to_history_at' not in df_all.columns:
                        positions = df_all['position_id'].unique().tolist()
                    else:
                        df_group = df_all.groupby('position_id')['moved_to_history_at'].max().reset_index()
                        df_group = df_group.sort_values('moved_to_history_at', ascending=False)
                        positions = df_group['position_id'].tolist()
                finally:
                    env.close()

                total = len(positions)
                if total == 0:
                    return html.Div("No deals yet"), no_update, True, True, "No deals"

                current_pos = None
                if pos_store_data and isinstance(pos_store_data, dict):
                    current_pos = pos_store_data.get('position_id')
                if not current_pos or current_pos not in positions:
                    current_pos = positions[0]

                current_idx = positions.index(current_pos)

                if 'deals-prev-btn' in triggered_prop:
                    if current_idx > 0:
                        current_idx -= 1
                        current_pos = positions[current_idx]
                elif 'deals-next-btn' in triggered_prop:
                    if current_idx < total - 1:
                        current_idx += 1
                        current_pos = positions[current_idx]

                env = DBSQLite3(db_path)
                try:
                    deals_manager = env['deals.history']
                    recs = deals_manager.search([
                        ('position_id', '=', current_pos)
                    ], order='history_id DESC')
                    if recs:
                        df = pd.DataFrame(recs.read())
                        for col in ['signal_timestamp', 'real_timestamp']:
                            if col in df.columns:
                                df[col] = pd.to_datetime(df[col], unit='ns', utc=True).dt.strftime('%Y-%m-%d %H:%M:%S')
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
                            page_size=len(df),
                            style_data_conditional=[
                                {
                                    "if": {"filter_query": "{signal_number} = -1 && {real_pnl} > 0"},
                                    "backgroundColor": colors.LIGHT_GREEN
                                },
                                {
                                    "if": {"filter_query": "{signal_number} = -2 && {real_pnl} > 0"},
                                    "backgroundColor": colors.LIGHT_GREEN
                                },
                                {
                                    "if": {"filter_query": "{signal_number} = -1 && {real_pnl} < 0"},
                                    "backgroundColor": colors.LIGHT_RED
                                },
                                {
                                    "if": {"filter_query": "{signal_number} = -2 && {real_pnl} < 0"},
                                    "backgroundColor": colors.LIGHT_RED
                                },
                            ]
                        )
                    else:
                        table = html.Div("No data for this position")
                finally:
                    env.close()

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
            return get_collector_tables(collector_id)

        # ------------------ Update bot header style -----------------
        @app.callback(
            Output({'type': f'grid-bot-header', 'index': MATCH}, 'style'),
            Input('global-interval', 'n_intervals'),
            State({'type': f'grid-bot-header', 'index': MATCH}, 'id')
        )
        def update_header_style(n, header_id):
            bot_id = header_id['index']
            bots = get_all_bots()
            bot = next((b for b in bots if b['id'] == bot_id), None)
            if not bot:
                return no_update
            return styles.bot_card_header_style(bot['status'])