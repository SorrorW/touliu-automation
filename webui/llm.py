"""LLM 调用与配置管理：deepseek / kimi（均为 OpenAI 兼容接口，urllib 直连，无新依赖）。

配置存 webui/assets/agent_config.json（本机明文，仅自用）::

    {"provider": "deepseek", "api_key": "sk-...", "model": "deepseek-chat"}
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CONFIG_FILE = Path(__file__).with_name("assets") / "agent_config.json"

PROVIDERS: dict[str, dict[str, str]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
        "label": "DeepSeek",
    },
    "kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "moonshot-v1-8k",
        "label": "Kimi（月之暗面）",
    },
}

_TIMEOUT = 30.0


class LLMError(RuntimeError):
    """LLM 调用失败，message 直接展示给操作员。"""


def load_config() -> dict[str, Any]:
    """读取配置；不存在时返回默认（无 Key）。"""
    if CONFIG_FILE.exists():
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
    else:
        raw = {}
    provider = raw.get("provider") if raw.get("provider") in PROVIDERS else "deepseek"
    return {
        "provider": provider,
        "api_key": raw.get("api_key", ""),
        "model": raw.get("model") or PROVIDERS[provider]["default_model"],
    }


def save_config(provider: str, api_key: str | None, model: str | None) -> dict[str, Any]:
    """保存配置。api_key 传 None 表示保留已有 Key。"""
    if provider not in PROVIDERS:
        raise LLMError(f"未知供应商: {provider}（可选: {'/'.join(PROVIDERS)}）")
    current = load_config()
    config = {
        "provider": provider,
        "api_key": current["api_key"] if api_key is None else api_key,
        "model": model or PROVIDERS[provider]["default_model"],
    }
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config


def masked_config() -> dict[str, Any]:
    """配置的可展示版本：Key 打码只露后 4 位。"""
    config = load_config()
    key = config["api_key"]
    config["api_key_masked"] = ("****" + key[-4:]) if key else ""
    config["has_key"] = bool(key)
    config["provider_label"] = PROVIDERS[config["provider"]]["label"]
    config.pop("api_key", None)
    return config


def chat(config: dict[str, Any], system: str, user: str) -> str:
    """调 chat/completions，返回文本。失败抛 LLMError（中文原因）。"""
    provider = PROVIDERS[config["provider"]]
    if not config.get("api_key"):
        raise LLMError("未配置 API Key")
    payload = {
        "model": config["model"],
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    req = urllib.request.Request(
        provider["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config['api_key']}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        from .logging_setup import get_logger

        if exc.code == 401:
            get_logger(__name__).warning("LLM 调用 401（Key 无效）: %s", provider["label"])
            raise LLMError("API Key 无效（401），请检查密钥") from exc
        body = exc.read().decode("utf-8", errors="replace")[:200]
        get_logger(__name__).warning("LLM 接口 %s: %s", exc.code, body)
        raise LLMError(f"LLM 接口返回 {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"网络错误，无法连接 {provider['label']}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise LLMError(f"调用 {provider['label']} 超时（{_TIMEOUT:.0f} 秒）") from exc

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"LLM 返回格式异常: {str(data)[:200]}") from exc


def test_connection(config: dict[str, Any]) -> str:
    """发一条 ping 验证 Key 可用，返回模型名。"""
    chat(config, "你是连通性测试助手，只回复 ok。", "ping")
    return config["model"]
