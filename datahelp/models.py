"""模型客户端抽象层 —— 统一不同 LLM 提供商的调用接口。"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod

import httpx

from datahelp.config import provider_env


# ── 模块级共享 HTTP 客户端（连接复用） ─────────────────

_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    """获取共享 httpx 客户端（首次调用时创建，后续复用 TCP 连接）。"""
    global _client
    if _client is None:
        _client = httpx.Client(
            proxy=None,             # 跳过系统代理（如 Clash），避免代理延迟
            timeout=httpx.Timeout(120.0, connect=30.0),
            follow_redirects=True,
        )
    return _client


def _reset_client():
    """重置 HTTP 客户端（释放旧连接，主要用于测试/重连）。"""
    global _client
    if _client is not None:
        _client.close()
        _client = None


class ModelClient(ABC):

    @abstractmethod
    def complete(self, prompt: str, max_tokens: int = 4096) -> str:
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        ...


class MockModelClient(ModelClient):

    def __init__(self, reply: str = "Mock reply。"):
        self._reply = reply
        self._call_count = 0

    def complete(self, prompt: str, max_tokens: int = 4096) -> str:
        self._call_count += 1
        if self._call_count == 1:
            return self._reply
        return "<final>任务完成。</final>"

    @property
    def model_name(self) -> str:
        return "mock-model"


class DeepSeekModelClient(ModelClient):

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        temperature: float | None = None,
    ):
        self._api_key = api_key or provider_env("DATAHELP_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY")
        if not self._api_key:
            raise ValueError("DeepSeek API key 未设置。请设置 DATAHELP_DEEPSEEK_API_KEY。")
        self._model = model or provider_env("DATAHELP_DEEPSEEK_MODEL", default="deepseek-v4-pro")
        self._base_url = base_url or provider_env("DATAHELP_DEEPSEEK_API_BASE") or "https://api.deepseek.com/anthropic"
        self._temperature = temperature

    @property
    def model_name(self) -> str:
        return self._model

    def complete(self, prompt: str, max_tokens: int = 4096) -> str:
        return _call_anthropic_messages(self._base_url, self._model, self._api_key, prompt, max_tokens, temperature=self._temperature)


class AnthropicModelClient(ModelClient):

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        temperature: float | None = None,
    ):
        self._api_key = api_key or provider_env("DATAHELP_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
        if not self._api_key:
            raise ValueError("Anthropic API key 未设置。请设置 DATAHELP_ANTHROPIC_API_KEY。")
        self._model = model or provider_env("DATAHELP_ANTHROPIC_MODEL", default="claude-sonnet-4-6")
        self._base_url = base_url or provider_env("DATAHELP_ANTHROPIC_API_BASE") or "https://api.anthropic.com"
        self._temperature = temperature

    @property
    def model_name(self) -> str:
        return self._model

    def complete(self, prompt: str, max_tokens: int = 4096) -> str:
        return _call_anthropic_messages(self._base_url, self._model, self._api_key, prompt, max_tokens, temperature=self._temperature)


class OpenAIModelClient(ModelClient):

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        temperature: float | None = None,
    ):
        self._api_key = api_key or provider_env("DATAHELP_OPENAI_API_KEY", "OPENAI_API_KEY")
        if not self._api_key:
            raise ValueError("OpenAI API key 未设置。请设置 DATAHELP_OPENAI_API_KEY。")
        self._model = model or provider_env("DATAHELP_OPENAI_MODEL", default="gpt-5.4")
        self._base_url = base_url or provider_env("DATAHELP_OPENAI_API_BASE") or "https://api.openai.com/v1"
        self._temperature = temperature

    @property
    def model_name(self) -> str:
        return self._model

    def complete(self, prompt: str, max_tokens: int = 4096) -> str:
        url = f"{self._base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        data = _do_request(url, headers, payload, self._base_url)
        choices = data.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "")
        return str(data)


class OllamaModelClient(ModelClient):

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        temperature: float | None = None,
    ):
        self._model = model or provider_env("DATAHELP_OLLAMA_MODEL", default="qwen3.5:4b")
        self._base_url = base_url or provider_env("DATAHELP_OLLAMA_BASE_URL") or "http://localhost:11434"
        self._temperature = temperature

    @property
    def model_name(self) -> str:
        return self._model

    def complete(self, prompt: str, max_tokens: int = 4096) -> str:
        url = f"{self._base_url.rstrip('/')}/api/generate"
        payload = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
        if self._temperature is not None:
            payload["options"]["temperature"] = self._temperature
        headers = {"Content-Type": "application/json"}
        data = _do_request(url, headers, payload, self._base_url)
        return data.get("response", str(data))


# ── 共享工具函数 ────────────────────────────────────

def _do_request(
    url: str,
    headers: dict[str, str],
    payload: dict,
    base_url: str,
    timeout: float = 120.0,
) -> dict:
    """发送 HTTP POST JSON 请求并解析响应，网络错误时自动重试（指数退避）。"""
    import time
    client = _get_client()
    max_retries = 2
    last_error = None
    for attempt in range(max_retries):
        try:
            resp = client.post(url, json=payload, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            error_body = e.response.text
            if status >= 500 and attempt < max_retries - 1:
                last_error = f"HTTP {status}"
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise RuntimeError(f"API 请求失败 (HTTP {status}): {error_body}")
        except httpx.RequestError as e:
            if attempt < max_retries - 1:
                last_error = str(e)
                time.sleep(0.5 * (2 ** attempt))
                continue
            if isinstance(e, httpx.ConnectError):
                raise RuntimeError(f"无法连接到 API ({base_url}): {e}")
            if isinstance(e, httpx.TimeoutException):
                raise RuntimeError(f"API 请求超时 ({base_url}): {e}")
            raise RuntimeError(f"API 请求失败 ({base_url}): {e}")
    raise RuntimeError(f"API 请求在 {max_retries} 次重试后失败: {last_error}")


def _call_anthropic_messages(base_url: str, model: str, api_key: str, prompt: str, max_tokens: int, temperature: float | None = None) -> str:
    """调用 Anthropic Messages API 格式（DeepSeek 兼容接口也用这个格式）。"""
    url = f"{base_url.rstrip('/')}/messages"
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if temperature is not None:
        payload["temperature"] = temperature
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    data = _do_request(url, headers, payload, base_url)
    content = data.get("content", [])
    if content and isinstance(content, list):
        for block in content:
            if block.get("type") == "text":
                return block["text"]
    return str(data.get("content", ""))


def create_model_client(
    provider: str = "mock",
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    temperature: float | None = None,
) -> ModelClient:
    providers = {
        "mock": lambda: MockModelClient(),
        "deepseek": lambda: DeepSeekModelClient(api_key, model, base_url, temperature=temperature),
        "anthropic": lambda: AnthropicModelClient(api_key, model, base_url, temperature=temperature),
        "openai": lambda: OpenAIModelClient(api_key, model, base_url, temperature=temperature),
        "ollama": lambda: OllamaModelClient(model, base_url, temperature=temperature),
    }
    factory = providers.get(provider)
    if factory is None:
        available = ", ".join(providers.keys())
        raise ValueError(f"不支持的 provider: '{provider}'。当前支持: {available}")
    return factory()


if __name__ == "__main__":
    import sys
    provider = sys.argv[1] if len(sys.argv) > 1 else "mock"
    client = create_model_client(provider)
    print(f"Using model: {client.model_name}")
    reply = client.complete("Say hello in one word.")
    print(f"Reply: {reply}")
