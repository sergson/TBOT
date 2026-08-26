# core/graphics.py
"""
Atomic Plotly graphic element factories.
Uses colors from core.colors and styles from core.styles.
Does not contain bot-specific logic.
"""

import plotly.graph_objects as go
from . import colors
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
                 title,
                 uirevision='grid',
                 show_rangeslider=False,
                 hovermode='x unified',
                 xaxis_range=None,
                 yaxis_range=None,
                 legend_bordercolor=colors.BLACK,
                 title_font_size=FONT_SIZE_BOTHEADER):   # fixed: using bot header font size
    """
    Applies a standard layout to the figure.
    legend_bordercolor=None removes the legend border.
    """

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
        title=title,
        title_font_size=title_font_size,
        xaxis_title="Time",
        yaxis_title="Price",
        legend=legend,
        uirevision=uirevision,
        xaxis_rangeslider_visible=show_rangeslider,
    )
    if hovermode:
        layout_updates['hovermode'] = hovermode
    if xaxis_range is not None:
        layout_updates['xaxis_range'] = xaxis_range
    if yaxis_range is not None:
        layout_updates['yaxis_range'] = yaxis_range

    fig.update_layout(**layout_updates)