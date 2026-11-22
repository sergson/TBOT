# modules/execution/components.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import asyncio
import concurrent.futures
import json
import time

from dash import (dcc, html, Input, Output, State, MATCH, ALL,
                  no_update, callback_context)
from core import auto_reg, colors, styles, graphics
from core.database import get_all_bots, get_bot_config, DBSQLite3
from core.logger import perf_logger
from core.time_sync import time_sync_service
import plotly.graph_objects as go
import pandas as pd
from modules.broker.lib.exchange_directory import (
    EXCHANGES, MARKETS, get_exchange_options, get_default_exchange_value,
)
from core import styles as ui_compact
from core.graphics import db_fill_badge
from .models import (ATTENTION_DOMAIN, ATTENTION_FIELDS, PRICE_MAX_ROWS,
                    DEFAULT_MAX_ORDER_ROWS, STATS_SAMPLE_SIZE,
                    PANIC_SIGNAL_NUMBER_FALLBACK)

MAX_LEVERAGE = 125
MIN_POLL_INTERVAL = 50
MAX_POLL_INTERVAL = 2000
DEFAULT_POLL_INTERVAL = 300
DEFAULT_SOURCE_TIMEOUT = 30
DEFAULT_POLL_DIVISOR = 5
STALE_PRICE_MAX = 60
DEFAULT_STALE_PRICE = 5

_BOT_TYPE_LABELS = {
    'grid': 'Grid Bot',
    'collector': 'Collector Bot',
    'analyst': 'Analyst Bot',
    'execution': 'Execution Bot',
}

def _source_bot_label(source_id):
    """"'Grid Bot #3' — the type is taken from the source config, not hardcoded.
	Falls back to 'Bot #N' if the type is unknown."""
    cfg = get_bot_config(source_id) or {}
    bot_type = cfg.get('bot_type')
    type_label = _BOT_TYPE_LABELS.get(bot_type, 'Bot')
    return f"{type_label} #{source_id}"

logger = perf_logger.get_logger('execution_bot', 'execution')


def _now_ns() -> int:
    """TBOT UTC, fallback to system time."""
    try:
        if time_sync_service.is_synced_event.is_set():
            return time_sync_service.get_utc_ns()
    except Exception:
        pass
    return time.time_ns()


# ----------------------------------------------------------------------
# Module-level helpers for order semantics.
# ----------------------------------------------------------------------
def _rgba(hex_color, alpha):
    """'#RRGGBB' → 'rgba(r,g,b,alpha)'. Non-hex values ​​are returned as is."""
    if not hex_color or not isinstance(hex_color, str):
        return hex_color
    if not hex_color.startswith('#'):
        return hex_color
    if len(hex_color) != 7:
        return hex_color
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    return f'rgba({r},{g},{b},{alpha})'

def _semantic_color(signal_number, side):
    if signal_number == PANIC_SIGNAL_NUMBER_FALLBACK: return colors.ORANGE
    if signal_number == -1: return colors.BLACK
    if signal_number == -2: return colors.BLUE
    if signal_number == 0:
        return colors.GREEN if side == 'buy' else colors.RED
    return colors.MEDIUM_GREEN if side == 'buy' else colors.MEDIUM_RED

def _state_edge(exchange_state):
    """Stroke color. Width is fixed at 1. None / unknown → GRAY."""
    if not exchange_state:
        return colors.GRAY
    return {
        'filled':    colors.BLACK,
        'partial':   colors.ORANGE,
        'canceled':  colors.GRAY,
        'expired':   colors.GRAY,
        'rejected':  colors.RED,
        'not_found': colors.DARK_RED,
        'not_sent':  colors.LIGHT_GRAY,
        'open':      colors.BLUE,
    }.get(exchange_state, colors.GRAY)


def _state_opacity(exchange_state):
    """For not_sent/not_found; 1.0 otherwise."""
    return 0.4 if exchange_state in ('not_sent', 'not_found') else 1.0


_LEGEND_COLORS = {
    'Entry buy':    _semantic_color(0, 'buy'),
    'Entry sell':   _semantic_color(0, 'sell'),
    'Avg buy':      _semantic_color(1, 'buy'),
    'Avg sell':     _semantic_color(1, 'sell'),
    'Close':        _semantic_color(-2, None),
    'Liquidation':  _semantic_color(-1, None),
    'Panic':        _semantic_color(PANIC_SIGNAL_NUMBER_FALLBACK, None),
}


def execution_form(current_bot_id=None):
    exchange_options = get_exchange_options()
    default_exchange = get_default_exchange_value()

    all_bots = get_all_bots()
    grid_bots = [{"label": f"{b['name']} (ID {b['id']})", "value": b['id']}
                 for b in all_bots if b['type'] == 'grid']
    feedback_options = [
        {"label": f"{b['name']} (ID {b['id']})", "value": b['id']}
        for b in all_bots if b['type'] == 'grid'
    ]

    source_bot_id = None
    exchange = default_exchange
    default_values = {}
    for item in EXCHANGES:
        if item.get('value') == default_exchange:
            default_values = item.get('defaults') or {}
            break
    market_type = default_values.get('market') or 'spot'
    symbol = default_values.get('symbol') or 'BTC/USDT'
    poll_divisor = DEFAULT_POLL_DIVISOR
    feedback_bot_id = None
    unit_volume = 1.0
    unit_volume_mode = 'quote'
    leverage = 10

    editing = current_bot_id is not None
    cfg = {}
    if editing:
        cfg = get_bot_config(current_bot_id) or {}
        if cfg:
            source_bot_id = cfg.get('source_bot_id', source_bot_id)
            exchange = cfg.get('exchange', exchange)
            market_type = cfg.get('market_type', market_type)
            symbol = cfg.get('symbol', symbol) or 'BTC/USDT'
            poll_divisor = cfg.get('poll_divisor', poll_divisor)
            feedback_bot_id = cfg.get('feedback_bot_id', feedback_bot_id)
            unit_volume = cfg.get('unit_volume', unit_volume)
            unit_volume_mode = cfg.get('unit_volume_mode', unit_volume_mode)
            leverage = cfg.get('leverage', leverage)

    # P1.2: auto-select the only grid bot when creating a new bot
    if not editing and len(grid_bots) == 1:
        source_bot_id = grid_bots[0]['value']

    if exchange not in [opt['value'] for opt in exchange_options]:
        exchange = default_exchange

    return html.Div([
        dcc.Store(
            id={"type": "execution-meta", "name": "editing_flag"},
            data=bool(editing),
        ),
        html.Div(
            id={"type": "execution-meta", "name": "prefill_status"},
            style={"fontSize": "small", "color": colors.GRAY,
                   "marginBottom": "1px"},
        ),
        ui_compact.dropdown_field(
            {"type": "execution-field", "field": "source_bot_id"},
            "Signal Source (Grid Bot)",
            options=grid_bots,
            value=source_bot_id,
            disabled=editing,
            inline=True,
        ),
        ui_compact.dropdown_field(
            {"type": "execution-field", "field": "feedback_bot_id"},
            "Recipient of feedback",
            options=feedback_options,
            value=feedback_bot_id,
            placeholder="Default = signal source",
            inline=True,
        ),
        ui_compact.dropdown_field(
            {"type": "execution-field", "field": "exchange"},
            "Exchange",
            options=exchange_options,
            value=exchange,
            disabled=editing,
            inline=True,
        ),
        ui_compact.dropdown_field(
            {"type": "execution-field", "field": "market_type"},
            "Market",
            options=MARKETS,
            value=market_type,
            disabled=editing,
            inline=True,
        ),
        ui_compact.dropdown_field(
            {"type": "execution-field", "field": "symbol"},
            "Trading pair",
            options=[],
            value=symbol,
            placeholder="Select a pair",
            disabled=editing,
            inline=True,
        ),
        ui_compact.input_field({"type": "execution-field", "field": "leverage"},
                               "Leverage (futures only; ignored on spot)",
                               value=leverage, min=1, max=MAX_LEVERAGE, unit="×", inline=True),
        ui_compact.dropdown_field(
            {"type": "execution-field", "field": "broker_key_id"},
            "Broker key", options=[],
            value=(cfg.get('broker_key_id') if editing else None),
            placeholder="Select a key from Broker",
            clearable=False,
            inline=True,
        ),
        ui_compact.input_field(
            {"type": "execution-field", "field": "stale_price_sec"},
            "Stale price threshold", type="number",
            value=(cfg.get('stale_price_sec', 5) if editing else 5),
            min=0, max=60, step=1, unit="seconds (0=off)", inline=True,
        ),
        ui_compact.input_field(
            {"type": "execution-field", "field": "poll_divisor"},
            "Polls per TTL",
            type="number",
            value=poll_divisor,
            min=1, max=100, step=1, unit="×", inline=True,
            help_text=(
                "Signal checks per TTL from source."
                "Square root of the number of price checks per TTL. "
            ),
        ),
        ui_compact.input_field({"type": "execution-field", "field": "unit_volume"},
                    "Unit volume (per 1 signal unit)",
                    value=unit_volume, min=0, step=0.00000001,
                    unit="base or quote — see next field", inline=True,
                    help_text=("In quote mode - cost of one quantum in quote currency; "
                    "in base mode — the size of one quantum in base currency. "),
        ),
        ui_compact.dropdown_field(
            {"type": "execution-field", "field": "unit_volume_mode"},
            "Unit volume currency",
            options=[
                {"label": "Base (BTC in BTC/USDT)", "value": "base"},
                {"label": "Quote (USDT in BTC/USDT)", "value": "quote"},
            ],
            value=unit_volume_mode,
            clearable=False,
            inline=True,
        ),
        html.Label("Unit volume auto-scale"),
        dcc.Checklist(
            id={"type": "execution-field", "field": "unit_volume_auto_scale"},
            options=[{"label": "Order volume auto-scale", "value": "on"}],
            value=['on'] if ((cfg.get('unit_volume_auto_scale', True)
                              if editing else True)) else [],
        ),
        ui_compact.input_field({"type": "execution-field", "field": "max_scale"},
                    "Auto-scale cap (0 = unlimited)",
                    value=(cfg.get('max_scale') if editing else 0) or 0,
                    min=0, step=0.1, unit="×", inline=True),
        ui_compact.input_field({"type": "execution-field", "field": "margin_buffer_ratio"},
                    "Funds buffer",
                    value=(cfg.get('margin_buffer_ratio') if editing else 0.05) or 0.05,
                    min=0, max=1, step=0.01, unit="0..1", inline=True),
        ui_compact.input_field({"type": "execution-field", "field": "breaker_invalid_order"},
                    "Max invalid orders",
                    value=(cfg.get('breaker_invalid_order') if editing else 3) or 3,
                    min=1, max=100, unit="failures", inline=True),
        ui_compact.input_field({"type": "execution-field", "field": "breaker_insufficient_funds"},
                    "Max funds errors",
                    value=(cfg.get('breaker_insufficient_funds') if editing else 1) or 1,
                    min=1, max=100, unit="failures", inline=True),
        ui_compact.input_field({"type": "execution-field", "field": "breaker_network"},
                    "Max network errors",
                    value=(cfg.get('breaker_network') if editing else 5) or 5,
                    min=1, max=100, unit="failures", inline=True),
        ui_compact.input_field({"type": "execution-field", "field": "max_order_rows"},
                               "Order history limit",
                               value=(cfg.get('max_order_rows') if editing else 10000) or 10000,
                               min=100, max=100000, unit="rows", inline=True),
        ui_compact.input_field({"type": "execution-field", "field": "stats_sample_size"},
                               "Latency history limit",
                               value=(cfg.get('stats_sample_size') if editing else 500) or 500,
                               min=100, max=10000, unit="rows", inline=True),
        html.Div(
            "On Binance USDT-M: leverage is applied via set_leverage at bot start. "
            "Must be in One-way Mode (not Hedge Mode).",
            style={"fontSize": "small", "color": colors.GRAY, "marginBottom": "1px"},
        ),
    ], style=ui_compact.STYLE_FORM)


def build_execution_figure(bot_id, config, relayout_store):
    db_path = f"data/bot_{bot_id}.db"
    env = DBSQLite3(db_path)
    fig = go.Figure()
    try:
        price_recs = env['execution.price'].search(
            [], order='timestamp DESC', limit=PRICE_MAX_ROWS)
        if price_recs:
            df = pd.DataFrame(price_recs.read()).sort_values('timestamp')
            df['datetime'] = pd.to_datetime(df['timestamp'], unit='ns', utc=True)
            fig.add_trace(graphics.line_trace(
                df['datetime'], df['bid'], 'Bid', colors.BLUE))
            fig.add_trace(graphics.line_trace(
                df['datetime'], df['ask'], 'Ask', colors.RED))

        order_recs = env['execution.order'].search([], order='id ASC', limit=5000)
        if order_recs:
            df_o = pd.DataFrame(order_recs.read())
            df_o = df_o[df_o['signal_timestamp'].notna()].copy()
            if not df_o.empty:
                df_o['datetime'] = pd.to_datetime(
                    df_o['signal_timestamp'], unit='ns', utc=True, errors='coerce')
                df_o = df_o.dropna(subset=['datetime'])
                if not df_o.empty:
                    _render_orders(fig, df_o)
    except Exception as e:
        logger.error(f"Error building execution figure: {e}")
    finally:
        env.close()

    graphics.apply_layout(fig,
                          uirevision='execution',
                          hovermode='x unified',
                          compact=True)

    stored = relayout_store.get(f"{bot_id}:execution-graph")
    if stored and isinstance(stored, dict):
        try:
            normalized = graphics.normalize_dash_relayout(stored)
            if normalized:
                fig.update_layout(normalized)
        except Exception as e:
            logger.warning(
                f"Failed to apply relayout: {e}; stored={stored!r}")

    return fig


def _render_orders(fig, df_o):
    """Shape by semantics, color by state; legend - semantics (dummy-trace)."""
    def _semantic(_sn, _side):
        if _side not in ('buy', 'sell'):
            _side = 'buy'
        if _sn == PANIC_SIGNAL_NUMBER_FALLBACK:
            return 'Panic', 'diamond', 10
        if _sn == -1: return 'Liquidation', 'star', 8
        if _sn == -2: return 'Close', 'x', 8
        if _sn == 0:
            return (f'Entry {_side}',
                    'triangle-up' if _side == 'buy' else 'triangle-down', 12)
        return (f'Avg {_side}',
                'triangle-up' if _side == 'buy' else 'triangle-down', 6)

    # --- vectorized NaN-safe colors ---
    sn    = df_o['signal_number'].fillna(0).astype(int)
    side  = df_o['side']
    state = df_o['exchange_state']

    # Shape — derived from the same `sn` as color; uses `zip` iteration
	# instead of `.at[idx]` (faster for 5000+ rows, avoids label lookup).
    sem, shapes, sizes = [], [], []
    for sn_val, side_val in zip(sn, side):
        name, shape, size = _semantic(sn_val, side_val)
        sem.append(name); shapes.append(shape); sizes.append(size)
    df_o['_semantic'] = sem
    df_o['_shape'] = shapes
    df_o['_size'] = sizes

    base_color = pd.Series(index=df_o.index, dtype=object)
    base_color[sn == PANIC_SIGNAL_NUMBER_FALLBACK] = colors.ORANGE
    base_color[sn == -1] = colors.BLACK
    base_color[sn == -2] = colors.BLUE
    base_color[(sn == 0) & (side == 'buy')] = colors.GREEN
    base_color[(sn == 0) & (side == 'sell')] = colors.RED
    base_color[(sn > 0) & (side == 'buy')] = colors.MEDIUM_GREEN
    base_color[(sn > 0) & (side == 'sell')] = colors.MEDIUM_RED
    base_color = base_color.fillna(colors.GRAY)

    alpha = state.map(_state_opacity)
    df_o['_color'] = pd.Series(
        [_rgba(b, a) for b, a in zip(base_color, alpha)],
        index=df_o.index,
    )
    df_o['_edge_color'] = state.map(_state_edge)

    order = ['Entry buy', 'Entry sell', 'Avg buy', 'Avg sell',
             'Close', 'Liquidation', 'Panic']
    for name in order:
        grp = df_o[df_o['_semantic'] == name]
        if grp.empty:
            continue
        shape = grp['_shape'].iloc[0]
        size = grp['_size'].iloc[0]
        fig.add_trace(go.Scatter(
            x=[None], y=[None], mode='markers', name=name,
            marker=dict(symbol=shape, size=size,
                        color=_LEGEND_COLORS.get(name, colors.GRAY)),
            showlegend=True, hoverinfo='skip', legendgroup=name,
        ))
        fig.add_trace(go.Scatter(
            x=grp['datetime'],
            y=grp['price_requested'].fillna(0),
            mode='markers',
            name=name,
            marker=dict(
                symbol=shape,
                size=grp['_size'],
                color=grp['_color'],
                line=dict(width=1, color=grp['_edge_color']),
            ),
            customdata=list(zip(
                grp['position_id'], grp['signal_number'], grp['attempt_n'],
                grp['exchange_state'], grp['amount_requested'],
                grp['amount_filled'], grp['price_requested'])),
            hovertemplate=(
                'pos=%{customdata[0]} sig=%{customdata[1]} '
                'att=%{customdata[2]}<br>'
                'state=%{customdata[3]}<br>'
                'req=%{customdata[4]:.6f} filled=%{customdata[5]:.6f}<br>'
                'price=%{customdata[6]:.4f}<extra></extra>'
            ),
            showlegend=False, legendgroup=name,
        ))


def _render_paused_badge(config):
    """PAUSED badge with reason, age, and countdown."""
    if config.get('status') != 'paused':
        return []
    paused = config.get('execution_paused') or {}
    reason = paused.get('reason') or '?'
    since_ns = int(paused.get('since_ns') or 0)
    check_at_ns = int(paused.get('check_at_ns') or 0)
    attempt = int(paused.get('attempt') or 0)

    hint = {
        'auth': 'Check API keys / permissions',
        'funds': 'Insufficient funds',
        'invalid_order': 'The exchange is rejecting orders',
        'invalid_order_persistent': (
            'The exchange is consistently rejecting orders — auto-wake is disabled'
        ),
        'network': 'Network issues',
    }.get(reason, reason)

    now_ns = _now_ns()
    paused_ago_s = max(0, (now_ns - since_ns) // 1_000_000_000) if since_ns else 0
    if check_at_ns > now_ns:
        check_in_s = max(0, (check_at_ns - now_ns) // 1_000_000_000)
        timer_text = f"next check in ~{check_in_s}s"
    else:
        timer_text = "checking conditions..."

    return html.Div([
        html.Div([
            html.Span("⏸ PAUSED: ", style={'fontWeight': 'bold'}),
            html.Span(hint),
        ]),
        html.Div(
            f"reason={reason}, attempts={attempt}, "
            f"pause {paused_ago_s}s, {timer_text}",
            style={'fontSize': 'small', 'color': colors.DARK_GRAY,
                   'marginTop': '1px'}),
        html.Div(
            "Signals are ignored until the pause is lifted. "
            "Panic close is always executed.",
            style={'fontSize': 'small', 'color': colors.DARK_GRAY,
                   'marginTop': '1px'}),
    ], style={
        'background': colors.LIGHT_YELLOW,
        'border': f'1px solid {colors.YELLOW}',
        'padding': '6px', 'borderRadius': '4px',
    })


#  The body of _render_account_card does NOT change.
# The signature remains (config). There are no changes compared to Patch 3 —
# a dummy diff was previously shown by mistake.
def _render_account_card(config):
    """Account card from balance_snapshot (updated every 30s)."""
    snap = config.get('balance_snapshot') or {}
    if not snap:
        return html.Div("Account: — (no data)",
                        style={'fontSize': ui_compact.FONT_SIZE_COMPACT,
                               'color': colors.GRAY})

    symbol = config.get('symbol') or 'BTC/USDT'
    core = symbol.split(':')[0]
    parts = core.split('/')
    if len(parts) == 2:
        base, quote = parts[0], parts[1]
    else:
        base, quote = parts[0], 'USDT'

    unit_vol = snap.get('_unit_volume') or 1.0
    unit_mode = snap.get('_unit_volume_mode') or 'quote'
    buff_ratio = snap.get('_margin_buffer_ratio') or 0.05

    q_snap = snap.get(quote) or {}
    b_snap = snap.get(base) or {}

    lines = [f"{quote}: free={q_snap.get('free', 0.0):.4f}"]
    if b_snap:
        lines.append(f"{base}: free={b_snap.get('free', 0.0):.6f}")

    if unit_mode == 'quote':
        unit_vol_quote = float(unit_vol)
        margin_buffer = buff_ratio * unit_vol_quote
        lines.append(f"unit_vol={unit_vol_quote:.4f} {quote}")
        lines.append(f"buffer={margin_buffer:.4f} {quote}")
    else:
        lines.append(f"unit_vol={float(unit_vol):.8f} {base} (base)")

    at_ns = int(snap.get('_at_ns') or 0)
    age_s = max(0, (_now_ns() - at_ns) // 1_000_000_000) if at_ns else 0

    return html.Div([
        html.Span("Счёт: ", style={'fontWeight': 'bold',
                                   'fontSize': ui_compact.FONT_SIZE_COMPACT}),
        html.Span(" · ".join(lines),
                  style={'fontSize': ui_compact.FONT_SIZE_COMPACT,
                         'color': colors.DARK_GRAY}),
        html.Span(f"  ({age_s}s back)",
                  style={'fontSize': ui_compact.FONT_SIZE_TINY,
                         'color': colors.GRAY}),
    ])


def _query_db_fill_stats(bot_id):
    """Read row counts via direct SQL; no bot instance required.

	Deviation from plan: this indicator has been moved to a separate output
	('execution-db-fill') — _render_account_card remains unchanged.

	Erratum-6 §4 (cosmetic change): uses a module-level `logger`
	(see the module header). The function is called from a callback and from
	render_execution_block — both contexts have access to the logger
	via the module. An explicit `logger` parameter is not needed.
	"""
    db_path = f"data/bot_{bot_id}.db"
    try:
        env = DBSQLite3(db_path)
    except Exception:
        return {}
    try:
        try:
            env.conn.execute("PRAGMA busy_timeout=2000")
        except Exception:
            pass
        cfg = get_bot_config(bot_id) or {}
        return {
            'orders': {
                'rows': env['execution.order'].search_count([]),
                'max': int(cfg.get('max_order_rows') or DEFAULT_MAX_ORDER_ROWS),
            },
            'prices': {
                'rows': env['execution.price'].search_count([]),
                'max': PRICE_MAX_ROWS,
            },
            'stats': {
                'rows': env['execution.stats'].search_count([]),
                'max': int(cfg.get('stats_sample_size') or STATS_SAMPLE_SIZE),
            },
        }
    except Exception as e:
        logger.debug(f"db_fill_stats {bot_id}: {e}")
        return {}
    finally:
        try:
            env.close()
        except Exception:
            pass


def _query_db_fill_stats_via_capability(bot_id, bot_manager, loop):
    """С4: try capability first, fall back to direct SQL."""
    bot = bot_manager.bots.get(bot_id)
    if bot and loop:
        method = getattr(bot, '_get_db_fill_stats', None)
        if method:
            try:
                future = asyncio.run_coroutine_threadsafe(method(), loop)
                result = future.result(timeout=2)
                if result:
                    return result
            except concurrent.futures.TimeoutError:
                # Benign: loop busy (REST reconnect, cross-bot polling).
                # Direct SQL fallback is the correct path here.
                logger.debug(
                    f"db_fill_stats: capability timed out for bot "
                    f"{bot_id} (loop busy) — using direct SQL")
            except Exception as e:
                logger.warning(
                    f"db_fill_stats: capability error for bot "
                    f"{bot_id}: {type(e).__name__}: {e}")
    return _query_db_fill_stats(bot_id)


def _query_attention_rows(bot_id):
    """Fallback: direct SQL if the capability is unavailable.

	The domain is taken from a single ATTENTION_DOMAIN
	(model.py) — it is not duplicated.
	"""
    db_path = f"data/bot_{bot_id}.db"
    try:
        env = DBSQLite3(db_path)
        try:
            env.conn.execute("PRAGMA busy_timeout=2000")
        except Exception:
            pass
    except Exception:
        return []
    try:
        # Unified ATTENTION_DOMAIN — synchronized
		# with ExecutionBot._get_attention_signals.
        cand = env['execution.order'].search(
            ATTENTION_DOMAIN, order='id DESC', limit=50)
        rows = [r for r in cand if not (r.ui_acknowledged_at_ns or 0)]
        fields = ATTENTION_FIELDS
        return [r.read(fields) for r in rows]
    except Exception as e:
        logger.error(f"attention query {bot_id}: {e}")
        return []
    finally:
        try:
            env.close()
        except Exception:
            pass


def _render_attention_items(bot_id, rows):
    """Render an attention-list from a list of dicts (read()).
	M-5: safeguard against `rid` being `None` (shouldn't happen, but good to have).
	"""
    if not rows:
        return []
    items = []
    for r in rows:
        rid = r.get('id')
        if rid is None:
            continue
        label = (f"id={rid} pos={r.get('position_id')} "
                 f"sig={r.get('signal_number')} att={r.get('attempt_n')} "
                 f"state={r.get('exchange_state')}")
        reason = r.get('reject_reason')
        if reason:
            label += f" reason={reason}"
        items.append(html.Div([
            html.Span(label, style={'fontSize': 'small'}),
            html.Button(
                "Close",
                id={'type': 'attention-close', 'index': bot_id,
                    'row': int(rid)},
                n_clicks=0,
                style={'marginLeft': '8px', 'fontSize': 'small'},
            ),
        ], style={'display': 'flex', 'alignItems': 'center',
                  'marginBottom': '1px'}))
    if not items:
        return []
    return html.Details([
        html.Summary(
            f"⚠ Requires attention ({len(items)})",
            style={'color': colors.DARK_RED, 'cursor': 'pointer',
                   'fontWeight': 'bold'}),
        html.Div(items, style={'marginTop': '1px',
                               'maxHeight': '200px',
                               'overflowY': 'auto'}),
    ], open=True, style={
        'background': colors.LIGHT_RED,
        'padding': '6px', 'borderRadius': '4px',
    })


def _render_attention_list(bot_id, config):
    """The first synchronous render of a card is performed via a direct SQL query."""
    return _render_attention_items(bot_id, _query_attention_rows(bot_id))


def render_execution_block(bot_id, config, relayout_store):
    graph_id = {"type": "execution-graph", "index": bot_id}
    fig = build_execution_figure(bot_id, config, relayout_store)
    pos_store = dcc.Store(id={'type': 'execution-events', 'index': bot_id},
                          data={'counter': 0})
    db_stats = _query_db_fill_stats(bot_id)
    source_id = config.get('source_bot_id')
    if source_id is not None:
        source_label = _source_bot_label(source_id)
    else:
        source_label = "—"

    return html.Details([
        html.Summary(
            f"#{bot_id} Execution Bot  ←  {source_label}",
            id={'type': 'execution-bot-header', 'index': bot_id},
            style=styles.bot_card_header_style(config.get('status')),
        ),
        html.Div([
            html.Div(id={'type': 'execution-paused-badge', 'index': bot_id},
                     children=_render_paused_badge(config)),
            graphics.info_panel([
                ("Exchange", f"{config.get('exchange')}"),
                ("Pair", f"{config.get('symbol')}"),
                ("Source", source_label),
                ("", html.Div(id={'type': 'execution-account-card', 'index': bot_id},
                              children=_render_account_card(config))),
                ("", html.Div(id={'type': 'execution-db-fill', 'index': bot_id},
                              children=db_fill_badge(db_stats))),
            ]),
            html.Div([
                html.Button("Stop" if config.get("status") in ("running", "paused")
                            else "Start",
                            id={'type': 'status-btn', 'index': bot_id}, n_clicks=0),
                html.Button("Edit", id={'type': 'edit-btn', 'index': bot_id}, n_clicks=0),
                html.Button("Delete", id={'type': 'delete', 'index': bot_id}, n_clicks=0),
            ], style=styles.STYLE_BUTTON_ROW),
            html.Hr(style={"margin": "1px 0"}),
            html.Div(id={'type': 'execution-attention-list', 'index': bot_id},
                     children=_render_attention_list(bot_id, config)),
            dcc.Graph(id=graph_id, figure=fig, config={"scrollZoom": True},
                      style={"height": "300px"}),
            pos_store,
        ], style=styles.STYLE_BOTCARD_BODY)
    ], open=True, style=styles.STYLE_BOTCARD)

def _normalize_divisor(config) -> int:
    try:
        v = int(config.get('poll_divisor') or DEFAULT_POLL_DIVISOR)
    except (TypeError, ValueError):
        v = DEFAULT_POLL_DIVISOR
    return max(1, min(100, v))


def _poll_interval_calc(config) -> int:
    """broker_poll_interval_ms = (source_timeout / N²) * 1000."""
    try:
        src_to = float(config.get('source_timeout') or DEFAULT_SOURCE_TIMEOUT)
        n = _normalize_divisor(config)
        derived_ms = int((src_to / (n * n)) * 1000)
        return max(MIN_POLL_INTERVAL, min(MAX_POLL_INTERVAL, derived_ms))
    except (TypeError, ValueError, ZeroDivisionError):
        return DEFAULT_POLL_INTERVAL

def _stale_price_calc(config):
    try:
        return max(0, min(STALE_PRICE_MAX, int(config.get('stale_price_sec', DEFAULT_STALE_PRICE) or DEFAULT_STALE_PRICE)))
    except (TypeError, ValueError):
        return DEFAULT_STALE_PRICE

@auto_reg
class ExecutionTypeMeta:
    _name = "execution.type"
    display_name = "Execution Bot"
    bot_model = "execution.bot"
    form_component = staticmethod(execution_form)
    render_block = staticmethod(render_execution_block)

    @staticmethod
    def _normalize_trading_fields(config):
        """Unified normalization of C-fields for create and edit paths."""
        try:
            config['max_scale'] = max(0.0, float(config.get('max_scale') or 0))
        except (TypeError, ValueError):
            config['max_scale'] = 0.0
        try:
            config['margin_buffer_ratio'] = max(
                0.0, min(1.0,
                         float(config.get('margin_buffer_ratio') or 0.05)))
        except (TypeError, ValueError):
            config['margin_buffer_ratio'] = 0.05
        try:
            config['max_order_rows'] = max(
                100, int(config.get('max_order_rows') or 10000))
        except (TypeError, ValueError):
            config['max_order_rows'] = 10000
        try:
            config['stats_sample_size'] = max(
                100, int(config.get('stats_sample_size') or 500))
        except (TypeError, ValueError):
            config['stats_sample_size'] = 500
        for key, default in (('breaker_invalid_order', 3),
                             ('breaker_insufficient_funds', 1),
                             ('breaker_network', 5)):
            try:
                config[key] = max(1, int(config.get(key) or default))
            except (TypeError, ValueError):
                config[key] = default
        auto = config.get('unit_volume_auto_scale')
        config['unit_volume_auto_scale'] = (
            bool(auto) if auto is not None else True)
        return config

    @staticmethod
    def _normalize_leverage(config):
        """Coerce leverage to int in [1, ...]."""
        try:
            v = int(config.get('leverage') or 10)
        except (TypeError, ValueError):
            v = 10
        v = max(1, min(MAX_LEVERAGE, v))
        config['leverage'] = v
        return config

    @staticmethod
    def _normalize_unit_volume(config):
        """Coerce unit_volume to a positive float and mode to base|quote."""
        try:
            v = float(config.get('unit_volume', 1.0) or 1.0)
        except (TypeError, ValueError):
            v = 1.0
        if v <= 0:
            v = 1.0
        config['unit_volume'] = v

        mode = config.get('unit_volume_mode', 'quote')
        if mode not in ('base', 'quote'):
            mode = 'quote'
        config['unit_volume_mode'] = mode
        return config

    @staticmethod
    def prepare_new_config(raw_config):
        config = raw_config.copy()
        if not config.get('symbol'):
            config['symbol'] = 'BTC/USDT'
        if not config.get('source_timeout'):
            config['source_timeout'] = 30
        for key in ('source_bot_id', 'feedback_bot_id'):
            v = config.get(key)
            if v is not None:
                try:
                    config[key] = int(v)
                except (TypeError, ValueError):
                    config[key] = None
        config = ExecutionTypeMeta._normalize_unit_volume(config)
        config = ExecutionTypeMeta._normalize_leverage(config)
        config = ExecutionTypeMeta._normalize_trading_fields(config)
        if not config.get('broker_key_id'):
            raise ValueError(
                "broker_key_id is required; add a key in Broker")
        # Always derive poll interval from divisor — single source of truth.
        config['poll_divisor'] = _normalize_divisor(config)
        config['broker_poll_interval_ms'] = _poll_interval_calc(config)
        config['stale_price_sec'] = _stale_price_calc(config)
        return config

    @staticmethod
    def process_edit_save(bot_id, new_fields, old_config):
        config = old_config.copy()
        config.update(new_fields)
        for immutable in ('exchange', 'market_type', 'symbol'):
            if immutable in old_config:
                new_val = new_fields.get(immutable)
                old_val = old_config.get(immutable)
                if new_val is not None and new_val != old_val:
                    raise ValueError(
                        f"Cannot change '{immutable}' on existing bot. "
                        f"Stop the bot, close all positions, and create "
                        f"a new bot for a different pair/exchange.")
                config[immutable] = old_val
        if 'source_bot_id' in old_config:
            config['source_bot_id'] = old_config['source_bot_id']
        config = ExecutionTypeMeta._normalize_unit_volume(config)
        config = ExecutionTypeMeta._normalize_leverage(config)
        config = ExecutionTypeMeta._normalize_trading_fields(config)
        if not config.get('broker_key_id'):
            raise ValueError(
                "broker_key_id is required; add a key in Broker")
        config['poll_divisor'] = _normalize_divisor(config)
        config['broker_poll_interval_ms'] = _poll_interval_calc(config)
        config['stale_price_sec'] = _stale_price_calc(config)
        if not config.get('symbol'):
            config['symbol'] = 'BTC/USDT'
        return config

    @staticmethod
    def register_callbacks(app, bot_manager, loop):

        # --- prefill from source bot's collector ---
        @app.callback(
            Output({'type': 'execution-field', 'field': 'exchange'}, 'value'),
            Output({'type': 'execution-field', 'field': 'market_type'}, 'value'),
            Output({'type': 'execution-field', 'field': 'symbol'}, 'value'),
            Output({'type': 'execution-field', 'field': 'leverage'}, 'value'),  # NEW
            Output({'type': 'execution-meta', 'name': 'prefill_status'}, 'children'),
            Input({'type': 'execution-field', 'field': 'source_bot_id'}, 'value'),
            State({'type': 'execution-meta', 'name': 'editing_flag'}, 'data'),
            prevent_initial_call=False,
        )
        def prefill_from_source(source_bot_id, editing):
            if editing:
                return (no_update, no_update, no_update, no_update, '')
            if not source_bot_id:
                return (no_update, no_update, no_update, no_update, '')

            grid_cfg = get_bot_config(source_bot_id) or {}
            collector_id = grid_cfg.get('collector_bot_id')

            # leverage from grid, clamped to execution-accepted range [1..MAX_LEVERAGE]
            try:
                lev_raw = int(grid_cfg.get('leverage') or 10)
            except (TypeError, ValueError):
                lev_raw = 10
            lev = max(1, min(MAX_LEVERAGE, lev_raw))

            if not collector_id:
                return (no_update, no_update, no_update, lev,
                        "Source does not contain collector_bot_id — "
                        "fill exchange/market_type/symbol in manually.")
            coll_cfg = get_bot_config(collector_id) or {}
            ex = coll_cfg.get('exchange')
            mt = coll_cfg.get('market_type')
            sym = coll_cfg.get('symbol')
            if not (ex and mt and sym):
                return (no_update, no_update, no_update, lev,
                        f"Collector #{collector_id} does not contain "
                        f"exchange/market_type/symbol — "
                        f"fill in manually.")

            status = f"Filled from collector #{collector_id}."
            if lev_raw != lev:
                status += (f" Leverage {lev_raw} exceeds max {MAX_LEVERAGE} — "
                           f"clamped to {lev}.")
            return ex, mt, sym, lev, status

        # --- market types (via broker) ---
        @app.callback(
            Output({'type': 'execution-field', 'field': 'market_type'},
                   'options'),
            Input({'type': 'execution-field', 'field': 'exchange'}, 'value'),
        )
        def update_market_types(exchange_id):
            if not exchange_id:
                return MARKETS
            broker = _get_broker_instance(bot_manager)
            if broker is None:
                return MARKETS
            try:
                supported = _run_broker_cap(
                    bot_manager, broker,
                    'get_supported_market_types', exchange_id,
                    owner="ui:executor_market_type")
                options = [m for m in MARKETS
                           if m['value'] in (supported or [])]
                return options or MARKETS
            except Exception as e:
                logger.error(f"market_types via broker failed: {e}")
                return MARKETS

        @app.callback(
            Output({'type': 'execution-bot-header', 'index': MATCH},
                   'style'),
            Input('global-interval', 'n_intervals'),
            State({'type': 'execution-bot-header', 'index': MATCH}, 'id'),
        )
        def update_execution_header(n, header_id):
            bot_id = header_id['index']
            bots = get_all_bots()
            bot = next((b for b in bots if b['id'] == bot_id), None)
            if not bot:
                return no_update
            return styles.bot_card_header_style(bot['status'])

        # --- symbols (via broker) ---
        @app.callback(
            Output({'type': 'execution-field', 'field': 'symbol'}, 'options'),
            Input({'type': 'execution-field', 'field': 'exchange'}, 'value'),
            Input({'type': 'execution-field', 'field': 'market_type'}, 'value'),
            State({'type': 'execution-field', 'field': 'symbol'}, 'value'),
        )
        def update_symbols(exchange_id, market_type, current_symbol):
            if not exchange_id or not market_type:
                return []
            broker = _get_broker_instance(bot_manager)
            if broker is None:
                return []
            try:
                symbols = _run_broker_cap(
                    bot_manager, broker, 'get_symbols',
                    exchange_id, market_type,
                    owner="ui:executor_symbols")
                options = [{'label': s, 'value': s} for s in (symbols or [])]
                if current_symbol and current_symbol not in [
                    o['value'] for o in options]:
                    options.insert(0, {'label': f"{current_symbol} (current)",
                                       'value': current_symbol})
                return options
            except Exception as e:
                logger.error(f"symbols via broker failed: {e}")
                if current_symbol:
                    return [{'label': current_symbol,
                             'value': current_symbol}]
                return []

        # --- broker keys ---
        @app.callback(
            Output({'type': 'execution-field', 'field': 'broker_key_id'},
                   'options'),
            Input({'type': 'execution-field', 'field': 'exchange'}, 'value'),
            Input({'type': 'execution-field', 'field': 'market_type'}, 'value'),
        )
        def update_broker_keys_options(exchange, market_type):
            broker = _get_broker_instance(bot_manager)
            if not broker:
                return []
            try:
                keys = _run_broker_cap(
                    bot_manager, broker, 'list_exchange_keys',
                    exchange=exchange, market_type=market_type,
                    owner="ui:executor_broker_keys")
            except Exception as e:
                logger.warning(f"list_exchange_keys failed: {e}")
                return []
            return [
                {'label': f"{k.get('label') or k['key_id'][:8]} "
                          f"({k['exchange']}/{k['market_type']}, "
                          f"{k['mode']})",
                 'value': k['key_id']}
                for k in (keys or [])
            ]

        # --- helpers ---
        def _get_broker_instance(_bot_manager):
            for _bid, b in _bot_manager.bots.items():
                if getattr(b, '_name', '') == 'broker.bot':
                    return b
            return None

        def _run_broker_cap(_bot_manager, broker, cap_name, *args,
                            owner="ui:execution_form", **kwargs):
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
                            getter(*args, **kwargs), _loop)
                        return fut.result(timeout=15)
                    finally:
                        _owner_ctx.reset(token)
            raise ValueError(
                f"Broker does not expose '{cap_name}'")

        # --- P1.1: UI event counter poller ---
        @app.callback(
            Output({'type': 'execution-events', 'index': MATCH}, 'data'),
            Input('global-interval', 'n_intervals'),
            State({'type': 'execution-events', 'index': MATCH}, 'id'),
            State({'type': 'execution-events', 'index': MATCH}, 'data'),
            prevent_initial_call=True,
        )
        def poll_execution_events(n, store_id, current_data):
            bot_id = store_id['index']
            bot = bot_manager.bots.get(bot_id)
            if not bot:
                return no_update
            counter = getattr(bot, 'ui_event_counter', 0)
            if current_data and current_data.get('counter') == counter:
                return no_update
            return {'counter': counter}

        # --- graph update (reacts to both timer & events) ---
        @app.callback(
            Output({'type': 'execution-graph', 'index': MATCH}, 'figure'),
            Input('global-interval', 'n_intervals'),
            Input({'type': 'execution-events', 'index': MATCH}, 'data'),
            State({'type': 'execution-graph', 'index': MATCH}, 'id'),
            State('relayout-store', 'data'),
        )
        def update_execution_graph(n, event_data, graph_id, relayout_store):
            bot_id = graph_id['index']
            # include_status=True — without it config.get('status') is None
            # and the callback would always return no_update.
            config = get_bot_config(bot_id, include_status=True)
            if not config or config.get('status') not in ('running', 'paused'):
                return no_update
            return build_execution_figure(bot_id, config, relayout_store)

        @app.callback(
            Output({'type': 'execution-paused-badge', 'index': MATCH}, 'children'),
            Output({'type': 'execution-account-card', 'index': MATCH}, 'children'),
            Output({'type': 'execution-attention-list', 'index': MATCH}, 'children'),
            Output({'type': 'execution-db-fill', 'index': MATCH}, 'children'),
            Input('global-interval', 'n_intervals'),
            Input({'type': 'execution-events', 'index': MATCH}, 'data'),
            State({'type': 'execution-paused-badge', 'index': MATCH}, 'id'),
            prevent_initial_call=True,
        )
        def update_execution_dynamic_blocks(n, event_data, block_id):
            bot_id = block_id['index']
            config = get_bot_config(bot_id, include_status=True)
            if not config:
                return no_update, no_update, no_update, no_update
            status = config.get('status')
            db_badge = db_fill_badge(_query_db_fill_stats_via_capability(
                bot_id, bot_manager, loop))
            if status not in ('running', 'paused'):
                # The attention-list and account card
				# are displayed for a stopped bot — the semantics are consistent
				# with the static markup in `render_execution_block`. 
				# Unresolved rows (`not_found` / `delivery_failed`) require
				# user action, regardless of the bot's status.
                return (_render_paused_badge(config),
                        _render_account_card(config),
                        _render_attention_list(bot_id, config),
                        db_badge)

            # Capability via a loop, with a fallback to direct SQL.
            rows = None
            bot = bot_manager.bots.get(bot_id)
            if bot and loop:
                method = getattr(bot, '_get_attention_signals', None)
                if method:
                    try:
                        future = asyncio.run_coroutine_threadsafe(method(), loop)
                        rows = future.result(timeout=2)
                    except concurrent.futures.TimeoutError:
                        logger.debug(
                            f"attention_signals: capability timed out for bot "
                            f"{bot_id} (loop busy) — using direct SQL")
                        rows = None
                    except Exception as e:
                        logger.warning(
                            f"attention_signals: capability error for bot "
                            f"{bot_id}: {type(e).__name__}: {e}")
                        rows = None
            if rows is None:
                rows = _query_attention_rows(bot_id)

            return (_render_paused_badge(config),
                    _render_account_card(config),
                    _render_attention_items(bot_id, rows),
                    db_badge)

        # --- close button ---
        @app.callback(
            Output('bots-trigger', 'data', allow_duplicate=True),
            Input({'type': 'attention-close', 'index': ALL, 'row': ALL},
                  'n_clicks'),
            [State({'type': 'attention-close', 'index': ALL, 'row': ALL}, 'id'),
             State('bots-trigger', 'data')],
            prevent_initial_call=True,
        )
        def close_attention_row(n_clicks_list, ids_list, trigger):
            ctx = callback_context
            if not ctx.triggered:
                return no_update
            triggered = ctx.triggered[0]
            if not triggered.get('value'):
                return no_update
            prop_id = triggered['prop_id'].split('.')[0]
            try:
                tid = json.loads(prop_id)
            except (json.JSONDecodeError, TypeError):
                return no_update
            row_id = tid.get('row')
            bot_id = tid.get('index')
            if row_id is None or bot_id is None:
                return no_update
            bot = bot_manager.bots.get(bot_id)
            if not bot or not loop:
                return no_update
            method = getattr(bot, '_close_attention_row', None)
            if not method:
                return no_update
            try:
                future = asyncio.run_coroutine_threadsafe(
                    method(int(row_id)), loop)
                future.result(timeout=3)
            except Exception as e:
                logger.error(f"attention close failed: {e}")
                return no_update
            return trigger + 1