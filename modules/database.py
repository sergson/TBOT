# modules/database.py (updated version)
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import sqlite3
import json
from typing import Optional, List, Dict, Any
from .logger import perf_logger
from .bot_registry import bot_registry

logger = perf_logger.get_logger('database', 'database')

DB_CONFIG = 'config.db'

def init_config_db():
    """Creates common tables and configuration tables for all registered bot types."""
    with sqlite3.connect(DB_CONFIG) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        # Common bots table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                name TEXT,
                status TEXT DEFAULT 'stopped',
                position INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                config_data TEXT  -- JSON with arbitrary configuration fields
            )
        ''')
        # Settings table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        conn.commit()

    # For each registered type, create its own configuration table if needed
    for type_id, meta in bot_registry._types.items():
        schema = meta.get('config_schema')
        if schema:
            columns = ', '.join([f"{col} {typ}" for col, typ in schema.items()])
            with sqlite3.connect(DB_CONFIG) as conn:
                conn.execute(f'''
                    CREATE TABLE IF NOT EXISTS config_{type_id} (
                        bot_id INTEGER PRIMARY KEY,
                        {columns},
                        FOREIGN KEY(bot_id) REFERENCES bots(id) ON DELETE CASCADE
                    )
                ''')
                conn.commit()

def add_bot(bot_type: str, name: str = None, config: Dict[str, Any] = None) -> int:
    """Adds a new bot. config – dictionary with configuration fields for the given type."""
    with sqlite3.connect(DB_CONFIG) as conn:
        config_json = json.dumps(config) if config else None
        cursor = conn.execute(
            'INSERT INTO bots (type, name, config_data) VALUES (?, ?, ?)',
            (bot_type, name, config_json)
        )
        bot_id = cursor.lastrowid

        # If the type has a separate configuration table, write to it
        meta = bot_registry.get_type(bot_type)
        if meta and meta.get('config_schema') and config:
            columns = ', '.join(config.keys())
            placeholders = ', '.join(['?' for _ in config])
            values = list(config.values())
            conn.execute(f'''
                INSERT INTO config_{bot_type} (bot_id, {columns})
                VALUES (?, {placeholders})
            ''', [bot_id] + values)
        conn.commit()
        return bot_id

def update_bot_status(bot_id: int, status: str):
    with sqlite3.connect(DB_CONFIG) as conn:
        conn.execute('UPDATE bots SET status = ? WHERE id = ?', (status, bot_id))
        conn.commit()

def delete_bot(bot_id: int):
    with sqlite3.connect(DB_CONFIG) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        # Delete from common table (cascade will delete records in config_*)
        conn.execute('DELETE FROM bots WHERE id = ?', (bot_id,))
        conn.commit()

def get_all_bots() -> List[Dict[str, Any]]:
    with sqlite3.connect(DB_CONFIG) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT * FROM bots ORDER BY position, id').fetchall()
        bots = []
        for row in rows:
            bot = dict(row)
            # Load config from JSON
            if bot.get('config_data'):
                bot['config'] = json.loads(bot['config_data'])
            else:
                bot['config'] = {}
            bots.append(bot)
        return bots

def get_bot_config(bot_id: int) -> Optional[Dict]:
    """Returns the full bot configuration (including fields from the specific table)."""
    with sqlite3.connect(DB_CONFIG) as conn:
        conn.row_factory = sqlite3.Row
        bot_row = conn.execute('SELECT type, config_data FROM bots WHERE id = ?', (bot_id,)).fetchone()
        if not bot_row:
            return None
        bot_type = bot_row['type']
        config = json.loads(bot_row['config_data']) if bot_row['config_data'] else {}

        # If there is a separate configuration table, read from it
        meta = bot_registry.get_type(bot_type)
        if meta and meta.get('config_schema'):
            row = conn.execute(f'SELECT * FROM config_{bot_type} WHERE bot_id = ?', (bot_id,)).fetchone()
            if row:
                config.update(dict(row))
        config['bot_type'] = bot_type
        return config

def save_setting(key, value):
    with sqlite3.connect(DB_CONFIG) as conn:
        conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
        conn.commit()

def get_setting(key, default=None):
    with sqlite3.connect(DB_CONFIG) as conn:
        cur = conn.execute('SELECT value FROM settings WHERE key = ?', (key,))
        row = cur.fetchone()
        return row[0] if row else default