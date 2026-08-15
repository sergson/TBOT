# core/database.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import sqlite3
import json
import os
from typing import Optional, List, Dict, Any
from .logger import perf_logger
from .registry import bot_registry

logger = perf_logger.get_logger('database', 'database')

DB_CONFIG = 'config.db'
DATA_DIR = 'data'

def init_config_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    with sqlite3.connect(DB_CONFIG) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute('''
            CREATE TABLE IF NOT EXISTS bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                name TEXT,
                status TEXT DEFAULT 'stopped',
                position INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        conn.commit()
    # Type-specific tables are no longer created

def add_bot(bot_type: str, name: str = None, config: Dict[str, Any] = None) -> int:
    with sqlite3.connect(DB_CONFIG) as conn:
        cursor = conn.execute(
            'INSERT INTO bots (type, name) VALUES (?, ?)',
            (bot_type, name)
        )
        bot_id = cursor.lastrowid
        conn.commit()
    if config is None:
        config = {}
    config['data_db_path'] = os.path.join(DATA_DIR, f'bot_{bot_id}.db')
    _save_config_to_local_db(bot_id, config)
    return bot_id

def _save_config_to_local_db(bot_id: int, config: Dict[str, Any]):
    db_path = os.path.join(DATA_DIR, f'bot_{bot_id}.db')
    os.makedirs(DATA_DIR, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        # Delete all previous settings
        conn.execute('DELETE FROM bot_settings')
        if config:
            for key, value in config.items():
                conn.execute(
                    'INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                    (key, json.dumps(value))
                )
        conn.commit()

def update_bot_config(bot_id: int, config: Dict[str, Any]):
    """Completely overwrites the bot's config in its local DB."""
    _save_config_to_local_db(bot_id, config)

def get_bot_config(bot_id: int, include_status: bool = False) -> Optional[Dict[str, Any]]:
    """Reads the bot's config from its local DB, adding bot_type and optionally status."""
    db_path = os.path.join(DATA_DIR, f'bot_{bot_id}.db')
    if not os.path.exists(db_path):
        return None
    config = {}
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute('SELECT key, value FROM bot_settings')
        for key, value in cur.fetchall():
            try:
                config[key] = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                config[key] = value   # fallback for strings without quotes
    # Pull bot_type and status from the common DB
    with sqlite3.connect(DB_CONFIG) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT type, status FROM bots WHERE id = ?', (bot_id,)).fetchone()
        if row:
            config['bot_type'] = row['type']
            if include_status:
                config['status'] = row['status']
    return config

def get_all_bots() -> List[Dict[str, Any]]:
    """Returns a list of bots with config pulled from local DBs."""
    with sqlite3.connect(DB_CONFIG) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT * FROM bots ORDER BY position, id').fetchall()
    bots = []
    for row in rows:
        bot = dict(row)
        bot_config = get_bot_config(bot['id'])
        if bot_config:
            bot['config'] = bot_config
            bot['config']['status'] = bot['status']   # for compatibility in UI
        else:
            bot['config'] = {}
        bots.append(bot)
    return bots

def update_bot_status(bot_id: int, status: str):
    with sqlite3.connect(DB_CONFIG) as conn:
        conn.execute('UPDATE bots SET status = ? WHERE id = ?', (status, bot_id))
        conn.commit()

def delete_bot(bot_id: int):
    with sqlite3.connect(DB_CONFIG) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute('DELETE FROM bots WHERE id = ?', (bot_id,))
        conn.commit()
    # The database file is deleted by BotManager in remove_bot

def get_setting(key: str, default=None) -> Optional[str]:
    with sqlite3.connect(DB_CONFIG) as conn:
        cur = conn.execute('SELECT value FROM settings WHERE key = ?', (key,))
        row = cur.fetchone()
        return row[0] if row else default

def save_setting(key: str, value: str):
    with sqlite3.connect(DB_CONFIG) as conn:
        conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
        conn.commit()

def cleanup_orphan_databases():
    import glob, re
    db_files = glob.glob('data/bot_*.db')
    active_ids = set()
    with sqlite3.connect(DB_CONFIG) as conn:
        rows = conn.execute('SELECT id FROM bots').fetchall()
        active_ids = {row[0] for row in rows}
    for f in db_files:
        m = re.search(r'bot_(\d+)\.db', f)
        if m and int(m.group(1)) not in active_ids:
            try:
                os.remove(f)
                logger.info(f"Removed orphan database {f}")
            except Exception as e:
                logger.error(f"Failed to remove {f}: {e}")