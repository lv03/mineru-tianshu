# MinerU 天枢 — 项目文档

MinerU 天枢是一个文档/媒体 → 结构化数据的解析平台，把非结构化文件（PDF、Office、图片、扫描件、音频、视频、生信格式）转换为 AI 可消费的 **Markdown + JSON**，主要用于 RAG / LLM 数据预处理。GPU 加速的任务流水线，配有 Web UI、JWT 鉴权、任务队列与 MCP Server。

- **后端**：Python 3.12，FastAPI + LitServe
- **前端**：Vue 3 + TypeScript + Vite + TailwindCSS + Pinia

## 文档目录

| 文档 | 内容 |
|------|------|
| [01 · 任务队列与处理](./01-任务队列与处理.md) | 三个后台进程、任务生命周期与状态机、SQLite 原子领取 + 可选 Redis 队列、Worker 主动循环、父子任务（大 PDF 拆分）、Scheduler 兜底、任务管理 API、数据表结构 |
| [02 · 文件解析方法](./02-文件解析方法.md) | 各引擎与文件类型对应关系、MinerU/PaddleOCR-VL/SenseVoice/视频/去水印/格式引擎的解析方法与选项、`auto` 模式兜底链、输出归一化 |
| [03 · 模型部署方式](./03-模型部署方式.md) | 模型清单、`download_models.py` 与 `mineru.json`、GPU/CPU/离线三套 Docker 栈、vLLM 服务化与容器互斥切换、Dockerfile 对比、部署脚本、端口/GPU 速查 |
| [04 · 本地开发环境与解析问题修复](./04-本地开发环境与解析问题修复.md) | 原生开发使用 `backend/model` 本地模型、MinerU pipeline 路径拼接修正、双向定位空框（normalizer 选 JSON）、PDF 预览路径、双向定位 bbox [0,1000] 归一化坐标偏移 |

## 三句话理解架构

1. **API Server**（:8000）接收上传、写源文件、向 SQLite 插入 `pending` 任务。
2. **LitServe Worker 池**（:8001）原子领取任务、按 `backend` + 扩展名路由到解析引擎、回写结果；重型 VLM 模型由独立 vLLM 容器服务化。
3. **Task Scheduler** 监控队列、健康检查、重置超时任务、清理过期结果。

> 更详细的开发/运行约定见仓库根目录的 [CLAUDE.md](../CLAUDE.md)。
