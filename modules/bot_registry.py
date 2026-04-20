# modules/bot_registry.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

from typing import Dict, Any, Callable, Type, Optional
from dash import html

class BotTypeRegistry:
    """Registry of bot types."""
    _types: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def register(cls,
                 type_id: str,
                 display_name: str,
                 form_component: Callable[[], html.Div],
                 config_schema: Dict[str, str],  # field name -> SQL type
                 bot_class: Type,
                 render_block: Optional[Callable] = None):
        """
        Registers a new bot type.
        :param type_id: unique identifier (e.g., 'collector')
        :param display_name: display name
        :param form_component: function returning a Dash component with the add form
        :param config_schema: dictionary {field: sql_type} for creating the configuration table
        :param bot_class: bot class (inherits from BaseBot)
        :param render_block: function to render the bot block in UI (bot_id, config) -> html.Div
        """
        cls._types[type_id] = {
            'display_name': display_name,
            'form_component': form_component,
            'config_schema': config_schema,
            'bot_class': bot_class,
            'render_block': render_block or cls._default_render_block
        }

    @classmethod
    def get_type(cls, type_id: str) -> Optional[Dict[str, Any]]:
        return cls._types.get(type_id)

    @classmethod
    def list_types(cls):
        return list(cls._types.keys())

    @classmethod
    def get_display_name(cls, type_id: str) -> str:
        t = cls._types.get(type_id)
        return t['display_name'] if t else type_id

    @staticmethod
    def _default_render_block(bot_id, config):
        """Fallback if the module does not provide its own renderer."""
        return html.Div(f"Bot {bot_id} ({config})")

# Global registry instance
bot_registry = BotTypeRegistry()