# modules/broker/components.py
# Copyright (c) 2026 sergson
# Licensed under GNU General Public License v3.0

import asyncio

from dash import (html, dcc, Output, Input, State, MATCH, ALL,
                  no_update, callback_context)

from core import auto_reg, graphics
from core import styles as ui_compact
from core.styles import dropdown_field, input_field
from core.database import get_bot_config, get_all_bots
from core.logger import perf_logger
from modules.broker.lib.exchange_directory import EXCHANGES, MARKETS

logger = perf_logger.get_logger('broker_module', 'app')


# ---------- helpers ----------

def _get_broker_bot(bot_manager, bot_id):
    bot = bot_manager.bots.get(bot_id)
    if bot is None or getattr(bot, '_name', '') != 'broker.bot':
        return None
    return bot


def _run_cap(bot_manager, bot, cap_name, *args, **kwargs):
    caps = bot.get_capabilities()
    cap = caps.get(cap_name)
    if cap is None:
        raise ValueError(f"Broker does not expose '{cap_name}'")
    getter = cap.get('getter')
    if getter is None:
        raise ValueError(f"capability '{cap_name}' has no getter")
    loop = bot_manager.loop
    if not loop or not loop.is_running():
        raise RuntimeError("bot event loop not running")
    fut = asyncio.run_coroutine_threadsafe(
        getter(*args, **kwargs), loop)
    return fut.result(timeout=15)


def _run_set(bot_manager, bot, cap_name, data):
    caps = bot.get_capabilities()
    cap = caps.get(cap_name)
    if cap is None:
        raise ValueError(f"Broker does not expose '{cap_name}'")
    setter = cap.get('setter')
    if setter is None:
        raise ValueError(f"capability '{cap_name}' has no setter")
    loop = bot_manager.loop
    if not loop or not loop.is_running():
        raise RuntimeError("bot event loop not running")
    fut = asyncio.run_coroutine_threadsafe(setter(data), loop)
    return fut.result(timeout=15)


# ---------- form: provider + keys CRUD ----------

def broker_form(current_bot_id=None):
    default_provider = 'ccxt'
    if current_bot_id is not None:
        cfg = get_bot_config(current_bot_id) or {}
        dp = cfg.get('default_provider', 'ccxt')
        if dp in ('ccxt', 'tbotex'):
            default_provider = dp

    # New key inputs
    return html.Div([
        dropdown_field(
            {'type': 'broker-field', 'field': 'default_provider'},
            "Default provider for new keys",
            options=[{'label': 'ccxt', 'value': 'ccxt'}],
            value='ccxt',
            disabled=True,  # or clearable=False with a single option
            inline=True,
            help_text=("Broker currently supports ccxt only. "
                       "Native provider support (tbotex) is reserved "
                       "for a future release."),
        ),
        html.Hr(style={"margin": "4px 0"}),
        html.H4("Exchange keys", style={"margin": "2px 0"}),
        html.Div(id={'type': 'broker-keys-list', 'index': current_bot_id or 0}),
        html.Hr(style={"margin": "4px 0"}),
        html.H4("Add new key", style={"margin": "2px 0"}),
        dropdown_field(
            {'type': 'broker-new-key', 'field': 'exchange'},
            "Exchange",
            options=[{'label': e['label'], 'value': e['value']}
                     for e in EXCHANGES],
            value='binance', clearable=False, inline=True,
        ),
        dropdown_field(
            {'type': 'broker-new-key', 'field': 'market_type'},
            "Market type",
            options=MARKETS, value='spot', clearable=False, inline=True,
        ),
        input_field(
            {'type': 'broker-new-key', 'field': 'label'},
            "Label", type="text", value='', inline=True,
        ),
        input_field(
            {'type': 'broker-new-key', 'field': 'api_key'},
            "API Key", type="password", value='', inline=True,
        ),
        input_field(
            {'type': 'broker-new-key', 'field': 'api_secret'},
            "API Secret", type="password", value='', inline=True,
        ),
        dropdown_field(
            {'type': 'broker-new-key', 'field': 'mode'},
            "Storage mode",
            options=[
                {'label': 'Plain (dev only)', 'value': 'plain'},
                {'label': 'Encrypted', 'value': 'encrypted'},
            ],
            value='encrypted', clearable=False, inline=True,
        ),
        input_field(
            {'type': 'broker-new-key', 'field': 'password'},
            "Encryption password (only for encrypted mode)",
            type="password", value='', inline=True,
        ),
        html.Button("Add key", id={'type': 'broker-add-key-btn',
                                    'index': current_bot_id or 0},
                    n_clicks=0),
        html.Hr(style={"margin": "4px 0"}),
        html.H4("Unlock", style={"margin": "2px 0"}),
        input_field(
            {'type': 'broker-unlock', 'field': 'password'},
            "Password to unlock encrypted keys",
            type="password", value='', inline=True,
        ),
        html.Button("Unlock keys",
                    id={'type': 'broker-unlock-btn',
                        'index': current_bot_id or 0},
                    n_clicks=0),
        html.Div(id={'type': 'broker-form-status',
                     'index': current_bot_id or 0},
                 style={'fontSize': 'small', 'color': 'gray'}),
    ], style=ui_compact.STYLE_FORM)


# ---------- health panel ----------

def _render_health_panel_from_status(bot_id, st):
    def _fmt_age(ns):
        if not ns:
            return "—"
        return f"{ns / 1e9:.1f}s"

    def _state_text(s):
        return {
            'off': '— (not subscribed)',
            'connecting': 'connecting…',
            'connected': 'connected (awaiting first tick)',
            'idle': 'healthy (idle, no orders yet)',
            'active': 'healthy',
            'stalled': 'down',
        }.get(s, s)

    def _badge(v):
        if v is None:
            return html.Span("n/a", style={"color": "gray"})
        if v:
            return html.Span("healthy", style={"color": "green"})
        return html.Span("down",
                         style={"color": "red", "fontWeight": "bold"})

    running = st.get('running', False)
    healthy = st.get('healthy', False)

    state_children = [
        "running=", _badge(running),
        " · healthy=", _badge(healthy),
    ]

    conn_line = (
        f"{st.get('ws_connections', 0)} WS · "
        f"{st.get('rest_clients', 0)} REST · "
        f"{st.get('keys_count', 0)} keys"
    )

    #categorizing REST clients by owner
    rest_info = st.get('rest_client_keys') or []
    rest_children = []
    if rest_info:
        for i, line in enumerate(rest_info):
            if i > 0:
                rest_children.append(html.Br())
            rest_children.append(html.Span(line))
    else:
        rest_children.append(html.Span("—",
            style={"color": "gray"}))

    price_state = st.get('price_state', '—')
    price_children = [
        f"{st.get('price_workers', 0)} price worker · "
        f"{st.get('price_symbols', 0)} symbol · "
        f"last {_fmt_age(st.get('last_price_age_ns', 0))} · "
        f"WS {_state_text(price_state)}",
    ]

    order_state = st.get('orders_state', '—')
    orders_children = [
        f"{st.get('orders_workers', 0)} order worker · "
        f"{st.get('orders_owners', 0)} owner · "
        f"{st.get('orders_with_events', 0)} with events · "
        f"WS {_state_text(order_state)}",
    ]

    return graphics.info_panel([
        ("State", html.Span(state_children)),
        ("Connections", conn_line),
        ("REST clients", html.Span(rest_children)),
        ("Price", html.Span(price_children)),
        ("Orders", html.Span(orders_children)),
    ])


def render_broker_block(bot_id: int, config: dict, relayout_store: dict):
    return html.Details([
        html.Summary(
            f"#{bot_id} Broker Bot",
            id={'type': 'broker-bot-header', 'index': bot_id},
            style=ui_compact.bot_card_header_style(config.get('status')),
        ),
        html.Div([
            html.Div(
                id={'type': 'broker-health-panel', 'index': bot_id},
                children=_render_health_panel_from_status(bot_id, {})),
            html.Div([
                html.Button("Edit",
                            id={'type': 'edit-btn', 'index': bot_id},
                            n_clicks=0),
            ], style=ui_compact.STYLE_BUTTON_ROW),
            html.Hr(style={"margin": "1px 0"}),
        ], style=ui_compact.STYLE_BOTCARD_BODY),
    ], open=True, style=ui_compact.STYLE_BOTCARD)


# ---------- type registration + callbacks ----------

@auto_reg
class BrokerTypeMeta:
    _name = "broker.type"
    display_name = "Broker"
    bot_model = "broker.bot"
    form_component = staticmethod(broker_form)
    render_block = staticmethod(render_broker_block)

    @staticmethod
    def prepare_new_config(raw_config: dict) -> dict:
        config = raw_config.copy()
        config.pop('bot_type', None)
        config.pop('data_db_path', None)
        dp = config.get('default_provider', 'ccxt')
        if dp not in ('ccxt', 'tbotex'):
            dp = 'ccxt'
        config['default_provider'] = dp
        return config

    @staticmethod
    def process_edit_save(bot_id, new_fields, old_config):
        if 'bot_type' in new_fields and \
                new_fields['bot_type'] != old_config.get('bot_type'):
            raise ValueError("Cannot change 'bot_type' on existing bot")
        config = old_config.copy()
        if 'default_provider' in new_fields:
            dp = new_fields['default_provider']
            if dp not in ('ccxt', 'tbotex'):
                dp = 'ccxt'
            config['default_provider'] = dp
        if not config.get('default_provider'):
            config['default_provider'] = 'ccxt'
        return config

    @staticmethod
    def register_callbacks(app, bot_manager, loop):
        @app.callback(
            Output({'type': 'broker-bot-header', 'index': MATCH}, 'style'),
            Input('global-interval', 'n_intervals'),
            State({'type': 'broker-bot-header', 'index': MATCH}, 'id'),
        )
        def update_broker_header(n, header_id):
            bot_id = header_id['index']
            bots = get_all_bots()
            bot = next((b for b in bots if b['id'] == bot_id), None)
            if not bot:
                return no_update
            return ui_compact.bot_card_header_style(bot['status'])

        @app.callback(
            Output({'type': 'broker-health-panel', 'index': MATCH},
                   'children'),
            Input('global-interval', 'n_intervals'),
            State({'type': 'broker-health-panel', 'index': MATCH}, 'id'),
        )
        def update_broker_health(n, panel_id):
            bot_id = panel_id['index']
            bot = _get_broker_bot(bot_manager, bot_id)
            if bot is None:
                return no_update
            try:
                st = _run_cap(bot_manager, bot, 'get_broker_status')
            except Exception as e:
                logger.debug(f"get_broker_status failed: {e}")
                st = {}
            return _render_health_panel_from_status(bot_id, st)

        @app.callback(
            Output({'type': 'broker-keys-list', 'index': MATCH}, 'children'),
            Input('global-interval', 'n_intervals'),
            State({'type': 'broker-keys-list', 'index': MATCH}, 'id'),
        )
        def update_keys_list(n, list_id):
            bot_id = list_id['index']
            bot = _get_broker_bot(bot_manager, bot_id)
            if bot is None:
                return no_update
            try:
                keys = _run_cap(bot_manager, bot, 'list_exchange_keys')
            except Exception:
                return no_update
            if not keys:
                return html.Div("No keys yet",
                                style={'fontSize': 'small', 'color': 'gray'})
            rows = []
            for k in keys:
                mode = k.get('mode', 'plain')
                if mode == 'plain':
                    state, color = 'plain', 'gray'
                elif k.get('locked'):
                    state, color = 'locked', 'red'
                else:
                    state, color = 'unlocked', 'green'
                rows.append(html.Div([
                    html.Span(
                        f"{k.get('label') or '(no label)'} — "
                        f"{k['exchange']}/{k['market_type']} [{state}]",
                        style={'fontSize': 'small', 'color': color}),
                    html.Button(
                        "Delete",
                        id={'type': 'broker-del-key-btn',
                            'index': bot_id,
                            'key_id': k['key_id']},
                        n_clicks=0,
                        style={'marginLeft': '8px', 'fontSize': 'small'}),
                ], style={'display': 'flex', 'alignItems': 'center'}))
            return html.Div(rows)

        @app.callback(
            Output({'type': 'broker-form-status', 'index': MATCH},
                   'children'),
            Input({'type': 'broker-add-key-btn', 'index': MATCH},
                  'n_clicks'),
            State({'type': 'broker-new-key', 'field': ALL}, 'value'),
            State({'type': 'broker-new-key', 'field': ALL}, 'id'),
            State({'type': 'broker-form-status', 'index': MATCH}, 'id'),
            prevent_initial_call=True,
        )
        def add_key(n_clicks, values, ids, status_id):
            if not n_clicks:
                return no_update
            bot_id = status_id['index']
            bot = _get_broker_bot(bot_manager, bot_id)
            if bot is None:
                return "Broker not available"
            fields = {}
            for v, i in zip(values, ids):
                fields[i['field']] = v
            try:
                result = _run_set(bot_manager, bot, 'add_key', {
                    'exchange': fields.get('exchange'),
                    'market_type': fields.get('market_type'),
                    'label': fields.get('label') or '',
                    'api_key': fields.get('api_key') or '',
                    'api_secret': fields.get('api_secret') or '',
                    'mode': fields.get('mode') or 'plain',
                    'password': fields.get('password') or None,
                })
                key_id = (result or {}).get('key_id', '')
                return f"OK: key added ({key_id[:8]})"
            except Exception as e:
                return f"Error: {e}"

        @app.callback(
            Output({'type': 'broker-form-status', 'index': MATCH},
                   'children', allow_duplicate=True),
            Input({'type': 'broker-unlock-btn', 'index': MATCH}, 'n_clicks'),
            State({'type': 'broker-unlock', 'field': 'password'}, 'value'),
            State({'type': 'broker-form-status', 'index': MATCH}, 'id'),
            prevent_initial_call=True,
        )
        def unlock_keys(n_clicks, password, status_id):
            if not n_clicks:
                return no_update
            bot_id = status_id['index']
            bot = _get_broker_bot(bot_manager, bot_id)
            if bot is None:
                return "Broker not available"
            if not password:
                return "Password required"
            try:
                result = _run_set(bot_manager, bot, 'unlock_keys',
                                  {'password': password})
                unlocked = result.get('unlocked', [])
                failed = result.get('failed', [])
                total = result.get('total_encrypted', 0)
                if total == 0:
                    return "No encrypted keys to unlock."
                if not failed:
                    return f"All {total} encrypted keys unlocked."
                return (f"Unlocked {len(unlocked)}/{total}. "
                        f"{len(failed)} still locked — "
                        f"enter their passwords separately.")
            except Exception as e:
                return f"Error: {e}"

        @app.callback(
            Output({'type': 'broker-form-status', 'index': MATCH},
                   'children', allow_duplicate=True),
            Input({'type': 'broker-del-key-btn', 'index': ALL,
                   'key_id': ALL}, 'n_clicks'),
            State({'type': 'broker-form-status', 'index': MATCH}, 'id'),
            prevent_initial_call=True,
        )
        def delete_key(n_clicks_list, status_id):
            ctx = callback_context
            if not ctx.triggered:
                return no_update
            triggered = ctx.triggered[0]
            if not triggered.get('value'):
                return no_update
            import json as _json
            prop_id = triggered['prop_id'].split('.')[0]
            try:
                tid = _json.loads(prop_id)
            except Exception:
                return no_update
            bot_id = tid.get('index')
            key_id = tid.get('key_id')
            bot = _get_broker_bot(bot_manager, bot_id)
            if bot is None:
                return "Broker not available"
            try:
                _run_set(bot_manager, bot, 'delete_key',
                          {'key_id': key_id})
                return f"Deleted: {key_id[:8]}"
            except Exception as e:
                return f"Error: {e}"