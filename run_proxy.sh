cd /Users/a58/gpc_code/openai_role_proxy/openai_proxy_vllm

export UPSTREAM_BASE_URL="http://127.0.0.1:8005/v1"

export RESPONSES_INPUT_JSON_STRINGIFY=true

# 上游真实模型 id（未在别名表里的 gpt-* / codex 也会落到这个）
export DEFAULT_UPSTREAM_MODEL="model/Qwen3_5_35B_A3B_GPTQ_Int4"

# Codex 用的名字 → 上游 id（JSON 一行）
export MODEL_ALIASES='{"gpt-5.4":"model/Qwen3_5_35B_A3B_GPTQ_Int4"}'

export PROXY_HOST="0.0.0.0"
export PROXY_PORT="8010"

python3 proxy_server.py