"""模型客户端抽象层 —— 统一不同 LLM 提供商的调用接口。"""

import json
import urllib.request
from abc import ABC, abstractmethod

from datahelp.config import provider_env


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
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        data = _do_request(req, self._base_url)
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
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        data = _do_request(req, self._base_url)
        return data.get("response", str(data))


# ── 共享工具函数 ────────────────────────────────────

def _do_request(req: urllib.request.Request, base_url: str) -> dict:
    """发送 HTTP 请求并解析 JSON 响应，网络错误时自动重试（指数退避）。"""
    import time
    max_retries = 3
    last_error = None
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            if e.code >= 500 and attempt < max_retries - 1:
                last_error = f"HTTP {e.code}"
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise RuntimeError(f"API 请求失败 (HTTP {e.code}): {error_body}")
        except urllib.error.URLError as e:
            if attempt < max_retries - 1:
                last_error = str(e.reason)
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise RuntimeError(f"无法连接到 API ({base_url}): {e.reason}")
    raise RuntimeError(f"API 请求在 {max_retries} 次重试后失败: {last_error}")


def _call_anthropic_messages(base_url: str, model: str, api_key: str, prompt: str, max_tokens: int, temperature: float | None = None) -> str:
    """调用 Anthropic Messages API 格式（DeepSeek 兼容接口也用这个格式）。"""
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if temperature is not None:
        payload["temperature"] = temperature
    url = f"{base_url.rstrip('/')}/messages"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    data = _do_request(req, base_url)
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
