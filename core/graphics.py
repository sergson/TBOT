# core/graphics.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

"""
Atomic Plotly graphic element factories.
Uses colors from core.colors and styles from core.styles.
Does not contain bot-specific logic.
"""

import plotly.graph_objects as go
from dash import html, dash_table
from dash.development.base_component import Component
from . import colors
from . import styles
from .styles import FONT_SIZE_BOTHEADER   # fixed: using bot header size
from .logger import perf_logger

logger = perf_logger.get_logger('graphics', 'app')


# ----------------------------------------------------------------------
# Lines
# ----------------------------------------------------------------------
def line_trace(x, y, name, color, width=1, dash='solid',
               hovertemplate=None, hoverinfo=None, mode='lines'):
    """
    Creates a line trace with arbitrary x, y arrays.
    x, y can be lists/arrays.
    """
    trace = go.Scatter(
        x=x, y=y,
        mode=mode,
        name=name,
        line=dict(color=color, width=width, dash=dash),
    )
    if hovertemplate:
        trace.hovertemplate = hovertemplate
    if hoverinfo:
        trace.hoverinfo = hoverinfo
    return trace


def hline(x_min, x_max, y, name, color, width=1, dash='solid',
          hovertemplate=None):
    """Horizontal line from x_min to x_max."""
    return line_trace(
        x=[x_min, x_max],
        y=[y, y],
        name=name,
        color=color,
        width=width,
        dash=dash,
        hovertemplate=hovertemplate,
    )


def price_line(x, y, name, color=colors.BLACK, width=1,
               hoverinfo=None, hovertemplate=None):
    """
    Price line (usually used for high/low).
    Has typical parameters, but they can be overridden.
    """
    return line_trace(
        x=x, y=y,
        name=name,
        color=color,
        width=width,
        hovertemplate=hovertemplate,
        hoverinfo=hoverinfo,
    )


# ----------------------------------------------------------------------
# Markers
# ----------------------------------------------------------------------
def marker(x, y, name, color, symbol, size,
           hovertemplate=None, customdata=None):
    """
    Creates a single marker (one point).
    x, y are scalar values.
    """
    trace = go.Scatter(
        x=[x], y=[y],
        mode='markers',
        name=name,
        marker=dict(color=color, symbol=symbol, size=size),
    )
    if hovertemplate:
        trace.hovertemplate = hovertemplate
    if customdata is not None:
        trace.customdata = [customdata]
    return trace


def markers_trace(x, y, name, color, symbol, size,
                  hovertemplate=None, customdata=None):
    """
    Creates a series of markers (multiple points).
    x, y are lists/arrays of the same length.
    """
    trace = go.Scatter(
        x=x, y=y,
        mode='markers',
        name=name,
        marker=dict(color=color, symbol=symbol, size=size),
    )
    if hovertemplate:
        trace.hovertemplate = hovertemplate
    if customdata is not None:
        trace.customdata = customdata
    return trace


# ----------------------------------------------------------------------
# Candlesticks and volume
# ----------------------------------------------------------------------
def candlestick_trace(df, name='Price',
                      increasing_color=colors.GREEN,
                      decreasing_color=colors.RED):
    """
    Creates a candlestick chart from a DataFrame with columns
    datetime, open, high, low, close.
    """
    return go.Candlestick(
        x=df['datetime'],
        open=df['open'],
        high=df['high'],
        low=df['low'],
        close=df['close'],
        name=name,
        increasing_line_color=increasing_color,
        decreasing_line_color=decreasing_color,
    )


def volume_bar_trace(df, name='Volume',
                     color=colors.LIGHT_BLUE, opacity=1.0):
    """
    Creates volume bars from a DataFrame with columns
    datetime and volume.
    """
    return go.Bar(
        x=df['datetime'],
        y=df['volume'],
        name=name,
        marker_color=color,
        opacity=opacity,
    )


# ----------------------------------------------------------------------
# Layout
# ----------------------------------------------------------------------
def apply_layout(fig,
                 title=None,
                 uirevision='grid',
                 show_rangeslider=False,
                 hovermode='x unified',
                 xaxis_range=None,
                 yaxis_range=None,
                 legend_bordercolor=colors.BLACK,
                 title_font_size=FONT_SIZE_BOTHEADER,
                 show_title=True,
                 show_axis_titles=True,
                 compact=False):
    """
    Applies a standard layout to the figure.
    `legend_bordercolor=None` removes the legend border.

    `show_title=False` removes the chart title;
    `show_axis_titles=False` removes the x/y axis titles.

    `title=None` is a valid value. In the non-compact branch, no title is set
    (Plotly handles `title=None` correctly).
    In the compact branch, the `title` parameter is ignored entirely.

    `compact=True`:
      OVERRIDDEN (caller arguments are ignored):
        `title`, `show_title`, `show_axis_titles`,
        `legend_bordercolor`, `legend.font.size`, `legend.tracegroupgap`,
        `legend.itemwidth`, `legend.borderwidth`,
        `margin`, `xaxis_rangeslider_visible`.
      TAKEN FROM ARGUMENTS: `uirevision`, `hovermode`, `xaxis_range`, `yaxis_range`.
      IGNORED: `title_font_size` (validation is skipped),
               `title` (can be omitted; defaults to `None`).

    Example of a compact call:
        apply_layout(fig, uirevision='execution',
                     hovermode='x unified', compact=True)
    """

    if compact:
        legend = dict(
            x=0, y=1,
            bgcolor=colors.LEGEND_BG,
            font=dict(size=10),
            itemsizing='trace',
            tracegroupgap=0,
            itemwidth=30,
            borderwidth=0,
        )
        layout_updates = dict(
            legend=legend,
            uirevision=uirevision,
            xaxis_rangeslider_visible=False,
            margin=dict(t=10, b=30, l=40, r=10),
        )
        if hovermode:
            layout_updates['hovermode'] = hovermode
        if xaxis_range is not None:
            layout_updates['xaxis_range'] = xaxis_range
        if yaxis_range is not None:
            layout_updates['yaxis_range'] = yaxis_range
        fig.update_layout(**layout_updates)
        return

    # --- non-compact branch ---
    # Convert font size if passed as a string with 'px'
    original_value = title_font_size
    if isinstance(title_font_size, str):
        if title_font_size.endswith('px'):
            try:
                title_font_size = int(title_font_size[:-2])
            except ValueError:
                logger.warning(f"Invalid title_font_size '{original_value}', using default 18")
                title_font_size = 18
        else:
            try:
                title_font_size = int(title_font_size)
            except ValueError:
                logger.warning(f"Invalid title_font_size '{original_value}', using default 18")
                title_font_size = 18
    elif not isinstance(title_font_size, (int, float)):
        logger.warning(f"Invalid title_font_size type {type(original_value).__name__}, using default 18")
        title_font_size = 18

    legend = dict(
        x=0, y=1,
        bgcolor=colors.LEGEND_BG,
    )
    if legend_bordercolor is not None:
        legend['bordercolor'] = legend_bordercolor
        legend['borderwidth'] = 1

    layout_updates = dict(
        title_font_size=title_font_size,
        legend=legend,
        uirevision=uirevision,
        xaxis_rangeslider_visible=show_rangeslider,
    )
    if show_title:
        layout_updates['title'] = title
    if show_axis_titles:
        layout_updates['xaxis_title'] = "Time"
        layout_updates['yaxis_title'] = "Price"
    if hovermode:
        layout_updates['hovermode'] = hovermode
    if xaxis_range is not None:
        layout_updates['xaxis_range'] = xaxis_range
    if yaxis_range is not None:
        layout_updates['yaxis_range'] = yaxis_range

    fig.update_layout(**layout_updates)


# ----------------------------------------------------------------------
# Compact info panel
# ----------------------------------------------------------------------
def info_panel(rows, title=None):
    """A compact info-panel for a bot card.

    rows: an iterable of (label, value) or (label, value, style_overrides) tuples.
    Rows where value is None or an empty container (e.g., []) are skipped—
    this allows for the safe handling of results from "optional" rendering logic.
    title: an optional header.

    If value is a Dash Component, it is rendered as-is (without a label).
    This enables the inclusion of output from optional rendering processes
    (such as _render_position_consistency) without forcing a conversion to a string.

    Included `str(value)` without a specific branch for Components—that version is now OBSOLETE.
    Patch 5 (this version) serves as the definitive reference: support
    for Components has been added, and Plan 10 has been updated accordingly.
    """
    children = []
    if title:
        children.append(html.Div(title, style={
            'fontWeight': 'bold',
            'fontSize': styles.FONT_SIZE_COMPACT,
            'marginBottom': '2px',
        }))
    for row in rows:
        if len(row) == 3:
            label, value, overrides = row
        else:
            label, value = row
            overrides = {}
        #skip an "empty" render.
        if value is None:
            continue
        if isinstance(value, (list, tuple)) and not value:
            continue
        #Dash Component — rendered directly;
        # a label for it makes no sense.
        if isinstance(value, Component):
            children.append(value)
            continue
        value_style = {'fontSize': styles.FONT_SIZE_COMPACT, **overrides}
        children.append(html.Div([
            html.Span(f"{label}: ", style={
                'flex': '0 0 100px',
                'fontWeight': 'bold',
                'fontSize': styles.FONT_SIZE_COMPACT,
                'overflow': 'hidden',
                'whiteSpace': 'nowrap',
                'textOverflow': 'ellipsis',
            }),
            html.Span(str(value), style={
                'flex': '1 1 auto',
                **value_style,
            }),
        ], style={'display': 'flex', 'lineHeight': '1.15'}))
    return html.Div(children, style=styles.INFO_PANEL_STYLE)


# ----------------------------------------------------------------------
# Compact DataTable
# ----------------------------------------------------------------------
def compact_table(data, columns, *, page_size=None,
                  style_data_conditional=None, **kwargs):
    """Compact DataTable factory. `style_data_conditional` is an explicit parameter
    (not a post-hoc mutation)."""
    style_table = kwargs.pop('style_table', {})
    style_cell = kwargs.pop('style_cell', {})
    style_header = kwargs.pop('style_header', {})
    return dash_table.DataTable(
        data=data,
        columns=columns,
        style_table={'overflowX': 'auto', **style_table},
        style_cell={
            'textAlign': 'center',
            'padding': '1px 4px',
            'fontSize': styles.FONT_SIZE_COMPACT,
            'height': '18px',
            'lineHeight': '1',
            'whiteSpace': 'nowrap',
            'overflow': 'hidden',
            'textOverflow': 'ellipsis',
            **style_cell,
        },
        style_header={
            'fontWeight': 'bold',
            'fontSize': styles.FONT_SIZE_TINY,
            'height': '18px',
            'lineHeight': '1',
            'padding': '1px 4px',
            **style_header,
        },
        style_data_conditional=style_data_conditional,
        page_size=page_size or max(len(data), 1),
        **kwargs,
    )


# ----------------------------------------------------------------------
# DB fill badge
# cosmetic: semantically, this is a UI indicator for the database,
# not a graphical element. It resides in graphics.py to avoid creating a
# separate module, bot_info_panel.py 
# Should the need for expanded functionality (histograms,
# progress bars) arise in the future, it can be moved to a dedicated
# UI-indicator module.
# ----------------------------------------------------------------------
def db_fill_badge(stats):
    """F2.3: DB fill indicator — small row of colored counts."""
    def _row(name, s):
        rows = int(s.get('rows') or 0)
        maxv_raw = int(s.get('max') or 0)
        if maxv_raw <= 0:
            return html.Span(
                f"{name} {rows}  ",
                style={'fontSize': styles.FONT_SIZE_TINY,
                       'color': colors.DARK_GRAY,
                       'fontFamily': 'monospace'})
        maxv = maxv_raw
        ratio = rows / maxv
        if ratio >= 0.9:
            color = colors.RED
        elif ratio >= 0.7:
            color = colors.ORANGE
        else:
            color = colors.DARK_GRAY
        return html.Span(
            f"{name} {rows}/{maxv}  ",
            style={'fontSize': styles.FONT_SIZE_TINY, 'color': color,
                   'fontFamily': 'monospace'})

    return html.Div([
        html.Span("DB: ", style={'fontSize': styles.FONT_SIZE_TINY,
                                 'fontWeight': 'bold'}),
        _row('orders', stats.get('orders') or {}),
        _row('prices', stats.get('prices') or {}),
        _row('stats', stats.get('stats') or {}),
    ])

def normalize_dash_relayout(relayout):
    """Convert Dash flat relayout to plotly nested form.

    Dash sends: {'xaxis.range[0]': a, 'xaxis.range[1]': b,
                 'xaxis.autorange': True, ...}
    plotly wants: {'xaxis': {'range': [a, b], 'autorange': True, ...}}
    """
    if not relayout or not isinstance(relayout, dict):
        return {}
    result = {}
    for k, v in relayout.items():
        if not isinstance(k, str):
            continue
        if '.range[0]' in k or '.range[1]' in k:
            base, idx_part = k.rsplit('.range[', 1)
            try:
                idx = int(idx_part.rstrip(']'))
            except ValueError:
                continue
            if idx not in (0, 1):
                continue
            entry = result.setdefault(base, {})
            if 'range' not in entry:
                entry['range'] = [None, None]
            entry['range'][idx] = v
        elif '.autorange' in k:
            base = k.rsplit('.autorange', 1)[0]
            result.setdefault(base, {})['autorange'] = v
        elif '.' in k:
            base, sub = k.split('.', 1)
            result.setdefault(base, {})[sub] = v
        else:
            result[k] = v
    return result