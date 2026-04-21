# core/registry.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

from typing import Dict, Type, Optional, List
from .logger import perf_logger

logger = perf_logger.get_logger('registry', 'app')

class BotRegistry:
    """Central registry with inheritance support and fixed model name."""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._models = {}
            cls._instance._pending_inherits = {}
        return cls._instance

    def register(self, cls: Type) -> Type:
        name = getattr(cls, '_name', None)
        inherit = getattr(cls, '_inherit', None)
        logger.debug(f"Register: cls={cls.__name__}, name={name}, inherit={inherit}")

        if not name and not inherit:
            logger.debug(f"Register: {cls.__name__} does not participate in inheritance system")
            return cls

        if name:
            if name in self._models:
                existing = self._models[name]
                new_cls = type(name, (cls, existing), {})
                self._models[name] = new_cls
                logger.debug(f"Register: Extended existing model '{name}'")
                return new_cls
            else:
                self._models[name] = cls
                logger.debug(f"Register: Registered new model '{name}'")
                if name in self._pending_inherits:
                    for ext_cls in self._pending_inherits[name]:
                        self._apply_inherit(name, ext_cls)
                    del self._pending_inherits[name]
                return cls

        elif inherit:
            if inherit in self._models:
                self._apply_inherit(inherit, cls)
                logger.debug(f"Register: Applied extension to '{inherit}' from {cls.__name__}")
                return self._models[inherit]
            else:
                self._pending_inherits.setdefault(inherit, []).append(cls)
                logger.debug(f"Register: Deferred extension for '{inherit}' from {cls.__name__}")
                return cls

    def _apply_inherit(self, base_name: str, extension_cls: Type):
        base_cls = self._models[base_name]
        new_cls = type(base_name, (extension_cls, base_cls), {})
        self._models[base_name] = new_cls

    def get_model(self, name: str) -> Optional[Type]:
        return self._models.get(name)

    def list_models(self):
        return list(self._models.keys())


bot_registry = BotRegistry()

def auto_reg(cls):
    logger.debug(f"Auto_reg decorating {cls.__name__}")
    return bot_registry.register(cls)