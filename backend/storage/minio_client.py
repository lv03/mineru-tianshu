"""
MinIO 对象存储客户端

基于 MinIO S3 兼容 API 实现，支持图片批量上传和 URL 生成。
"""

import os
import secrets
import string
import time
from pathlib import Path
from typing import Dict, Optional, List
from urllib.parse import urlparse
from datetime import timedelta
from loguru import logger
from minio import Minio
from minio.error import S3Error
from datetime import datetime

# 公开可读对象前缀（如系统 Logo），登录页未鉴权即需加载；其余对象一律私有 + presigned 访问
PUBLIC_PREFIX = "logos/"
# 对象引用 scheme：存储层只写入此引用，API 读时再生成 presigned URL，避免把过期 URL 戳进文件
REFERENCE_SCHEME = "minio://"
# presigned URL 默认有效期
PRESIGN_TTL = timedelta(seconds=int(os.getenv("MINIO_PRESIGN_TTL", "3600")))


class MinIOClient:
    """
    MinIO 对象存储客户端

    特性：
    - S3 兼容 API
    - 自动创建 Bucket
    - 批量上传图片
    - 生成可访问的公开 URL
    - 自动检测主机 IP（避免使用 localhost）
    - 短且唯一的文件名生成（时间 + NanoID）
    """

    @staticmethod
    def _generate_nanoid(size: int = 4) -> str:
        """
        生成 NanoID（URL安全的随机字符串）

        Args:
            size: 字符串长度

        Returns:
            随机字符串（包含大小写字母、数字、-_）
        """
        alphabet = string.ascii_letters + string.digits + "-_"
        return "".join(secrets.choice(alphabet) for _ in range(size))

    @staticmethod
    def _base62_encode(num: int) -> str:
        """
        Base62 编码（使用 0-9a-zA-Z）

        Args:
            num: 要编码的整数

        Returns:
            Base62 编码的字符串
        """
        alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        if num == 0:
            return alphabet[0]
        result = []
        while num:
            num, rem = divmod(num, 62)
            result.append(alphabet[rem])
        return "".join(reversed(result))

    @staticmethod
    def _generate_short_filename(extension: str) -> str:
        """
        生成短且唯一的文件名: msec_nanoid.ext

        格式说明:
        - msec: 毫秒时间戳后5位（Base62编码，5字符）
        - nanoid: 随机字符串（4字符，64^4 ≈ 1600万种可能）
        - 总长度: 约11字符（不含扩展名）

        并发安全性:
        - 毫秒级时间精度，多 Worker 很难在同一毫秒上传
        - 即使同一毫秒，还有 1600万种随机可能
        - 10个Worker同时每毫秒上传100张，碰撞概率 < 0.003%

        Args:
            extension: 文件扩展名（如 .jpg）

        Returns:
            短文件名（如: a3f2K_V1St.jpg）
        """
        # 获取毫秒时间戳
        timestamp_ms = int(time.time() * 1000)

        # Base62 编码并取后5位（足够表示时间差异）
        timestamp_encoded = MinIOClient._base62_encode(timestamp_ms)
        timestamp_part = timestamp_encoded[-5:]  # 取后5位

        # 生成随机后缀
        nano_part = MinIOClient._generate_nanoid(4)  # 4字符

        return f"{timestamp_part}_{nano_part}{extension}"

    def __init__(
        self,
        endpoint: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        bucket_name: Optional[str] = None,
        secure: bool = False,
        public_url: Optional[str] = None,
    ):
        """
        初始化 MinIO 客户端

        Args:
            endpoint: MinIO 服务地址 (例如: minio:9000)
            access_key: 访问密钥
            secret_key: 密钥
            bucket_name: 存储桶名称
            secure: 是否使用 HTTPS
            public_url: 公开访问 URL (必须设置，例如: http://192.168.1.100:9000)
        """
        # 从配置层读取（集中声明，兼容旧 RUSTFS_*）；config 不可用时回退直接读 env
        try:
            from config import get_settings

            _s = get_settings()
            _endpoint, _ak, _sk, _bucket, _public = (
                _s.minio_endpoint,
                _s.minio_access_key,
                _s.minio_secret_key,
                _s.minio_bucket,
                _s.minio_public_url,
            )
        except Exception:
            _endpoint = os.getenv("MINIO_ENDPOINT") or os.getenv("RUSTFS_ENDPOINT", "minio:9000")
            _ak = os.getenv("MINIO_ACCESS_KEY") or os.getenv("RUSTFS_ACCESS_KEY")
            _sk = os.getenv("MINIO_SECRET_KEY") or os.getenv("RUSTFS_SECRET_KEY")
            _bucket = os.getenv("MINIO_BUCKET") or os.getenv("RUSTFS_BUCKET", "ts-img")
            _public = os.getenv("MINIO_PUBLIC_URL") or os.getenv("RUSTFS_PUBLIC_URL", "")

        self.endpoint = endpoint or _endpoint
        # 凭据必须显式配置，不再回落到公开已知的 minioadmin/minioadmin
        self.access_key = access_key or _ak
        self.secret_key = secret_key or _sk
        self.bucket_name = bucket_name or _bucket
        self.secure = secure or (os.getenv("MINIO_SECURE", os.getenv("RUSTFS_SECURE", "false")).lower() == "true")

        if not self.access_key or not self.secret_key:
            raise ValueError(
                "MinIO 凭据未配置。请在 .env 中设置 MINIO_ACCESS_KEY 与 MINIO_SECRET_KEY，"
                "不要使用默认的 minioadmin/minioadmin。"
            )

        # 弱凭据检测：生产模式禁用众所周知的 minioadmin（持有=对象存储管理员），开发模式仅告警
        weak_creds = {"minioadmin", "admin", "password", "root"}
        if self.access_key.lower() in weak_creds or self.secret_key.lower() in weak_creds:
            try:
                from config import get_settings

                is_production = get_settings().is_production
            except Exception:
                is_production = (
                    os.getenv("ENV", "").lower() == "production"
                    or os.getenv("REQUIRE_SECRETS", "").lower() == "true"
                )
            if is_production:
                raise ValueError(
                    "MinIO 凭据为弱口令（如 minioadmin）。生产环境必须改为强随机凭据 "
                    "(例如: openssl rand -hex 16)，否则任何人拿到密钥即获得对象存储完整读写权限。"
                )
            logger.warning(
                "⚠️  MinIO 凭据为弱口令（如 minioadmin）——仅限本地开发！"
                "上线前务必在 .env 改为强随机的 MINIO_ACCESS_KEY / MINIO_SECRET_KEY。"
            )

        # 公开 URL 配置：必须通过 MINIO_PUBLIC_URL 环境变量设置
        self.public_url = (public_url or _public or "").strip()

        if not self.public_url:
            logger.error("❌ MINIO_PUBLIC_URL not configured!")
            logger.error("   Please set MINIO_PUBLIC_URL in .env file")
            logger.error("")
            logger.error("   Example:")
            logger.error("   MINIO_PUBLIC_URL=http://192.168.1.100:9000")
            logger.error("")
            logger.error("   For Windows/WSL users:")
            logger.error("   1. Run 'ipconfig' in Windows to get your IP")
            logger.error("   2. Set MINIO_PUBLIC_URL=http://YOUR_WINDOWS_IP:9000")
            raise ValueError("MINIO_PUBLIC_URL not configured. Please set MINIO_PUBLIC_URL in .env file.")

        # 移除末尾的斜杠
        self.public_url = self.public_url.rstrip("/")
        logger.info(f"🌐 MinIO Public URL: {self.public_url}")

        # 验证配置
        if not all([self.endpoint, self.access_key, self.secret_key, self.bucket_name]):
            raise ValueError(
                "MinIO configuration incomplete. Please set: "
                "MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, MINIO_BUCKET"
            )

        # 初始化 MinIO 客户端
        try:
            self.client = Minio(
                self.endpoint,
                access_key=self.access_key,
                secret_key=self.secret_key,
                secure=self.secure,
            )
            logger.info(f"🚀 MinIO client initialized: {self.endpoint}")
            logger.info(f"   Bucket: {self.bucket_name}")
            logger.info(f"   Public URL: {self.public_url}")

            # 用于生成 presigned URL 的客户端：必须以「浏览器可达的公开 endpoint」签名，
            # 否则签名里的 host 是内网地址（如 minio:9000），浏览器无法访问。
            parsed = urlparse(self.public_url)
            public_host = parsed.netloc or parsed.path  # 容错：public_url 可能不带 scheme
            self._presign_client = Minio(
                public_host,
                access_key=self.access_key,
                secret_key=self.secret_key,
                secure=(parsed.scheme == "https"),
            )

            # 确保 Bucket 存在
            self._ensure_bucket()

        except Exception as e:
            logger.error(f"❌ Failed to initialize MinIO client: {e}")
            raise

    def _ensure_bucket(self):
        """确保 Bucket 存在，并将访问策略收敛为「仅 logos/ 前缀公开读，其余私有」。"""
        try:
            if not self.client.bucket_exists(self.bucket_name):
                self.client.make_bucket(self.bucket_name)
                logger.info(f"✅ Created bucket: {self.bucket_name}")
            else:
                logger.debug(f"✅ Bucket exists: {self.bucket_name}")

            # 每次初始化都重设策略（幂等），使存量桶也从「整桶公开」收敛到「仅 logos/ 公开」，
            # 任务图片不再世界可读，改由 API 读时签发 presigned URL 访问。
            try:
                policy = {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"AWS": ["*"]},
                            "Action": ["s3:GetObject"],
                            "Resource": [f"arn:aws:s3:::{self.bucket_name}/{PUBLIC_PREFIX}*"],
                        }
                    ],
                }
                import json

                self.client.set_bucket_policy(self.bucket_name, json.dumps(policy))
                logger.info(f"✅ Set bucket policy: public read only for '{PUBLIC_PREFIX}*', rest private")
            except Exception as e:
                logger.warning(f"⚠️  Failed to set bucket policy (may need manual configuration): {e}")
        except S3Error as e:
            logger.error(f"❌ Failed to ensure bucket: {e}")
            raise

    def upload_file(
        self,
        file_path: str,
        object_name: Optional[str] = None,
        content_type: Optional[str] = None,
        public: bool = False,
    ) -> str:
        """
        上传单个文件到 MinIO

        Args:
            file_path: 本地文件路径
            object_name: 对象名称 (不指定则自动生成: YYYYMMDD/msec_nano.ext)
            content_type: MIME 类型 (不指定则自动检测)
            public: 是否为公开资产（如 Logo）。公开资产返回直链 URL（需位于 PUBLIC_PREFIX 下），
                    其余返回稳定对象引用 minio://bucket/object，由 API 读时换成 presigned URL。

        Returns:
            public=True 时返回公开直链；否则返回 minio://bucket/object 引用
        """
        file_path = Path(file_path)

        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        # 生成对象名称: YYYYMMDD/msec_nano.ext
        if object_name is None:
            file_extension = file_path.suffix
            date_prefix = datetime.now().strftime("%Y%m%d")
            short_filename = self._generate_short_filename(file_extension)
            object_name = f"{date_prefix}/{short_filename}"

        # 自动检测 Content-Type
        if content_type is None:
            content_type = self._get_content_type(file_path)

        try:
            # 上传文件
            self.client.fput_object(
                self.bucket_name,
                object_name,
                str(file_path),
                content_type=content_type,
            )

            if public:
                # 公开资产：返回直链（依赖 PUBLIC_PREFIX 的公开读策略）
                result = f"{self.public_url}/{self.bucket_name}/{object_name}"
            else:
                # 私有资产：返回稳定引用，读时再签发 presigned URL
                result = f"{REFERENCE_SCHEME}{self.bucket_name}/{object_name}"

            logger.debug(f"✅ Uploaded: {file_path.name} -> {object_name}")
            return result

        except S3Error as e:
            logger.error(f"❌ Failed to upload {file_path.name}: {e}")
            raise

    def upload_directory(
        self,
        dir_path: str,
        prefix: Optional[str] = None,
        extensions: Optional[List[str]] = None,
    ) -> Dict[str, str]:
        """
        批量上传目录中的文件

        Args:
            dir_path: 目录路径
            prefix: 对象名称前缀 (例如: "custom_prefix"，不指定则使用日期前缀 YYYYMMDD)
            extensions: 允许的文件扩展名列表 (例如: [".jpg", ".png"])

        Returns:
            {本地文件名: 公开 URL} 的映射字典

        Note:
            使用毫秒时间戳+NanoID方案，多Worker并发安全
            - 毫秒级时间精度 + 1600万种随机可能
            - 10个Worker同时每毫秒上传100张，碰撞概率 < 0.003%
        """
        dir_path = Path(dir_path)

        if not dir_path.exists() or not dir_path.is_dir():
            raise ValueError(f"Invalid directory: {dir_path}")

        # 默认只上传图片文件
        if extensions is None:
            extensions = [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg"]

        # 查找所有符合条件的文件
        files = [f for f in dir_path.iterdir() if f.is_file() and f.suffix.lower() in extensions]

        if not files:
            logger.warning(f"⚠️  No files found in {dir_path}")
            return {}

        logger.info(f"📤 Uploading {len(files)} files from {dir_path.name}/")

        url_mapping = {}

        for file_path in files:
            try:
                # 生成对象名称: YYYYMMDD/msec_nano.ext
                file_extension = file_path.suffix
                date_prefix = datetime.now().strftime("%Y%m%d")
                short_filename = self._generate_short_filename(file_extension)

                if prefix:
                    object_name = f"{prefix}/{short_filename}"
                else:
                    object_name = f"{date_prefix}/{short_filename}"

                # 上传文件
                url = self.upload_file(file_path, object_name)

                # 记录映射 (原始文件名 -> URL)
                url_mapping[file_path.name] = url

            except Exception as e:
                logger.error(f"❌ Failed to upload {file_path.name}: {e}")
                # 继续上传其他文件
                continue

        logger.info(f"✅ Successfully uploaded {len(url_mapping)}/{len(files)} files")
        return url_mapping

    def _get_content_type(self, file_path: Path) -> str:
        """根据文件扩展名返回 MIME 类型"""
        extension = file_path.suffix.lower()

        content_types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
            ".webp": "image/webp",
            ".svg": "image/svg+xml",
        }

        return content_types.get(extension, "application/octet-stream")

    @staticmethod
    def is_reference(value: str) -> bool:
        """判断字符串是否为对象引用 minio://bucket/object"""
        return isinstance(value, str) and value.startswith(REFERENCE_SCHEME)

    def presign_reference(self, reference: str, expires: timedelta = PRESIGN_TTL) -> str:
        """
        将对象引用 minio://bucket/object 换成浏览器可达的 presigned GET URL。

        若 reference 不是合法引用则原样返回（兼容历史直链 URL）。
        """
        if not self.is_reference(reference):
            return reference
        rest = reference[len(REFERENCE_SCHEME) :]
        bucket, _, object_name = rest.partition("/")
        if not object_name:
            return reference
        try:
            return self._presign_client.presigned_get_object(bucket, object_name, expires=expires)
        except Exception as e:
            logger.error(f"❌ Failed to presign {reference}: {e}")
            return reference

    def delete_file(self, object_name: str) -> bool:
        """
        删除对象

        Args:
            object_name: 对象名称

        Returns:
            是否成功
        """
        try:
            self.client.remove_object(self.bucket_name, object_name)
            logger.debug(f"✅ Deleted: {object_name}")
            return True
        except S3Error as e:
            logger.error(f"❌ Failed to delete {object_name}: {e}")
            return False

    def health_check(self) -> bool:
        """
        健康检查

        Returns:
            MinIO 是否可用
        """
        try:
            self.client.bucket_exists(self.bucket_name)
            return True
        except Exception as e:
            logger.error(f"❌ MinIO health check failed: {e}")
            return False


# 全局单例实例（延迟初始化）
_minio_client: Optional[MinIOClient] = None


def get_minio_client() -> MinIOClient:
    """
    获取全局 MinIO 客户端实例（单例模式）

    Returns:
        MinIOClient 实例
    """
    global _minio_client

    if _minio_client is None:
        _minio_client = MinIOClient()

    return _minio_client


# 兼容旧导入，后续代码请使用 MinIOClient/get_minio_client。
RustFSClient = MinIOClient
get_rustfs_client = get_minio_client
