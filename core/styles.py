# core/styles.py
"""
Standard alignment, font, and spacing styles for T.B.O.T.
Use these constants instead of "magic" strings for interface consistency.
"""

from . import colors


# === Font sizes ===
FONT_SIZE_BOTHEADER = '18px'     # bot card header
FONT_SIZE_APPHEADER = '22px'     # T.B.O.T header

# === Margins ===
MARGIN_ZERO = '0 0'
MARGIN_SMALL = '1px 1px'
MARGIN_NORMAL = '5px 5px'
MARGIN_BIG = '10px 10px'
MARGIN_AUTO = 'auto'  # for horizontal centering

# === Padding ===
PADDING_ZERO = '0'
PADDING_SMALL = '1px 1px'
PADDING_NORMAL = '5px 5px'
PADDING_BIG = '10px 10px'

# === Borders ===
BORDER_DEFAULT = '1px solid black'

# === Pre‑combined styles ===
# Button row with left alignment
STYLE_BUTTON_ROW = {
    'display': 'flex',
    'flexDirection': 'row',
    'justifyContent': 'flex-start',
    'alignItems': 'center',
}

# Bot card (outer container)
STYLE_BOTCARD = {
    'border': BORDER_DEFAULT,
    'padding': PADDING_NORMAL,
    'margin': MARGIN_NORMAL,
}

# Bot card header
def bot_card_header_style(status: str) -> dict:
    """
    Returns the style for a bot card header.
    status: 'running' or 'stopped'.
    """
    if status == 'running':
        bg_color = colors.LIGHT_GREEN
        text_color = colors.BLACK
    else:
        bg_color = colors.LIGHT_RED
        text_color = colors.BLACK

    return {
        'cursor': 'pointer',
        'fontWeight': 'bold',
        'fontSize': FONT_SIZE_BOTHEADER,
        'backgroundColor': bg_color,
        'color': text_color,
        'listStyle': 'none',
        'padding': PADDING_NORMAL,
        'borderRadius': '5px',
    }