# app.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import dash
from dash import dcc, html, Input, Output, State, ALL, MATCH, no_update
import threading
import asyncio
import os
import json

# Import core, but do NOT create logger immediately
from core import (
    load_modules, init_config_db, add_bot, get_all_bots, get_bot_config,
    update_bot_status, delete_bot, get_setting, save_setting,
    BotManager, bot_registry, perf_logger
)

class SettingsStorage:
    @staticmethod
    def get_setting(key): return get_setting(key)
    @staticmethod
    def save_setting(key, value): save_setting(key, value)

os.makedirs('data', exist_ok=True)
init_config_db()

# Load logger settings from DB BEFORE creating the first logger
perf_logger.initialize_with_storage(SettingsStorage)

# Now create logger
logger = perf_logger.get_logger('app', 'app')
logger.debug("Loading modules...")

# Load modules (they will also get loggers with already applied settings)
load_modules("modules")
logger.debug(f"Registry models after load: {bot_registry._models}")

# Create missing tables for any newly registered bot types
from core.database import ensure_type_tables
ensure_type_tables()
loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()

bot_manager = BotManager()
bot_manager.set_loop(loop)
bot_manager.load_bots()

app = dash.Dash(__name__, title='T.B.O.T')
app.config.suppress_callback_exceptions = True

app.layout = html.Div([
    html.H1('T.B.O.T', style={'textAlign': 'center'}),
    html.Hr(),
    html.Div([
        html.Button('➕', id='add-bot-btn', n_clicks=0, style={'marginRight': '10px'}),
        html.Button('⚙️', id='settings-btn', n_clicks=0),

        html.Div(id='add-bot-form-container', children=[
            html.Div(id='dynamic-bot-form-content'),
            html.Button('Save', id='save-bot-btn', style={'marginRight': '10px'}),
            html.Button('Cancel', id='cancel-add-btn')
        ], style={'display': 'none'}),

        html.Div(id='settings-panel', children=[
            html.H3('Settings'),
            html.Div(id='global-interval-debug'),
            html.Div([
                html.Label('Debug mode'),
                dcc.Checklist(id='debug-checkbox', options=[{'label': ' Enable', 'value': 'debug'}],
                              value=['debug'] if get_setting('debug_mode', 'False') == 'True' else []),
                html.Div('* Changes will take effect after restart', style={'fontSize': 'small', 'color': 'gray'})
            ], style={'marginBottom': '20px'}),
            html.H4('Logging levels'),
            html.Div(id='logging-levels-container', children=[
                html.Div([
                    html.Label(module),
                    dcc.Dropdown(id={'type': 'log-level-dropdown', 'module': module},
                                 options=[{'label': lvl, 'value': lvl} for lvl in ['DEBUG', 'INFO', 'WARNING', 'ERROR']],
                                 value=perf_logger.settings.get(f'{module}_level', 'DEBUG'))
                ], style={'marginBottom': '10px'}) for module in ['app', 'collector', 'fetcher', 'database', 'analytics']
            ]),
            html.Button('Save Settings', id='save-settings-btn'),
            html.Button('Close', id='close-settings-btn')
        ], style={'display': 'none', 'border': '1px solid black', 'padding': '10px', 'margin': '10px 0'}),

        dcc.Store(id='bots-trigger', data=0),
        dcc.Store(id='relayout-store', data={}),
        dcc.Location(id='url', refresh=False),
    ]),
    html.Div(id='bots-container'),
    dcc.Interval(id='global-interval', interval=5000, n_intervals=0)
])

# ---------- Callbacks ----------
@app.callback(
    [Output('add-bot-form-container', 'style'),
     Output('dynamic-bot-form-content', 'children'),
     Output('settings-panel', 'style'),
     Output('add-bot-btn', 'n_clicks'),
     Output('settings-btn', 'n_clicks')],
    [Input('add-bot-btn', 'n_clicks'),
     Input('settings-btn', 'n_clicks'),
     Input('cancel-add-btn', 'n_clicks'),
     Input('close-settings-btn', 'n_clicks'),
     Input('save-bot-btn', 'n_clicks')],
    prevent_initial_call=True
)
def toggle_forms(add_clicks, settings_clicks, cancel_clicks, close_clicks, save_clicks):
    ctx = dash.callback_context
    if not ctx.triggered:
        return no_update, no_update, no_update, no_update, no_update

    triggered_id = ctx.triggered[0]['prop_id'].split('.')[0]

    add_style = {'display': 'none'}
    settings_style = {'display': 'none'}
    form_content = no_update
    new_add = add_clicks
    new_settings = settings_clicks

    if triggered_id == 'add-bot-btn':
        if add_clicks % 2 == 1:
            add_style = {'display': 'block'}
            settings_style = {'display': 'none'}
            new_settings = 0
            # Get list of available types from registry (classes with _name ending with ".type")
            type_options = []
            for model_name in bot_registry.list_models():
                if model_name.endswith('.type'):
                    cls = bot_registry.get_model(model_name)
                    display = getattr(cls, 'display_name', model_name)
                    type_id = model_name.split('.')[0]  # e.g., "collector"
                    type_options.append({'label': display, 'value': type_id})
            form_content = html.Div([
                html.H3('Add Bot'),
                dcc.Dropdown(id='bot-type-selector', options=type_options,
                             value=type_options[0]['value'] if type_options else None),
                html.Div(id='dynamic-bot-form')
            ])
            logger.debug(f"Available types: {[opt['value'] for opt in type_options]}")
        else:
            add_style = {'display': 'none'}
        new_add = add_clicks

    elif triggered_id == 'settings-btn':
        if settings_clicks % 2 == 1:
            settings_style = {'display': 'block'}
            add_style = {'display': 'none'}
            new_add = 0
        else:
            settings_style = {'display': 'none'}
        new_settings = settings_clicks

    elif triggered_id in ['cancel-add-btn', 'save-bot-btn']:
        add_style = {'display': 'none'}
        new_add = 0

    elif triggered_id == 'close-settings-btn':
        settings_style = {'display': 'none'}
        new_settings = 0

    return add_style, form_content, settings_style, new_add, new_settings

@app.callback(
    Output('dynamic-bot-form', 'children'),
    Input('bot-type-selector', 'value')
)
def update_dynamic_form(bot_type):
    if not bot_type:
        return no_update
    meta_cls = bot_registry.get_model(f"{bot_type}.type")
    if meta_cls and hasattr(meta_cls, 'form_component'):
        return meta_cls.form_component()
    return html.Div(f"Form for type '{bot_type}' not found")

@app.callback(
    Output('bots-trigger', 'data', allow_duplicate=True),
    Input('save-bot-btn', 'n_clicks'),
    [State('bot-type-selector', 'value'),
     State({'type': ALL, 'field': ALL}, 'value'),
     State({'type': ALL, 'field': ALL}, 'id'),
     State('bots-trigger', 'data')],
    prevent_initial_call=True
)
def save_new_bot(n_clicks, bot_type, field_values, field_ids, trigger):
    if not n_clicks or not bot_type:
        return no_update

    config = {}
    for val, id_dict in zip(field_values, field_ids):
        field = id_dict.get('field')
        if field:
            config[field] = val

    if bot_type == 'collector':
        bot_id_temp = add_bot(bot_type, f"{config.get('exchange', '')} {config.get('symbol', '')}")
        config['data_db_path'] = f"data/bot_{bot_id_temp}.db"
        from core.database import DB_CONFIG
        import sqlite3
        with sqlite3.connect(DB_CONFIG) as conn:
            conn.execute('UPDATE bots SET config_data = ? WHERE id = ?',
                         (json.dumps(config), bot_id_temp))
            conn.execute('UPDATE config_collector_type SET data_db_path = ? WHERE bot_id = ?',
                         (config['data_db_path'], bot_id_temp))
        bot_manager.add_bot(bot_id_temp)
        return trigger + 1

    bot_id = add_bot(bot_type, f"{bot_type} bot", config)
    bot_manager.add_bot(bot_id)
    return trigger + 1

@app.callback(
    Output('bots-container', 'children'),
    [Input('bots-trigger', 'data'),
     Input('url', 'pathname')],
    [State('relayout-store', 'data')]
)
def render_bots(trigger, pathname, relayout_store):
    bots = get_all_bots()
    if not bots:
        return html.Div('No active bots. Click "+" to add one.')

    bot_blocks = []
    for bot in bots:
        bot_type = bot['type']
        meta_cls = bot_registry.get_model(f"{bot_type}.type")
        if not meta_cls:
            continue
        config = get_bot_config(bot['id'])
        if not config:
            continue
        config['status'] = bot['status']
        if hasattr(meta_cls, 'render_block'):
            block = meta_cls.render_block(bot['id'], config, relayout_store)
            bot_blocks.append(block)
    return bot_blocks

@app.callback(
    Output('relayout-store', 'data'),
    Input({'type': 'graph', 'index': ALL}, 'relayoutData'),
    State('relayout-store', 'data'),
    prevent_initial_call=True
)
def save_relayout(relayout_list, stored):
    ctx = dash.callback_context
    if not ctx.triggered:
        return no_update
    triggered = ctx.triggered[0]
    prop_id = triggered['prop_id']
    try:
        graph_id_str = prop_id.split('.')[0]
        graph_id = json.loads(graph_id_str)
        bot_id = graph_id['index']
        new_relayout = triggered['value']
    except:
        return no_update
    if new_relayout is None or not isinstance(new_relayout, dict):
        return no_update
    stored = stored.copy() if stored else {}
    stored[str(bot_id)] = new_relayout
    return stored

@app.callback(
    Output({'type': 'status-btn', 'index': MATCH}, 'children'),
    Input({'type': 'status-btn', 'index': MATCH}, 'n_clicks'),
    State({'type': 'status-btn', 'index': MATCH}, 'id'),
    prevent_initial_call=True
)
def toggle_bot(n_clicks, btn_id):
    if not n_clicks:
        return no_update
    bot_id = btn_id['index']
    bots = get_all_bots()
    bot = next((b for b in bots if b['id'] == bot_id), None)
    if not bot:
        return no_update
    if bot['status'] == 'running':
        bot_manager.stop_bot(bot_id)
        update_bot_status(bot_id, 'stopped')
        return "Start"
    else:
        bot_manager.start_bot(bot_id)
        update_bot_status(bot_id, 'running')
        return "Stop"

@app.callback(
    Output('bots-trigger', 'data', allow_duplicate=True),
    Input({'type': 'delete', 'index': ALL}, 'n_clicks'),
    State({'type': 'delete', 'index': ALL}, 'id'),
    State('bots-trigger', 'data'),
    prevent_initial_call=True
)
def delete_bot_callback(n_clicks_list, ids_list, trigger):
    ctx = dash.callback_context
    if not ctx.triggered:
        return no_update
    triggered = ctx.triggered[0]['prop_id'].split('.')[0]
    triggered_id = json.loads(triggered)
    bot_id = triggered_id['index']
    for i, id_dict in enumerate(ids_list):
        if id_dict['index'] == bot_id and n_clicks_list[i]:
            bot_manager.remove_bot(bot_id)
            delete_bot(bot_id)
            return trigger + 1
    return no_update

@app.callback(
    Output({'type': 'graph', 'index': MATCH}, 'figure'),
    Input('global-interval', 'n_intervals'),
    State({'type': 'graph', 'index': MATCH}, 'id'),
    State('relayout-store', 'data')
)
def update_graph(n, graph_id, relayout_store):
    bot_id = graph_id['index']
    logger.debug(f"Update_graph: n={n}, bot_id={bot_id}")

    bots = get_all_bots()
    bot = next((b for b in bots if b['id'] == bot_id), None)
    if not bot:
        logger.debug(f"Update_graph: bot {bot_id} not found in database")
        return no_update
    if bot['status'] != 'running':
        logger.debug(f"Update_graph: bot {bot_id} status is '{bot['status']}', not running")
        return no_update

    config = get_bot_config(bot_id)
    if not config:
        logger.debug(f"Update_graph: no config for bot {bot_id}")
        return no_update
    if bot['type'] != 'collector':
        logger.debug(f"Update_graph: bot {bot_id} is not collector (type={bot['type']})")
        return no_update

    from modules.collector.components import build_figure
    fig = build_figure(bot_id, config, relayout_store)
    logger.debug(f"Update_graph: built figure with {len(fig.data[0].x) if fig.data else 0} candles")
    return fig

@app.callback(
    Output('settings-panel', 'children', allow_duplicate=True),
    Input('save-settings-btn', 'n_clicks'),
    [State('debug-checkbox', 'value'),
     State({'type': 'log-level-dropdown', 'module': ALL}, 'value'),
     State({'type': 'log-level-dropdown', 'module': ALL}, 'id')],
    prevent_initial_call=True
)
def save_settings(n_clicks, debug_val, log_levels, level_ids):
    if not n_clicks:
        return no_update
    debug_mode = 'True' if debug_val and 'debug' in debug_val else 'False'
    save_setting('debug_mode', debug_mode)
    settings_update = {}
    for level_val, id_dict in zip(log_levels, level_ids):
        module = id_dict['module']
        settings_update[f'{module}_level'] = level_val
    perf_logger.update_settings(settings_update)
    save_setting('logging_settings', json.dumps(perf_logger.settings))
    return no_update

@app.callback(
    Output('global-interval-debug', 'children'),
    Input('global-interval', 'n_intervals')
)
def debug_interval(n):
    logger.debug(f"Global-interval fired: {n}")
    return f"Interval: {n}"

if __name__ == '__main__':
    debug_mode = get_setting('debug_mode', 'False') == 'True'
    app.run(debug=debug_mode)