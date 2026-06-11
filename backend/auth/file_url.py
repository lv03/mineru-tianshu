"""
MinerU Tianshu - 文件 URL 签名工具
File URL Signing Utility

对 /api/v1/files/output 与 /api/v1/files/upload 下发的文件 URL 做 HMAC 签名,
解决浏览器 <img>/iframe 无法携带 Authorization 头的鉴权难题。

设计:
    - 签名负载: f"{scope}:{rel_path}:{exp}"，scope 区分 output/upload，防止跨命名空间重放。
    - 读时签名: API 在构造响应时对文件 URL 签名，短 TTL，不把过期戳进存储文件。
    - 密钥优先 FILE_URL_SECRET，回落到 JWT_SECRET_KEY（与 jwt_handler 保持一致）。
"""

import os
import time
import hmac
import hashlib
from urllib.parse import quote

from .jwt_handler import JWT_SECRET_KEY

try:
    from config import get_settings

    _settings = get_settings()
    DEFAULT_TTL = _settings.file_url_ttl
    _FILE_URL_SECRET = _settings.file_url_secret
except Exception:
    # 兜底：config 不可用时回退到直接读 env，保证模块可独立加载
    DEFAULT_TTL = int(os.getenv("FILE_URL_TTL", "3600"))
    _FILE_URL_SECRET = os.getenv("FILE_URL_SECRET")


def _secret() -> str:
    """签名密钥：优先 FILE_URL_SECRET，回落到 JWT_SECRET_KEY。"""
    return _FILE_URL_SECRET or JWT_SECRET_KEY


def _compute_sig(scope: str, rel_path: str, exp: int) -> str:
    """计算 HMAC-SHA256 签名（对解码后的相对路径，避免编码歧义）。"""
    payload = f"{scope}:{rel_path}:{exp}".encode("utf-8")
    return hmac.new(_secret().encode("utf-8"), payload, hashlib.sha256).hexdigest()


def build_signed_url(scope: str, rel_path: str, ttl: int = None) -> str:
    """
    构造带签名的文件访问 URL。

    Args:
        scope: "output" 或 "upload"
        rel_path: 相对于 OUTPUT_DIR / UPLOAD_DIR 的路径（解码后的原始路径）
        ttl: 有效期秒数，默认 DEFAULT_TTL

    Returns:
        形如 /api/v1/files/{scope}/{quoted_path}?exp=<ts>&sig=<hex>
    """
    rel_path = rel_path.lstrip("/")
    exp = int(time.time()) + (ttl if ttl is not None else DEFAULT_TTL)
    sig = _compute_sig(scope, rel_path, exp)
    encoded = quote(rel_path, safe="/")
    return f"/api/v1/files/{scope}/{encoded}?exp={exp}&sig={sig}"


def verify_signature(scope: str, rel_path: str, exp: str, sig: str) -> bool:
    """
    校验文件 URL 签名。

    Args:
        scope: "output" 或 "upload"
        rel_path: 端点解码后的相对路径
        exp: query 中的过期时间戳（字符串）
        sig: query 中的签名

    Returns:
        bool: 签名有效且未过期返回 True
    """
    if not exp or not sig:
        return False
    try:
        exp_int = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_int < int(time.time()):
        return False  # 已过期
    expected = _compute_sig(scope, rel_path.lstrip("/"), exp_int)
    return hmac.compare_digest(expected, sig)
