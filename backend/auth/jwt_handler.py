"""
MinerU Tianshu - JWT Token Handler
JWT Token 处理器

负责 JWT Token 的生成和验证
"""

import os
from datetime import datetime, timedelta
from typing import Optional
import jwt
from loguru import logger

from .models import TokenData, UserRole

# JWT 配置（集中到 config.Settings；config 不可用时回退直接读 env）
_DEFAULT_SECRET_PLACEHOLDER = "your-secret-key-change-in-production"
try:
    from config import get_settings

    _s = get_settings()
    JWT_SECRET_KEY = _s.jwt_secret_key
    JWT_EXPIRE_MINUTES = _s.jwt_expire_minutes
    _IS_PRODUCTION = _s.is_production
except Exception:
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", _DEFAULT_SECRET_PLACEHOLDER)
    JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "1440"))
    _IS_PRODUCTION = os.getenv("ENV", "").lower() == "production" or os.getenv("REQUIRE_SECRETS", "").lower() == "true"
JWT_ALGORITHM = "HS256"


def validate_secret() -> None:
    """
    校验 JWT 密钥安全性，杜绝用公开占位符签发令牌（否则可被伪造为管理员）。

    - 生产模式（ENV=production 或 REQUIRE_SECRETS=true）：缺省/占位符直接 raise，启动失败。
    - 开发模式：仅打印醒目 warning，便于本地快速启动。
    """
    # 不安全密钥判定：为空、精确占位符、或仍含模板标记 "change-in-production"
    # （后者覆盖三个 .env 模板的默认值，捕获"忘了改模板"这一最常见疏漏）
    key = JWT_SECRET_KEY or ""
    insecure = (not key) or key == _DEFAULT_SECRET_PLACEHOLDER or ("change-in-production" in key.lower())

    if insecure:
        if _IS_PRODUCTION:
            raise RuntimeError(
                "JWT_SECRET_KEY 未设置或仍为默认占位符。生产环境必须在 .env 中设置一个随机强密钥 "
                "(例如: openssl rand -hex 32)，否则任何人都可伪造管理员令牌。"
            )
        logger.warning(
            "⚠️  JWT_SECRET_KEY 仍为默认占位符——令牌可被伪造！仅限本地开发使用，"
            "上线前务必在 .env 设置随机强密钥 (openssl rand -hex 32)。"
        )


# 模块加载即校验（开发态 warning，生产态 fail-fast）
validate_secret()


def create_access_token(user_id: str, username: str, role: UserRole, expires_delta: Optional[timedelta] = None) -> str:
    """
    创建 JWT Access Token

    Args:
        user_id: 用户ID
        username: 用户名
        role: 用户角色
        expires_delta: 过期时间增量 (None 则使用默认值)

    Returns:
        str: JWT Token
    """
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=JWT_EXPIRE_MINUTES)

    to_encode = {
        "sub": user_id,
        "username": username,
        "role": role.value,
        "exp": expire,
        "iat": datetime.utcnow(),
    }

    encoded_jwt = jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
    return encoded_jwt


def verify_token(token: str) -> Optional[TokenData]:
    """
    验证 JWT Token

    Args:
        token: JWT Token

    Returns:
        TokenData: Token 数据，验证失败返回 None
    """
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        user_id: str = payload.get("sub")
        username: str = payload.get("username")
        role_str: str = payload.get("role")

        if user_id is None or username is None or role_str is None:
            return None

        return TokenData(user_id=user_id, username=username, role=UserRole(role_str))

    except jwt.ExpiredSignatureError:
        logger.debug("Token expired")
        return None
    except jwt.InvalidSignatureError:
        logger.debug("Invalid signature")
        return None
    except (jwt.DecodeError, jwt.InvalidTokenError) as e:
        logger.debug(f"JWT validation error: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error validating token: {e}")
        return None
