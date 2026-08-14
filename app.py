# app.py (final version)
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import dash
from dash import dcc, html, Input, Output, State, ALL, MATCH, no_update, callback_context
import os
import json
from core.database import DATA_DIR, cleanup_orphan_databases, update_bot_config
import atexit
import signal
import logging


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

os.makedirs(DATA_DIR, exist_ok=True)
init_config_db()
cleanup_orphan_databases()  # now the function is defined in core and imported
perf_logger.initialize_with_storage(SettingsStorage)
logger = perf_logger.get_logger('app', 'app')

load_modules("modules")

bot_manager = BotManager()
bot_manager.start_loop_in_thread()
atexit.register(bot_manager.shutdown)
signal.signal(signal.SIGINT, lambda s, f: bot_manager.shutdown())
signal.signal(signal.SIGTERM, lambda s, f: bot_manager.shutdown())
bot_manager.load_bots()

app = dash.Dash(__name__, title='T.B.O.T')
app.config.suppress_callback_exceptions = True

# Callback registration uses bot_manager.loop
for model_name in bot_registry.list_models():
    if model_name.endswith('.type'):
        meta_cls = bot_registry.get_model(model_name)
        if hasattr(meta_cls, 'register_callbacks'):
            meta_cls.register_callbacks(app, bot_manager, bot_manager.loop)

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
        dcc.Store(id='editing-bots', data={}),
        dcc.Store(id='prev-edit-clicks', data=[]),
        dcc.Location(id='url', refresh=False),
    ]),
    html.Div(id='bots-container'),
    dcc.Interval(id='global-interval', interval=5000, n_intervals=0)
])

# ---------- Common callbacks ----------

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
    ctx = callback_context
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
            new_settings = 0
            type_options = []
            for model_name in bot_registry.list_models():
                if model_name.endswith('.type'):
                    cls = bot_registry.get_model(model_name)
                    display = getattr(cls, 'display_name', model_name)
                    type_id = model_name.split('.')[0]
                    type_options.append({'label': display, 'value': type_id})
            if not type_options:
                form_content = html.Div("No bot types registered. Check modules.")
            else:
                form_content = html.Div([
                    html.H3('Add Bot'),
                    dcc.Dropdown(id='bot-type-selector', options=type_options, value=type_options[0]['value']),
                    html.Div(id='dynamic-bot-form')
                ])
        else:
            add_style = {'display': 'none'}
        new_add = add_clicks

    elif triggered_id == 'settings-btn':
        if settings_clicks % 2 == 1:
            settings_style = {'display': 'block'}
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
        return html.Div("Select a bot type")
    meta_cls = bot_registry.get_model(f"{bot_type}.type")
    if not meta_cls or not hasattr(meta_cls, 'form_component'):
        return html.Div(f"Form for type '{bot_type}' not found")
    try:
        return meta_cls.form_component(current_bot_id=None)
    except TypeError:
        return meta_cls.form_component()
    except Exception as e:
        logger.error(f"Error rendering form: {e}")
        return html.Div(f"Error loading form: {e}")


@app.callback(
    [Output('bots-trigger', 'data', allow_duplicate=True),
     Output('editing-bots', 'data')],
    Input('save-bot-btn', 'n_clicks'),
    [State('bot-type-selector', 'value'),
     State({'type': ALL, 'field': ALL}, 'value'),
     State({'type': ALL, 'field': ALL}, 'id'),
     State('bots-trigger', 'data')],
    prevent_initial_call=True
)
def save_bot(n_clicks, bot_type, field_values, field_ids, trigger):
    if not n_clicks or not bot_type:
        return no_update, no_update

    config = {}
    for val, id_dict in zip(field_values, field_ids):
        field = id_dict.get('field')
        if field:
            config[field] = val

    meta_cls = bot_registry.get_model(f"{bot_type}.type")
    if meta_cls and hasattr(meta_cls, 'prepare_new_config'):
        config = meta_cls.prepare_new_config(config)

    bot_id = add_bot(bot_type, f"{bot_type} bot", config)
    bot_manager.add_bot(bot_id)
    return trigger + 1, {}


@app.callback(
    Output('bots-container', 'children'),
    [Input('bots-trigger', 'data'),
     Input('editing-bots', 'data'),
     Input('url', 'pathname')],
    [State('relayout-store', 'data')]
)
def render_bots(trigger, editing_bots, pathname, relayout_store):
    bots = get_all_bots()
    if not bots:
        return html.Div('No active bots. Click "+" to add one.')

    editing_bots = editing_bots or {}
    bot_blocks = []
    for bot in bots:
        bot_id = bot['id']
        bot_type = bot['type']
        meta_cls = bot_registry.get_model(f"{bot_type}.type")
        if not meta_cls:
            continue
        config = get_bot_config(bot_id)
        if not config:
            continue
        config['status'] = bot['status']

        if editing_bots.get(str(bot_id)):
            # Edit mode
            if hasattr(meta_cls, 'form_component'):
                try:
                    form = meta_cls.form_component(current_bot_id=bot_id)
                except TypeError:
                    form = meta_cls.form_component()
                except Exception as e:
                    form = html.Div(f"Error loading form: {e}")
            else:
                form = html.Div("Edit form not available for this type.")

            edit_block = html.Div([
                html.H4(f"Editing {config.get('name', f'Bot {bot_id}')}"),
                form,
                html.Button('Save', id={'type': 'edit-save-btn', 'index': bot_id}, style={'marginRight': '10px'}),
                html.Button('Cancel', id={'type': 'edit-cancel-btn', 'index': bot_id})
            ], style={'border': '1px solid #aaa', 'padding': '10px', 'margin': '10px 0'})
            bot_blocks.append(html.Div(edit_block, id={'type': 'bot-card', 'index': bot_id}, key=str(bot_id)))
        else:
            # Normal mode
            if hasattr(meta_cls, 'render_block'):
                block = meta_cls.render_block(bot_id, config, relayout_store)
                bot_blocks.append(html.Div(block, id={'type': 'bot-card', 'index': bot_id}, key=str(bot_id)))
            else:
                bot_blocks.append(html.Div(
                    f"Bot {bot_id} ({bot_type}) - no render_block",
                    id={'type': 'bot-card', 'index': bot_id}, key=str(bot_id)
                ))

    return bot_blocks


@app.callback(
    Output('relayout-store', 'data'),
    Input({'type': 'graph', 'index': ALL}, 'relayoutData'),
    State('relayout-store', 'data'),
    prevent_initial_call=True
)
def save_relayout(relayout_list, stored):
    ctx = callback_context
    if not ctx.triggered:
        return no_update
    triggered = ctx.triggered[0]
    try:
        graph_id_str = triggered['prop_id'].split('.')[0]
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
    [Input({'type': 'status-btn', 'index': MATCH}, 'n_clicks'),
     Input('bots-trigger', 'data')],
    [State({'type': 'status-btn', 'index': MATCH}, 'id')],
    prevent_initial_call=False
)
def toggle_bot(n_clicks, trigger, btn_id):
    bot_id = btn_id['index']
    bots = get_all_bots()
    bot = next((b for b in bots if b['id'] == bot_id), None)
    if not bot:
        return "Start"

    ctx = callback_context
    if not ctx.triggered:
        return "Stop" if bot['status'] == 'running' else "Start"

    prop_id = ctx.triggered[0]['prop_id'].split('.')[0]

    # If only the interval or bot list change triggered, just return current status
    if prop_id == 'bots-trigger':
        return "Stop" if bot['status'] == 'running' else "Start"

    # If it's the button but no click occurred (e.g., button recreated), also return current status
    if prop_id == 'status-btn' and (not n_clicks or n_clicks <= 0):
        return "Stop" if bot['status'] == 'running' else "Start"

    # Here we definitely have a real click
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
    ctx = callback_context
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


# ---------- Inline editing (fixed callbacks) ----------

@app.callback(
    [Output('editing-bots', 'data', allow_duplicate=True),
     Output('prev-edit-clicks', 'data')],
    Input({'type': 'edit-btn', 'index': ALL}, 'n_clicks'),
    State('editing-bots', 'data'),
    State('prev-edit-clicks', 'data'),
    prevent_initial_call=True
)
def enter_edit_mode(n_clicks_list, editing, prev_clicks):
    ctx = callback_context
    if not ctx.triggered:
        return no_update, no_update

    # Initialize previous state
    if not prev_clicks or len(prev_clicks) != len(n_clicks_list):
        # On first run or when number of bots changes, just save current
        return no_update, n_clicks_list

    # Find index where value increased (real click)
    for i, (cur, prev) in enumerate(zip(n_clicks_list, prev_clicks)):
        if cur > prev:
            # Found clicked button. Need to know its bot_id.
            # Unfortunately, we cannot directly link index to component id.
            # But we can get the list of button ids from context: they are in inputs_list.
            # Use callback_context.inputs_list to access id.
            # Get the id of the component that generated this n_clicks.
            # But simpler: in render_bots buttons are generated in order of bot_id,
            # and in get_all_bots() they are sorted by position, id.
            # So the index corresponds to the order of bots in this list.
            bots = get_all_bots()
            if i < len(bots):
                bot_id = str(bots[i]['id'])
                editing = editing or {}
                editing[bot_id] = True
                return editing, n_clicks_list
    # If no increase found, just update saved state
    return no_update, n_clicks_list


@app.callback(
    [Output('editing-bots', 'data', allow_duplicate=True),
     Output('bots-trigger', 'data', allow_duplicate=True)],
    Input({'type': 'edit-save-btn', 'index': ALL}, 'n_clicks'),
    [State({'type': 'edit-save-btn', 'index': ALL}, 'id'),
     State({'type': ALL, 'field': ALL}, 'value'),
     State({'type': ALL, 'field': ALL}, 'id'),
     State('editing-bots', 'data'),
     State('bots-trigger', 'data')],
    prevent_initial_call=True
)
def save_editing(n_clicks_list, btn_ids, field_values, field_ids, editing, trigger):
    ctx = callback_context
    if not ctx.triggered:
        return no_update, no_update
    triggered = ctx.triggered[0]
    dict_str = triggered['prop_id'].split('.')[0]
    btn_id = json.loads(dict_str)
    bot_id = btn_id['index']

    idx = None
    for i, id_dict in enumerate(btn_ids):
        if id_dict['index'] == bot_id and n_clicks_list[i]:
            idx = i
            break
    if idx is None:
        return no_update, no_update

    new_fields = {}
    for val, id_dict in zip(field_values, field_ids):
        field = id_dict.get('field')
        if field:
            new_fields[field] = val
    new_fields.pop('data_db_path', None)

    old_config = get_bot_config(bot_id) or {}
    bots = get_all_bots()
    bot = next((b for b in bots if b['id'] == bot_id), None)
    if not bot:
        return no_update, no_update
    bot_type = bot['type']
    meta_cls = bot_registry.get_model(f"{bot_type}.type")

    if meta_cls and hasattr(meta_cls, 'process_edit_save'):
        config = meta_cls.process_edit_save(bot_id, new_fields, old_config)
    else:
        config = old_config.copy()
        config.update(new_fields)

    # Save config to bot's local DB
    update_bot_config(bot_id, config)

    # Notify bot about config change
    if bot_id in bot_manager.bots:
        bot_instance = bot_manager.bots[bot_id]
        # Mark config as "dirty", the bot's loop will call on_config_updated() itself
        bot_instance.config_dirty = True
        # If bot is not running, update its config in memory so it's current on start
        if not bot_instance.running:
            bot_instance.config = config

    # Notify bot about config change (without restart)
    '''if bot_id in bot_manager.bots:
        bot_instance = bot_manager.bots[bot_id]
        bot_instance.config_dirty = True
        future = asyncio.run_coroutine_threadsafe(
            bot_instance.on_config_updated(), bot_manager.loop
        )
        try:
            future.result(timeout=5)
            # Reset flag so the bot's loop does not call the method again
            bot_instance.config_dirty = False
        except Exception as e:
            logger.error(f"Error in on_config_updated for bot {bot_id}: {e}")'''

    editing = editing or {}
    editing.pop(str(bot_id), None)
    return editing, trigger + 1

@app.callback(
    Output('editing-bots', 'data', allow_duplicate=True),
    Input('bots-trigger', 'data'),
    State('editing-bots', 'data'),
    prevent_initial_call=True
)
def cleanup_editing_on_list_change(trigger, editing):
    """Remove from editing-bots bots that are no longer in the system."""
    if not editing:
        return {}
    bots = get_all_bots()
    active_ids = {str(b['id']) for b in bots}
    return {bid: v for bid, v in editing.items() if bid in active_ids}


@app.callback(
    Output('editing-bots', 'data', allow_duplicate=True),
    Input({'type': 'edit-cancel-btn', 'index': ALL}, 'n_clicks'),
    State('editing-bots', 'data'),
    prevent_initial_call=True
)
def cancel_editing(n_clicks_list, editing):
    ctx = callback_context
    if not ctx.triggered:
        return no_update

    triggered = ctx.triggered[0]
    # Check that it was a real click
    if not triggered.get('value') or triggered['value'] <= 0:
        return no_update

    dict_str = triggered['prop_id'].split('.')[0]
    btn_id = json.loads(dict_str)
    bot_id = str(btn_id['index'])

    editing = editing or {}
    editing.pop(bot_id, None)
    return editing


# ---------- Settings ----------

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
    return f"Interval: {n}"


if __name__ == '__main__':
    debug_mode = get_setting('debug_mode', 'False') == 'True'
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    app.run(debug=debug_mode)