"""安全与凭据中心：PBKDF2 密码哈希、标准 HS256 JWT、Fernet 对称加密与脱敏、项目 Serving API Key 签发与校验"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Dict, Optional, Tuple

from cryptography.fernet import Fernet

from app.core.config import settings


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


# ==================== 密码哈希（PBKDF2-HMAC-SHA256） ====================

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"pbkdf2_sha256${salt}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        _, salt, expected = stored_hash.split("$", 2)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
        return hmac.compare_digest(dk.hex(), expected)
    except Exception:
        return False


# ==================== 标准 HS256 JWT ====================

def create_access_token(user_id: str, email: str, role: str, workspace_id: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "workspace_id": workspace_id,
        "iat": now,
        "exp": now + settings.jwt_expire_minutes * 60,
    }
    h_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(settings.jwt_secret_key.encode("utf-8"), f"{h_b64}.{p_b64}".encode("ascii"), hashlib.sha256).digest()
    return f"{h_b64}.{p_b64}.{_b64url_encode(sig)}"


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        h_b64, p_b64, s_b64 = parts
        expected_sig = hmac.new(
            settings.jwt_secret_key.encode("utf-8"), f"{h_b64}.{p_b64}".encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_b64url_encode(expected_sig), s_b64):
            return None
        payload = json.loads(_b64url_decode(p_b64).decode("utf-8"))
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


# ==================== Fernet 对称加密（DB 密码 & LLM API Key） ====================

def _get_fernet() -> Fernet:
    raw = settings.app_encryption_key.strip()
    if raw:
        key = raw.encode("utf-8")
    else:
        # 从 JWT_SECRET_KEY 稳定派生 32 字节 Fernet Key，避免重启后解不开本地开发库
        digest = hashlib.sha256(f"fernet:{settings.jwt_secret_key}".encode("utf-8")).digest()
        key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    return _get_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")


def mask_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    if len(plaintext) <= 8:
        return "****"
    return f"{plaintext[:4]}****{plaintext[-4:]}"


# ==================== 项目级对外 Serving API Key ====================

def generate_project_api_key() -> Tuple[str, str, str]:
    """返回 (明文 Key 仅展示一次, 前缀, SHA-256 不可逆哈希)"""
    raw = "su_live_" + secrets.token_urlsafe(28)
    prefix = raw[:14]
    key_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return raw, prefix, key_hash


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
