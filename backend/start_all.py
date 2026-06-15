#!/usr/bin/env python3
"""
MinerU Tianshu - 启动所有服务

1. API Server (FastAPI) - 端口 8000
2. LitServe Worker Pool - 端口 8001
3. Task Scheduler (可选) - 后台任务调度
4. MCP Server (可选) - 端口 8002

自动检查并下载 OCR 模型（PaddleOCR-VL）
支持 GPU 加速、任务队列、优先级管理
"""

import subprocess
import signal
import sys
import time
import os
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from loguru import logger
from pathlib import Path
import argparse
from utils import parse_list_arg
from dotenv import load_dotenv


def configure_local_models():
    """配置本地模型路径（原生开发：直接使用 backend/model 下的模型，不联网下载）。

    各引擎在代码中的默认值指向的是容器路径（SenseVoice/Paraformer/水印 YOLO -> MODEL_PATH
    默认 /app/models；PaddleOCR-VL -> PADDLEX_HOME 默认 /root/.paddlex；MinerU -> ~/mineru.json，
    其内容写死 /app/models/...）。这些路径在 Mac/原生环境并不存在，导致启动即报“本地模型缺失”。

    这里在拉起子进程之前，按仓库内 backend/model 的实际位置注入对应环境变量，让全部引擎都走
    本地模型；同时为 MinerU 生成一份带宿主机绝对路径的配置文件并指向它。
    - 仅在用户未显式设置对应变量时生效（setdefault），保留覆盖能力。
    - Docker 不经过本脚本（各服务有独立 command + entrypoint），因此不受影响。
    """
    model_root = (Path(__file__).parent / "model").resolve()
    if not model_root.exists():
        logger.warning(f"⚠️  本地模型目录不存在: {model_root}，将沿用各引擎默认路径")
        return

    # SenseVoice / Paraformer / 水印 YOLO 统一读 MODEL_PATH
    os.environ.setdefault("MODEL_PATH", str(model_root))
    # PaddleOCR-VL 读 PADDLEX_HOME，本地模型在 paddlex_cache/official_models 下
    os.environ.setdefault("PADDLEX_HOME", str(model_root / "paddlex_cache"))

    # MinerU 本地模式：生成带宿主机绝对路径的配置文件并指向它
    # （仓库内自带的 mineru.json 写死了 /app/models/...，仅供 Docker 使用）
    if "MINERU_TOOLS_CONFIG_JSON" not in os.environ:
        config_path = model_root / "mineru.local.json"
        config_path.write_text(
            json.dumps(
                {
                    "models-dir": {
                        # MinerU 3.0 pipeline 的相对路径常量本身已含 "models/"
                        # （如 models/MFR/unimernet_hf_small_2503），因此根目录到
                        # PDF-Extract-Kit-1.0 即可，不能再带 /models，否则会拼成 models/models/。
                        "pipeline": str(model_root / "PDF-Extract-Kit-1.0"),
                        "vlm": str(model_root / "MinerU2.5-Pro-2605-1.2B"),
                    },
                    "config_version": "1.3.1",
                },
                ensure_ascii=False,
                indent=4,
            ),
            encoding="utf-8",
        )
        os.environ["MINERU_TOOLS_CONFIG_JSON"] = str(config_path)

    logger.info("📦 本地模型环境已配置 (backend/model)，启动不会联网下载:")
    logger.info(f"   MODEL_PATH               = {os.environ['MODEL_PATH']}")
    logger.info(f"   PADDLEX_HOME             = {os.environ['PADDLEX_HOME']}")
    logger.info(f"   MINERU_TOOLS_CONFIG_JSON = {os.environ.get('MINERU_TOOLS_CONFIG_JSON')}")
    if os.getenv("MODEL_DOWNLOAD_SOURCE", "").lower() != "local":
        logger.warning(
            "⚠️  MODEL_DOWNLOAD_SOURCE 当前不是 'local'，MinerU 可能仍会尝试联网下载；"
            "建议在 backend/.env 中设置 MODEL_DOWNLOAD_SOURCE=local"
        )


class TianshuLauncher:
    """天枢服务启动器"""

    def __init__(
        self,
        output_dir="./mineru_tianshu_output",
        api_port=8000,
        worker_port=8001,
        workers_per_device=1,
        devices="auto",
        accelerator="auto",
        enable_mcp=False,
        mcp_port=8002,
        paddleocr_vl_vllm_engine_enabled=False,  # 新增paddle ocr vllm engine 配置
        paddleocr_vl_vllm_api_list=[],  # 新增paddle ocr vllm engine 配置
        paddleocr_vl_mlx_server_url="http://127.0.0.1:8111/v1",
        paddleocr_vl_mlx_model_path=None,
        auto_start_mlx_server=False,
        mlx_venv_path=None,
        mineru_vllm_api_list=None,  # MinerU VLM 远端 API 列表
    ):
        self.output_dir = output_dir
        self.api_port = api_port
        self.worker_port = worker_port
        self.workers_per_device = workers_per_device
        self.devices = devices
        self.accelerator = accelerator
        self.enable_mcp = enable_mcp
        self.mcp_port = mcp_port
        self.processes = []  # list of dicts: {name, proc, cmd, env, restarts, restartable}
        self.max_restarts = int(os.getenv("SERVICE_MAX_RESTARTS", "5"))
        self._shutting_down = False
        self.paddleocr_vl_vllm_engine_enabled = paddleocr_vl_vllm_engine_enabled
        self.paddleocr_vl_vllm_api_list = paddleocr_vl_vllm_api_list
        self.paddleocr_vl_mlx_server_url = paddleocr_vl_mlx_server_url
        self.paddleocr_vl_mlx_model_path = paddleocr_vl_mlx_model_path or str(
            (Path(__file__).parent / "model" / "paddlex_cache" / "official_models" / "PaddleOCR-VL-1.6-0.9B").resolve()
        )
        self.auto_start_mlx_server = auto_start_mlx_server
        self.mlx_venv_path = mlx_venv_path or str((Path(__file__).parent / ".venv-mlx").resolve())
        self.mineru_vllm_api_list = mineru_vllm_api_list or []

    def _register(self, name, proc, cmd, env, restartable=True):
        """登记子进程及其重启所需的启动规格。"""
        self.processes.append(
            {
                "name": name,
                "proc": proc,
                "cmd": cmd,
                "env": env,
                "restarts": 0,
                "restartable": restartable,
            }
        )

    def check_ocr_models(self):
        """检查并下载所有 OCR 模型（异步，不阻塞启动）"""
        import threading

        # 1. 检查 PaddleOCR-VL 模型
        def check_paddleocr_vl():
            try:
                from paddleocr_vl import PaddleOCRVLEngine

                logger.info("🔍 Checking PaddleOCR-VL...")
                logger.info("   Note: 使用本地模型，不会联网下载")

                # 检查本地模型缓存（PADDLEX_HOME/official_models）
                pdx_home = Path(os.getenv("PADDLEX_HOME", "/root/.paddlex"))
                model_cache_dir = pdx_home / "official_models"

                if model_cache_dir.exists():
                    logger.info(f"✅ PaddleOCR model cache found at: {model_cache_dir}")
                else:
                    logger.warning(f"⚠️  PaddleOCR model cache not found at: {model_cache_dir}")

                if self.accelerator == "cpu":
                    logger.info("   CPU/Apple Silicon mode detected; PaddleOCR-VL tasks will use MLX server backend")
                    logger.info(f"   MLX server URL: {self.paddleocr_vl_mlx_server_url}")
                    return

                # 简单初始化引擎（不触发下载）
                try:
                    PaddleOCRVLEngine()
                    logger.info("✅ PaddleOCR-VL engine initialized successfully")
                except Exception as e:
                    logger.warning(f"⚠️  PaddleOCR-VL initialization failed: {e}")
                    logger.info("   This is normal if GPU is not available or dependencies are missing")

            except ImportError:
                logger.debug("PaddleOCR-VL not installed, skipping check")
            except Exception as e:
                logger.debug(f"PaddleOCR-VL check skipped: {e}")

        # 在后台线程中下载模型
        thread_paddleocr = threading.Thread(target=check_paddleocr_vl, daemon=True)
        thread_paddleocr.start()

    def _mlx_server_host_port(self):
        parsed = urllib.parse.urlparse(self.paddleocr_vl_mlx_server_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 8111
        return host, port

    def _mlx_http_ok(self, url):
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                return 200 <= resp.status < 400
        except urllib.error.HTTPError as e:
            return 200 <= e.code < 400
        except Exception:
            return False

    def _mlx_server_ready(self):
        base_url = self.paddleocr_vl_mlx_server_url.rstrip("/")
        root_url = base_url[:-3] if base_url.endswith("/v1") else base_url
        return self._mlx_http_ok(f"{root_url}/health") or self._mlx_http_ok(f"{base_url}/models")

    def _mlx_port_in_use(self):
        host, port = self._mlx_server_host_port()
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            return False

    def _wait_for_mlx_vlm_server(self, proc, timeout=180):
        deadline = time.time() + timeout

        while time.time() < deadline:
            if proc.poll() is not None:
                logger.error(f"❌ MLX VLM Server exited early (exit={proc.returncode})")
                return False

            if self._mlx_server_ready():
                logger.info("   ✅ MLX VLM Server is ready")
                return True

            time.sleep(2)

        logger.error(f"❌ MLX VLM Server not ready after {timeout}s: {self.paddleocr_vl_mlx_server_url}")
        return False

    def _start_mlx_vlm_server(self):
        model_path = Path(self.paddleocr_vl_mlx_model_path).expanduser().resolve()
        venv_path = Path(self.mlx_venv_path).expanduser().resolve()
        python_bin = venv_path / "bin" / "python"

        if self._mlx_server_ready():
            logger.info(f"   ✅ Reusing existing MLX VLM Server at {self.paddleocr_vl_mlx_server_url}")
            return None

        if self._mlx_port_in_use():
            raise RuntimeError(
                f"Port for MLX VLM Server is already in use but {self.paddleocr_vl_mlx_server_url} is not healthy. "
                "Stop the existing process or choose another --paddleocr-vl-mlx-server-url."
            )

        if not python_bin.exists():
            raise FileNotFoundError(
                f"MLX venv Python not found: {python_bin}. Create it with backend/.venv-mlx and install mlx-vlm."
            )
        if not model_path.exists():
            raise FileNotFoundError(f"PaddleOCR-VL MLX model path not found: {model_path}")

        host, port = self._mlx_server_host_port()
        mlx_env = os.environ.copy()
        mlx_env["MLX_VLM_SERVER_URL"] = self.paddleocr_vl_mlx_server_url
        mlx_env["MLX_VLM_API_MODEL_NAME"] = str(model_path)
        mlx_env.pop("PYTHONPATH", None)

        cmd = [
            str(python_bin),
            "-m",
            "mlx_vlm.server",
            "--host",
            host,
            "--port",
            str(port),
            "--model",
            str(model_path),
            "--trust-remote-code",
        ]

        logger.info(f"🧠 Starting MLX VLM Server on {self.paddleocr_vl_mlx_server_url}")
        logger.info(f"   Python: {python_bin}")
        logger.info(f"   Model:  {model_path}")
        proc = subprocess.Popen(cmd, cwd=Path(__file__).parent, env=mlx_env)
        self._register("MLX VLM Server", proc, cmd, mlx_env)

        if not self._wait_for_mlx_vlm_server(proc):
            raise RuntimeError("MLX VLM Server failed to become ready")

        return proc

    def start_services(self):
        """启动所有服务"""
        logger.info("=" * 70)
        logger.info("🚀 MinerU Tianshu - AI Data Preprocessing Platform")
        logger.info("=" * 70)
        logger.info("天枢 - 企业级 AI 数据预处理平台")
        logger.info("支持文档、图片、音频、视频等多模态数据处理")
        logger.info("")

        try:
            total_services = 3 + (1 if self.auto_start_mlx_server else 0) + (1 if self.enable_mcp else 0)
            service_index = 1

            # 1. 启动 API Server
            logger.info(f"📡 [{service_index}/{total_services}] Starting API Server...")
            env = os.environ.copy()
            env["API_PORT"] = str(self.api_port)
            env["OUTPUT_PATH"] = self.output_dir  # 设置输出目录（与 Worker 保持一致）
            api_cmd = [sys.executable, "api_server.py"]
            api_proc = subprocess.Popen(api_cmd, cwd=Path(__file__).parent, env=env)
            self._register("API Server", api_proc, api_cmd, env)
            time.sleep(3)

            if api_proc.poll() is not None:
                logger.error("❌ API Server failed to start!")
                return False

            logger.info(f"   ✅ API Server started (PID: {api_proc.pid})")
            logger.info(f"   📖 API Docs: http://localhost:{self.api_port}/docs")
            logger.info("")
            service_index += 1

            if self.auto_start_mlx_server:
                logger.info(f"🧠 [{service_index}/{total_services}] Starting MLX VLM Server...")
                self._start_mlx_vlm_server()
                logger.info("")
                service_index += 1

            # 2. 启动 LitServe Worker Pool
            logger.info(f"⚙️  [{service_index}/{total_services}] Starting LitServe Worker Pool...")
            worker_env = os.environ.copy()
            worker_env["WORKER_PORT"] = str(self.worker_port)
            worker_env["OUTPUT_PATH"] = self.output_dir
            worker_env["MLX_VLM_SERVER_URL"] = self.paddleocr_vl_mlx_server_url
            worker_env["MLX_VLM_API_MODEL_NAME"] = str(Path(self.paddleocr_vl_mlx_model_path).expanduser().resolve())

            worker_cmd = [
                sys.executable,
                "litserve_worker.py",
                "--output-dir",
                self.output_dir,
                "--accelerator",
                self.accelerator,
                "--workers-per-device",
                str(self.workers_per_device),
                "--port",
                str(self.worker_port),
                "--devices",
                str(self.devices) if isinstance(self.devices, str) else ",".join(map(str, self.devices)),
            ]

            # 只在启用时才添加 paddleocr-vl-vllm-engine-enabled 参数
            if self.paddleocr_vl_vllm_engine_enabled:
                worker_cmd.extend(["--paddleocr-vl-vllm-engine-enabled"])
            # 添加 paddleocr-vl-vllm-api-list 参数
            worker_cmd.extend(["--paddleocr-vl-vllm-api-list", str(self.paddleocr_vl_vllm_api_list)])
            worker_cmd.extend(["--paddleocr-vl-mlx-server-url", self.paddleocr_vl_mlx_server_url])
            # 透传 MinerU VLM API 列表（此前从未传递，导致 native 模式 vlm-*/hybrid-* 拿不到远端端点）
            if self.mineru_vllm_api_list:
                worker_cmd.extend(["--mineru-vllm-api-list", str(self.mineru_vllm_api_list)])

            worker_proc = subprocess.Popen(worker_cmd, cwd=Path(__file__).parent, env=worker_env)
            self._register("LitServe Workers", worker_proc, worker_cmd, worker_env)
            time.sleep(5)

            if worker_proc.poll() is not None:
                logger.error("❌ LitServe Workers failed to start!")
                return False

            logger.info(f"   ✅ LitServe Workers started (PID: {worker_proc.pid})")
            logger.info(f"   🔌 Worker Port: {self.worker_port}")
            logger.info(f"   👷 Workers per Device: {self.workers_per_device}")
            logger.info("")
            service_index += 1

            # 3. 启动 Task Scheduler
            logger.info(f"🔄 [{service_index}/{total_services}] Starting Task Scheduler...")
            scheduler_cmd = [
                sys.executable,
                "task_scheduler.py",
                "--litserve-url",
                f"http://localhost:{self.worker_port}/predict",
                "--wait-for-workers",
            ]

            scheduler_proc = subprocess.Popen(scheduler_cmd, cwd=Path(__file__).parent)
            self._register("Task Scheduler", scheduler_proc, scheduler_cmd, None)
            time.sleep(3)

            if scheduler_proc.poll() is not None:
                logger.error("❌ Task Scheduler failed to start!")
                return False

            logger.info(f"   ✅ Task Scheduler started (PID: {scheduler_proc.pid})")
            logger.info("")
            service_index += 1

            # 4. 启动 MCP Server（可选）
            if self.enable_mcp:
                logger.info(f"🔌 [{service_index}/{total_services}] Starting MCP Server...")
                mcp_env = os.environ.copy()
                mcp_env["API_BASE_URL"] = f"http://localhost:{self.api_port}"
                mcp_env["MCP_PORT"] = str(self.mcp_port)
                mcp_env["MCP_HOST"] = "0.0.0.0"

                mcp_proc = subprocess.Popen([sys.executable, "mcp_server.py"], cwd=Path(__file__).parent, env=mcp_env)
                self._register("MCP Server", mcp_proc, [sys.executable, "mcp_server.py"], mcp_env)
                time.sleep(3)

                if mcp_proc.poll() is not None:
                    logger.error("❌ MCP Server failed to start!")
                    return False

                logger.info(f"   ✅ MCP Server started (PID: {mcp_proc.pid})")
                logger.info(f"   🌐 MCP Endpoint: http://localhost:{self.mcp_port}/mcp")
                logger.info("")

            # 启动成功
            logger.info("=" * 70)
            logger.info("✅ All Services Started Successfully!")
            logger.info("=" * 70)
            logger.info("")
            logger.info("📚 Quick Start:")
            logger.info(f"   • API Documentation: http://localhost:{self.api_port}/docs")
            logger.info(f"   • Submit Task:       POST http://localhost:{self.api_port}/api/v1/tasks/submit")
            logger.info(f"   • Query Status:      GET  http://localhost:{self.api_port}/api/v1/tasks/{{task_id}}")
            logger.info(f"   • Queue Stats:       GET  http://localhost:{self.api_port}/api/v1/queue/stats")
            if self.enable_mcp:
                logger.info(f"   • MCP Endpoint:      http://localhost:{self.mcp_port}/mcp/sse")
            logger.info("")
            logger.info("🔧 Service Details:")
            for entry in self.processes:
                logger.info(f"   • {entry['name']:20s} PID: {entry['proc'].pid}")
            logger.info("")
            logger.info("⚠️  Press Ctrl+C to stop all services")
            # 所有服务启动完成后，检查并下载所有 OCR 模型
            self.check_ocr_models()

            return True

        except Exception as e:
            logger.error(f"❌ Failed to start services: {e}")
            self.stop_services()
            return False

    def stop_services(self, signum=None, frame=None):
        """停止所有服务"""
        self._shutting_down = True
        logger.info("")
        logger.info("=" * 70)
        logger.info("⏹️  Stopping All Services...")
        logger.info("=" * 70)

        for entry in self.processes:
            name, proc = entry["name"], entry["proc"]
            if proc.poll() is None:  # 进程仍在运行
                logger.info(f"   Stopping {name} (PID: {proc.pid})...")
                proc.terminate()

        # 等待所有进程结束
        for entry in self.processes:
            name, proc = entry["name"], entry["proc"]
            try:
                proc.wait(timeout=10)
                logger.info(f"   ✅ {name} stopped")
            except subprocess.TimeoutExpired:
                logger.warning(f"   ⚠️  {name} did not stop gracefully, forcing...")
                proc.kill()
                proc.wait()

        logger.info("=" * 70)
        logger.info("✅ All Services Stopped")
        logger.info("=" * 70)
        sys.exit(0)

    def _restart_entry(self, entry) -> bool:
        """按指数退避重启单个崩溃的子进程；超过上限返回 False。"""
        if not entry.get("restartable", True):
            return False
        if entry["restarts"] >= self.max_restarts:
            logger.error(f"❌ {entry['name']} 已达最大重启次数 ({self.max_restarts})，放弃重启")
            return False

        entry["restarts"] += 1
        backoff = min(60, 2 ** entry["restarts"])  # 2,4,8,... 上限 60s
        logger.warning(
            f"♻️  Restarting {entry['name']} "
            f"(attempt {entry['restarts']}/{self.max_restarts}, backoff {backoff}s)..."
        )
        time.sleep(backoff)
        try:
            entry["proc"] = subprocess.Popen(entry["cmd"], cwd=Path(__file__).parent, env=entry["env"])
            logger.info(f"   ✅ {entry['name']} restarted (PID: {entry['proc'].pid})")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to restart {entry['name']}: {e}")
            return False

    def wait(self):
        """监控所有子进程；单个崩溃时尝试重启，仅在不可恢复时才整体退出。"""
        try:
            while True:
                time.sleep(1)
                if self._shutting_down:
                    return

                for entry in self.processes:
                    if entry["proc"].poll() is not None:
                        logger.error(f"❌ {entry['name']} unexpectedly stopped (exit={entry['proc'].returncode})!")
                        # 尝试重启该进程；重启失败（达上限/不可重启）才整体停服
                        if not self._restart_entry(entry):
                            logger.error(f"❌ {entry['name']} 无法恢复，停止全部服务")
                            self.stop_services()
                            return

        except KeyboardInterrupt:
            self.stop_services()


def main():
    """主函数"""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        # override=True: 让 .env 成为权威配置，覆盖 shell 中可能残留的同名变量
        # （例如残留的 Docker 路径 DATABASE_PATH=/app/...，本地原生运行会因 /app 只读而崩溃）
        load_dotenv(dotenv_path=env_path, override=True)
        logger.info(f"✅ Loaded .env from: {env_path}")
    else:
        logger.error(f"❌ .env file not found at: {env_path}")
        logger.error("Please create a .env file in the backend directory with required environment variables")
        sys.exit(1)

    # 注入本地模型路径（原生开发：使用 backend/model 下的模型，须在 .env 之后、子进程之前执行）
    configure_local_models()

    # 安全校验：生产模式下若 JWT 密钥缺省/占位符，fail-fast，避免子进程起来后才暴露问题
    try:
        from auth.jwt_handler import validate_secret

        validate_secret()
    except RuntimeError as e:
        logger.error(f"❌ 安全配置校验失败: {e}")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="MinerU Tianshu - 统一启动脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 使用默认配置启动（自动检测GPU）
  python start_all.py

  # 使用CPU模式
  python start_all.py --accelerator cpu

  # 指定输出目录和端口
  python start_all.py --output-dir /data/output --api-port 8080

  # 每个GPU启动2个worker
  python start_all.py --accelerator cuda --workers-per-device 2

  # 只使用指定的GPU
  python start_all.py --accelerator cuda --devices 0,1

  # 启用 MCP Server 支持（用于 AI 助手调用）
  python start_all.py --enable-mcp --mcp-port 8002
        """,
    )

    parser.add_argument(
        "--output-dir",
        type=str,
        default="./mineru_tianshu_output",
        help="输出目录 (默认: ./mineru_tianshu_output)",
    )
    parser.add_argument("--api-port", type=int, default=8000, help="API服务器端口 (默认: 8000)")
    parser.add_argument("--worker-port", type=int, default=8001, help="Worker服务器端口 (默认: 8001)")
    parser.add_argument(
        "--accelerator",
        type=str,
        default="auto",
        choices=["auto", "cuda", "cpu"],
        help="加速器类型 (默认: auto，自动检测)",
    )
    parser.add_argument("--workers-per-device", type=int, default=1, help="每个GPU的worker数量 (默认: 1)")
    parser.add_argument("--devices", type=str, default="auto", help="使用的GPU设备，逗号分隔 (默认: auto，使用所有GPU)")
    parser.add_argument(
        "--enable-mcp", action="store_true", help="启用 MCP Server（支持 Model Context Protocol 远程调用）"
    )
    parser.add_argument("--mcp-port", type=int, default=8002, help="MCP Server 端口 (默认: 8002)")
    # 配置 paddleocr-vl-vllm engine
    parser.add_argument(
        "--paddleocr-vl-vllm-engine-enabled",
        action="store_true",
        default=False,
        help="是否启用 PaddleOCR VL VLLM 引擎 (默认: False)",
    )
    parser.add_argument(
        "--paddleocr-vl-vllm-api-list",
        type=parse_list_arg,
        default=[],
        help='PaddleOCR VL VLLM API 列表（Python list 字面量格式，如: \'["http://0.0.0.0:17300/v1", "http://0.0.0.0:17301/v1"]\'）',
    )
    parser.add_argument(
        "--mineru-vllm-api-list",
        type=parse_list_arg,
        default=[],
        help='MinerU VLM (vlm-*/hybrid-*) 远端 API 列表（Python list 字面量格式）',
    )
    parser.add_argument(
        "--paddleocr-vl-mlx-server-url",
        type=str,
        default="http://127.0.0.1:8111/v1",
        help="PaddleOCR-VL MLX server OpenAI-compatible URL (默认: http://127.0.0.1:8111/v1)",
    )
    parser.add_argument(
        "--paddleocr-vl-mlx-model-path",
        type=str,
        default=str(
            (
                Path(__file__).parent
                / "model"
                / "paddlex_cache"
                / "official_models"
                / "PaddleOCR-VL-1.6-0.9B"
            ).resolve()
        ),
        help="PaddleOCR-VL model path used by auto-started mlx-vlm server",
    )
    parser.add_argument(
        "--auto-start-mlx-server",
        action="store_true",
        default=False,
        help="自动使用 backend/.venv-mlx 拉起 mlx-vlm server",
    )
    parser.add_argument(
        "--mlx-venv-path",
        type=str,
        default=str((Path(__file__).parent / ".venv-mlx").resolve()),
        help="mlx-vlm 独立虚拟环境路径 (默认: backend/.venv-mlx)",
    )

    args = parser.parse_args()

    # 统一为绝对路径：OUTPUT_PATH 会被 resolve() 成绝对路径，若 output_dir 为相对路径，
    # 存入数据库的 result_path 会是相对路径，导致 API 端 relative_to(OUTPUT_DIR) 失配、
    # 预览 PDF 链接丢失。这里提前 resolve，保证 worker 写入与 API 读取路径一致。
    args.output_dir = str(Path(args.output_dir).resolve())

    # 处理 devices 参数
    devices = args.devices
    if devices != "auto":
        try:
            devices = [int(d) for d in devices.split(",")]
        except ValueError:
            logger.warning(f"Invalid devices format: {devices}, using 'auto'")
            devices = "auto"
    if args.paddleocr_vl_vllm_engine_enabled:
        logger.success("start_all 脚本中 PaddleOCR VL VLLM 引擎已设置启用")
        if not args.paddleocr_vl_vllm_api_list:
            logger.error(
                "请配置 --paddleocr-vl-vllm-api-list 参数, 或者移除 --paddleocr-vl-vllm-engine-enabled 来关闭 PaddleOCR VL VLLM 引擎"
            )
            sys.exit(1)
        else:
            logger.success(f"PaddleOCR VL VLLM 引擎，API 列表为: {args.paddleocr_vl_vllm_api_list}")
    else:
        logger.info("start_all 脚本中PaddleOCR VL VLLM 引擎已设置不启用")
    # 并发数解析：MAX_CONCURRENT_TASKS 环境变量（文档化的旧知）优先，未设置时用 CLI --workers-per-device。
    # 解析出单一有效值后透传给 worker，消除「CLI flag 被 worker 端 env 静默覆盖」的旧契约冲突。
    env_concurrency = os.getenv("MAX_CONCURRENT_TASKS")
    effective_workers = int(env_concurrency) if env_concurrency else args.workers_per_device

    # 创建启动器
    launcher = TianshuLauncher(
        output_dir=args.output_dir,
        api_port=args.api_port,
        worker_port=args.worker_port,
        workers_per_device=effective_workers,
        devices=devices,
        accelerator=args.accelerator,
        enable_mcp=args.enable_mcp,
        mcp_port=args.mcp_port,
        paddleocr_vl_vllm_engine_enabled=args.paddleocr_vl_vllm_engine_enabled,
        paddleocr_vl_vllm_api_list=args.paddleocr_vl_vllm_api_list,
        paddleocr_vl_mlx_server_url=args.paddleocr_vl_mlx_server_url,
        paddleocr_vl_mlx_model_path=args.paddleocr_vl_mlx_model_path,
        auto_start_mlx_server=args.auto_start_mlx_server,
        mlx_venv_path=args.mlx_venv_path,
        mineru_vllm_api_list=args.mineru_vllm_api_list,
    )

    # 设置信号处理
    signal.signal(signal.SIGINT, launcher.stop_services)
    signal.signal(signal.SIGTERM, launcher.stop_services)

    # 启动服务
    if launcher.start_services():
        launcher.wait()
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
