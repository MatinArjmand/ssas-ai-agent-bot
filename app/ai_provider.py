"""Global OpenAI-compatible AI provider configuration and async tool-calling client."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from typing import Any, Mapping
from urllib.parse import urlsplit


class AIConfigError(ValueError):
    pass


_PROVIDERS = {
    "qwen": ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "qwen3.8-flash"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", ""),
    "openai": ("https://api.openai.com/v1", ""),
    "deepseek": ("https://api.deepseek.com", ""),
    "custom": ("", ""),
}


@dataclass(frozen=True)
class AISettings:
    provider: str
    model: str
    api_key: str = field(repr=False)
    base_url: str = field(repr=False)
    timeout_seconds: float = 60.0
    temperature: float | None = None
    qwen_disable_thinking: bool = True
    max_agent_steps: int = 12


def _text(env: Mapping[str, str], name: str, default: str = "") -> str:
    return env.get(name, "").strip() or default


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = _text(env, name).lower()
    if not value:
        return default
    if value in ("true", "1", "yes", "on"):
        return True
    if value in ("false", "0", "no", "off"):
        return False
    raise AIConfigError(f"{name} must be true or false in .env.")


def _number(value: str, name: str, low: float, high: float) -> float:
    try:
        number = float(value)
    except ValueError:
        raise AIConfigError(f"{name} must be a number in .env.") from None
    if not math.isfinite(number) or not low <= number <= high:
        raise AIConfigError(f"{name} must be between {low:g} and {high:g} in .env.")
    return number


def _integer(value: str, name: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except ValueError:
        raise AIConfigError(f"{name} must be an integer in .env.") from None
    if not low <= number <= high:
        raise AIConfigError(f"{name} must be between {low} and {high} in .env.")
    return number


def load_ai_settings(env: Mapping[str, str] | None = None) -> AISettings:
    env = os.environ if env is None else env
    provider = _text(env, "AI_PROVIDER", "qwen").lower()
    if provider not in _PROVIDERS:
        raise AIConfigError("AI_PROVIDER must be one of: " + ", ".join(_PROVIDERS) + ".")
    prefix = provider.upper()
    default_url, default_model = _PROVIDERS[provider]
    api_key = _text(env, f"{prefix}_API_KEY")
    model = _text(env, f"{prefix}_MODEL", default_model)
    base_url = _text(env, f"{prefix}_BASE_URL", default_url)
    missing = [
        name for name, value in (
            (f"{prefix}_API_KEY", api_key),
            (f"{prefix}_MODEL", model),
            (f"{prefix}_BASE_URL", base_url),
        ) if not value
    ]
    if missing:
        raise AIConfigError(f"AI_PROVIDER={provider}: set " + ", ".join(missing) + " in .env.")
    try:
        url = urlsplit(base_url)
        valid = (
            url.scheme in ("https", "http") and bool(url.hostname)
            and url.username is None and url.password is None
            and not url.query and not url.fragment and not any(c.isspace() for c in base_url)
        )
        url.port
    except ValueError:
        valid = False
    if not valid:
        raise AIConfigError(f"{prefix}_BASE_URL must be a valid http(s) API base URL.")
    if url.path.rstrip("/").endswith("/chat/completions"):
        raise AIConfigError(f"{prefix}_BASE_URL must omit the /chat/completions suffix.")
    raw_temperature = _text(env, f"{prefix}_TEMPERATURE")
    if raw_temperature.lower() in ("default", "omit"):
        temperature = None
    elif raw_temperature:
        temperature = _number(raw_temperature, f"{prefix}_TEMPERATURE", 0, 2)
    else:
        temperature = 0.0 if provider == "qwen" else None
    return AISettings(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=_number(_text(env, "AI_TIMEOUT_SECONDS", "60"), "AI_TIMEOUT_SECONDS", 1, 3600),
        temperature=temperature,
        qwen_disable_thinking=_bool(env, "QWEN_DISABLE_THINKING", True) if provider == "qwen" else False,
        max_agent_steps=_integer(_text(env, "AI_MAX_AGENT_STEPS", "12"), "AI_MAX_AGENT_STEPS", 2, 30),
    )


class AsyncAIClient:
    def __init__(self, settings: AISettings, client=None):
        self.settings = settings
        if client is None:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(
                api_key=settings.api_key,
                base_url=settings.base_url,
                timeout=settings.timeout_seconds,
                max_retries=4,
            )
        self.client = client

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        request: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        if self.settings.temperature is not None:
            request["temperature"] = self.settings.temperature
        if self.settings.provider == "qwen" and self.settings.qwen_disable_thinking:
            request["extra_body"] = {"enable_thinking": False}
        response = await self.client.chat.completions.create(**request)
        choices = getattr(response, "choices", None)
        if not choices:
            raise RuntimeError(f"{self.settings.provider} returned no answer choices.")
        choice = choices[0]
        message = getattr(choice, "message", None)
        if message is None:
            raise RuntimeError(f"{self.settings.provider} returned no message.")
        if getattr(message, "refusal", None) or getattr(choice, "finish_reason", None) == "content_filter":
            raise RuntimeError(f"{self.settings.provider} declined the request.")
        if getattr(choice, "finish_reason", None) == "length":
            raise RuntimeError(f"{self.settings.provider} returned an incomplete response.")
        return message
