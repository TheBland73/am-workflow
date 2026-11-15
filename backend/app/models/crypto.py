"""密钥：Fernet 加密落盘、响应脱敏（模型接入配置设计 §9）。"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


def _derive(secret: str) -> bytes:
    """允许 AM_SECRET_KEY 是任意口令：用 SHA-256 派生 32 字节 Fernet key。"""
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())


def _load_or_create_key() -> bytes:
    if settings.secret_key_env:
        raw = settings.secret_key_env.encode()
        try:
            Fernet(raw)
            return raw
        except (ValueError, TypeError):
            return _derive(settings.secret_key_env)
    path = settings.secret_key_file
    if path.exists():
        return path.read_bytes().strip()
    settings.ensure_dirs()
    key = Fernet.generate_key()
    path.write_bytes(key)
    return key


_fernet: Fernet | None = None


def fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def encrypt_key(plain: str | None) -> str | None:
    if not plain:
        return None
    return fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_key(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def mask_key(plain_or_token: str | None) -> str:
    if not plain_or_token:
        return ""
    return "sk-****" + plain_or_token[-4:]
