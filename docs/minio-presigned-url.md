# MinIO/S3 Presigned URL（SigV4 预签名链接）详解

> 背景：天枢的任务图片存储桶为**私有**桶。API 在返回任务结果时，对 Markdown/JSON 中的
> `minio://bucket/object` 引用**读时现签**一条临时 presigned URL 返回给前端；浏览器凭该 URL
> 直接访问 MinIO，无需任何额外登录/凭据。这是「读时签名、短 TTL、不把永久公开 URL 戳进存储」
> 设计的落地形式。相关实现见 `backend/storage/minio_client.py`（`presign_reference`、`_presign_client`）
> 与 `backend/api_server.py`（`presign_minio_text` / `presign_minio_json`）。

## 示例链接

```
http://localhost:9000/mineru-tianshu/20260611/ccPVz_DZaO.jpg
  ?X-Amz-Algorithm=AWS4-HMAC-SHA256
  &X-Amz-Credential=minioadmin%2F20260611%2Fus-east-1%2Fs3%2Faws4_request
  &X-Amz-Date=20260611T015806Z
  &X-Amz-Expires=3600
  &X-Amz-SignedHeaders=host
  &X-Amz-Signature=8b2c80d4a8b638d363ff11dfecd8b9f838bcf121950d1b2e9e2c1e207646ff48
```

结构：

```
http://localhost:9000 / mineru-tianshu / 20260611/ccPVz_DZaO.jpg ? <查询参数...>
└── endpoint ──────┘   └── bucket ───┘  └──── object key ──────┘
```

## 基础部分

| 段 | 值 | 含义 |
|---|---|---|
| scheme + host | `http://localhost:9000` | MinIO 服务地址。来自 `MINIO_PUBLIC_URL`，由 `_presign_client` 用它签名，保证 host 是浏览器可达的地址（而非内网 `minio:9000`）。 |
| bucket | `mineru-tianshu` | 存储桶名（`MINIO_BUCKET`）。 |
| object key | `20260611/ccPVz_DZaO.jpg` | 对象键。`20260611` 是上传日期前缀；`ccPVz_DZaO` 是 `_generate_short_filename` 生成的「毫秒时间戳后 5 位 + 4 位随机」短名。 |

## 查询参数（AWS Signature V4 签名信息）

| 参数 | 示例值 | 含义 |
|---|---|---|
| `X-Amz-Algorithm` | `AWS4-HMAC-SHA256` | 签名算法：SigV4，使用 HMAC-SHA256。 |
| `X-Amz-Credential` | `minioadmin/20260611/us-east-1/s3/aws4_request` | **凭据范围（credential scope）**，5 段：`<access_key>/<日期YYYYMMDD>/<region>/<service>/aws4_request`，即「哪个 key、哪天、哪个区域、哪个服务」。 |
| `X-Amz-Date` | `20260611T015806Z` | 签名生成的 UTC 时间戳（ISO8601 basic 格式）。有效期从这一刻起算。 |
| `X-Amz-Expires` | `3600` | 有效期（秒）= 1 小时。来自 `MINIO_PRESIGN_TTL`（默认 3600）。超过 `X-Amz-Date + Expires` 后链接失效，返回 403。 |
| `X-Amz-SignedHeaders` | `host` | 参与签名计算的请求头列表。仅签 `host`，表示请求时 Host 头必须与签名时一致（防止换 host 重放）。 |
| `X-Amz-Signature` | `8b2c80d4...46ff48` | 最终签名（HMAC-SHA256 十六进制）。MinIO 用同样的密钥+参数重算，比对一致才放行。 |

## 工作原理

1. 后端用 **secret key**（**不出现在 URL 里**）对「HTTP 方法 + object key + 上述查询参数 + SignedHeaders」做 SigV4 计算，得出 `X-Amz-Signature`。
2. 浏览器拿着整条 URL 直接 `GET` MinIO，**不需要任何额外凭据/登录**——签名本身就是临时授权。
3. MinIO 用自己存的 secret key 重算签名比对 + 检查是否过期 + Host 是否匹配；通过则返回图片。
4. 1 小时后签名过期，旧链接失效。**任务图片桶是私有的，只有 API 现签的短期链接能访问。**

> 整条链 = `公开地址 + 桶 + 对象键 + (算法 / 凭据范围 / 时间 / 有效期 / 签名头 / 签名)`，
> 缺一不可、改一个字符签名就对不上。

## 行为特性与权衡

- **每次打开都会现签一条新链接**：前端每次 `GET /api/v1/tasks/{task_id}`，后端实时对 `minio://`
  引用现签，`X-Amz-Date` / `X-Amz-Signature` 都会变，旧链接到点自动失效。
- **副作用——跨会话缓存几乎失效**：URL 每次都不同，浏览器会当成新资源重新下载。同一次页面打开内
  的多张图是同一批 URL，不受影响；影响的是「反复打开同一任务」的重复下载。
- **调优**：`MINIO_PRESIGN_TTL` 调大（如 `86400`=1 天）可减少链接频繁失效的体感，但泄露后的有效
  窗口也更长——这是安全与便利的权衡。文档图片通常不大、访问频率低，默认 1h 一般足够。

## 安全提醒

- `X-Amz-Credential` 里的 access_key 若为 `minioadmin` 等弱口令，仅适合本机开发。
- 代码已移除 `minioadmin/minioadmin` 硬编码回退；当 `ENV=production` 或 `REQUIRE_SECRETS=true`
  时，弱口令会在 `MinIOClient` 初始化处 **fail-fast** 拒绝启动。上线前务必改为强随机凭据
  （例：`openssl rand -hex 16`）。
- presigned URL 只授予**临时只读**访问；而持有 secret key 等于对象存储管理员权限，切勿泄露。
