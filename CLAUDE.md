# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

MinerU Tianshu (天枢) is a document/media → structured-data parsing platform. It converts unstructured files
(PDF, Word/Excel/PPT, images, scans, audio, video, bioinformatics formats) into AI-consumable **Markdown + JSON**,
primarily for RAG / LLM ingestion. It is a GPU-accelerated job pipeline with a web UI, JWT auth, a task queue,
and an MCP server — not a library.

- **Backend**: Python 3.12 (exactly — `pyproject.toml` pins `requires-python == "3.12"`), FastAPI + LitServe.
- **Frontend**: Vue 3 + TypeScript + Vite + TailwindCSS + Pinia.
- Comments and logs are mostly in Chinese; match that style when editing existing files.

## Running locally (native, no Docker — the common dev path on Mac)

```bash
# Backend: starts API (8000) + LitServe Worker pool (8001) + Task Scheduler in one process group
cd backend && python start_all.py          # requires backend/.env to exist (see "Environment" below)
#   useful flags: --api-port --worker-port --accelerator cpu|cuda --devices 0,1
#                 --workers-per-device N --output-dir <abs> --enable-mcp --mcp-port 8002

# Frontend: dev server on :3000, proxies /api -> http://localhost:8000 (vite.config.ts)
cd frontend && npm install && npm run dev
```

Default login is created on first DB init: `admin` / `admin123`.

## Running with Docker

```bash
# GPU (Linux, requires NVIDIA Container Toolkit) — main docker-compose.yml
make setup        # copy .env, build, start    | make start | make stop | make logs
docker compose up -d

# Mac Apple Silicon CPU-only — separate stack, uses Rosetta (linux/amd64) + RustFS instead of MinIO
bash scripts/build-local-cpu.sh
bash scripts/start-local-cpu.sh   # docker-compose.cpu.yml + .env.cpu
```

## Lint / build / models / tests

```bash
ruff check .            # config in pyproject.toml: line-length 120, rules E,F (ignores E402, E501)
ruff format .
cd frontend && npm run build     # tsc && vite build, output to dist/
python backend/download_models.py --output ./models   # ~15GB; also generates models/mineru.json
```

There is **no test suite** in this repo (no pytest/conftest). Verify changes by running the services and
submitting a task through the UI or `POST /api/v1/tasks/submit`.

## Architecture: how a task flows

The three backend processes are launched together by `backend/start_all.py`:

1. **API Server** (`api_server.py`, FastAPI :8000) — receives an upload, writes the raw file to `UPLOAD_DIR`
   (local `input/`), inserts a row into the SQLite `tasks` table with `status='pending'`, and serves results.
2. **LitServe Worker pool** (`litserve_worker.py` :8001) — does the actual GPU parsing. It **atomically claims**
   the next pending task from SQLite (`task_db.py`: `BEGIN IMMEDIATE` + `UPDATE … SET status='processing'
   WHERE status='pending'`), which is what makes concurrent multi-worker safe with no double-processing.
3. **Task Scheduler** (`task_scheduler.py`) — polls worker health, nudges the worker's `/predict` endpoint,
   resets stale `processing` tasks back to `pending` after a timeout, and cleans old output files.

Optionally, **Redis queue** (`redis_queue.py`, gated by `REDIS_QUEUE_ENABLED`) replaces SQLite polling to avoid
the single-writer SQLite lock bottleneck under concurrency.

### Engine routing (the heart of the worker)

In `litserve_worker.py` the `predict` path dispatches by the requested `backend` option and the file extension:

- Office formats (`.docx/.xlsx/.pptx/.doc/.xls/.ppt`) → optionally converted to PDF via LibreOffice first.
- Large PDFs → split into **child tasks** (parent/child task system, `PDF_SPLIT_*` env) processed in parallel
  and merged back, preserving page order.
- PDF + `remove_watermark` → YOLO11x + LaMa preprocessing pass.
- `backend` selects the engine: `sensevoice` (audio), `video`, `paddleocr-vl` / `paddleocr-vl-vllm` (multilingual
  OCR), `pipeline`/`auto`/`vlm-*` (MinerU), MarkItDown fallback, and pluggable `format_engines/` (FASTA/GenBank).

The heavy VLM models (PaddleOCR-VL, MinerU 2.5 VLM) run as **separate vLLM containers** in the GPU compose; the
worker reaches them over an API list (`--paddleocr-vl-vllm-api-list`, `--mineru-vllm-api-list`). In CPU/native mode
these are absent and parsing uses the MinerU **pipeline** engine on CPU (slow).

### Output normalization & object storage

After parsing, `output_normalizer/` standardizes every engine's result into `result.md` + `result.json` +
an `images/` dir. If there are images **and** object storage is enabled, it uploads **only the extracted images**
to MinIO and rewrites the image links in the Markdown/JSON to public URLs. Source files and the Markdown/JSON text
themselves stay on local disk — MinIO is an image host, not document storage.

Storage layer (`storage/`): `storage/__init__.py` aliases the legacy `RustFSClient` to `MinIOClient`.
`minio_client.py` reads `MINIO_*` env first, then falls back to `RUSTFS_*`, and **raises if `MINIO_PUBLIC_URL`
is unset**. It auto-creates the bucket (`_ensure_bucket`) on first client init.

## Environment & config gotchas (these cause most setup failures)

- `start_all.py` loads **`backend/.env`** via `load_dotenv(override=True)` — the .env is authoritative and
  overrides any same-named shell variable. Only keys present in the file are overridden, so to neutralize a stale
  shell var you must set that key explicitly in `backend/.env`.
- There are **three** env templates with different conventions — do not mix them:
  - root `.env.example` → Docker/GPU, container paths like `/app/data/db`, `MINIO_ENDPOINT=minio:9000`.
  - `backend/.env.example` → leaner, host-oriented (`MINIO_ENDPOINT=localhost:9000`).
  - `.env.cpu` → Mac CPU Docker stack, uses `RUSTFS_*` and `MODEL_DOWNLOAD_SOURCE=local` + `HF_OFFLINE=1`.
  Container paths (`/app/...`) in a native run cause `Read-only file system: '/app'` crashes — use absolute host
  paths for `DATABASE_PATH` / `OUTPUT_PATH` natively, or leave them unset to fall back to `<repo>/data/...`.
- `start_all.py` forces `OUTPUT_PATH = self.output_dir` (default `/tmp/mineru_tianshu_output`, ephemeral) for both
  API and Worker, overriding any `OUTPUT_PATH` in `.env`. Pass `--output-dir <abs>` to persist outputs.
- Object storage is off unless `MINIO_ENABLED=true` **and** `MINIO_PUBLIC_URL` is set; otherwise images fall back
  to the built-in local file service (`/api/v1/files/...`).
- Model behavior depends on `MODEL_DOWNLOAD_SOURCE` (`auto`/`modelscope`/`huggingface`/`local`) and `HF_OFFLINE`;
  MinerU 3.0 reads `models/mineru.json` (nested `{"models-dir": {"pipeline","vlm"}}`, `config_version` 1.3.1)
  generated by `download_models.py`.

## Ports

API 8000 (`/docs`) · Worker 8001 · MCP 8002 · Frontend dev 3000 / Docker 80 · MinIO 9000 (console 9001) · Redis 6379.
