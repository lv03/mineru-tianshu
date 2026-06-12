"""
PaddleOCR-VL-MLX parsing engine.

The main Python environment keeps PaddleOCR/PaddleX dependencies, while the
mlx-vlm server runs in a separate venv because mlx-vlm requires transformers>=5.
This wrapper only talks to the OpenAI-compatible MLX server endpoint.
"""

import gc
import os
import traceback
from pathlib import Path
from threading import Lock
from typing import Optional

import requests
from loguru import logger

from paddleocr_vl_vllm.engine import PaddleOCRVLVLLMEngine

# Must be set before importing paddle/paddlex through PaddleOCRVL.
os.environ["PADDLEX_INFERENCE_PARALLEL_WORKER_NUM"] = "1"
os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"


class PaddleOCRVLMLXEngine(PaddleOCRVLVLLMEngine):
    """PaddleOCR-VL engine that delegates VLM recognition to mlx-vlm server."""

    _instance: Optional["PaddleOCRVLMLXEngine"] = None
    _lock = Lock()
    _pipeline = None
    _initialized = False

    def __init__(
        self,
        mlx_server_url: str = None,
        model_name: str = "PaddleOCR-VL-1.6-0.9B",
        api_model_name: str = None,
    ):
        if self._initialized:
            return

        with self._lock:
            if self._initialized:
                return

            self.mlx_server_url = mlx_server_url or os.getenv("MLX_VLM_SERVER_URL")
            if not self.mlx_server_url:
                self.mlx_server_url = "http://127.0.0.1:8111/v1"
                logger.warning(
                    "MLX_VLM_SERVER_URL is not set; using default http://127.0.0.1:8111/v1. "
                    "Start mlx-vlm server or use --auto-start-mlx-server."
                )

            default_model_path = (
                Path(__file__).resolve().parent.parent
                / "model"
                / "paddlex_cache"
                / "official_models"
                / model_name
            )
            self.model_name = model_name
            self.api_model_name = (
                api_model_name
                or os.getenv("MLX_VLM_API_MODEL_NAME")
                or os.getenv("MLX_VLM_MODEL_PATH")
                or str(default_model_path)
            )
            self._initialized = True

            logger.info("PaddleOCR-VL-MLX Engine initialized")
            logger.info(f"   MLX server: {self.mlx_server_url}")
            logger.info(f"   API model:  {self.api_model_name}")
            logger.info("   Concurrency: Strict Serial Streaming")

    def _check_mlx_health(self) -> bool:
        try:
            base_url = self.mlx_server_url.rstrip("/")
            root_url = base_url[:-3] if base_url.endswith("/v1") else base_url

            try:
                resp = requests.get(f"{root_url}/health", timeout=2)
                if 200 <= resp.status_code < 400:
                    return True
            except Exception:
                pass

            resp = requests.get(f"{base_url}/models", timeout=2)
            return resp.status_code == 200
        except Exception as e:
            logger.warning(f"MLX VLM service check failed: {e}")
            return False

    def _load_pipeline(self):
        if self._pipeline is not None:
            return self._pipeline

        with self._lock:
            if self._pipeline is not None:
                return self._pipeline

            if not self._check_mlx_health():
                logger.error(f"MLX VLM service unreachable at {self.mlx_server_url}")

            logger.info("Loading PaddleOCR-VL-MLX Pipeline...")
            try:
                from paddleocr import PaddleOCRVL

                default_paddlex_home = Path(__file__).resolve().parent.parent / "model" / "paddlex_cache"
                os.environ.setdefault("PADDLEX_HOME", str(default_paddlex_home))

                self._pipeline = PaddleOCRVL(
                    vl_rec_backend="mlx-vlm-server",
                    vl_rec_server_url=self.mlx_server_url,
                    vl_rec_api_model_name=self.api_model_name,
                )

                logger.info("PaddleOCR-VL-MLX pipeline loaded successfully")
                return self._pipeline

            except Exception as e:
                logger.error(f"PaddleOCR-VL-MLX pipeline load failed: {e}")
                logger.error(traceback.format_exc())
                raise

    def cleanup(self):
        with self._lock:
            self._pipeline = None
            gc.collect()

    def parse(self, file_path: str, output_path: str, **kwargs):
        unsupported = ("minPixels", "maxPixels", "min_pixels", "max_pixels")
        filtered_kwargs = {k: v for k, v in kwargs.items() if k not in unsupported}
        dropped = sorted(set(kwargs) - set(filtered_kwargs))
        if dropped:
            logger.info(f"Ignoring unsupported MLX VLM options: {', '.join(dropped)}")
        return super().parse(file_path, output_path, **filtered_kwargs)


_engine = None


def get_engine(
    mlx_server_url: str = None,
    model_name: str = "PaddleOCR-VL-1.6-0.9B",
    api_model_name: str = None,
) -> PaddleOCRVLMLXEngine:
    global _engine
    if _engine is None:
        _engine = PaddleOCRVLMLXEngine(
            mlx_server_url=mlx_server_url,
            model_name=model_name,
            api_model_name=api_model_name,
        )
    return _engine
