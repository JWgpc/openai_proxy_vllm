# OpenAI-Compatible Proxy for vLLM / SGLang + Codex

[中文](#简介) · [English](#overview)

---

## 简介

`proxy_server.py` 是一个**仅标准库**实现的轻量 HTTP 代理，把 **OpenAI 风格的 API 请求**（尤其 **Responses** 与 **Chat Completions**）转发到本机或内网的 **vLLM / SGLang 等 OpenAI 兼容服务**。

设计目标：在 **Cursor / Codex** 使用 `wire_api = "responses"` 与 `base_url` 指向本代理时，尽量解决流式 **SSE 形态、缺 `response.completed`、reasoning / output_text 混用、模型名与上游不一致** 等兼容问题，并可选写**审计日志**（完整 JSON 或仅单行事件）。

**支持端点（POST）**

| 路径 | 行为 |
|------|------|
| `/v1/responses` 或 `/responses` | 转发至 `{UPSTREAM_BASE_URL}/responses` |
| `/v1/chat/completions` 或 `/chat/completions` | 转发至 `{UPSTREAM_BASE_URL}/chat/completions` |

**健康检查（GET）**

| 路径 | 行为 |
|------|------|
| `/healthz` | JSON：`ok`、`upstream`、`aliases` |
| `/v1/models` | 代理到上游 `{UPSTREAM_BASE_URL}/models` |

---

## 快速开始（中文）

### 依赖

- Python **3.9+**（推荐 3.10+）
- 无第三方包，仅标准库

### 启动

```bash
cd openai_proxy_vllm

# 必填：改成你的上游 OpenAI 兼容服务根路径（须以 /v1 结尾的路径前缀由 UPSTREAM_BASE_URL 提供）
export UPSTREAM_BASE_URL="http://127.0.0.1:8000/v1"

# 可选：监听地址与端口（默认 0.0.0.0:8010）
export PROXY_HOST="0.0.0.0"
export PROXY_PORT="8010"

python3 proxy_server.py
```

### Codex / `~/.codex/config.toml` 示例

将 API 指向本代理（注意路径包含 **`/v1`**）：

```toml
wire_api = "responses"
base_url = "http://127.0.0.1:8010/v1"
```

若 Codex 跑在另一台机器上，请把 `127.0.0.1` 换成运行代理机器的内网 IP，且代理需监听 `0.0.0.0`（默认已是）。

### 模型名映射

Codex 可能请求 `gpt-5.4`、`gpt-5.1-codex-mini` 等名称。代理会将 **别名** 替换为上游真实 `model` 字符串；未匹配的 `gpt-*` / 含 `codex` 的名称会落到 `DEFAULT_UPSTREAM_MODEL`。

覆盖别名（JSON 字符串）：

```bash
export DEFAULT_UPSTREAM_MODEL="/your/repo/model-id"
export MODEL_ALIASES='{"gpt-5.4":"/your/repo/model-id","gpt-5.1-codex-mini":"/your/repo/model-id"}'
```

### 审计日志

| 变量 | 含义 |
|------|------|
| `PROXY_LOG_FILE` | 审计文件路径；`-` / `stdout` = 仅 stdout；`off` = 不写文件 |
| `PROXY_AUDIT_FULL` | `true`（默认）写完整请求/上游 body；`false` 只写单行 `EVENT ...`，不存长 JSON |
| `PROXY_LOG_MAX_REQUEST_CHARS` | 请求侧最大字符（`0` = 不截断） |
| `PROXY_LOG_MAX_UPSTREAM_CHARS` | 上游响应最大字符（`0` = 不截断） |
| `PROXY_LOG_STREAM_CAPTURE_BYTES` | 流式场景累积写入审计的上游字节上限（`0` = 依实现不限制） |

### 常见问题（上游 400 / Codex 断流）

| 现象 | 可尝试 |
|------|--------|
| 上游报 `input` 应为 string，实为数组 | `export RESPONSES_INPUT_JSON_STRINGIFY=true` 后重启代理（部分后端误声明 schema） |
| Codex：`stream closed before response.completed` | 保持 `RESPONSES_SSE_COMPLETION_SHIM=true`（默认） |
| 对话区不显示正文，只有 reasoning | `RESPONSES_MIRROR_REASONING_TO_OUTPUT=true`（默认） |
| 极简上游不认多余字段 | `RESPONSES_MINIMAL_COMPAT=true`（会丢失部分字段，慎用） |

---

## 环境变量一览（中文）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `UPSTREAM_BASE_URL` | 脚本内置示例地址 | **请改为你的上游** `http(s)://host:port/v1` |
| `UPSTREAM_API_KEY` | 空 | 当客户端未带 `Authorization` 时转发给上游的 Bearer |
| `PROXY_HOST` | `0.0.0.0` | 监听地址 |
| `PROXY_PORT` | `8010` | 监听端口 |
| `DEFAULT_UPSTREAM_MODEL` | 见源码 | 默认上游模型 id |
| `MODEL_ALIASES` | 内置 JSON | Codex 模型名 → 上游模型 id |
| `PROXY_LOG_FILE` | 脚本目录下 `proxy_audit.log` | 见上文 |
| `PROXY_AUDIT_FULL` | `true` | 完整审计 vs 仅 `EVENT` 单行 |
| `PROXY_LOG_MAX_REQUEST_CHARS` | `0` | `0` 不截断 |
| `PROXY_LOG_MAX_UPSTREAM_CHARS` | `0` | `0` 不截断 |
| `PROXY_LOG_STREAM_CAPTURE_BYTES` | `0` | 流式累积审计上限 |
| `FORCE_RESPONSES_JSON` | `false` | 强制非流式 JSON |
| `RESPONSES_CLIENT_MODE` | `chunked_sse` | `chunked_sse` / `passthrough` 等；非流式集合则走 buffer |
| `RESPONSES_SSE_COMPLETION_SHIM` | `true` | 补齐缺失的 `response.completed` |
| `RESPONSES_OPENAI_ALIGN` | `true` | 响应体与 OpenAI/Codex 期望对齐 |
| `RESPONSES_MIRROR_REASONING_TO_OUTPUT` | `true` | reasoning 通道映射到 output_text（利于界面渲染） |
| `RESPONSES_STREAM_SSE_ALIGN` | `true` | 流式模式下行级 SSE JSON 对齐 |
| `RESPONSES_SSE_CHUNKED_DELIVERY` | `true` | SSE 用 chunked 写给客户端 |
| `RESPONSES_EXPAND_SDK_STYLE_INPUT` | `true` | 展开 SDK 简写 input |
| `RESPONSES_MINIMAL_COMPAT` | `false` | 极简兼容（字段更少） |
| `RESPONSES_INPUT_JSON_STRINGIFY` | `false` | 将 `input` 序列化为 JSON 字符串再转发 |
| `RESPONSES_REASONING_OUTPUT_TEXT_FIX` | `true` | 修正 reasoning 与 output_text 事件混用 |
| `RESPONSES_STRIP_REASONING_OUTPUT` | `true` | 剥离 reasoning 项以贴近仅 message+text 的形态 |
| `RESPONSES_LOG_SSE_TYPES` | `false` | 调试：打印 SSE 事件 type 序列 |

启动时控制台会打印 `PROXY_ROUTE_BUILD`、上游地址、别名与部分关键开关，便于确认是否为新进程。

---

## Overview

`proxy_server.py` is a **stdlib-only** HTTP reverse proxy for **OpenAI-style** clients. It forwards **`/responses`** and **`/chat/completions`** to your **vLLM / SGLang** (or compatible) **`/v1`** server, with optional **SSE shaping** (e.g. synthetic `response.completed`), **reasoning ↔ output_text** fixes, **model aliases** for Codex-style model names, and **audit logging** (full bodies or one-line events).

**POST routes**

| Path | Upstream |
|------|----------|
| `/v1/responses` or `/responses` | `{UPSTREAM_BASE_URL}/responses` |
| `/v1/chat/completions` or `/chat/completions` | `{UPSTREAM_BASE_URL}/chat/completions` |

**GET routes**

| Path | Behavior |
|------|----------|
| `/healthz` | JSON with `ok`, `upstream`, `aliases` |
| `/v1/models` | Proxied to `{UPSTREAM_BASE_URL}/models` |

---

## Quick start (English)

### Requirements

- Python **3.9+** (3.10+ recommended)
- No pip dependencies

### Run

```bash
cd openai_proxy_vllm

export UPSTREAM_BASE_URL="http://127.0.0.1:8000/v1"
export PROXY_HOST="0.0.0.0"
export PROXY_PORT="8010"

python3 proxy_server.py
```

### Codex example (`~/.codex/config.toml`)

```toml
wire_api = "responses"
base_url = "http://127.0.0.1:8010/v1"
```

Use your LAN IP instead of `127.0.0.1` if Codex runs on another machine; bind with `PROXY_HOST=0.0.0.0` (default).

### Model aliases

Codex may send names like `gpt-5.4`. Map them to your real upstream model id:

```bash
export DEFAULT_UPSTREAM_MODEL="/your/repo/model-id"
export MODEL_ALIASES='{"gpt-5.4":"/your/repo/model-id","gpt-5.1-codex-mini":"/your/repo/model-id"}'
```

### Audit log

| Variable | Meaning |
|----------|---------|
| `PROXY_LOG_FILE` | Path; `-` / `stdout` = stdout only; `off` = disable file |
| `PROXY_AUDIT_FULL` | `true` = full bodies; `false` = single-line `EVENT ...` only |
| `PROXY_LOG_MAX_*` | Truncation limits (`0` = no limit) |
| `PROXY_LOG_STREAM_CAPTURE_BYTES` | Cap for streamed upstream capture in audit |

### Troubleshooting

| Issue | Try |
|-------|-----|
| Upstream 400: `input` must be string | `RESPONSES_INPUT_JSON_STRINGIFY=true` |
| Codex: stream closed before `response.completed` | Keep `RESPONSES_SSE_COMPLETION_SHIM=true` |
| Empty assistant text; only reasoning | Keep `RESPONSES_MIRROR_REASONING_TO_OUTPUT=true` |
| Minimal backend rejects extra fields | `RESPONSES_MINIMAL_COMPAT=true` (may drop fields) |

---

## Environment variables (English)

See the Chinese **「环境变量一览」** table above for the full list and defaults—the same variables apply.

---

## License

Repository maintainers may add a `LICENSE` file. This README does not impose one.
