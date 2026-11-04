# modules/broker/__init__.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.
# Import both to trigger @auto_reg registration of "broker.bot" and "broker.type".
from . import models       # noqa: F401
from . import components   # noqa: F401