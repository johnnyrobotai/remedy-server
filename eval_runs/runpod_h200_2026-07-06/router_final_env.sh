# Remedy Qwen3-VL safe router profile
# qwen3vl-32b-remedy remains the stable alt-text v2 alias.
# Do not repoint it to qwen3vl-32b-remedy-multitask-v1 without a separate promotion decision.

export OLLAMA_API_KEY=dummy
export VISION_BASE_URL=http://<served-host>:8000/v1
export OLLAMA_BASE_URL=http://<served-host>:8000/v1
export OLLAMA_VISION_MODEL=qwen3vl-32b-remedy
export OLLAMA_VISION_TASK_MODELS=contrast:qwen3vl-32b-remedy-contrast-v1,reading_order:qwen3vl-32b-remedy-reading-order-v1,heading_hierarchy:qwen3vl-32b-remedy-heading-v1,table_structure:qwen3vl-32b-remedy-table-v1
export OLLAMA_VISION_TASK_BASE_URLS=
export OLLAMA_VISION_ROUTER_ALLOW_FALLBACK=0
export OLLAMA_ESCALATION_MAX_INFLIGHT=8
export OLLAMA_VISION_MAX_INFLIGHT=8
export OLLAMA_VISION_GATE_TIMEOUT_SECONDS=600
export OLLAMA_VISION_MAX_TOKENS=768
