# modules/grid/__manifest__.py
{
    "name": "Grid Bot",
    "version": "1.0.0",
    "description": "Grid trading bot with averaging, smart averaging, liquidation, and take-profit.",
    "author": "sergson",
    "license": "GPL-3.0",
    "depends": ["collector"],
    "keywords": ["grid", "averaging", "trading"],
    "bot_model": "grid.bot",
    "type_model": "grid.type"
}