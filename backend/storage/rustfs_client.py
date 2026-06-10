"""
Backward-compatible imports for the old RustFS storage client module.

New code should import from storage.minio_client.
"""

from .minio_client import MinIOClient, get_minio_client

RustFSClient = MinIOClient
get_rustfs_client = get_minio_client

__all__ = ["RustFSClient", "get_rustfs_client", "MinIOClient", "get_minio_client"]
