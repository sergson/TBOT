# core/database.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import sqlite3
import json
import os
import time
import threading
from collections import defaultdict
from typing import Optional, List, Dict, Any, Tuple, Type, cast
from .logger import perf_logger

logger = perf_logger.get_logger('database', 'database')

DB_CONFIG = 'config.db'
DATA_DIR = 'data'

# ----------------------------------------------------------------------
# TimeSync persistent state (separate DB from config.db)
# ----------------------------------------------------------------------
TIMESYNC_DB = 'timesync_config.db'
TIMESYNC_DB_LOCK = threading.RLock()
_TIMESYNC_DB_READY = False
DRIFT_HISTORY_MAX = 100

# ----------------------------------------------------------------------
# Field classes
# ----------------------------------------------------------------------
class Field:
    autoincrement = False

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
MODEL_REGISTRY: Dict[str, Type["Model"]] = {}

class ModelMeta(type):
    def __new__(mcls, name, bases, attrs):
        cls = super().__new__(mcls, name, bases, attrs)
        _name = attrs.get('_name')
        _inherit = attrs.get('_inherit')

        # Collect field definitions from class attributes (and bases)
        fields: Dict[str, Field] = {}
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
                base_model.get_fields().update(fields)
                logger.debug(f"Model extension applied to '{_inherit}' with fields: {list(fields.keys())}")
                return base_model
            else:
                if _name in MODEL_REGISTRY:
                    existing = MODEL_REGISTRY[_name]
                    existing.get_fields().update(fields)
                    logger.debug(f"Model '{_name}' extended with fields: {list(fields.keys())}")
                    return existing

                model_cls = cast("Type[Model]", cls)
                model_cls._name = _name
                model_cls._fields = fields

                # NEW: do not auto-generate _table if the model is dynamic
                if not getattr(cls, '_dynamic', False):
                    if not getattr(cls, '_table', None):
                        model_cls._table = _name.replace('.', '_')
                # else: keep _table as is (possibly None)

                MODEL_REGISTRY[_name] = model_cls
                logger.debug(f"Registered model '{_name}' -> table '{cls._table}' with fields: {list(fields.keys())}")
        return cls

class Model(metaclass=ModelMeta):
    _name: Optional[str] = None
    _table: Optional[str] = None
    _fields: Dict[str, Field] = {}

    @classmethod
    def get_fields(cls) -> Dict[str, "Field"]:
        """Return a reference to the dict of fields (can be mutated)."""
        return cls._fields

    @classmethod
    def get_table(cls) -> Optional[str]:
        return cls._table
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
        _token = domain[index]
        if isinstance(_token, str) and _token in ('&', '|', '!'):
            if _token == '!':
                condition, parameters, next_idx = parse_expression(index + 1)
                return f"NOT ({condition})", parameters, next_idx
            else:
                left_cond, left_params, idx1 = parse_expression(index + 1)
                right_cond, right_params, idx2 = parse_expression(idx1)
                op = 'AND' if _token == '&' else 'OR'
                return f"({left_cond} {op} {right_cond})", left_params + right_params, idx2
        elif isinstance(_token, (tuple, list)) and len(_token) == 3:
            return _parse_condition(_token)[0], _parse_condition(_token)[1], index + 1
        else:
            raise ValueError(f"Invalid domain element: {_token}")

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
    def __contains__(self, key: str) -> bool:
        return key in self._data
    def write(self, vals):
        self._manager.write_records([self], vals)
    def unlink(self):
        self._manager.unlink_records([self])
    def read(self, fields=None):
        return self._manager.read_records([self], fields)[0]
    def refresh(self):
        new_data = self._manager.read_records([self])[0]
        self._data.update(new_data)

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
        self.table_name = table_name or model_class.get_table()
        if self.table_name is None:
            raise ValueError(f"Model '{model_name}' requires table_name")
        self.fields = model_class.get_fields()

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

    def browse(self, ids):
        if not isinstance(ids, (list, tuple)):
            ids = [ids]
        if not ids:
            return Recordset(self, [])
        ids = [i for i in ids if i is not None]
        if not ids:
            return Recordset(self, [])
        chunk_size = 500
        all_records: List[Record] = []
        for i in range(0, len(ids), chunk_size):
            chunk = ids[i:i + chunk_size]
            rs = self.search([(self.primary_key_field, 'in', chunk)])
            all_records.extend(rs._records)
        by_id = {r[self.primary_key_field]: r for r in all_records}
        ordered = [by_id[i] for i in ids if i in by_id]
        return Recordset(self, ordered)

    def create(self, vals_list: List[dict]) -> Recordset:
        if not vals_list:
            return Recordset(self, [])

        complete_vals_list = []
        for vals in vals_list:
            v = dict(vals)
            for field_name, field in self.fields.items():
                if field_name not in v and field.default is not None:
                    v[field_name] = field.default
            complete_vals_list.append(v)

        groups: Dict[frozenset, List[dict]] = defaultdict(list)
        for vals in complete_vals_list:
            prepared = self._prepare_values(vals)
            groups[frozenset(prepared.keys())].append(prepared)

        pk_field = self.fields[self.primary_key_field]
        is_autoincrement = getattr(pk_field, 'autoincrement', False)
        created_ids: List = []

        with self.env.transaction():
            for keyset, items in groups.items():
                created_ids.extend(
                    self._insert_group(keyset, items, is_autoincrement))

        return self.browse(created_ids)

    def _insert_group(self, keyset, items, is_autoincrement):
        if not items:
            return []
        if not keyset:
            return self._insert_per_row_default(items, is_autoincrement)
        ordered = sorted(keyset)
        cols = ','.join(ordered)
        ph = ','.join(['?'] * len(ordered))
        sql = f"INSERT INTO {self.table_name} ({cols}) VALUES ({ph})"
        values = [tuple(p[c] for c in ordered) for p in items]
        try:
            with self.env.transaction():
                cur = self.env.conn.executemany(sql, values)
                if is_autoincrement:
                    last = cur.lastrowid
                    n = len(values)
                    if last is None or last < n:
                        # lastrowid is misleading — let’s read MAX(id) directly from the table
                        max_cur = self.env.conn.execute(
                            f"SELECT MAX({self.primary_key_field}) "
                            f"FROM {self.table_name}")
                        max_id = max_cur.fetchone()[0] or 0
                        if max_id <= 0:
                            raise _SuspiciousLastrowid(last, n)
                        last = max_id
                        first_id = last - n + 1
                        if first_id <= 0:
                            raise _SuspiciousLastrowid(last, n)
                    else:
                        first_id = last - n + 1
                        if first_id <= 0:
                            raise _SuspiciousLastrowid(last, n)
            if is_autoincrement:
                return list(range(first_id, last + 1))
            return self._extract_explicit_ids(items)
        except _SuspiciousLastrowid as e:
            if self.env.strict_batch:
                raise RuntimeError(
                    f"executemany on {self.table_name}: {e}") from e
            logger.warning(
                f"{self.table_name}: {e}; retrying per-row")
            return self._insert_per_row(sql, keyset, items, is_autoincrement)
        except sqlite3.ProgrammingError as e:
            if self.env.strict_batch:
                raise
            logger.warning(
                f"{self.table_name}: executemany failed ({e}); "
                f"retrying per-row")
            return self._insert_per_row(sql, keyset, items, is_autoincrement)

    def _insert_per_row(self, sql, keyset, items, is_autoincrement):
        ordered = sorted(keyset)
        ids: List = []
        for p in items:
            cur = self.env.conn.execute(sql, tuple(p[c] for c in ordered))
            if is_autoincrement:
                if cur.lastrowid is None:
                    raise RuntimeError(
                        f"{self.table_name}: lastrowid is None")
                ids.append(cur.lastrowid)
            else:
                pk_val = p.get(self.primary_key_field)
                if pk_val is None:
                    raise ValueError(
                        f"Primary key '{self.primary_key_field}' not "
                        f"provided for model '{self.model_name}'")
                ids.append(pk_val)
        return ids

    def _insert_per_row_default(self, items, is_autoincrement):
        if not is_autoincrement:
            raise ValueError(
                f"Cannot insert empty-keyset rows into {self.table_name}: "
                f"non-AUTOINCREMENT primary key "
                f"'{self.primary_key_field}' must be supplied")
        sql = f"INSERT INTO {self.table_name} DEFAULT VALUES"
        ids: List = []
        for _ in items:
            cur = self.env.conn.execute(sql)
            if cur.lastrowid is None:
                raise RuntimeError(
                    f"{self.table_name}: lastrowid None after DEFAULT VALUES")
            ids.append(cur.lastrowid)
        return ids

    def _extract_explicit_ids(self, items):
        ids: List = []
        for p in items:
            pk_val = p.get(self.primary_key_field)
            if pk_val is None:
                raise ValueError(
                    f"Primary key '{self.primary_key_field}' not provided "
                    f"for model '{self.model_name}'")
            ids.append(pk_val)
        return ids

    def write_records(self, records: List[Record], vals: dict):
        if not records or not vals:
            return
        prepared = self._prepare_values(vals)
        set_clause = ', '.join([f"{col}=?" for col in prepared.keys()])
        ids = [r[self.primary_key_field] for r in records]
        chunk_size = 500
        with self.env.transaction():
            for i in range(0, len(ids), chunk_size):
                chunk = ids[i:i + chunk_size]
                placeholders = ','.join(['?'] * len(chunk))
                sql = (f"UPDATE {self.table_name} SET {set_clause} "
                       f"WHERE {self.primary_key_field} IN ({placeholders})")
                self.env.conn.execute(sql, list(prepared.values()) + chunk)
        for r in records:
            for k, v in vals.items():
                r[k] = v

    def unlink_records(self, records: List[Record]):
        if not records:
            return
        ids = [r[self.primary_key_field] for r in records]
        chunk_size = 500
        with self.env.transaction():
            for i in range(0, len(ids), chunk_size):
                chunk = ids[i:i + chunk_size]
                placeholders = ','.join(['?'] * len(chunk))
                sql = (f"DELETE FROM {self.table_name} "
                       f"WHERE {self.primary_key_field} IN ({placeholders})")
                self.env.conn.execute(sql, chunk)

    def read_records(self, records: List[Record], fields: List[str] = None) -> List[dict]:
        if fields is None:
            fields = list(self.fields.keys())
        result = []
        for r in records:
            data = {f: r[f] for f in fields if f in r}
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
    def __init__(self, db_path: str,
                 registry: Optional[Dict[str, Type[Model]]] = None):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.registry = registry or MODEL_REGISTRY
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30,
                                    isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.lock = threading.RLock()
        self.tx_depth = 0
        self.strict_batch = os.environ.get('TBOT_DB_STRICT_BATCH', '0') == '1'
        self._managers: Dict[str, ModelManager] = {}
        self._dynamic_managers: Dict[Tuple[str, str], ModelManager] = {}

    def __getitem__(self, model_name: str) -> ModelManager:
        if model_name not in self._managers:
            model_cls = self.registry.get(model_name)
            if not model_cls:
                raise KeyError(f"Model '{model_name}' not found in registry")
            if model_cls.get_table() is None:
                raise KeyError(
                    f"Model '{model_name}' is dynamic; use get_model_manager() with table name"
                )
            manager = ModelManager(self, model_name, model_cls)
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
    """Nested-transaction-aware context.

    depth 0 -> BEGIN / COMMIT / ROLLBACK
    depth >0 -> SAVEPOINT sp_N / RELEASE sp_N / (ROLLBACK TO + RELEASE) sp_N

    Under isolation_level=None, sqlite3 does not auto-begin. All DML in
    ModelManager goes through `with self.env.transaction()`.
    """

    def __init__(self, env: DBSQLite3):
        self.env = env
        self._my_depth = -1

    def __enter__(self):
        self.env.lock.acquire()
        try:
            depth = self.env.tx_depth
            if depth == 0:
                self.env.conn.execute("BEGIN")
            else:
                self.env.conn.execute(f"SAVEPOINT sp_{depth}")
            self._my_depth = depth
            self.env.tx_depth = depth + 1
        except Exception:
            self.env.lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        depth = self._my_depth
        try:
            if depth == 0:
                if exc_type is None:
                    self.env.conn.execute("COMMIT")
                else:
                    self.env.conn.execute("ROLLBACK")
            else:
                if exc_type is None:
                    self.env.conn.execute(f"RELEASE sp_{depth}")
                else:
                    self.env.conn.execute(f"ROLLBACK TO sp_{depth}")
                    self.env.conn.execute(f"RELEASE sp_{depth}")
        finally:
            self.env.tx_depth = depth
            self.env.lock.release()
        return False


class _SuspiciousLastrowid(Exception):
    def __init__(self, lastrowid, n):
        self.lastrowid = lastrowid
        self.n = n
        super().__init__(
            f"suspicious lastrowid={lastrowid!r} after executemany(n={n})")

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
    db_files = glob.glob(os.path.join(DATA_DIR, 'bot_*.db'))
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

def cleanup_orphan_bots() -> List[int]:
    """Remove entries for bots from `config.db` if their corresponding 
	per-bot configuration database is missing.

    This complements `cleanup_orphan_databases()`: the latter removes `.db` 
	files that have no corresponding entries, while this function removes 
	entries that have no corresponding `.db` files. It is called from 
	`BotManager.load_bots()` before the iteration begins; otherwise, 
	`_create_instance()` would log a "config not found" error and 
	leave a "dead" entry in `bots` indefinitely.
    Returns a list of the `bot_id`s that were removed.
    """
    if not os.path.isdir(DATA_DIR):
        logger.error(
            f"cleanup_orphan_bots: DATA_DIR {DATA_DIR!r} does not "
            f"exist — skipping to avoid purging all bot records")
        return []
    removed: List[int] = []
    with sqlite3.connect(DB_CONFIG, timeout=30) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        rows = conn.execute('SELECT id, type, name FROM bots').fetchall()
        for bot_id, bot_type, bot_name in rows:
            db_path = os.path.join(DATA_DIR, f'bot_{bot_id}.db')
            if not os.path.exists(db_path):
                conn.execute('DELETE FROM bots WHERE id = ?', (bot_id,))
                removed.append(bot_id)
                logger.warning(
                    f"cleanup_orphan_bots: removed bot "
                    f"id={bot_id} type={bot_type!r} "
                    f"name={bot_name!r} (missing {db_path})")
        if removed:
            conn.commit()
    return removed

# ----------------------------------------------------------------------
# TimeSync config DB
# ----------------------------------------------------------------------

def init_timesync_db():
    """Idempotent. Creates timesync_config.db + drift_history."""
    global _TIMESYNC_DB_READY
    if _TIMESYNC_DB_READY:
        return
    with TIMESYNC_DB_LOCK:
        if _TIMESYNC_DB_READY:
            return
        with sqlite3.connect(TIMESYNC_DB, timeout=30) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute('''
                CREATE TABLE IF NOT EXISTS drift_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_ns INTEGER NOT NULL,
                    drift_slope_ppm REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_drift_history_ts
                ON drift_history (timestamp_ns)
            ''')
            conn.commit()
        _TIMESYNC_DB_READY = True

def append_drift_sample(drift_slope_ppm: float,
                        timestamp_ns: Optional[int] = None,
                        max_rows: int = DRIFT_HISTORY_MAX) -> None:
    """Insert one rate sample and rotate oldest rows beyond max_rows."""
    init_timesync_db()
    if timestamp_ns is None:
        timestamp_ns = time.time_ns()
    with TIMESYNC_DB_LOCK:
        with sqlite3.connect(TIMESYNC_DB, timeout=30) as conn:
            conn.execute(
                'INSERT INTO drift_history (timestamp_ns, drift_slope_ppm) VALUES (?, ?)',
                (int(timestamp_ns), float(drift_slope_ppm))
            )
            conn.execute('''
                DELETE FROM drift_history
                WHERE id NOT IN (
                    SELECT id FROM drift_history ORDER BY id DESC LIMIT ?
                )
            ''', (int(max_rows),))
            conn.commit()

def load_drift_history(limit: Optional[int] = None) -> List[Tuple[int, float]]:
    """Oldest-first list of (timestamp_ns, drift_slope_ppm)."""
    init_timesync_db()
    with TIMESYNC_DB_LOCK:
        with sqlite3.connect(TIMESYNC_DB, timeout=30) as conn:
            if limit is not None:
                cur = conn.execute(
                    'SELECT timestamp_ns, drift_slope_ppm FROM drift_history '
                    'ORDER BY id DESC LIMIT ?', (int(limit),)
                )
                rows = cur.fetchall()
                rows.reverse()
            else:
                cur = conn.execute(
                    'SELECT timestamp_ns, drift_slope_ppm FROM drift_history '
                    'ORDER BY id ASC'
                )
                rows = cur.fetchall()
    return [(int(t), float(v)) for t, v in rows]

# ----------------------------------------------------------------------
# Base model(s) for bot settings (shared across bots)
# ----------------------------------------------------------------------
class BotSettings(Model):
    _name = 'bot.settings'
    _table = 'bot_settings'

    key = Char(primary_key=True, required=True)
    value = Text()