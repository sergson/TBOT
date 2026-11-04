# modules/broker/lib/key_vault.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

"""Encrypted key storage for broker.

Contract (plan v8 §3.2):
  * table broker_keys (key_id PK, exchange, market_type, label, mode,
    api_key, api_secret, api_key_enc, api_secret_enc, salt_key, salt_secret,
    created_ns, updated_ns)
  * per-key salt
  * password priority: UI-unlocked > env TBOT_KEY_PASSWORD > None
  * KeyLockedError on missing/wrong password.
Password model: single UI password, applied sequentially to all
encrypted keys. Each successfully decrypted key is cached in
_decrypted and stays unlocked until invalidate/delete/restart.
Keys not decrypted by the last password remain locked — enter
their passwords separately. _ui_password is a fallback for
cache-miss after invalidate and may not match every cached key.
"""

import asyncio
import os
import time
import uuid
from .crypto import encrypt_with_salt, decrypt, new_salt

class KeyLockedError(Exception):
    def __init__(self, key_id: str):
        self.key_id = key_id
        super().__init__(f"Key '{key_id}' is locked")


class KeyVault:
    TABLE = 'broker_keys'

    def __init__(self, env, logger, password_provider):
        self._env = env
        self.logger = logger
        self._password_provider = password_provider
        self._decrypted: dict = {}
        self._lock = asyncio.Lock()
        self._ensure_table()

    def _ensure_table(self):
        conn = self._env.conn
        conn.execute(f'''
            CREATE TABLE IF NOT EXISTS {self.TABLE} (
                key_id TEXT PRIMARY KEY,
                exchange TEXT NOT NULL,
                market_type TEXT NOT NULL,
                label TEXT,
                mode TEXT NOT NULL,
                api_key TEXT,
                api_secret TEXT,
                api_key_enc TEXT,
                api_secret_enc TEXT,
                salt_key TEXT,
                salt_secret TEXT,
                created_ns INTEGER,
                updated_ns INTEGER
            )
        ''')
        conn.execute(
            f'CREATE INDEX IF NOT EXISTS idx_{self.TABLE}_ex_mt '
            f'ON {self.TABLE} (exchange, market_type)')

    async def list_keys(self, exchange=None, market_type=None) -> list:
        where, params = [], []
        if exchange:
            where.append('exchange = ?')
            params.append(exchange)
        if market_type:
            where.append('market_type = ?')
            params.append(market_type)
        sql = (f"SELECT key_id, exchange, market_type, label, mode, "
               f"created_ns FROM {self.TABLE}")
        if where:
            sql += " WHERE " + " AND ".join(where)
        async with self._lock:
            cur = self._env.conn.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            for r in rows:
                # encrypted key is "unlocked" iff its credentials are in cache
                r['locked'] = (
                        r['mode'] == 'encrypted'
                        and r['key_id'] not in self._decrypted
                )
            return rows

    async def get_key_info(self, key_id) -> dict:
        async with self._lock:
            cur = self._env.conn.execute(
                f"SELECT key_id, exchange, market_type, label, mode, "
                f"created_ns, updated_ns FROM {self.TABLE} "
                f"WHERE key_id = ?", (key_id,))
            row = cur.fetchone()
            if not row:
                return {}
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))

    async def get_credentials(self, key_id) -> tuple:
        async with self._lock:
            if key_id in self._decrypted:
                return self._decrypted[key_id]
            cur = self._env.conn.execute(
                f"SELECT mode, api_key, api_secret, api_key_enc, "
                f"api_secret_enc, salt_key, salt_secret "
                f"FROM {self.TABLE} WHERE key_id = ?", (key_id,))
            row = cur.fetchone()
            if not row:
                raise KeyLockedError(key_id)
            mode, ak, sk, ak_enc, sk_enc, salt_k, salt_s = row
            if mode == 'plain':
                creds = (ak or '', sk or '')
            else:
                password = self._password_provider()
                if not password:
                    raise KeyLockedError(key_id)
                try:
                    ak_dec = decrypt(ak_enc, salt_k, password)
                    sk_dec = decrypt(sk_enc, salt_s, password)
                    creds = (ak_dec, sk_dec)
                except Exception:
                    raise KeyLockedError(key_id)
            self._decrypted[key_id] = creds
            return creds

    async def unlock(self, password: str) -> dict:
        async with self._lock:
            cur = self._env.conn.execute(
                f"SELECT key_id, api_key_enc, api_secret_enc, "
                f"salt_key, salt_secret FROM {self.TABLE} "
                f"WHERE mode = 'encrypted'")
            rows = cur.fetchall()
        if not rows:
            return {'unlocked': [], 'failed': [], 'total_encrypted': 0}

        unlocked: list = []
        failed: list = []
        for key_id, ak_enc, sk_enc, salt_k, salt_s in rows:
            # Already decrypted previously using a different password — do not overwrite,
            # and do not count as a failure (otherwise, the UI would falsely indicate that the key has stopped working).
            if key_id in self._decrypted:
                unlocked.append(key_id)
                continue
            try:
                ak = decrypt(ak_enc, salt_k, password)
                sk = decrypt(sk_enc, salt_s, password)
                async with self._lock:
                    self._decrypted[key_id] = (ak, sk)
                unlocked.append(key_id)
            except Exception:
                failed.append(key_id)

        if not unlocked:
            raise ValueError("Wrong password")

        return {
            'unlocked': unlocked,
            'failed': failed,
            'total_encrypted': len(rows),
        }

    async def add_key(self, exchange, market_type, label,
                      api_key, api_secret, mode, password=None) -> str:
        if mode not in ('plain', 'encrypted'):
            raise ValueError(f"Invalid mode: {mode}")
        key_id = str(uuid.uuid4())
        now_ns = time.time_ns()
        if mode == 'plain':
            async with self._lock:
                self._env.conn.execute(
                    f"INSERT INTO {self.TABLE} (key_id, exchange, "
                    f"market_type, label, mode, api_key, api_secret, "
                    f"created_ns, updated_ns) "
                    f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (key_id, exchange, market_type, label, mode,
                     api_key, api_secret, now_ns, now_ns))
            return key_id
        if not password:
            raise ValueError("Encrypted mode requires password")
        salt_k = new_salt()
        salt_s = new_salt()
        ak_enc = encrypt_with_salt(api_key, password, salt_k)
        sk_enc = encrypt_with_salt(api_secret, password, salt_s)
        async with self._lock:
            self._env.conn.execute(
                f"INSERT INTO {self.TABLE} (key_id, exchange, market_type, "
                f"label, mode, api_key_enc, api_secret_enc, salt_key, "
                f"salt_secret, created_ns, updated_ns) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key_id, exchange, market_type, label, mode,
                 ak_enc, sk_enc, salt_k, salt_s, now_ns, now_ns))
            self._decrypted[key_id] = (api_key, api_secret)
        return key_id

    async def delete_key(self, key_id) -> None:
        async with self._lock:
            self._env.conn.execute(
                f"DELETE FROM {self.TABLE} WHERE key_id = ?", (key_id,))
            self._decrypted.pop(key_id, None)

    async def invalidate(self, key_id) -> None:
        async with self._lock:
            self._decrypted.pop(key_id, None)



def env_password_provider():
    """Password priority: UI-unlocked (set externally on broker) > env."""
    return os.environ.get('TBOT_KEY_PASSWORD')