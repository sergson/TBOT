# core/database.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import sqlite3
import json
import os
import threading
from typing import Optional, List, Dict, Any, Union, Tuple, Callable
from .logger import perf_logger
from .registry import bot_registry

logger = perf_logger.get_logger('database', 'database')

DB_CONFIG = 'config.db'
DATA_DIR = 'data'

# ----------------------------------------------------------------------
# Field classes
# ----------------------------------------------------------------------
class Field:
    def __init__(self, required=False, default=None, primary_key=False, unique=False, index=False, string=None):
        self.required = required
        self.default = default
        self.primary_key = primary_key
        self.unique = unique
        self.index = index
        self.string = string

    def sql_type(self) -> str:
        raise NotImplementedError

    def to_db(self, value):
        return value

    def from_db(self, value):
        return value


class Char(Field):
    def sql_type(self):
        return "TEXT"

class Text(Field):
    def sql_type(self):
        return "TEXT"

class Integer(Field):
    def __init__(self, autoincrement=False, **kwargs):
        super().__init__(**kwargs)
        self.autoincrement = autoincrement

    def sql_type(self):
        if self.primary_key and self.autoincrement:
            return "INTEGER PRIMARY KEY AUTOINCREMENT"
        return "INTEGER"

class Boolean(Field):
    def sql_type(self):
        return "INTEGER"
    def to_db(self, value):
        return 1 if value else 0
    def from_db(self, value):
        return bool(value)

class Float(Field):
    def sql_type(self):
        return "REAL"

class Selection(Field):
    def __init__(self, selection, **kwargs):
        super().__init__(**kwargs)
        self.selection = selection
    def sql_type(self):
        return "TEXT"

class Json(Field):
    def sql_type(self):
        return "TEXT"
    def to_db(self, value):
        if value is None:
            return None
        if isinstance(value, str):
            return value  # already JSON
        return json.dumps(value)
    def from_db(self, value):
        if value is None:
            return None
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value


# ----------------------------------------------------------------------
# Model registry and metaclass
# ----------------------------------------------------------------------
MODEL_REGISTRY: Dict[str, type] = {}

class ModelMeta(type):
    def __new__(mcls, name, bases, attrs):
        cls = super().__new__(mcls, name, bases, attrs)
        _name = attrs.get('_name')
        _inherit = attrs.get('_inherit')

        # Collect field definitions from class attributes (and bases)
        fields = {}
        for base in reversed(bases):
            base_fields = getattr(base, '_fields', {})
            fields.update(base_fields)
        for key, value in attrs.items():
            if isinstance(value, Field):
                fields[key] = value

        if _name or _inherit:
            if _inherit and not _name:
                base_model = MODEL_REGISTRY.get(_inherit)
                if not base_model:
                    raise ValueError(f"Cannot inherit from unknown model '{_inherit}'")
                base_model._fields.update(fields)
                logger.debug(f"Model extension applied to '{_inherit}' with fields: {list(fields.keys())}")
                return base_model
            else:
                if _name in MODEL_REGISTRY:
                    existing = MODEL_REGISTRY[_name]
                    existing._fields.update(fields)
                    logger.debug(f"Model '{_name}' extended with fields: {list(fields.keys())}")
                    return existing

                cls._name = _name
                cls._fields = fields

                # NEW: do not auto-generate _table if the model is dynamic
                if not getattr(cls, '_dynamic', False):
                    if not getattr(cls, '_table', None):
                        cls._table = _name.replace('.', '_')
                # else: keep _table as is (possibly None)

                MODEL_REGISTRY[_name] = cls
                logger.debug(f"Registered model '{_name}' -> table '{cls._table}' with fields: {list(fields.keys())}")
        return cls


class Model(metaclass=ModelMeta):
    _name = None
    _table = None
    _fields = {}


# ----------------------------------------------------------------------
# Domain parser
# ----------------------------------------------------------------------
def _parse_condition(token):
    if not (isinstance(token, (tuple, list)) and len(token) == 3):
        raise ValueError(f"Invalid condition: {token}")
    field, op, value = token
    if op == 'in':
        if not isinstance(value, (list, tuple)):
            value = [value]
        placeholders = ','.join(['?'] * len(value))
        return f"{field} IN ({placeholders})", list(value)
    elif op == 'not in':
        if not isinstance(value, (list, tuple)):
            value = [value]
        placeholders = ','.join(['?'] * len(value))
        return f"{field} NOT IN ({placeholders})", list(value)
    elif op == 'like':
        return f"{field} LIKE ?", [f"%{value}%"]
    elif op == 'ilike':
        return f"LOWER({field}) LIKE LOWER(?)", [f"%{value}%"]
    elif op == 'between':
        return f"{field} BETWEEN ? AND ?", [value[0], value[1]]
    elif op in ('=', '!=', '>', '<', '>=', '<='):
        return f"{field} {op} ?", [value]
    else:
        raise ValueError(f"Unsupported operator '{op}'")


def _domain_to_sql(domain: list) -> Tuple[str, list]:
    if not domain:
        return "1=1", []

    def parse_expression(index):
        if index >= len(domain):
            raise ValueError("Unexpected end of domain")
        token = domain[index]
        if isinstance(token, str) and token in ('&', '|', '!'):
            if token == '!':
                cond, params, next_idx = parse_expression(index + 1)
                return f"NOT ({cond})", params, next_idx
            else:
                left_cond, left_params, idx1 = parse_expression(index + 1)
                right_cond, right_params, idx2 = parse_expression(idx1)
                op = 'AND' if token == '&' else 'OR'
                return f"({left_cond} {op} {right_cond})", left_params + right_params, idx2
        elif isinstance(token, (tuple, list)) and len(token) == 3:
            return _parse_condition(token)[0], _parse_condition(token)[1], index + 1
        else:
            raise ValueError(f"Invalid domain element: {token}")

    # Check for logical operators
    has_logical = any(isinstance(item, str) and item in ('&', '|', '!') for item in domain)
    if has_logical:
        cond, params, idx = parse_expression(0)
        if idx != len(domain):
            raise ValueError("Invalid domain: extra elements after expression")
        return cond, params
    else:
        # Combine all tuples with AND
        conditions = []
        params = []
        for token in domain:
            cond, p = _parse_condition(token)
            conditions.append(cond)
            params.extend(p)
        if not conditions:
            return "1=1", []
        return " AND ".join(conditions), params


# ----------------------------------------------------------------------
# Record and Recordset
# ----------------------------------------------------------------------
class Record:
    def __init__(self, manager, data: dict):
        self._manager = manager
        self._data = data
    def __getattr__(self, name):
        if name in self._data:
            return self._data[name]
        raise AttributeError(name)
    def __getitem__(self, key):
        return self._data[key]
    def __setitem__(self, key, value):
        self._data[key] = value
    def write(self, vals):
        self._manager.write_records([self], vals)
    def unlink(self):
        self._manager.unlink_records([self])
    def read(self, fields=None):
        return self._manager.read_records([self], fields)[0]


class Recordset:
    def __init__(self, manager, records: List[Record]):
        self._manager = manager
        self._records = records
    def __iter__(self):
        return iter(self._records)
    def __len__(self):
        return len(self._records)
    def __bool__(self):
        return bool(self._records)
    def __getitem__(self, idx):
        if isinstance(idx, slice):
            return Recordset(self._manager, self._records[idx])
        return self._records[idx]
    def read(self, fields=None):
        return self._manager.read_records(self._records, fields)
    def write(self, vals):
        self._manager.write_records(self._records, vals)
    def unlink(self):
        self._manager.unlink_records(self._records)
    def filtered(self, func):
        new_records = [r for r in self._records if func(r)]
        return Recordset(self._manager, new_records)
    def mapped(self, field):
        return [r[field] for r in self._records]
    def sorted(self, key, reverse=False):
        if isinstance(key, str):
            key_func = lambda r: r[key]
        else:
            key_func = key
        new_records = sorted(self._records, key=key_func, reverse=reverse)
        return Recordset(self._manager, new_records)


# ----------------------------------------------------------------------
# ModelManager
# ----------------------------------------------------------------------
class ModelManager:
    def __init__(self, env: 'DBSQLite3', model_name: str, model_class, table_name=None):
        self.env = env
        self.model_name = model_name
        self.model_class = model_class
        self.table_name = table_name or model_class._table
        if self.table_name is None:
            raise ValueError(f"Model '{model_name}' requires table_name")
        self.fields = model_class._fields

        # Determine primary key
        self.primary_key_field = None
        for fname, fld in self.fields.items():
            if fld.primary_key:
                self.primary_key_field = fname
                break
        if self.primary_key_field is None:
            raise ValueError(f"Model '{model_name}' has no primary key defined")

        self._init_table()

    def _init_table(self):
        conn = self.env.conn
        lock = self.env.lock
        with lock:
            # Check table existence
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (self.table_name,))
            exists = cur.fetchone() is not None
            if not exists:
                # Create table
                columns = []
                for field_name, field in self.fields.items():
                    col_type = field.sql_type()
                    col_def = f"{field_name} {col_type}"
                    if field.primary_key and not field.autoincrement:
                        col_def += " PRIMARY KEY"
                    elif field.primary_key and field.autoincrement:
                        # sql_type already includes PRIMARY KEY AUTOINCREMENT
                        pass
                    if field.required and field.default is None and not field.primary_key:
                        col_def += " NOT NULL"
                    if field.unique:
                        col_def += " UNIQUE"
                    if field.default is not None:
                        default_val = field.to_db(field.default)
                        col_def += f" DEFAULT {default_val}"
                    columns.append(col_def)
                sql = f"CREATE TABLE IF NOT EXISTS {self.table_name} ({', '.join(columns)})"
                conn.execute(sql)
                logger.info(f"Created table {self.table_name}")
            else:
                # Check existing columns
                cur = conn.execute(f"PRAGMA table_info({self.table_name})")
                existing_cols = {row[1] for row in cur.fetchall()}
                for field_name, field in self.fields.items():
                    if field_name not in existing_cols:
                        col_type = field.sql_type()
                        if field.required and field.default is None:
                            logger.warning(f"Adding required field {field_name} without default to {self.table_name}. Using NULL.")
                            col_def = f"{field_name} {col_type}"
                        else:
                            default_val = field.to_db(field.default) if field.default is not None else None
                            col_def = f"{field_name} {col_type}"
                            if default_val is not None:
                                col_def += f" DEFAULT {default_val}"
                        conn.execute(f"ALTER TABLE {self.table_name} ADD COLUMN {col_def}")
                        logger.info(f"Added column {field_name} to {self.table_name}")
            # Create indices
            for field_name, field in self.fields.items():
                if field.index:
                    idx_name = f"idx_{self.table_name}_{field_name}"
                    conn.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {self.table_name} ({field_name})")
            conn.commit()

    def _prepare_values(self, vals: dict) -> dict:
        prepared = {}
        for field_name, value in vals.items():
            field = self.fields.get(field_name)
            if field:
                prepared[field_name] = field.to_db(value)
            else:
                # field not defined, try to use raw value
                logger.warning(f"Field {field_name} not in model {self.model_name}, storing raw.")
                prepared[field_name] = value
        return prepared

    def search(self, domain: list = None, order: str = None, limit: int = None, offset: int = None) -> Recordset:
        cond, params = _domain_to_sql(domain or [])
        sql = f"SELECT * FROM {self.table_name} WHERE {cond}"
        if order:
            sql += f" ORDER BY {order}"
        if limit is not None:
            sql += f" LIMIT {limit}"
        if offset is not None:
            sql += f" OFFSET {offset}"
        with self.env.lock:
            cur = self.env.conn.execute(sql, params)
            rows = cur.fetchall()
            # Convert to dict with field names
            col_names = [desc[0] for desc in cur.description]
            records = []
            for row in rows:
                data = dict(zip(col_names, row))
                # apply from_db for known fields
                for field_name, value in data.items():
                    field = self.fields.get(field_name)
                    if field:
                        data[field_name] = field.from_db(value)
                records.append(Record(self, data))
            return Recordset(self, records)

    def search_count(self, domain: list = None) -> int:
        cond, params = _domain_to_sql(domain or [])
        with self.env.lock:
            cur = self.env.conn.execute(f"SELECT COUNT(*) FROM {self.table_name} WHERE {cond}", params)
            return cur.fetchone()[0]

    def browse(self, ids: Union[int, List[int]]) -> Recordset:
        if not isinstance(ids, (list, tuple)):
            ids = [ids]
        if not ids:
            return Recordset(self, [])
        # Filter out None (e.g., if lastrowid is not supported)
        ids = [i for i in ids if i is not None]
        if not ids:
            return Recordset(self, [])
        domain = [(self.primary_key_field, 'in', ids)]
        return self.search(domain)

    def create(self, vals_list: List[dict]) -> Recordset:
        if not vals_list:
            return Recordset(self, [])
        complete_vals_list = []
        for vals in vals_list:
            for field_name, field in self.fields.items():
                if field_name not in vals and field.default is not None:
                    vals[field_name] = field.default
            complete_vals_list.append(vals)
        created_ids = []
        with self.env.transaction():
            for vals in complete_vals_list:
                prepared = self._prepare_values(vals)
                cols = ','.join(prepared.keys())
                placeholders = ','.join(['?'] * len(prepared))
                sql = f"INSERT INTO {self.table_name} ({cols}) VALUES ({placeholders})"
                with self.env.lock:
                    cur = self.env.conn.execute(sql, list(prepared.values()))
                    # Check if primary key is autoincrement
                    pk_field = self.fields[self.primary_key_field]
                    if getattr(pk_field, 'autoincrement', False):
                        created_ids.append(cur.lastrowid)
                    else:
                        # Use primary key value from inserted data
                        pk_val = prepared.get(self.primary_key_field)
                        if pk_val is None:
                            raise ValueError(
                                f"Primary key '{self.primary_key_field}' not provided for model '{self.model_name}'")
                        created_ids.append(pk_val)
        return self.browse(created_ids)

    def write_records(self, records: List[Record], vals: dict):
        if not records or not vals:
            return
        prepared = self._prepare_values(vals)
        set_clause = ', '.join([f"{col}=?" for col in prepared.keys()])
        ids = [r[self.primary_key_field] for r in records]
        placeholders = ','.join(['?'] * len(ids))
        sql = f"UPDATE {self.table_name} SET {set_clause} WHERE {self.primary_key_field} IN ({placeholders})"
        with self.env.transaction():
            with self.env.lock:
                self.env.conn.execute(sql, list(prepared.values()) + ids)
        for r in records:
            r._data.update(vals)

    def unlink_records(self, records: List[Record]):
        if not records:
            return
        ids = [r[self.primary_key_field] for r in records]
        placeholders = ','.join(['?'] * len(ids))
        sql = f"DELETE FROM {self.table_name} WHERE {self.primary_key_field} IN ({placeholders})"
        with self.env.transaction():
            with self.env.lock:
                self.env.conn.execute(sql, ids)

    def read_records(self, records: List[Record], fields: List[str] = None) -> List[dict]:
        if fields is None:
            fields = list(self.fields.keys())
        result = []
        for r in records:
            data = {f: r[f] for f in fields if f in r._data}
            result.append(data)
        return result

    def execute(self, sql: str, params: list = None) -> List[dict]:
        """Execute raw SQL (SELECT) and return list of dicts."""
        params = params or []
        with self.env.lock:
            cur = self.env.conn.execute(sql, params)
            rows = cur.fetchall()
            col_names = [desc[0] for desc in cur.description]
            return [dict(zip(col_names, row)) for row in rows]


# ----------------------------------------------------------------------
# DBSQLite3 environment
# ----------------------------------------------------------------------
class DBSQLite3:
    def __init__(self, db_path: str, registry: Dict[str, type] = None):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.registry = registry or MODEL_REGISTRY
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.lock = threading.RLock()
        self._managers: Dict[str, ModelManager] = {}
        self._dynamic_managers: Dict[Tuple[str, str], ModelManager] = {}

    def __getitem__(self, model_name: str) -> ModelManager:
        if model_name not in self._managers:
            model_cls = self.registry.get(model_name)
            if not model_cls:
                raise KeyError(f"Model '{model_name}' not found in registry")
            if model_cls._table is None:
                raise KeyError(
                    f"Model '{model_name}' is dynamic; use get_model_manager() with table name"
                )
            manager = ModelManager(self, model_name, model_cls)
            manager._init_table()
            self._managers[model_name] = manager
        return self._managers[model_name]

    def get_model_manager(self, model_name: str, table_name: str) -> ModelManager:
        if table_name is None:
            return self[model_name]
        key = (model_name, table_name)
        if key not in self._dynamic_managers:
            model_cls = self.registry.get(model_name)
            if not model_cls:
                raise KeyError(f"Model '{model_name}' not in registry")
            manager = ModelManager(self, model_name, model_cls, table_name=table_name)
            manager._init_table()
            self._dynamic_managers[key] = manager
        return self._dynamic_managers[key]

    def init_schema(self):
        # Kept for compatibility, but does nothing because managers are created on demand.
        pass

    def transaction(self):
        return _TransactionContext(self)

    def close(self):
        self.conn.close()


    def drop_table(self, table_name: str):
        with self.lock:
            self.conn.execute(f"DROP TABLE IF EXISTS {table_name}")
            self.conn.commit()

    def list_tables(self) -> List[str]:
        with self.lock:
            cur = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            return [row[0] for row in cur.fetchall()]



class _TransactionContext:
    def __init__(self, env: DBSQLite3):
        self.env = env

    def __enter__(self):
        self.env.lock.acquire()          # acquire lock
        self.env.conn.execute("BEGIN")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None:
                self.env.conn.commit()
            else:
                self.env.conn.rollback()
        finally:
            self.env.lock.release()      # release lock
        return False


# ----------------------------------------------------------------------
# Original core database functions (preserved)
# ----------------------------------------------------------------------
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
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        # Update only the given keys, without deleting others
        for key, value in config.items():
            conn.execute(
                'INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                (key, json.dumps(value))
            )
        conn.commit()

def update_bot_config(bot_id: int, config: Dict[str, Any]):
    _save_config_to_local_db(bot_id, config)

def get_bot_config(bot_id: int, include_status: bool = False) -> Optional[Dict[str, Any]]:
    db_path = os.path.join(DATA_DIR, f'bot_{bot_id}.db')
    if not os.path.exists(db_path):
        return None
    config = {}
    with sqlite3.connect(db_path, timeout=30) as conn:
        # Check if bot_settings table exists
        table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bot_settings'"
        ).fetchone() is not None
        if table_exists:
            cur = conn.execute('SELECT key, value FROM bot_settings')
            for key, value in cur.fetchall():
                try:
                    config[key] = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    config[key] = value
    # bot_type and status from main DB
    with sqlite3.connect(DB_CONFIG, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT type, status FROM bots WHERE id = ?', (bot_id,)).fetchone()
        if row:
            config['bot_type'] = row['type']
            if include_status:
                config['status'] = row['status']
    return config

def get_all_bots() -> List[Dict[str, Any]]:
    with sqlite3.connect(DB_CONFIG) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT * FROM bots ORDER BY position, id').fetchall()
    bots = []
    for row in rows:
        bot = dict(row)
        bot_config = get_bot_config(bot['id'])
        if bot_config:
            bot['config'] = bot_config
            bot['config']['status'] = bot['status']
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

# ----------------------------------------------------------------------
# Base model(s) for bot settings (shared across bots)
# ----------------------------------------------------------------------
class BotSettings(Model):
    _name = 'bot.settings'
    _table = 'bot_settings'

    key = Char(primary_key=True, required=True)
    value = Text()