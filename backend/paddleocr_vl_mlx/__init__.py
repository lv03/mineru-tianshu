"""
PaddleOCR-VL MLX 解析引擎
Apple Silicon 上通过独立 mlx-vlm server 执行视觉语言识别。
"""

from .engine import PaddleOCRVLMLXEngine, get_engine

__all__ = ["PaddleOCRVLMLXEngine", "get_engine"]
