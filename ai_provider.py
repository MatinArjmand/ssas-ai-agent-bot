"""AI provider configuration and the shared DAX/answer completion client.

Only the selected provider's credentials are read. The OpenAI SDK transports
requests to that provider's endpoint; using it does not route Gemini to OpenAI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from typing import Mapping
from urllib.parse import urlsplit


class AIConfigError(ValueError):
    """An actionable, credential-free startup configuration error."""


# Keep the existing Qwen defaults for upgrades. New providers require an
# explicit model ID so a future model retirement never changes it silently.
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
    json_mode: bool = True
    qwen_disable_thinking: bool = True


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


def load_ai_settings(env: Mapping[str, str] | None = None) -> AISettings:
    """Read the environment after the entry point loads its local .env file."""
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
        raise AIConfigError(
            f"AI_PROVIDER={provider}: set " + ", ".join(missing) + " in .env, then restart the bot."
        )

    try:
        url = urlsplit(base_url)
        valid_url = (
            url.scheme in ("https", "http") and bool(url.hostname)
            and url.username is None and url.password is None
            and not url.query and not url.fragment
            and not any(char.isspace() for char in base_url)
        )
        # Accessing .port validates malformed ports too.
        url.port
    except ValueError:
        valid_url = False
    if not valid_url:
        raise AIConfigError(
            f"{prefix}_BASE_URL must be an http(s) API base URL without credentials, query or fragment."
        )
    if url.path.rstrip("/").endswith("/chat/completions"):
        raise AIConfigError(f"{prefix}_BASE_URL must omit the /chat/completions suffix.")

    # Preserve Qwen's old temperature. Other providers use their own defaults;
    # this also works with models that do not accept an explicit temperature.
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
        json_mode=_bool(env, f"{prefix}_JSON_MODE", True),
        qwen_disable_thinking=(
            _bool(env, "QWEN_DISABLE_THINKING", True) if provider == "qwen" else False
        ),
    )


class AIClient:
    def __init__(self, settings: AISettings, client=None):
        self.settings = settings
        if client is None:
            from openai import OpenAI

            client = OpenAI(
                api_key=settings.api_key,
                base_url=settings.base_url,
                timeout=settings.timeout_seconds,
                max_retries=4,
            )
        self.client = client

    def complete(self, prompt: str, json_mode: bool = False, max_attempts: int = 5) -> str:
        """Generate DAX JSON or answer text using the one selected provider.

        The SDK owns bounded retries for connection/timeouts, HTTP 408/409/429,
        and 5xx responses. There is no outer retry loop or provider fallback.
        """
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer.")
        settings = self.settings
        request = {
            "model": settings.model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if settings.temperature is not None:
            request["temperature"] = settings.temperature
        if settings.provider == "qwen" and settings.qwen_disable_thinking:
            request["extra_body"] = {"enable_thinking": False}
        if json_mode and settings.json_mode:
            if settings.provider == "gemini":
                request["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "dax_query",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {"dax": {"type": "string"}},
                            "required": ["dax"],
                            "additionalProperties": False,
                        },
                    },
                }
            else:
                request["response_format"] = {"type": "json_object"}

        response = self.client.with_options(max_retries=max_attempts - 1).chat.completions.create(**request)
        choices = getattr(response, "choices", None)
        if not choices:
            raise RuntimeError(f"{settings.provider} returned no answer choices.")
        choice = choices[0]
        message = getattr(choice, "message", None)
        if getattr(message, "refusal", None) or getattr(choice, "finish_reason", None) == "content_filter":
            raise RuntimeError(f"{settings.provider} declined to answer this request.")
        if getattr(choice, "finish_reason", None) == "length":
            raise RuntimeError(f"{settings.provider} returned an incomplete answer (output limit reached).")
        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError(f"{settings.provider} returned an empty text response.")
        return content.strip()
