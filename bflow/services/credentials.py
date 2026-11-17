"""密钥的加解密存储。

方案见 spec/04-data-model.md §2.4：
- AES-256-GCM，主密钥来自 BFLOW_MASTER_KEY 环境变量；
- 未设置时自动生成并写入 <data_root>/master.key（0600）；
- 密钥**永不回传 API**，读接口一律返回掩码。
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import struct
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..config import settings

_MAGIC = b"BFK1"
_NONCE_LEN = 12


def _derive(master: bytes) -> bytes:
    """把任意长度的主密钥派生成 32 字节 AES 密钥。"""
    return hashlib.sha256(b"bflow-master-v1:" + master).digest()


def load_master_key() -> bytes:
    """取主密钥：环境变量优先，否则读/建本地 master.key。"""
    env = os.environ.get("BFLOW_MASTER_KEY")
    if env:
        return env.encode("utf-8")
    path = settings.master_key_path
    if path.exists():
        return path.read_bytes().strip()
    key = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def encrypt(plaintext: str) -> bytes:
    """加密 → 存库。"""
    master = load_master_key()
    nonce = secrets.token_bytes(_NONCE_LEN)
    cipher = AESGCM(_derive(master))
    ct = cipher.encrypt(nonce, plaintext.encode("utf-8"), None)
    return _MAGIC + nonce + ct


def decrypt(blob: bytes) -> str:
    """解密 ← 读库。"""
    if not blob or not blob.startswith(_MAGIC):
        raise ValueError("密钥数据格式不正确")
    master = load_master_key()
    nonce = blob[len(_MAGIC):len(_MAGIC) + _NONCE_LEN]
    ct = blob[len(_MAGIC) + _NONCE_LEN:]
    cipher = AESGCM(_derive(master))
    return cipher.decrypt(nonce, ct, None).decode("utf-8")


def mask(secret: str | None) -> str:
    """接口返回用的掩码。"""
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:3]}{'*' * 6}{secret[-2:]}"
