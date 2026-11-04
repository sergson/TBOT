# core/styles.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

"""
Standard alignment, font, and spacing styles for T.B.O.T.
Use these constants instead of "magic" strings for interface consistency.
"""

from dash import dcc, html

from . import colors


# === Font sizes ===
FONT_SIZE_BOTHEADER = '16px'
FONT_SIZE_APPHEADER = '20px'
FONT_SIZE_FORM_FIELD = '14px'
FONT_SIZE_COMPACT = '12px'
FONT_SIZE_TINY = '11px'

# === Margins ===
MARGIN_ZERO = '0 0'
MARGIN_SMALL = '1px 1px'
MARGIN_NORMAL = '2px 2px'
MARGIN_BIG = '5px 5px'
MARGIN_AUTO = 'auto'
MARGIN_BETWEEN_BLOCKS = '0'

# === Padding ===
PADDING_ZERO = '0'
PADDING_SMALL = '1px 1px'
PADDING_NORMAL = '2px 2px'
PADDING_BIG = '5px 5px'

# === Borders ===
BORDER_DEFAULT = '1px solid black'

# === Pre‑combined styles ===
STYLE_BUTTON_ROW = {
    'display': 'flex',
    'flexWrap': 'wrap',
    'gap': '2px',
    'alignItems': 'center',
}

STYLE_BOTCARD = {
    'border': BORDER_DEFAULT,
    'padding': PADDING_NORMAL,
    'margin': MARGIN_NORMAL,
}

STYLE_BOTCARD_BODY = {
    'display': 'flex',
    'flexDirection': 'column',
    'gap': '1px',
}


def bot_card_header_style(status: str) -> dict:
    """status: 'running' | 'paused' | 'stopped' | any other."""
    if status == 'running':
        bg_color = colors.LIGHT_GREEN
    elif status == 'paused':
        bg_color = colors.LIGHT_YELLOW
    else:
        bg_color = colors.LIGHT_RED
    return {
        'cursor': 'pointer',
        'fontWeight': 'bold',
        'fontSize': FONT_SIZE_BOTHEADER,
        'backgroundColor': bg_color,
        'color': colors.BLACK,
        'listStyle': 'none',
        'padding': '1px 2px',
        'borderRadius': '2px',
    }


# === Compact UI styles (F5.1 / F5.5 / F5.7) ===

LABEL_STYLE = {
    'fontSize': FONT_SIZE_FORM_FIELD,
    'marginTop': '1px',
    'marginBottom': '0',
    'display': 'block',
}

# The `padding-right` property for the input is overridden by the CSS rule
# `#bots-container input[type="password"] { padding-right: 32px }`.
INPUT_STYLE = {
    'width': '100%',
    'padding': '1px 2px',
    'fontSize': FONT_SIZE_FORM_FIELD,
    'height': '18px',
    'minHeight': '18px',
    'boxSizing': 'border-box',
}

DROPDOWN_STYLE = {
    'width': '100%',
    'fontSize': FONT_SIZE_FORM_FIELD,
}

FIELD_ROW_STYLE = {
    'display': 'flex',
    'flexDirection': 'column',
    'marginBottom': '1px',
}

STYLE_FORM = {
    'display': 'flex',
    'flexDirection': 'column',
    'gap': '1px',
    'padding': '1px 2px',
}

HELP_STYLE = {
    'fontSize': FONT_SIZE_TINY,
    'color': colors.GRAY,
    'marginTop': '1px',
}

INFO_PANEL_STYLE = {
    'display': 'flex',
    'flexDirection': 'column',
    'fontSize': FONT_SIZE_COMPACT,
    'lineHeight': '1.15',
    'padding': '0',
    'rowGap': '1px',
}

# === Inline form variant: label | input | comment ===

FIELD_ROW_INLINE_STYLE = {
    'display': 'flex',
    'flexDirection': 'row',
    'flexWrap': 'wrap',
    'alignItems': 'center',
    'gap': '6px',
    'marginBottom': '2px',
}

LABEL_INLINE_STYLE = {
    'fontSize': FONT_SIZE_FORM_FIELD,
    'marginBottom': '0',
    'flex': '0 0 260px',
    'overflow': 'hidden',
    'whiteSpace': 'nowrap',
    'textOverflow': 'ellipsis',
}

INPUT_INLINE_STYLE = {
    **INPUT_STYLE,
    'width': '120px',
    'flex': '0 0 120px',
}

HELP_INLINE_STYLE = {
    'fontSize': FONT_SIZE_TINY,
    'color': colors.GRAY,
    'marginTop': '0',
    'flex': '1 1 auto',
    'overflow': 'hidden',
    'whiteSpace': 'nowrap',
    'textOverflow': 'ellipsis',
}

DROPDOWN_INLINE_STYLE = {
    **DROPDOWN_STYLE,
    'width': '220px',
    'flex': '0 0 220px',
}

def input_field(field_id, label, *, type='number', value=None,
                min=None, max=None, step=None, disabled=False,
                unit=None, help_text=None, inline=False):
    """Compact numeric/text field. INPUT_STYLE applies only to dcc.Input.

    inline=False (default): label at the top, input, and comment at the bottom.
    inline=True: label | input | comment, all on a single line.
    In inline mode, the unit and help_text are combined into a single comment
    (help_text takes precedence).
    """
    style = INPUT_INLINE_STYLE if inline else INPUT_STYLE
    props = dict(id=field_id, type=type, value=value, style=style)
    if min is not None:
        props['min'] = min
    if max is not None:
        props['max'] = max
    if step is not None:
        props['step'] = step
    if disabled:
        props['disabled'] = True

    if inline:
        rows = [html.Label(label, style=LABEL_INLINE_STYLE), dcc.Input(**props)]
        comment = help_text or unit
        if comment:
            rows.append(html.Div(comment, style=HELP_INLINE_STYLE))
        return html.Div(rows, style=FIELD_ROW_INLINE_STYLE)

    rows = [html.Label(label, style=LABEL_STYLE), dcc.Input(**props)]
    if unit:
        rows.append(html.Div(unit, style=HELP_STYLE))
    if help_text:
        rows.append(html.Div(help_text, style=HELP_STYLE))
    return html.Div(rows, style=FIELD_ROW_STYLE)

def dropdown_field(field_id, label, *, options, value=None,
                   disabled=False, clearable=True, help_text=None,
                   inline=False, placeholder=None):
    """Compact dropdown field. Never receives INPUT_STYLE.

    inline=False (default): label above, dropdown, comment below.
    inline=True: label | dropdown | comment all on one line.
    """
    style = DROPDOWN_INLINE_STYLE if inline else DROPDOWN_STYLE
    dropdown = dcc.Dropdown(
        id=field_id, options=options, value=value,
        disabled=disabled, clearable=clearable, style=style,
        placeholder=placeholder,
    )

    if inline:
        rows = [html.Label(label, style=LABEL_INLINE_STYLE), dropdown]
        if help_text:
            rows.append(html.Div(help_text, style=HELP_INLINE_STYLE))
        return html.Div(rows, style=FIELD_ROW_INLINE_STYLE)

    rows = [html.Label(label, style=LABEL_STYLE), dropdown]
    if help_text:
        rows.append(html.Div(help_text, style=HELP_STYLE))
    return html.Div(rows, style=FIELD_ROW_STYLE)