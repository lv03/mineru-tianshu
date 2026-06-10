"""
对象存储模块

提供统一的对象存储接口，支持 MinIO (S3 兼容)
"""

from .minio_client import MinIOClient, get_minio_client

# 兼容旧导入
RustFSClient = MinIOClient
get_rustfs_client = get_minio_client

__all__ = ["MinIOClient", "get_minio_client", "RustFSClient", "get_rustfs_client"]
