"""多模型 Provider 客户端（内置 7 套 + 用户自定义 OpenAI 兼容接口 + 离线演示回放）"""

import json
from typing import Any, Dict, List, Optional

BUILTIN_PROVIDERS: List[Dict[str, str]] = [
    {"provider_key": "offline", "name": "离线演示模式（免 Key）", "base_url": "", "default_model": "offline-demo-judge"},
    {"provider_key": "deepseek", "name": "DeepSeek（深度求索）", "base_url": "https://api.deepseek.com/v1", "default_model": "deepseek-chat"},
    {"provider_key": "qwen", "name": "通义千问（DashScope）", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "default_model": "qwen-plus"},
    {"provider_key": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1", "default_model": "gpt-4o-mini"},
    {"provider_key": "zhipu", "name": "智谱清言（GLM）", "base_url": "https://open.bigmodel.cn/api/paas/v4", "default_model": "glm-4-flash"},
    {"provider_key": "moonshot", "name": "Moonshot（Kimi）", "base_url": "https://api.moonshot.cn/v1", "default_model": "moonshot-v1-8k"},
    {"provider_key": "ollama", "name": "Ollama（本地私有部署）", "base_url": "http://127.0.0.1:11434/v1", "default_model": "qwen2.5:7b"},
]


def call_llm_json(prompt: str, llm_cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    cfg = llm_cfg or {}
    pkey = str(cfg.get("provider_key") or "offline").lower()
    api_key = (cfg.get("api_key") or "").strip()
    base_url = (cfg.get("base_url") or "").strip()
    model = (cfg.get("model") or "gpt-4o-mini").strip()

    if pkey == "offline" or (not api_key and pkey != "ollama"):
        return None

    from openai import OpenAI

    client = OpenAI(api_key=api_key or "ollama", base_url=base_url or "https://api.openai.com/v1")
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0,
    )
    parsed = json.loads(resp.choices[0].message.content or "{}")
    parsed["_mode"] = f"LIVE ({cfg.get('name') or pkey} / {model})"
    return parsed


def test_llm_connectivity(llm_cfg: Dict[str, Any]) -> Dict[str, Any]:
    pkey = str(llm_cfg.get("provider_key") or "offline").lower()
    if pkey == "offline":
        return {"status": "ok", "message": "离线演示模式已就绪（无需 API Key）。"}
    res = call_llm_json('请严格返回 JSON: {"ok": true}', llm_cfg)
    if res is None:
        return {"status": "error", "message": "未配置 API Key，请先填入 Key 或切换为离线演示模式。"}
    return {"status": "ok", "message": f"连通性测试通过（{res.get('_mode')}）。"}
