# PaddleOCR-VL 解析引擎（新版本）

基于最新的 PaddleOCR-VL API，支持 109+ 语言的自动识别，无需指定语言参数。

**参考文档**: [PaddleOCR-VL 官方文档](http://www.paddleocr.ai/main/version3.x/pipeline_usage/PaddleOCR-VL.html)

## ⚠️ 重要提示

- **CUDA 本地推理**: `paddleocr-vl` 后端需要 NVIDIA GPU，Compute Capability ≥ 8.5
- **Apple Silicon**: 可使用 `paddleocr-vl-mlx` 后端，通过独立 `mlx-vlm` server 在 Apple Silicon 上执行 VLM 识别
- **推荐 CUDA GPU**: RTX 3090, RTX 4090, A10, A100, H100
- **依赖隔离**: `mlx-vlm >= 0.4.2` 需要 `transformers>=5.1`，与 MinerU 3.2.3 的 `transformers<5.0` 约束冲突，必须安装到独立 venv

## ✨ 特性

### 🎯 核心功能（已启用最佳配置）

- ✅ **文档方向分类**: 自动检测并旋转文档（0°/90°/180°/270°）
- ✅ **文本图像矫正**: 修正拍照导致的变形、透视扭曲
- ✅ **版面区域检测**: 智能识别和排序，保持内容逻辑结构
- ✅ **自动语言识别**: 支持 109+ 语言，无需手动指定
- ✅ **硬件加速**: 支持 NVIDIA GPU 本地推理，也支持 Apple Silicon 通过 MLX server 执行 VLM 识别
- ✅ **单例模式**: 每个进程只加载一次模型，节省资源
- ✅ **PDF 原生支持**: 无需手动转换，直接处理 PDF 多页文档
- ✅ **结构化输出**: 支持 Markdown、JSON 等多种输出格式

### 📊 识别能力

- ✅ **文本识别**: 印刷体、手写体
- ✅ **表格识别**: 复杂表格结构
- ✅ **公式识别**: 数学公式、化学式
- ✅ **图表识别**: 图片、图表说明
- ✅ **混合版面**: 多列、多语言混排

## 📦 安装

### 前置要求

1. **NVIDIA GPU**: Compute Capability ≥ 8.5 (推荐)
2. **CUDA 12.6**: 需要 CUDA 环境
3. **Linux 系统**: Windows 用户请使用 WSL 或 Docker

### 安装步骤

**方式一：一键安装（推荐）**

```bash
# 安装统一的后端依赖
cd backend
pip install -r requirements.txt
```

或使用清华源加速：

```bash
cd backend
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

**方式二：分步安装（如遇依赖冲突）**

详见 [backend/INSTALL.md](../INSTALL.md)

### 验证安装

```bash
# 检查 PaddlePaddle GPU 支持
python -c "import paddle; print('CUDA available:', paddle.device.is_compiled_with_cuda())"

# 检查 GPU 信息
python -c "import paddle; print('GPU count:', paddle.device.cuda.device_count()); print('GPU name:', paddle.device.cuda.get_device_name(0))"

# 验证 PaddleOCR-VL
python -c "from paddleocr import PaddleOCRVL; print('✅ PaddleOCR-VL 安装成功')"
```

### 运行环境检查

```bash
cd backend/paddleocr_vl
python check_environment.py
```

这个脚本会检查:

- ✅ Python 版本
- ✅ PaddlePaddle GPU 支持
- ✅ GPU 可用性和计算能力
- ✅ 依赖包安装情况
- ✅ 模型文件完整性

## Apple Silicon (MLX) 部署

Apple Silicon 使用两个 Python 环境：

- 主环境：运行 MinerU Tianshu、PaddleOCR/PaddleX、LitServe Worker
- MLX 环境：只运行 `mlx-vlm` OpenAI-compatible server

### 1. 创建独立 MLX venv

```bash
cd backend
python3 -m venv .venv-mlx
.venv-mlx/bin/python -m pip install -U pip
.venv-mlx/bin/python -m pip install "mlx-vlm>=0.4.2"
```

不要把 `mlx-vlm` 安装进主 `.venv`，否则会因为 `transformers` 版本约束影响 MinerU。

### 2. 准备模型

默认使用仓库内模型路径：

```bash
backend/model/paddlex_cache/official_models/PaddleOCR-VL-1.6-0.9B
```

也可以通过 `--paddleocr-vl-mlx-model-path` 指向其他本地模型目录。

### 3. 启动方式

推荐由 `start_all.py` 自动托管 MLX server：

```bash
cd backend
python start_all.py \
  --accelerator cpu \
  --auto-start-mlx-server \
  --paddleocr-vl-mlx-server-url http://127.0.0.1:8111/v1
```

也可以手动启动 server：

```bash
cd backend
.venv-mlx/bin/python -m mlx_vlm.server \
  --host 127.0.0.1 \
  --port 8111 \
  --model model/paddlex_cache/official_models/PaddleOCR-VL-1.6-0.9B \
  --trust-remote-code
```

然后启动主服务：

```bash
python start_all.py \
  --accelerator cpu \
  --paddleocr-vl-mlx-server-url http://127.0.0.1:8111/v1
```

提交任务时指定：

```bash
curl -X POST http://localhost:8000/api/v1/tasks/submit \
  -F "file=@document.pdf" \
  -F "backend=paddleocr-vl-mlx"
```

## 🚀 使用

### API 调用

提交任务时指定 `backend` 参数为 `paddleocr-vl`:

```bash
curl -X POST http://localhost:8000/api/v1/tasks/submit \
  -F "file=@document.pdf" \
  -F "backend=paddleocr-vl"
```

### 参数说明

| 参数 | 说明 | 可选值 | 默认值 |
|------|------|--------|--------|
| `backend` | 解析引擎 | `pipeline` / `paddleocr-vl` / `paddleocr-vl-mlx` | `pipeline` |

**注意**: PaddleOCR-VL 新版本会自动识别语言，无需指定 `paddleocr_lang` 参数。CUDA 本地模式可由 PaddleOCR 自动管理模型缓存；MLX 模式使用 `--paddleocr-vl-mlx-model-path` 指定本地模型目录。

## 🌍 支持的语言

PaddleOCR-VL 新版本支持 **109+ 种语言的自动识别**，包括但不限于：

- **中文**: 简体、繁体
- **英文**: English
- **日文**: 日本語
- **韩文**: 한국어
- **欧洲语言**: 法文、德文、西班牙文、意大利文、俄文等
- **东南亚语言**: 泰文、越南文、印尼文等
- **中东语言**: 阿拉伯文、希伯来文等
- **印度语言**: 印地文、泰米尔文等

**关键优势**:

- ✅ **无需指定语言**: 模型会自动检测文档中的语言
- ✅ **混合语言支持**: 可以识别包含多种语言的文档
- ✅ **高准确率**: 基于最新的视觉-语言大模型

**模型管理说明：**

- **CUDA 本地模式**: 模型可由 PaddleOCR 自动下载和管理，无需手动操作
- **CUDA 缓存位置**: `~/.paddleocr/models/` 目录（由 PaddleOCR 自动创建）
- **MLX 模式**: 使用 `backend/model/paddlex_cache/official_models/PaddleOCR-VL-1.6-0.9B`，可通过 `--paddleocr-vl-mlx-model-path` 覆盖
- **首次使用**: 自动下载模型（约 2GB）
- **下载源**: 从 Hugging Face 或 ModelScope 自动下载
- **加载时机**: 首次使用时自动下载并加载到内存

**使用示例：**

```bash
# 使用 PaddleOCR-VL（模型自动管理）
curl -X POST http://localhost:8000/api/v1/tasks/submit \
  -F "file=@document.pdf" \
  -F "backend=paddleocr-vl"
```

## 📤 输出格式

PaddleOCR-VL 解析完成后会生成以下文件:

```
output/
├── filename.md          # 标准 Markdown 文件 (主要输出)
├── result.json          # 原始 OCR 结果 (JSON 格式)
└── images/              # PDF 转换的图像（如果输入是 PDF）
    ├── filename_page1.png
    ├── filename_page2.png
    └── ...
```

### 主要输出文件

- **`filename.md`**: 标准 Markdown 文件
  - ✅ 可以直接用任何 Markdown 工具打开
  - ✅ 包含识别的文本内容，按页面组织
  - ✅ 格式统一，与其他 Backend 兼容

- **`result.json`**: 原始 OCR 结果
  - 包含文本、位置坐标、置信度等详细信息
  - 可用于精确定位和后处理

### 与其他 Backend 的统一性

所有 Backend 现在都输出标准 `.md` 文件:

| Backend | 输出文件 | 格式 | 特点 |
|---------|---------|------|------|
| MinerU | `filename.md` | 标准 Markdown | 完整文档解析 |
| PaddleOCR-VL | `filename.md` | 标准 Markdown | 多语言支持 |

## 🆚 Backend 对比

| Backend | 引擎 | 特点 | 适用场景 | GPU 需求 |
|---------|------|------|----------|----------|
| `pipeline` | MinerU | 完整文档解析，支持表格、公式 | 通用文档 | 建议使用 |
| `paddleocr-vl` | PaddleOCR-VL | 100+ 语言，CUDA 本地推理 | 多语言文档 | **必须使用 NVIDIA GPU** |
| `paddleocr-vl-mlx` | PaddleOCR-VL + MLX | Apple Silicon 原生 VLM 识别 | Mac 原生开发、多语言文档 | **Apple Silicon** |

### 选择建议

- **多语言文档识别（NVIDIA GPU）** → 选择 `paddleocr-vl`
- **多语言文档识别（Apple Silicon）** → 选择 `paddleocr-vl-mlx`
- **完整文档解析** → 选择 `pipeline`（MinerU，需要 GPU）

## 🎯 性能对比

| Backend | CPU 支持 | GPU 加速 | 多语言 | 表格识别 | 公式识别 |
|---------|---------|---------|--------|---------|---------|
| MinerU | ❌ | ✅ | ✅ | ✅ | ✅ |
| PaddleOCR-VL | ❌ | ✅ NVIDIA GPU | ✅✅ (109+) | ✅ | ✅ |
| PaddleOCR-VL-MLX | ✅ CPU 版面检测 + MLX VLM server | ✅ Apple Silicon MLX/ANE | ✅✅ (109+) | ✅ | ✅ |

## 💡 示例

### Python 客户端

```
import aiohttp

async with aiohttp.ClientSession() as session:
    data = aiohttp.FormData()
    data.add_field('file', open('document.pdf', 'rb'))
    data.add_field('backend', 'paddleocr-vl')
    data.add_field('paddleocr_lang', 'ch')

    async with session.post(
        'http://localhost:8000/api/v1/tasks/submit',
        data=data
    ) as resp:
        result = await resp.json()
        print(result)
```

### cURL

```
# 中文文档
curl -X POST http://localhost:8000/api/v1/tasks/submit \
  -F "file=@chinese_doc.pdf" \
  -F "backend=paddleocr-vl" \
  -F "paddleocr_lang=ch"

# 英文文档
curl -X POST http://localhost:8000/api/v1/tasks/submit \
  -F "file=@english_doc.pdf" \
  -F "backend=paddleocr-vl" \
  -F "paddleocr_lang=en"

# 日文文档
curl -X POST http://localhost:8000/api/v1/tasks/submit \
  -F "file=@japanese_doc.pdf" \
  -F "backend=paddleocr-vl" \
  -F "paddleocr_lang=japan"
```

## 🔧 高级配置

### GPU 配置

如果安装了 GPU 版本，PaddleOCR 会自动使用 GPU 加速。

检查 GPU 是否可用：

```bash
python -c "import paddle; print(paddle.device.is_compiled_with_cuda())"
```

### 模型缓存

CUDA 本地模式下，PaddleOCR-VL 会自动管理模型缓存：

- **默认位置**: `~/.paddleocr/models/`
- **自动下载**: 首次使用时自动下载
- **缓存复用**: 后续使用直接从缓存加载
- **MLX 模式**: 不使用该自动缓存路径，解析调用的模型由 `MLX_VLM_API_MODEL_NAME` 或 `--paddleocr-vl-mlx-model-path` 指定

## 📝 注意事项

1. **首次使用**: CUDA 本地模式首次使用时会自动下载模型文件，请耐心等待并确保网络畅通
2. **模型管理**: CUDA 本地模式使用 PaddleOCR 缓存；MLX 模式使用显式本地模型路径
3. **硬件需求**: `paddleocr-vl` CUDA 本地推理需要 NVIDIA GPU；Apple Silicon 原生开发请使用 `paddleocr-vl-mlx` 或在 CPU 模式下选择 `paddleocr-vl` 自动路由到 MLX
4. **显存占用**: GPU 模式需要足够的显存
5. **识别精度**: 对于复杂版面，建议使用 `pipeline`

## 🐛 故障排查

### 问题 1: 模型下载失败

**原因**: PaddleOCR-VL 会自动从 Hugging Face 或 ModelScope 下载模型

**解决方案:**

1. 确保网络连接正常，可以访问 Hugging Face 或 ModelScope
2. 如果网络受限，可以配置代理环境变量：

```bash
export http_proxy=http://your-proxy:port
export https_proxy=http://your-proxy:port
```

3. 等待模型自动下载完成（首次使用需要一些时间，取决于网络速度）

### 问题 2: GPU 不可用

**检查:**

```bash
python -c "import paddle; print(paddle.device.is_compiled_with_cuda())"
```

**解决方案:**

1. 确保安装了 NVIDIA 驱动和 CUDA
2. 安装 GPU 版本的 PaddlePaddle：

```bash
pip uninstall paddlepaddle paddlepaddle-gpu
pip install paddlepaddle-gpu==3.2.0 -i https://www.paddlepaddle.org.cn/packages/stable/cu126/
```

3. 验证安装：

```bash
python -c "import paddle; print(paddle.device.is_compiled_with_cuda())"
```

### 问题 3: 显存不足

**现象**: 处理大图像时出现 CUDA out of memory 错误

**解决方案:**

1. 降低输入图像的分辨率
2. 关闭其他占用 GPU 的程序
3. 使用更小的 batch size（如果支持）

### 问题 4: OCR 结果不准确

**现象**: 识别的文字与图像内容不符

**解决方案:**

1. 检查图像质量，确保清晰度足够
2. 对于倾斜的文本，可以先进行图像矫正
3. 对于复杂版面，考虑使用其他 OCR 引擎

## 📚 参考资料

- [PaddleOCR 官方文档](https://github.com/PaddlePaddle/PaddleOCR)
- [PaddlePaddle 官网](https://www.paddlepaddle.org.cn/)
- [Hugging Face 模型页面](https://huggingface.co/paddlepaddle)
- [ModelScope 模型页面](https://modelscope.cn/models/paddlepaddle)
