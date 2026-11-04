# modules/broker/lib/crypto.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

import os, base64
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.fernet import Fernet

def derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32, salt=salt, iterations=200_000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))

def encrypt(plaintext: str, password: str) -> tuple[str, str]:
    salt = os.urandom(16)
    key = derive_key(password, salt)
    token = Fernet(key).encrypt(plaintext.encode()).decode()
    return token, base64.b64encode(salt).decode()

def decrypt(token: str, salt_b64: str, password: str) -> str:
    salt = base64.b64decode(salt_b64)
    key = derive_key(password, salt)
    return Fernet(key).decrypt(token.encode()).decode()

def encrypt_with_salt(plaintext: str, password: str, salt_b64: str) -> str:
    salt = base64.b64decode(salt_b64)
    key = derive_key(password, salt)
    return Fernet(key).encrypt(plaintext.encode()).decode()

def new_salt() -> str:
    return base64.b64encode(os.urandom(16)).decode()