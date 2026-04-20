#!/usr/bin/env python3
import http.client
import json
import os
import re
import socket
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

# 修改路由逻辑时请 bump，启动日志会打印，便于确认跑的是否为新进程
PROXY_ROUTE_BUILD = "route-v22-audit-minimal-switch"

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# 默认写到脚本同目录；设 PROXY_LOG_FILE=- 仅 stdout；PROXY_LOG_FILE=off 关闭审计文件（仍 print）
_RAW_AUDIT = os.getenv("PROXY_LOG_FILE", os.path.join(_SCRIPT_DIR, "proxy_audit.log")).strip()
PROXY_LOG_FILE = (
    ""
    if _RAW_AUDIT.lower() in ("off", "disable", "false", "0")
    else (_RAW_AUDIT if _RAW_AUDIT.lower() not in ("-", "stdout") else "-")
)
# 审计全文：0 = 不截断（完整写入）。磁盘/内存紧张时再设正整数上限（字符或字节）。
PROXY_LOG_MAX_REQUEST_CHARS = int(os.getenv("PROXY_LOG_MAX_REQUEST_CHARS", "0"))
PROXY_LOG_MAX_UPSTREAM_CHARS = int(os.getenv("PROXY_LOG_MAX_UPSTREAM_CHARS", "0"))
PROXY_LOG_STREAM_CAPTURE_BYTES = int(os.getenv("PROXY_LOG_STREAM_CAPTURE_BYTES", "0"))
_audit_lock = threading.Lock()

UPSTREAM_BASE = os.getenv("UPSTREAM_BASE_URL", "http://10.253.63.178:8005/v1").rstrip("/")
# 仅 127.0.0.1 时，Codex 配 http://<局域网IP>:8010/v1 会连不上（connection refused → hyper 报 error sending request）。
# 默认 0.0.0.0 同时接受本机 127.0.0.1 与内网 IP；若只要本机可设 PROXY_HOST=127.0.0.1。
LISTEN_HOST = os.getenv("PROXY_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("PROXY_PORT", "8010"))

# Codex + 本地 vLLM：流式 SSE 事件格式常与官方不一致，界面不显示正文。
# 默认不强制 stream=false（避免 Codex 报 response.completed 缺失）。
def _env_truthy(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes", "on")


# true（默认）：审计文件写入完整请求/上游 body。false：只写单行 EVENT（状态码、字节数等），不写长 JSON。
PROXY_AUDIT_FULL = _env_truthy("PROXY_AUDIT_FULL", "true")

FORCE_RESPONSES_JSON = _env_truthy("FORCE_RESPONSES_JSON", "false")

# 部分 vLLM/SGLang 的 Responses SSE 未发送 response.completed，Codex 会报「stream closed before response.completed」。
# 在完整缓冲上游 body 后检测并追加一条合成事件（默认开启；调试可设 RESPONSES_SSE_COMPLETION_SHIM=false）。
RESPONSES_SSE_COMPLETION_SHIM = _env_truthy("RESPONSES_SSE_COMPLETION_SHIM", "true")

# 与官方 Responses 非流式样例对齐：assistant message 带 phase=final_answer，output_text 带 annotations（Codex 依赖）。
RESPONSES_OPENAI_ALIGN = _env_truthy("RESPONSES_OPENAI_ALIGN", "true")

# 部分本地后端只发 reasoning 通道的 SSE（response.reasoning_text.delta），Codex 只渲染 response.output_text.delta → 正文空白。
# 在 OPENAI_ALIGN 开启时，将 reasoning 增量事件与 content part 的 reasoning_text 映射为 output_text（可设 false 关闭）。
RESPONSES_MIRROR_REASONING_TO_OUTPUT = _env_truthy("RESPONSES_MIRROR_REASONING_TO_OUTPUT", "true")

# SSE 的 event: 行与 JSON data 里的 type 需同时改写，否则客户端只认其一仍会丢字。
_SSE_REASONING_DELTA_TYPES = frozenset(
    (
        "response.reasoning_text.delta",
        "response.reasoning_summary_text.delta",
        "response.reasoning_text.done",
        "response.reasoning_summary_text.done",
    )
)

_SSE_REASONING_TO_OUTPUT_EVENT_TYPES = frozenset(
    (
        "response.output_item.added",
        "response.output_item.done",
        "response.content_part.added",
        "response.content_part.done",
        "response.output_text.delta",
        "response.output_text.done",
    )
).union(_SSE_REASONING_DELTA_TYPES)

# 调试：缓冲模式下扫描上游 SSE，打印每条 data JSON 的 type 出现顺序（不打印 delta 正文）。
RESPONSES_LOG_SSE_TYPES = _env_truthy("RESPONSES_LOG_SSE_TYPES", "false")

# 与 model_use.py / 官方 SDK 简写一致：input 里 {role, content: "..."} → type=message + content 数组（input_text / output_text）
RESPONSES_EXPAND_SDK_STYLE_INPUT = _env_truthy("RESPONSES_EXPAND_SDK_STYLE_INPUT", "true")

# Codex / 官方 SDK 会带 tools、conversation、include、reasoning、metadata 等；此前仅透传 5 个字段会把这些全丢掉，导致工具/会话/流式异常。
# 默认完整透传（仍经 _sanitize_payload 归一化）。仅当 dumb 上游不认多余字段时设 RESPONSES_MINIMAL_COMPAT=true。
RESPONSES_MINIMAL_COMPAT = _env_truthy("RESPONSES_MINIMAL_COMPAT", "false")

# 上游若错误地把 body.input 声明成 str，结构化数组会 400；可设 true 把 input 序列化为 JSON 字符串再转发（部分实现会在服务端再 json.loads）。
# 若上游正确支持 OpenAI Responses，应保持 false。
RESPONSES_INPUT_JSON_STRINGIFY = _env_truthy("RESPONSES_INPUT_JSON_STRINGIFY", "false")

# 上游若对 type=reasoning 的 output_item 仍发 response.output_text.delta，Codex 可能不渲染；改为 reasoning_text.delta/done（与官方语义一致）。
RESPONSES_REASONING_OUTPUT_TEXT_FIX = _env_truthy("RESPONSES_REASONING_OUTPUT_TEXT_FIX", "true")

# 与仅含 message+output_text 的 OpenAI 回答对齐：丢弃 reasoning 相关 SSE 与最终 output 中的 reasoning 项（model_use_ssh 里第一项）。
RESPONSES_STRIP_REASONING_OUTPUT = _env_truthy("RESPONSES_STRIP_REASONING_OUTPUT", "true")

# 仅当未剥离 reasoning 时，才对误标 delta 做 reasoning_text 修正；剥离时事件直接丢弃。
_REASONING_AUX_SSE_TYPES = frozenset(
    (
        "response.reasoning_part.added",
        "response.reasoning_part.done",
    )
)

# beecode/OpenAI 官方 SSE 通常为 HTTP/1.1 + Transfer-Encoding: chunked，并带 Cache-Control。
# 本代理 buffer 模式若用 HTTP/1.0 + Content-Length 一次性写满 body，Codex（hyper）里可能出现「请求成功但对话区不渲染」。
# 默认对 SSE 走 chunked；若与其它客户端不兼容可设 RESPONSES_SSE_CHUNKED_DELIVERY=false。
RESPONSES_SSE_CHUNKED_DELIVERY = _env_truthy("RESPONSES_SSE_CHUNKED_DELIVERY", "true")

# 流式透传时从首包解析 response id（用于合成 response.completed）
_RESP_ID_IN_PREAMBLE = re.compile(rb'"id"\s*:\s*"(resp_[^"]+)"')
_RESPONSE_COMPLETED_TYPE = re.compile(rb'"type"\s*:\s*"response\.completed"')

# Codex 为流式消费 Responses SSE：默认 chunked_sse（边收上游边写客户端，贴近 beecode）。
# 若断流、缺 response.completed 或需整包改写字段，可设 RESPONSES_CLIENT_MODE=buffer。
_RESPONSES_STREAM_MODES = frozenset(("chunked_sse", "chunked", "stream", "sse", "passthrough"))

# 流式模式下按行做 SSE JSON 对齐（与 buffer 一致）；关则字节透传（略快，上游形状需自洽）。
RESPONSES_STREAM_SSE_ALIGN = _env_truthy("RESPONSES_STREAM_SSE_ALIGN", "true")


def _responses_client_mode() -> str:
    return os.getenv("RESPONSES_CLIENT_MODE", "chunked_sse").lower().strip()


def _use_responses_buffer_delivery() -> bool:
    return _responses_client_mode() not in _RESPONSES_STREAM_MODES


def _audit_truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [审计截断: 共 {len(text)} 字符, 仅记录前 {max_chars} 字]\n"


def _audit_flatten_message_content(content: object) -> str:
    """从 Responses message 的 content（字符串或 part 数组）抽出可读文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                t = part.get("text")
                if isinstance(t, str) and t:
                    parts.append(t)
            elif isinstance(part, str):
                parts.append(part)
    return "\n".join(parts)


def _audit_request_summary(payload: object) -> str:
    """便于在日志里快速看到「输入」摘要，无需在几万行 JSON 里翻。"""
    if not isinstance(payload, dict):
        return f"(非 dict 请求体 type={type(payload).__name__})"
    lines: list[str] = []
    lines.append(f"model = {payload.get('model')!r}")
    lines.append(f"stream = {payload.get('stream', True)!r}")
    tools = payload.get("tools")
    if isinstance(tools, list):
        lines.append(f"tools 数量 = {len(tools)}")
    inp = payload.get("input")
    if isinstance(inp, list):
        lines.append(f"input 数组长度 = {len(inp)}")
        last_user = ""
        for item in reversed(inp):
            if isinstance(item, dict) and item.get("role") == "user":
                last_user = _audit_flatten_message_content(item.get("content"))
                break
        lines.append("")
        lines.append("—— 用户侧最后一条 user 文本预览（便于对照「我问了什么」）——")
        if last_user:
            cap = 2000
            if len(last_user) > cap:
                lines.append(last_user[:cap] + f"\n...（共 {len(last_user)} 字，预览 {cap} 字）")
            else:
                lines.append(last_user)
        else:
            lines.append("(未找到 role=user 的消息)")
    elif isinstance(inp, str):
        lines.append(f"input 为纯字符串，预览: {inp[:1200]!r}" + ("..." if len(inp) > 1200 else ""))
    else:
        lines.append(f"input 类型: {type(inp).__name__!r}")
    return "\n".join(lines)


def _audit_user_turns_only(payload: object) -> str:
    """仅列出 input 里每条 role=user 的全文，置顶写入日志，避免用户在数万字 JSON 里找不到一句话。"""
    if not isinstance(payload, dict):
        return f"(payload 非 dict: {type(payload).__name__})"
    inp = payload.get("input")
    if not isinstance(inp, list):
        return f"(无 input 数组: {type(inp).__name__})"
    blocks: list[str] = []
    uidx = 0
    for item in inp:
        if isinstance(item, dict) and item.get("role") == "user":
            uidx += 1
            text = _audit_flatten_message_content(item.get("content")).strip()
            blocks.append(f"———— user 第 {uidx} 条 ————\n{text}")
    if not blocks:
        return "(input 中没有任何 role=user 的消息)"
    return "\n\n".join(blocks)


def _proxy_audit_log(section: str, body: str) -> None:
    """写入审计文件；section 为标题，body 为全文（已可含多行）。"""
    if not PROXY_LOG_FILE or PROXY_LOG_FILE == "-":
        if PROXY_LOG_FILE == "-":
            print(
                f"\n{'=' * 72}\n[{datetime.now().isoformat()}] {section}\n{'-' * 72}\n{body}\n",
                flush=True,
            )
        return
    block = (
        f"\n{'=' * 72}\n"
        f"[{datetime.now().isoformat()}] {section}\n"
        f"{'-' * 72}\n"
        f"{body}\n"
    )
    try:
        with _audit_lock:
            with open(PROXY_LOG_FILE, "a", encoding="utf-8", errors="replace") as f:
                f.write(block)
                f.flush()
    except OSError as e:
        print(f"[proxy] audit file write failed: {e!r}", flush=True)


def _estimate_payload_json_chars(payload: object) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=False))
    except (TypeError, ValueError):
        return -1


def _proxy_audit_event(line: str) -> None:
    """单行审计（PROXY_AUDIT_FULL=false），不含长 JSON。"""
    ts = datetime.now().isoformat()
    out = f"{ts} {line}\n"
    if not PROXY_LOG_FILE or PROXY_LOG_FILE == "-":
        if PROXY_LOG_FILE == "-":
            print(out, end="", flush=True)
        return
    try:
        with _audit_lock:
            with open(PROXY_LOG_FILE, "a", encoding="utf-8", errors="replace") as f:
                f.write(out)
                f.flush()
    except OSError as e:
        print(f"[proxy] audit event write failed: {e!r}", flush=True)


def _proxy_audit_codex_and_forward(
    endpoint: str,
    client_addr: tuple,
    user_agent: str,
    x_request_id: str,
    raw_body: bytes,
    payload_forward: object,
) -> None:
    """完整记录：① Codex 发到代理的原始 HTTP body；② 代理改写后实际 POST 给上游的 JSON。"""
    host = client_addr[0] if client_addr else "?"
    port = client_addr[1] if client_addr and len(client_addr) > 1 else "?"
    model_id = payload_forward.get("model") if isinstance(payload_forward, dict) else None
    stream_b = payload_forward.get("stream") if isinstance(payload_forward, dict) else None
    fj_n = _estimate_payload_json_chars(payload_forward)

    if not PROXY_AUDIT_FULL:
        _proxy_audit_event(
            f"EVENT request POST {endpoint} client={host}:{port} model={model_id!r} "
            f"raw_body_bytes={len(raw_body)} forward_json_chars≈{fj_n} stream={stream_b!r} "
            f"(set PROXY_AUDIT_FULL=true for full JSON)"
        )
        return

    try:
        raw_text = raw_body.decode("utf-8")
    except UnicodeDecodeError:
        raw_text = raw_body.decode("utf-8", errors="replace")

    raw_audited = _audit_truncate_text(raw_text, PROXY_LOG_MAX_REQUEST_CHARS)

    try:
        if isinstance(payload_forward, dict):
            fwd = json.dumps(payload_forward, ensure_ascii=False, indent=2)
        else:
            fwd = json.dumps(payload_forward, ensure_ascii=False) if payload_forward is not None else ""
    except (TypeError, ValueError):
        fwd = repr(payload_forward)
    fwd_audited = _audit_truncate_text(fwd, PROXY_LOG_MAX_REQUEST_CHARS)

    summary = _audit_request_summary(payload_forward)
    user_only = _audit_user_turns_only(payload_forward)
    header = f"client={host}:{port} UA={user_agent or '-'} X-Request-ID={x_request_id or '-'}"
    body = (
        "【0 · 只看用户输入 · 先在这里搜你的问题】（以下为 input 里每条 role=user 的合并文本）\n"
        f"{user_only}\n\n"
        "【1 · Codex → 代理】原始 HTTP POST body（与收到的字节一致，未经 normalize）\n"
        f"原始字节数 = {len(raw_body)}\n\n"
        f"{raw_audited}\n\n"
        "【2 · 代理 → 上游】改写后实际转发的 JSON body（sanitize / alias / compat 之后）\n\n"
        f"{fwd_audited}\n\n"
        "【3 · 可读摘要】\n"
        f"{summary}\n"
    )
    _proxy_audit_log(f"【请求审计】POST {endpoint} ({header})", body)


def _proxy_audit_upstream_raw(
    label: str,
    http_status: int,
    content_type: str,
    raw_body: bytes,
) -> None:
    """记录上游原始响应（对齐/改写之前）；默认全文，仅受 PROXY_LOG_MAX_UPSTREAM_CHARS 限制。"""
    if not PROXY_AUDIT_FULL:
        ok = 200 <= http_status < 300
        _proxy_audit_event(
            f"EVENT upstream {label} http={http_status} raw_bytes={len(raw_body)} "
            f"ct={content_type or '-'} ok={ok} (set PROXY_AUDIT_FULL=true for body)"
        )
        return

    try:
        text = raw_body.decode("utf-8", errors="replace")
    except Exception:
        text = repr(raw_body[:4096])
    full = _audit_truncate_text(text, PROXY_LOG_MAX_UPSTREAM_CHARS)
    head = f"HTTP {http_status} Content-Type={content_type or '-'} raw_bytes={len(raw_body)}"
    body = (
        "【上游 → 代理】完整原始响应 body（解码为 UTF-8；未经本代理对齐/shim）\n"
        f"{full}\n"
    )
    _proxy_audit_log(f"【上游响应审计】UPSTREAM_RAW {label} ({head})", body)


def _empty_response_usage() -> dict:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }


def _ensure_response_resource_required_fields(obj: dict, status: str = "completed") -> None:
    if not isinstance(obj, dict):
        return
    if not obj.get("id"):
        obj["id"] = "resp_shim_proxy"
    obj["object"] = "response"
    if not isinstance(obj.get("created_at"), int):
        obj["created_at"] = int(time.time())
    if not isinstance(obj.get("status"), str):
        obj["status"] = status
    if not isinstance(obj.get("model"), str) or not obj.get("model"):
        obj["model"] = DEFAULT_UPSTREAM_MODEL
    if not isinstance(obj.get("output"), list):
        obj["output"] = []
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        obj["usage"] = _empty_response_usage()
    else:
        usage.setdefault("input_tokens", 0)
        usage.setdefault("output_tokens", 0)
        usage.setdefault("total_tokens", usage["input_tokens"] + usage["output_tokens"])

# Alias map format:
# MODEL_ALIASES='{"gpt-5.4":"/path/model","gpt-5.1-codex-mini":"/path/model"}'
# Codex 可能使用 gpt-5.1-codex-mini 等名称，需映射到上游真实 id。
DEFAULT_UPSTREAM_MODEL = os.getenv(
    "DEFAULT_UPSTREAM_MODEL",
    "/dev/gpc_code/model/Qwen3_5_4B",
)
_RAW_DEFAULT_ALIASES = {
    "gpt-5.4": DEFAULT_UPSTREAM_MODEL,
    "gpt-5.1-codex-mini": DEFAULT_UPSTREAM_MODEL,
}
RAW_ALIASES = os.getenv("MODEL_ALIASES", json.dumps(_RAW_DEFAULT_ALIASES, ensure_ascii=False))
try:
    MODEL_ALIASES = json.loads(RAW_ALIASES)
    if not isinstance(MODEL_ALIASES, dict):
        MODEL_ALIASES = {}
except json.JSONDecodeError:
    MODEL_ALIASES = {}


def _route_path(raw_path: str) -> str:
    """
    从 self.path 得到用于路由的路径（与 query / fragment 无关）。
    少数客户端会用 absolute-form：POST http://host/v1/responses?x=1
    """
    s = (raw_path or "").strip()
    if not s:
        return "/"
    if s.startswith("http://") or s.startswith("https://"):
        s = urlparse(s).path or "/"
    else:
        s = s.split("?", 1)[0].split("#", 1)[0]
    s = s.strip()
    if len(s) > 1:
        s = s.rstrip("/")
    return s or "/"


def _normalize_roles(obj):
    """Recursively rewrite role=developer to role=system."""
    if isinstance(obj, dict):
        role = obj.get("role")
        if role == "developer":
            obj["role"] = "system"
        for key, value in list(obj.items()):
            obj[key] = _normalize_roles(value)
        return obj
    if isinstance(obj, list):
        return [_normalize_roles(item) for item in obj]
    return obj


def _apply_model_alias(obj):
    if not isinstance(obj, dict):
        return obj
    model = obj.get("model")
    if not isinstance(model, str):
        return obj
    if model in MODEL_ALIASES:
        obj["model"] = MODEL_ALIASES[model]
        return obj
    # 未列出的 OpenAI/Codex 风格 id：统一落到本地上游模型（避免 404）
    if model.startswith("gpt-") or "codex" in model.lower():
        obj["model"] = DEFAULT_UPSTREAM_MODEL
    return obj


def _merge_instructions_into_input(payload):
    """
    Responses API 可能同时带 instructions 与 input 里的 system，部分后端会误判顺序。
    将 instructions 合并进首条 system（或插入一条），并移除顶层 instructions。
    """
    if not isinstance(payload, dict):
        return payload
    instr = payload.get("instructions")
    if instr is None or instr == "":
        return payload
    if isinstance(instr, (dict, list)):
        instr_text = json.dumps(instr, ensure_ascii=False)
    else:
        instr_text = str(instr)
    instr_text = instr_text.strip()
    if not instr_text:
        payload.pop("instructions", None)
        return payload

    merged_any = False
    for key in ("input", "messages"):
        arr = payload.get(key)
        if not isinstance(arr, list):
            continue
        merged = False
        for item in arr:
            if isinstance(item, dict) and item.get("role") == "system":
                prev = item.get("content")
                if isinstance(prev, str):
                    item["content"] = instr_text + "\n\n" + prev
                elif prev is None:
                    item["content"] = instr_text
                else:
                    item["content"] = instr_text + "\n\n" + _content_to_text(prev)
                merged = True
                merged_any = True
                break
        if not merged:
            arr.insert(0, {"role": "system", "content": instr_text})
            merged_any = True
        if merged_any:
            break
    payload.pop("instructions", None)
    return payload


def _is_message_dict(item):
    return isinstance(item, dict) and isinstance(item.get("role"), str)


def _reorder_system_messages(sequence):
    """Keep relative order but force all system-role messages to the beginning."""
    system_msgs = []
    other_msgs = []
    for item in sequence:
        role = item.get("role") if isinstance(item, dict) else None
        if role == "system":
            system_msgs.append(item)
        else:
            other_msgs.append(item)
    return system_msgs + other_msgs


def _content_to_text(content):
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    try:
        return json.dumps(content, ensure_ascii=False)
    except Exception:
        return str(content)


def _merge_system_messages(sequence):
    """Collapse multiple system messages into one leading system message."""
    system_indexes = []
    for idx, item in enumerate(sequence):
        if isinstance(item, dict) and item.get("role") == "system":
            system_indexes.append(idx)
    if len(system_indexes) <= 1:
        return sequence

    first_index = system_indexes[0]
    first_item = sequence[first_index]
    merged_lines = []
    for idx in system_indexes:
        item = sequence[idx]
        merged_lines.append(_content_to_text(item.get("content")))

    merged_item = dict(first_item)
    merged_item["content"] = "\n\n".join([line for line in merged_lines if line])

    new_seq = []
    for idx, item in enumerate(sequence):
        if idx == first_index:
            new_seq.append(merged_item)
            continue
        if idx in system_indexes:
            continue
        new_seq.append(item)
    return new_seq


def _normalize_message_sequences(obj):
    """
    Recursively normalize message arrays so `system` is at the beginning.
    Applies to common OpenAI payload keys: `messages` and `input`.
    """
    if isinstance(obj, dict):
        for key, value in list(obj.items()):
            normalized_value = _normalize_message_sequences(value)
            if key in ("messages", "input") and isinstance(normalized_value, list):
                # Some clients send mixed arrays (message items + non-message items).
                # We still need to guarantee that any system message appears first.
                if any(_is_message_dict(item) for item in normalized_value):
                    normalized_value = _reorder_system_messages(normalized_value)
                    normalized_value = _merge_system_messages(normalized_value)
            obj[key] = normalized_value
        return obj
    if isinstance(obj, list):
        return [_normalize_message_sequences(item) for item in obj]
    return obj


def _expand_responses_input_string_messages(payload: object) -> object:
    """
    OpenAI Python SDK 允许 responses.create(input=[{"role":"user","content":"hi"}]) 这种简写；
    也常见 input 为纯字符串（input="hi"）或 input 数组里混有字符串项。
    部分本地上游只接受完整 Item：type=message，content 为 [{type: input_text|output_text, ...}]。
    在 instructions 合并、system 合并之后再展开，避免破坏 _merge_system_messages。
    """
    if not RESPONSES_EXPAND_SDK_STYLE_INPUT or not isinstance(payload, dict):
        return payload
    arr = payload.get("input")
    if isinstance(arr, str):
        payload["input"] = [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": arr}],
            }
        ]
        return payload
    if isinstance(arr, dict):
        arr = [arr]
    if not isinstance(arr, list):
        return payload
    out: list = []
    for item in arr:
        if isinstance(item, str):
            out.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": item}],
                }
            )
            continue
        if not isinstance(item, dict):
            out.append(item)
            continue
        ex = item.get("type")
        if ex is not None and ex != "message":
            out.append(item)
            continue
        role = item.get("role")
        content = item.get("content")
        if not isinstance(content, str):
            if ex is None and isinstance(role, str):
                # 即使 content 已是结构化数组，也补上 type=message 以兼容更严格的上游。
                new_item = dict(item)
                new_item["type"] = "message"
                out.append(new_item)
            else:
                out.append(item)
            continue
        if not isinstance(role, str):
            role = "user"
        if role == "assistant":
            parts = [{"type": "output_text", "text": content, "annotations": []}]
        else:
            # user / system 等
            parts = [{"type": "input_text", "text": content}]
        new_item = dict(item)
        new_item["type"] = "message"
        new_item["role"] = role
        new_item["content"] = parts
        out.append(new_item)
    payload["input"] = out
    return payload


def _responses_input_json_stringify_if_enabled(payload: object) -> object:
    """
    部分本地上游 /responses 的 Pydantic 误把 input 写成 str，导致 OpenAI 风格的数组报 400。
    开启 RESPONSES_INPUT_JSON_STRINGIFY 时，将 list/dict 的 input 转为 JSON 字符串（仍是一条合法 JSON 字段）。
    """
    if not RESPONSES_INPUT_JSON_STRINGIFY or not isinstance(payload, dict):
        return payload
    inp = payload.get("input")
    if isinstance(inp, (list, dict)):
        payload["input"] = json.dumps(inp, ensure_ascii=False)
    return payload


def _sanitize_payload(payload):
    payload = _normalize_roles(payload)
    payload = _normalize_message_sequences(payload)
    payload = _merge_instructions_into_input(payload)
    payload = _expand_responses_input_string_messages(payload)
    payload = _apply_model_alias(payload)
    return payload


def _sanitize_responses_compat(payload: object) -> object:
    """
    默认：透传完整 body（Codex / 完整 OpenAI Responses 客户端需要 tools、conversation 等）。
    RESPONSES_MINIMAL_COMPAT=true 时：仅保留 model_use_ssh 风格最小字段（给只认少量参数的上游用）。
    """
    if not isinstance(payload, dict):
        return payload

    if not RESPONSES_MINIMAL_COMPAT:
        sanitized = dict(payload)
    else:
        sanitized = {}
        allow_keys = ("model", "input", "stream", "temperature", "max_output_tokens")
        for key in allow_keys:
            if key in payload:
                sanitized[key] = payload[key]

    # 轻量兜底：避免类型异常把请求打坏
    if "stream" in sanitized and not isinstance(sanitized["stream"], bool):
        sanitized["stream"] = bool(sanitized["stream"])
    if "temperature" in sanitized and not isinstance(sanitized["temperature"], (int, float)):
        sanitized.pop("temperature", None)
    if "max_output_tokens" in sanitized:
        mot = sanitized["max_output_tokens"]
        if not isinstance(mot, int) or mot <= 0:
            sanitized.pop("max_output_tokens", None)

    return sanitized


def _extract_role_order(payload):
    roles = []
    if isinstance(payload, dict):
        for key in ("messages", "input"):
            value = payload.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and isinstance(item.get("role"), str):
                        roles.append(item.get("role"))
    return roles


def _ensure_responses_sse_completed(body: bytes) -> tuple[bytes, bool]:
    """
    若 Responses 流式 body 中已有 type=response.completed，原样返回。
    否则追加一条最小合法的 response.completed（满足 Codex 对 SSE 结束事件的依赖）。
    """
    if not body:
        return body, False
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body, False
    stripped = text.lstrip()
    if stripped.startswith("{"):
        return body, False

    max_seq = 0
    response_snapshot = None
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("data:"):
            continue
        raw = s[5:].strip()
        if raw == "[DONE]":
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "response.completed":
            return body, False
        seq = obj.get("sequence_number")
        if isinstance(seq, int):
            max_seq = max(max_seq, seq)
        t = obj.get("type")
        if t == "response.created" and isinstance(obj.get("response"), dict):
            response_snapshot = obj["response"]
        r = obj.get("response")
        if isinstance(r, dict) and r.get("object") == "response":
            response_snapshot = r

    if response_snapshot is None:
        response_snapshot = {
            "id": "resp_shim_proxy",
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": DEFAULT_UPSTREAM_MODEL,
            "output": [],
            "usage": _empty_response_usage(),
        }
    else:
        response_snapshot = dict(response_snapshot)
        response_snapshot["status"] = "completed"
        response_snapshot.setdefault("output", [])
    if not response_snapshot.get("id"):
        response_snapshot["id"] = "resp_shim_proxy"
    _ensure_response_resource_required_fields(response_snapshot)
    if RESPONSES_OPENAI_ALIGN:
        _align_openai_response_shapes_inplace(response_snapshot)

    event = {
        "type": "response.completed",
        "sequence_number": max_seq + 1,
        "response": response_snapshot,
    }
    suffix = "\n\nevent: response.completed\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n"
    return body + suffix.encode("utf-8"), True


class _SseReasoningItemState:
    """跟踪 response.output_item.* 里 type=reasoning 的 item_id，修正误挂在 reasoning 上的 output_text 流式事件。"""

    __slots__ = ("_reasoning_item_ids", "_item_output_indexes")

    def __init__(self) -> None:
        self._reasoning_item_ids: set[str] = set()
        self._item_output_indexes: dict[str, int] = {}

    def observe(self, obj: dict) -> None:
        t = obj.get("type")
        if t == "response.output_item.added":
            item = obj.get("item")
            if isinstance(item, dict) and item.get("type") == "reasoning":
                iid = item.get("id")
                if isinstance(iid, str):
                    self._reasoning_item_ids.add(iid)
                    oi = obj.get("output_index")
                    if isinstance(oi, int):
                        self._item_output_indexes[iid] = oi
        elif t == "response.output_item.done":
            item = obj.get("item")
            if isinstance(item, dict) and item.get("type") == "reasoning":
                iid = item.get("id")
                if isinstance(iid, str):
                    oi = obj.get("output_index")
                    if isinstance(oi, int):
                        self._item_output_indexes[iid] = oi

    def fill_missing_output_refs(self, obj: dict) -> None:
        t = obj.get("type")
        iid = obj.get("item_id")
        if not isinstance(t, str) or not isinstance(iid, str):
            return
        oi = self._item_output_indexes.get(iid)
        if isinstance(oi, int) and not isinstance(obj.get("output_index"), int):
            obj["output_index"] = oi
        if t in _SSE_REASONING_TO_OUTPUT_EVENT_TYPES and not isinstance(obj.get("content_index"), int):
            obj["content_index"] = 0

    def fix_mislabeled_output_text_on_reasoning_items(self, obj: dict) -> None:
        if RESPONSES_STRIP_REASONING_OUTPUT or not RESPONSES_REASONING_OUTPUT_TEXT_FIX:
            return
        t = obj.get("type")
        iid = obj.get("item_id")
        if not isinstance(iid, str) or iid not in self._reasoning_item_ids:
            return
        if t == "response.output_text.delta":
            obj["type"] = "response.reasoning_text.delta"
        elif t == "response.output_text.done":
            obj["type"] = "response.reasoning_text.done"


def _strip_reasoning_from_response_payload(obj: dict) -> None:
    """从整段 JSON 或 response.completed 快照里去掉 type=reasoning 的 output 项。"""
    if not RESPONSES_STRIP_REASONING_OUTPUT:
        return
    out = obj.get("output")
    if isinstance(out, list):
        obj["output"] = [x for x in out if not (isinstance(x, dict) and x.get("type") == "reasoning")]
    r = obj.get("response")
    if isinstance(r, dict):
        ro = r.get("output")
        if isinstance(ro, list):
            r["output"] = [x for x in ro if not (isinstance(x, dict) and x.get("type") == "reasoning")]


def _sse_data_line_should_drop_reasoning(obj: dict, state: _SseReasoningItemState) -> bool:
    """是否丢弃本条 SSE data（reasoning 通道）；丢弃前登记 item_id 供后续 delta 过滤。"""
    if not RESPONSES_STRIP_REASONING_OUTPUT:
        return False
    t = obj.get("type")
    if not isinstance(t, str):
        return False
    if t in _REASONING_AUX_SSE_TYPES:
        iid = obj.get("item_id")
        if isinstance(iid, str):
            state._reasoning_item_ids.add(iid)
        return True
    if t == "response.output_item.added":
        item = obj.get("item")
        if isinstance(item, dict) and item.get("type") == "reasoning":
            iid = item.get("id")
            if isinstance(iid, str):
                state._reasoning_item_ids.add(iid)
            return not RESPONSES_MIRROR_REASONING_TO_OUTPUT
    if t == "response.output_item.done":
        item = obj.get("item")
        if isinstance(item, dict) and item.get("type") == "reasoning":
            iid = item.get("id")
            if isinstance(iid, str):
                if RESPONSES_MIRROR_REASONING_TO_OUTPUT:
                    state._reasoning_item_ids.add(iid)
                else:
                    state._reasoning_item_ids.discard(iid)
            return not RESPONSES_MIRROR_REASONING_TO_OUTPUT
    if t in ("response.content_part.added", "response.content_part.done"):
        iid = obj.get("item_id")
        if isinstance(iid, str) and iid in state._reasoning_item_ids:
            return not RESPONSES_MIRROR_REASONING_TO_OUTPUT
    if RESPONSES_MIRROR_REASONING_TO_OUTPUT and t in _SSE_REASONING_DELTA_TYPES:
        return False
    iid = obj.get("item_id")
    if isinstance(iid, str) and iid in state._reasoning_item_ids:
        return not RESPONSES_MIRROR_REASONING_TO_OUTPUT
    return False


def _apply_sse_event_json_transforms(obj: dict, state: _SseReasoningItemState) -> None:
    state.observe(obj)
    state.fix_mislabeled_output_text_on_reasoning_items(obj)
    state.fill_missing_output_refs(obj)
    _align_openai_response_shapes_inplace(obj)


def _reasoning_item_to_message_item(item: dict) -> dict:
    text_parts: list[dict] = []

    def _append_text(value) -> None:
        if not isinstance(value, str):
            return
        text = value.strip()
        if not text:
            return
        text_parts.append({"type": "output_text", "text": text, "annotations": []})

    content = item.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in ("reasoning_text", "output_text", "text"):
                _append_text(part.get("text"))
    elif isinstance(content, dict):
        if content.get("type") in ("reasoning_text", "output_text", "text"):
            _append_text(content.get("text"))
    else:
        _append_text(content)

    summary = item.get("summary")
    if isinstance(summary, list):
        for part in summary:
            if isinstance(part, dict):
                _append_text(part.get("text"))
            else:
                _append_text(part)
    else:
        _append_text(summary)

    out = {
        "type": "message",
        "id": item.get("id") or "msg_proxy_reasoning",
        "role": "assistant",
        "content": text_parts,
    }
    status = item.get("status")
    if isinstance(status, str):
        out["status"] = status
    return out


def _align_openai_response_shapes_inplace(obj: object) -> None:
    """
    递归改写响应体，贴近 OpenAI 官方 SDK 见到的结构（model_use.py 打印的样式）：
    - output / item 里 type=message & role=assistant 且未设 phase -> phase=final_answer
    - content 里 type=output_text 且无 annotations -> annotations=[]
    - （可选）reasoning 流式事件 / reasoning_text 片段 -> output_text，供 Codex 渲染正文
    - SGLang/Harmony 等会在 content_part 里用 type=text；OpenAI/Codex 期望 output_text，否则正文不跟流绑定
    """
    if isinstance(obj, dict):
        if obj.get("object") == "response":
            _ensure_response_resource_required_fields(obj, status=str(obj.get("status") or "completed"))
        t = obj.get("type")
        if RESPONSES_OPENAI_ALIGN and RESPONSES_MIRROR_REASONING_TO_OUTPUT:
            if isinstance(t, str):
                if t in _SSE_REASONING_DELTA_TYPES:
                    if t.endswith(".done"):
                        obj["type"] = "response.output_text.done"
                    else:
                        obj["type"] = "response.output_text.delta"
                elif t == "reasoning_text":
                    obj["type"] = "output_text"
                    obj.setdefault("annotations", [])
                elif t == "reasoning":
                    msg_item = _reasoning_item_to_message_item(obj)
                    obj.clear()
                    obj.update(msg_item)
                    t = obj.get("type")
        # 与 input_text 区分：输出侧片段常为 {"type":"text","text":"..."}（非顶层的 response.* 事件）
        if RESPONSES_OPENAI_ALIGN and isinstance(t, str) and t == "text" and isinstance(obj.get("text"), str):
            obj["type"] = "output_text"
            obj.setdefault("annotations", [])
        if obj.get("type") == "message" and obj.get("role") == "assistant" and obj.get("phase") is None:
            obj["phase"] = "final_answer"
        if obj.get("type") == "output_text" and "annotations" not in obj:
            obj["annotations"] = []
        for _k, v in list(obj.items()):
            _align_openai_response_shapes_inplace(v)
    elif isinstance(obj, list):
        for item in obj:
            _align_openai_response_shapes_inplace(item)


def _align_responses_sse_text(text: str) -> str:
    """整段 SSE：event/data 配对后改写 data JSON，并用 JSON.type 重写 event 行（与 OpenAI 一致）。"""
    if not RESPONSES_OPENAI_ALIGN:
        return text
    state = _SseReasoningItemState()
    out: list[str] = []
    lines = text.splitlines(True)
    i = 0
    pending_event: Optional[tuple[str, str]] = None
    while i < len(lines):
        line = lines[i]
        st = line.rstrip("\r\n")
        ending = line[len(st) :]
        trimmed = st.strip()
        tl = trimmed.lower()
        if tl.startswith("event:"):
            pending_event = (st, ending)
            i += 1
            continue
        if trimmed.startswith("data:"):
            payload = trimmed[5:].strip()
            if not payload or payload == "[DONE]":
                if pending_event:
                    out.append(pending_event[0] + pending_event[1])
                    pending_event = None
                out.append(st + ending)
                i += 1
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                if pending_event:
                    out.append(pending_event[0] + pending_event[1])
                    pending_event = None
                out.append(st + ending)
                i += 1
                continue
            if isinstance(obj, dict):
                if _sse_data_line_should_drop_reasoning(obj, state):
                    pending_event = None
                    i += 1
                    continue
                _apply_sse_event_json_transforms(obj, state)
                if obj.get("type") == "response.completed":
                    _strip_reasoning_from_response_payload(obj)
                if pending_event:
                    et = obj.get("type", "")
                    out.append(f"event: {et}{pending_event[1]}")
                    pending_event = None
                out.append("data: " + json.dumps(obj, ensure_ascii=False) + ending)
            else:
                if pending_event:
                    out.append(pending_event[0] + pending_event[1])
                    pending_event = None
                out.append(st + ending)
            i += 1
            continue
        if pending_event:
            out.append(pending_event[0] + pending_event[1])
            pending_event = None
        out.append(st + ending)
        i += 1
    if pending_event:
        out.append(pending_event[0] + pending_event[1])
    return "".join(out)


def _align_responses_body_bytes(body: bytes) -> bytes:
    """对整段 JSON 或 SSE 文本做 OpenAI 形状对齐。"""
    if not RESPONSES_OPENAI_ALIGN or not body:
        return body
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                _align_openai_response_shapes_inplace(data)
                _strip_reasoning_from_response_payload(data)
                return json.dumps(data, ensure_ascii=False).encode("utf-8")
        except json.JSONDecodeError:
            return body
        return body
    return _align_responses_sse_text(text).encode("utf-8")


def _build_synthetic_completed_sse(response_id: Optional[str], sequence_number: int) -> bytes:
    rid = response_id or "resp_shim_proxy"
    response_snapshot = {
        "id": rid,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": DEFAULT_UPSTREAM_MODEL,
        "output": [],
        "usage": _empty_response_usage(),
    }
    _ensure_response_resource_required_fields(response_snapshot)
    event = {
        "type": "response.completed",
        "sequence_number": sequence_number,
        "response": response_snapshot,
    }
    if RESPONSES_OPENAI_ALIGN:
        _align_openai_response_shapes_inplace(event)
    return ("\n\nevent: response.completed\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8")


def _log_responses_sse_event_types(body: bytes) -> None:
    """扫描 SSE 中每条 data: 的 JSON.type，按首次出现顺序打印，便于对照 OpenAI 官方事件名。"""
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return
    if text.lstrip().startswith("{"):
        return
    order: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("data:"):
            continue
        raw = s[5:].strip()
        if raw in ("", "[DONE]"):
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            t = obj.get("type")
            if isinstance(t, str) and t not in order:
                order.append(t)
    if order:
        print(
            f"[proxy] /responses sse data types (first-seen order, unique={len(order)}): {order}",
            flush=True,
        )


def _log_responses_upstream_summary(endpoint_path: str, http_status: int, body: bytes) -> None:
    """只打结构：HTTP 状态、JSON 里 response.status、output[].type，不打印正文。"""
    if endpoint_path != "/responses":
        return
    try:
        text = body.decode("utf-8", errors="replace").strip()
        if not text.startswith("{"):
            print(
                f"[proxy] /responses out http={http_status} body=non-json_or_stream len={len(body)}",
                flush=True,
            )
            return
        data = json.loads(text)
    except Exception as exc:
        print(f"[proxy] /responses out http={http_status} json_parse_err={exc!r}", flush=True)
        return
    if isinstance(data, dict) and data.get("error"):
        err = data.get("error")
        msg = (err or {}).get("message", "") if isinstance(err, dict) else str(err)
        typ = (err or {}).get("type", "") if isinstance(err, dict) else ""
        print(
            f"[proxy] /responses out http={http_status} error_type={typ!r} error_msg={msg[:240]!r}",
            flush=True,
        )
        return
    out = data.get("output") if isinstance(data, dict) else None
    types: list = []
    if isinstance(out, list):
        for item in out:
            if isinstance(item, dict):
                types.append(item.get("type"))
            else:
                types.append(type(item).__name__)
    rsp_status = data.get("status") if isinstance(data, dict) else None
    incomplete = data.get("incomplete_details") if isinstance(data, dict) else None
    print(
        f"[proxy] /responses out http={http_status} response_status={rsp_status!r} "
        f"output_types={types} incomplete_details={incomplete!r}",
        flush=True,
    )


def _responses_body_looks_sse(resp_body: bytes, content_type: str) -> bool:
    ct = (content_type or "").lower()
    if "event-stream" in ct:
        return True
    if not resp_body:
        return False
    if resp_body.lstrip().startswith(b"{"):
        return False
    return b"data:" in resp_body


def _ensure_event_stream_charset(content_type: str) -> str:
    ct = (content_type or "text/event-stream").strip()
    if "text/event-stream" in ct.lower() and "charset" not in ct.lower():
        return ct + "; charset=utf-8"
    return ct


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "OpenAICompatRoleProxy/0.1"
    # 与 HTTP/1.1 长连接相比，1.0 响应结束语义更简单，部分客户端解析流式体更稳。
    protocol_version = "HTTP/1.0"

    def _tcp_nodelay(self) -> None:
        try:
            self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def _write_chunked_body(self, data: bytes) -> None:
        if not data:
            return
        self.wfile.write(format(len(data), "x").encode("ascii"))
        self.wfile.write(b"\r\n")
        self.wfile.write(data)
        self.wfile.write(b"\r\n")

    def _finalize_chunked_response(self) -> None:
        self.wfile.write(b"0\r\n\r\n")

    def _send_buffered_http11_chunked_sse(self, status: int, body: bytes, content_type: str) -> None:
        """与 beecode/OpenAI 常见行为一致：HTTP/1.1 + chunked，便于 Codex 解析 event-stream。"""
        saved = self.protocol_version
        self.protocol_version = "HTTP/1.1"
        try:
            self.send_response(status)
            ct = _ensure_event_stream_charset(content_type)
            self.send_header("Content-Type", ct)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close")
            if status == 200:
                self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            if body:
                self._write_chunked_body(body)
            self._finalize_chunked_response()
        finally:
            self.protocol_version = saved

    def _proxy_responses_upstream_error(self, e: HTTPError) -> None:
        err_body = e.read()
        try:
            ct_err = e.headers.get("Content-Type", "application/json") if e.headers else "application/json"
            _proxy_audit_upstream_raw("responses/HTTPError", e.code, ct_err, err_body)
        except Exception:
            pass
        _log_responses_upstream_summary("/responses", e.code, err_body)
        try:
            err_preview = err_body.decode("utf-8", errors="replace")[:400]
        except Exception:
            err_preview = "<decode_error>"
        print(f"[proxy] upstream_http_error code={e.code} body={err_preview}", flush=True)
        self.send_response(e.code)
        self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(err_body)))
        self.end_headers()
        self.wfile.write(err_body)

    def _proxy_responses_buffer(
        self,
        upstream_url: str,
        forward_body: bytes,
        headers: dict,
    ) -> None:
        """整段缓冲 + Content-Length，避免部分客户端解析 chunked 失败导致断流重试。"""
        req = Request(upstream_url, data=forward_body, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=900) as resp:
                resp_body = resp.read()
                status = resp.getcode()
                content_type = resp.headers.get("Content-Type", "application/json")
                _proxy_audit_upstream_raw("responses/buffer (upstream before shim/align)", status, content_type, resp_body)
                if (
                    status == 200
                    and RESPONSES_LOG_SSE_TYPES
                    and not resp_body.lstrip().startswith(b"{")
                    and b"data:" in resp_body
                ):
                    _log_responses_sse_event_types(resp_body)
                if RESPONSES_SSE_COMPLETION_SHIM and status == 200:
                    ct = (content_type or "").lower()
                    looks_sse = "event-stream" in ct or (
                        not resp_body.lstrip().startswith(b"{") and b"data:" in resp_body
                    )
                    if looks_sse:
                        resp_body, shimmed = _ensure_responses_sse_completed(resp_body)
                        if shimmed:
                            print(
                                "[proxy] /responses buffer: sse_shim appended response.completed",
                                flush=True,
                            )
                if status == 200:
                    resp_body = _align_responses_body_bytes(resp_body)
                _log_responses_upstream_summary("/responses", status, resp_body)
                looks_sse = _responses_body_looks_sse(resp_body, content_type)
                if status == 200 and looks_sse and RESPONSES_SSE_CHUNKED_DELIVERY:
                    self._send_buffered_http11_chunked_sse(status, resp_body, content_type)
                else:
                    self.send_response(status)
                    if status == 200 and looks_sse:
                        ct_out = _ensure_event_stream_charset(content_type)
                    else:
                        ct_out = content_type or "application/json"
                    self.send_header("Content-Type", ct_out)
                    self.send_header("Content-Length", str(len(resp_body)))
                    if status == 200 and looks_sse:
                        self.send_header("Cache-Control", "no-cache, no-transform")
                        self.send_header("X-Accel-Buffering", "no")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(resp_body)
                print(
                    f"[proxy] /responses buffer http={status} bytes={len(resp_body)} "
                    f"sse={looks_sse} chunked={bool(status == 200 and looks_sse and RESPONSES_SSE_CHUNKED_DELIVERY)}",
                    flush=True,
                )
        except HTTPError as e:
            self._proxy_responses_upstream_error(e)
        except URLError as e:
            self._send_json(
                502,
                {
                    "error": {
                        "message": f"Upstream request failed: {e.reason}",
                        "type": "BadGatewayError",
                        "code": 502,
                    }
                },
            )

    def _proxy_responses_streaming(
        self,
        upstream_url: str,
        forward_body: bytes,
        headers: dict,
    ) -> None:
        """
        边读上游边写给客户端（Codex 流式）：不设 Content-Length，Connection: close 结束 body。
        可选按行对齐 SSE（RESPONSES_STREAM_SSE_ALIGN），与 buffer 路径同一套 JSON/event 改写。
        """
        req = Request(upstream_url, data=forward_body, headers=headers, method="POST")
        resp = None
        try:
            resp = urlopen(req, timeout=900)
        except HTTPError as e:
            self._proxy_responses_upstream_error(e)
            return
        except URLError as e:
            self._send_json(
                502,
                {
                    "error": {
                        "message": f"Upstream request failed: {e.reason}",
                        "type": "BadGatewayError",
                        "code": 502,
                    }
                },
            )
            return

        status = resp.getcode()
        content_type = resp.headers.get("Content-Type", "text/event-stream; charset=utf-8")

        seen_completed = False
        response_id = None
        preamble = bytearray()
        total_out = 0
        scan_tail = b""
        tail_max = 262144
        pending = bytearray()
        line_align = RESPONSES_OPENAI_ALIGN and RESPONSES_STREAM_SSE_ALIGN
        sse_state = _SseReasoningItemState()
        pending_ev: Optional[tuple[str, str]] = None
        upstream_capture = bytearray()

        def _capture_upstream_chunk(chunk: bytes) -> None:
            if not chunk:
                return
            if PROXY_LOG_STREAM_CAPTURE_BYTES <= 0:
                upstream_capture.extend(chunk)
                return
            if len(upstream_capture) >= PROXY_LOG_STREAM_CAPTURE_BYTES:
                return
            take = min(len(chunk), PROXY_LOG_STREAM_CAPTURE_BYTES - len(upstream_capture))
            upstream_capture.extend(chunk[:take])

        def _forward_block(block: bytes) -> None:
            nonlocal seen_completed, scan_tail, total_out, preamble, response_id, pending, pending_ev
            if not block:
                return
            total_out += len(block)
            window = scan_tail + block
            if not seen_completed and _RESPONSE_COMPLETED_TYPE.search(window):
                seen_completed = True
            scan_tail = window[-tail_max:]
            if len(preamble) < 65536:
                need = min(len(block), 65536 - len(preamble))
                preamble.extend(block[:need])
                if response_id is None:
                    m = _RESP_ID_IN_PREAMBLE.search(preamble)
                    if m:
                        response_id = m.group(1).decode("utf-8", errors="replace")
            if not line_align:
                self.wfile.write(block)
                self.wfile.flush()
                return
            pending.extend(block)
            while True:
                nl = pending.find(b"\n")
                if nl < 0:
                    break
                line_b = bytes(pending[: nl + 1])
                del pending[: nl + 1]
                line_s = line_b.decode("utf-8", errors="replace")
                st = line_s.rstrip("\r\n")
                ending = line_s[len(st) :]
                trimmed = st.strip()
                tl = trimmed.lower()
                if tl.startswith("event:"):
                    pending_ev = (st, ending)
                    continue
                if trimmed.startswith("data:"):
                    payload = trimmed[5:].strip()
                    if not payload or payload == "[DONE]":
                        if pending_ev:
                            self.wfile.write((pending_ev[0] + pending_ev[1]).encode("utf-8"))
                            pending_ev = None
                        self.wfile.write(line_s.encode("utf-8"))
                        continue
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        if pending_ev:
                            self.wfile.write((pending_ev[0] + pending_ev[1]).encode("utf-8"))
                            pending_ev = None
                        self.wfile.write(line_s.encode("utf-8"))
                        continue
                    if isinstance(obj, dict):
                        if _sse_data_line_should_drop_reasoning(obj, sse_state):
                            pending_ev = None
                            continue
                        _apply_sse_event_json_transforms(obj, sse_state)
                        if obj.get("type") == "response.completed":
                            _strip_reasoning_from_response_payload(obj)
                        if pending_ev:
                            et = obj.get("type", "")
                            self.wfile.write((f"event: {et}" + pending_ev[1]).encode("utf-8"))
                            pending_ev = None
                        self.wfile.write(
                            ("data: " + json.dumps(obj, ensure_ascii=False) + ending).encode("utf-8")
                        )
                    else:
                        if pending_ev:
                            self.wfile.write((pending_ev[0] + pending_ev[1]).encode("utf-8"))
                            pending_ev = None
                        self.wfile.write(line_s.encode("utf-8"))
                    continue
                if pending_ev:
                    self.wfile.write((pending_ev[0] + pending_ev[1]).encode("utf-8"))
                    pending_ev = None
                self.wfile.write(line_s.encode("utf-8"))
            self.wfile.flush()

        try:
            self.send_response(status)
            self.send_header("Content-Type", _ensure_event_stream_charset(content_type))
            self.send_header("Connection", "close")
            self.send_header("Cache-Control", "no-cache, no-transform")
            if status == 200:
                self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            while True:
                try:
                    block = resp.read(65536)
                except http.client.IncompleteRead as e:
                    print(
                        f"[proxy] /responses upstream IncompleteRead partial={len(e.partial or b'')!r}",
                        flush=True,
                    )
                    _capture_upstream_chunk(e.partial or b"")
                    _forward_block(e.partial or b"")
                    break
                except (BrokenPipeError, ConnectionResetError, OSError) as ex:
                    print(f"[proxy] /responses upstream read OSError {ex!r}", flush=True)
                    break

                if not block:
                    break
                _capture_upstream_chunk(block)
                _forward_block(block)

            if pending_ev:
                self.wfile.write((pending_ev[0] + pending_ev[1]).encode("utf-8"))
                pending_ev = None
            if pending:
                self.wfile.write(bytes(pending))
                self.wfile.flush()

            if status == 200 and not seen_completed and RESPONSES_SSE_COMPLETION_SHIM:
                extra = _build_synthetic_completed_sse(response_id, 999999)
                print(
                    "[proxy] /responses stream_close: appended synthetic response.completed",
                    flush=True,
                )
                self.wfile.write(extra)
                self.wfile.flush()

            if upstream_capture:
                _proxy_audit_upstream_raw(
                    "responses/stream (SSE 上游原始累积；stream 捕获上限见 PROXY_LOG_STREAM_CAPTURE_BYTES=0 为不限制)",
                    status,
                    content_type,
                    bytes(upstream_capture),
                )
            print(
                f"[proxy] /responses stream_close http={status} bytes={total_out} "
                f"completed_event_seen={seen_completed} line_align={line_align}",
                flush=True,
            )
        except Exception:
            print("[proxy] /responses stream_close exception:\n" + traceback.format_exc(), flush=True)
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass

    def _send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy_json(self, method, endpoint_path):
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            try:
                if PROXY_AUDIT_FULL:
                    _proxy_audit_log(
                        f"【请求审计】POST {endpoint_path} JSON 解析失败（以下为 Codex 原始 body）",
                        raw_body.decode("utf-8", errors="replace"),
                    )
                else:
                    _proxy_audit_event(
                        f"EVENT request_bad_json POST {endpoint_path} raw_body_bytes={len(raw_body)} "
                        f"(set PROXY_AUDIT_FULL=true for raw body)"
                    )
            except Exception:
                pass
            self._send_json(
                400,
                {"error": {"message": "Invalid JSON body", "type": "BadRequestError", "code": 400}},
            )
            return

        before_roles = _extract_role_order(payload)
        payload = _sanitize_payload(payload)
        if endpoint_path == "/responses":
            payload = _sanitize_responses_compat(payload)
            payload = _responses_input_json_stringify_if_enabled(payload)
        after_roles = _extract_role_order(payload)
        print(
            f"[proxy] {endpoint_path} model={payload.get('model')} roles_before={before_roles} roles_after={after_roles}",
            flush=True,
        )
        try:
            _proxy_audit_codex_and_forward(
                endpoint_path,
                self.client_address,
                (self.headers.get("User-Agent") or "").strip(),
                (self.headers.get("X-Request-Id") or self.headers.get("x-request-id") or "").strip(),
                raw_body,
                payload,
            )
        except Exception:
            print("[proxy] audit codex_and_forward skipped:\n" + traceback.format_exc(), flush=True)
        if endpoint_path == "/responses" and FORCE_RESPONSES_JSON:
            payload["stream"] = False
            print("[proxy] /responses FORCE_RESPONSES_JSON: stream=false -> expect application/json body", flush=True)
        forward_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        upstream_url = f"{UPSTREAM_BASE}{endpoint_path}"

        headers = {"Content-Type": "application/json"}
        if endpoint_path == "/responses" and FORCE_RESPONSES_JSON:
            headers["Accept"] = "application/json"
        auth = self.headers.get("Authorization")
        if auth:
            headers["Authorization"] = auth
        else:
            upstream_key = os.getenv("UPSTREAM_API_KEY")
            if upstream_key:
                headers["Authorization"] = f"Bearer {upstream_key}"

        stream_requested = bool(payload.get("stream", True))
        if (
            endpoint_path == "/responses"
            and stream_requested
            and not FORCE_RESPONSES_JSON
        ):
            if _use_responses_buffer_delivery():
                self._proxy_responses_buffer(upstream_url, forward_body, headers)
            else:
                self._proxy_responses_streaming(upstream_url, forward_body, headers)
            return

        req = Request(upstream_url, data=forward_body, headers=headers, method=method)
        _timeout = 900 if endpoint_path == "/responses" else 120
        try:
            with urlopen(req, timeout=_timeout) as resp:
                resp_body = resp.read()
                status = resp.getcode()
                content_type = resp.headers.get("Content-Type", "application/json")
                _proxy_audit_upstream_raw(
                    f"{endpoint_path}/non-stream (upstream before shim/align)",
                    status,
                    content_type or "",
                    resp_body,
                )
                if (
                    endpoint_path == "/responses"
                    and RESPONSES_SSE_COMPLETION_SHIM
                    and status == 200
                ):
                    ct = (content_type or "").lower()
                    looks_sse = "event-stream" in ct or (
                        not resp_body.lstrip().startswith(b"{") and b"data:" in resp_body
                    )
                    if looks_sse:
                        resp_body, shimmed = _ensure_responses_sse_completed(resp_body)
                        if shimmed:
                            print(
                                "[proxy] /responses sse_shim: appended synthetic response.completed",
                                flush=True,
                            )
                if endpoint_path == "/responses" and status == 200:
                    resp_body = _align_responses_body_bytes(resp_body)
                _log_responses_upstream_summary(endpoint_path, status, resp_body)
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
        except HTTPError as e:
            err_body = e.read()
            _log_responses_upstream_summary(endpoint_path, e.code, err_body)
            try:
                err_preview = err_body.decode("utf-8", errors="replace")[:400]
            except Exception:
                err_preview = "<decode_error>"
            print(f"[proxy] upstream_http_error code={e.code} body={err_preview}", flush=True)
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)
        except URLError as e:
            self._send_json(
                502,
                {
                    "error": {
                        "message": f"Upstream request failed: {e.reason}",
                        "type": "BadGatewayError",
                        "code": 502,
                    }
                },
            )

    def do_GET(self):
        self._tcp_nodelay()
        req_path = _route_path(self.path)
        if req_path == "/healthz":
            self._send_json(200, {"ok": True, "upstream": UPSTREAM_BASE, "aliases": MODEL_ALIASES})
            return
        if req_path == "/v1/models":
            # /v1/models usually does not carry message roles, but we still proxy it.
            upstream_url = f"{UPSTREAM_BASE}/models"
            headers = {}
            auth = self.headers.get("Authorization")
            if auth:
                headers["Authorization"] = auth
            else:
                upstream_key = os.getenv("UPSTREAM_API_KEY")
                if upstream_key:
                    headers["Authorization"] = f"Bearer {upstream_key}"
            req = Request(upstream_url, headers=headers, method="GET")
            try:
                with urlopen(req, timeout=60) as resp:
                    resp_body = resp.read()
                    status = resp.getcode()
                    content_type = resp.headers.get("Content-Type", "application/json")
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(resp_body)))
                    self.end_headers()
                    self.wfile.write(resp_body)
            except HTTPError as e:
                err_body = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(err_body)))
                self.end_headers()
                self.wfile.write(err_body)
            except URLError as e:
                self._send_json(
                    502,
                    {
                        "error": {
                            "message": f"Upstream request failed: {e.reason}",
                            "type": "BadGatewayError",
                            "code": 502,
                        }
                    },
                )
            return

        self._send_json(
            404,
            {
                "error": {
                    "message": f"Not Found: GET {req_path}",
                    "type": "NotFoundError",
                    "code": 404,
                }
            },
        )

    def do_POST(self):
        self._tcp_nodelay()
        req_path = _route_path(self.path)
        # 兼容 base_url 写成 ...:8010（无 /v1）时，SDK 会 POST /responses、/chat/completions
        if req_path in ("/v1/chat/completions", "/chat/completions"):
            self._proxy_json("POST", "/chat/completions")
            return
        if req_path in ("/v1/responses", "/responses"):
            self._proxy_json("POST", "/responses")
            return
        print(
            f"[proxy] 404 POST raw_path={self.path!r} path={req_path!r}",
            flush=True,
        )
        self._send_json(
            404,
            {
                "error": {
                    "message": f"Not Found: POST {req_path} (use /v1/responses or /v1/chat/completions)",
                    "type": "NotFoundError",
                    "code": 404,
                }
            },
        )

    def log_message(self, format_str, *args):
        # Keep logs concise and avoid default noisy stderr format.
        print(f"[proxy] {self.address_string()} - {format_str % args}")


def main():
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    print(f"Proxy listening on http://{LISTEN_HOST}:{LISTEN_PORT} ({PROXY_ROUTE_BUILD})")
    if PROXY_LOG_FILE and PROXY_LOG_FILE != "-":
        print(
            f"[proxy] Audit log file: {PROXY_LOG_FILE} | PROXY_AUDIT_FULL={PROXY_AUDIT_FULL} "
            f"(false=只写 EVENT 单行，不写长 JSON；PROXY_LOG_*=0 为全文截断上限)",
            flush=True,
        )
    elif PROXY_LOG_FILE == "-":
        print("[proxy] Audit log: stdout only (PROXY_LOG_FILE=-)", flush=True)
    else:
        print("[proxy] Audit log: disabled (PROXY_LOG_FILE=off)", flush=True)
    if LISTEN_HOST in ("127.0.0.1", "::1", "localhost"):
        print(
            "[proxy] WARN: 仅监听回环地址时，请把 Codex base_url 设为 http://127.0.0.1:.../v1；"
            "若使用局域网 IP，请 PROXY_HOST=0.0.0.0",
            flush=True,
        )
    print(f"Upstream: {UPSTREAM_BASE}")
    print(f"Model aliases: {MODEL_ALIASES}")
    print(
        f"/responses client mode: {_responses_client_mode()} "
        f"(default chunked_sse=Codex流式; buffer=整包+可选chunked响应; passthrough=透传)"
    )
    print(
        f"[proxy] RESPONSES_INPUT_JSON_STRINGIFY={RESPONSES_INPUT_JSON_STRINGIFY} "
        "(true=把 input 数组打成 JSON 字符串再转发，缓解上游误声明 input:str 的 400)",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
