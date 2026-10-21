"""Model providers.

The model API is used for inference only.  It receives a rendered prompt and
returns text/tool calls; it never gets filesystem, shell, network or CANNJudge
credentials.  All tools execute in the host under the versioned action protocol.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .protocol import TOOLS

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
USER_AGENT = "AscendForge/0.1"


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.http_status = http_status

    def public(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "http_status": self.http_status,
        }


@dataclass(slots=True)
class ModelConfig:
    base_url: str
    api_key: str
    model: str
    provider: str = "openai-compatible"
    supports_tool_calls: bool = False
    max_tokens: int = 2048
    temperature: float = 0.2
    timeout: int = 120
    price_prompt_per_1k: float | None = None
    price_completion_per_1k: float | None = None

    @classmethod
    def from_payload(cls, payload: dict) -> "ModelConfig":
        base_url = str(payload.get("base_url") or "").strip()
        api_key = str(payload.get("api_key") or "").strip()
        model = str(payload.get("model") or "").strip()
        if not base_url or not model:
            raise ProviderError("MODEL_NOT_CONFIGURED", "模型 base_url 和 model 必须配置", retryable=False)
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in ("https", "http"):
            raise ProviderError("BAD_MODEL_URL", "模型 base_url 必须是 http(s) 地址", retryable=False)
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ProviderError(
                "INSECURE_MODEL_URL",
                "模型 API 使用 HTTP 会明文传输密钥；请使用 HTTPS（本机回环地址除外）",
                retryable=False,
            )
        if not api_key:
            raise ProviderError("MODEL_NOT_CONFIGURED", "模型 API key 未配置", retryable=False)
        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            provider=str(payload.get("provider") or "openai-compatible"),
            supports_tool_calls=bool(payload.get("supports_tool_calls", False)),
            max_tokens=max(16, min(32768, int(payload.get("max_tokens") or 2048))),
            temperature=max(0.0, min(2.0, float(payload.get("temperature", 0.2)))),
            timeout=max(10, min(600, int(payload.get("timeout") or 120))),
            price_prompt_per_1k=_float_or_none(payload.get("price_prompt_per_1k")),
            price_completion_per_1k=_float_or_none(payload.get("price_completion_per_1k")),
        )

    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return base + "/chat/completions"
        return base + "/v1/chat/completions"

    def cost(self, prompt_tokens: int, completion_tokens: int) -> tuple[float | None, bool]:
        if self.price_prompt_per_1k is None or self.price_completion_per_1k is None:
            return None, False
        value = (
            prompt_tokens / 1000.0 * self.price_prompt_per_1k
            + completion_tokens / 1000.0 * self.price_completion_per_1k
        )
        return round(value, 8), True

    def public(self) -> dict:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "supports_tool_calls": self.supports_tool_calls,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "endpoint": self.endpoint(),
        }


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class ProviderResponse:
    content: str
    tool_calls: list[dict] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    raw_usage: dict = field(default_factory=dict)
    tool_fallback: bool = False


class BaseProvider:
    def __init__(self, config: ModelConfig):
        self.config = config

    def complete(self, messages: list[dict], *, tools: bool = True) -> ProviderResponse:
        raise NotImplementedError


class OpenAICompatibleProvider(BaseProvider):
    def _payload(self, messages: list[dict], tools: bool) -> dict:
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": False,
        }
        if tools and self.config.supports_tool_calls:
            body["tools"] = openai_tool_schemas()
            body["tool_choice"] = "auto"
        return body

    def complete(self, messages: list[dict], *, tools: bool = True) -> ProviderResponse:
        try:
            return self._call(messages, tools=tools)
        except ProviderError as exc:
            # Some gateways reject the `tools` field.  A second request without
            # it is safe: the first request produced no accepted action.
            if tools and self.config.supports_tool_calls and exc.http_status in (400, 422):
                response = self._call(messages, tools=False)
                response.tool_fallback = True
                return response
            raise

    def _call(self, messages: list[dict], *, tools: bool) -> ProviderResponse:
        body = self._payload(messages, tools)
        request = urllib.request.Request(
            self.config.endpoint(),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(4096).decode("utf-8", errors="replace")
            except Exception:
                pass
            raise _http_error(exc.code, detail) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise ProviderError(
                "MODEL_NETWORK",
                f"无法连接模型 API：{str(getattr(exc, 'reason', exc))[:200]}",
                retryable=True,
            ) from None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProviderError("MODEL_BAD_RESPONSE", "模型 API 返回了非 JSON 响应", retryable=True) from exc
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError("MODEL_BAD_RESPONSE", "模型 API 响应缺少 choices", retryable=True)
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise ProviderError("MODEL_BAD_RESPONSE", "模型 API 响应缺少 message", retryable=True)
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
        content = content if isinstance(content, str) else ""
        tool_calls = []
        for item in message.get("tool_calls") or []:
            if not isinstance(item, dict):
                continue
            function = item.get("function") if isinstance(item.get("function"), dict) else {}
            name = function.get("name")
            arguments = function.get("arguments", {})
            if not name:
                continue
            tool_calls.append({"id": item.get("id") or "", "name": str(name), "arguments": arguments})
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        if not content and not tool_calls:
            raise ProviderError("MODEL_EMPTY", "模型没有返回内容或工具调用", retryable=True)
        return ProviderResponse(
            content=content,
            tool_calls=tool_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=str(payload.get("model") or self.config.model),
            raw_usage=usage,
        )


def _http_error(status: int, detail: str) -> ProviderError:
    message = detail[:500]
    try:
        payload = json.loads(detail)
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                message = error["message"][:500]
            elif isinstance(payload.get("message"), str):
                message = payload["message"][:500]
    except ValueError:
        pass
    if status == 401:
        return ProviderError("MODEL_AUTH", f"模型 API 鉴权失败（401）：{message}", retryable=False, http_status=status)
    if status == 403:
        return ProviderError("MODEL_FORBIDDEN", f"模型 API 拒绝访问（403）：{message}", retryable=False, http_status=status)
    if status == 429:
        return ProviderError("MODEL_RATE_LIMITED", f"模型 API 限流（429）：{message}", retryable=True, http_status=status)
    if status >= 500:
        return ProviderError("MODEL_UNAVAILABLE", f"模型 API 服务异常（{status}）：{message}", retryable=True, http_status=status)
    return ProviderError("MODEL_REJECTED", f"模型 API 拒绝请求（{status}）：{message}", retryable=False, http_status=status)


def openai_tool_schemas() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": spec["name"],
                "description": spec["description"],
                "parameters": spec["parameters"],
            },
        }
        for spec in TOOLS.values()
    ]


class MockProvider(BaseProvider):
    """Deterministic provider for tests/demos. Never used for real scoring."""

    def __init__(self, config: ModelConfig, script: list[Any] | None = None):
        super().__init__(config)
        self.script = list(script or [])
        self.calls = 0

    def complete(self, messages: list[dict], *, tools: bool = True) -> ProviderResponse:
        self.calls += 1
        if self.script:
            item = self.script.pop(0)
        else:
            item = {
                "tool_name": "finish_task",
                "arguments": {"reason": "other", "summary": "mock script exhausted"},
                "summary": "mock finish",
            }
        if isinstance(item, str):
            content = item
        else:
            content = json.dumps(item, ensure_ascii=False)
        return ProviderResponse(
            content=content,
            prompt_tokens=100,
            completion_tokens=50,
            model="mock",
            raw_usage={"prompt_tokens": 100, "completion_tokens": 50},
        )


def make_provider(config: ModelConfig, *, mock_script: list[Any] | None = None) -> BaseProvider:
    if config.provider in ("mock", "scripted"):
        return MockProvider(config, mock_script)
    return OpenAICompatibleProvider(config)
