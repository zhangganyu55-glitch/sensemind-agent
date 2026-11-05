"""DeepSeek transport and configuration. Secrets never enter API responses."""
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr
from typing import Literal


class LLMError(Exception):
    """Public error code only; never include provider bodies or credentials."""

    def __init__(self, code: str, usage: dict | None = None,
                 detail: str | None = None):
        super().__init__(code)
        self.code = code
        self.usage = usage or {}
        self.detail = detail


class LLMSettings(BaseModel):
    mode: Literal["off", "deepseek"] = "off"
    api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    model: str = Field(default="deepseek-flash", min_length=1, max_length=100)
    timeout_seconds: float = Field(default=45, ge=1, le=120)

    @property
    def enabled(self):
        return self.mode == "deepseek" and bool(self.api_key.get_secret_value())

    @classmethod
    def from_env(cls):
        load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
        key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        return cls(
            mode=os.getenv("GUARDIAN_LLM_MODE", "deepseek" if key else "off"),
            api_key=key,
            model=os.getenv("GUARDIAN_LLM_MODEL", "deepseek-flash"),
            timeout_seconds=os.getenv("GUARDIAN_LLM_TIMEOUT", "45"),
        )

    def public_status(self):
        return {"enabled": self.enabled, "mode": self.mode, "model": self.model if self.enabled else None}


class DeepSeekClient:
    def __init__(self, settings: LLMSettings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    def complete(self, messages: list, tools: list, tool_choice, timeout: float,
                 max_tokens: int | None = None):
        body = {
            "model": self.settings.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
            "thinking": {"type": "disabled"},
            "temperature": 0.2,
            "max_tokens": max_tokens or 1200,
            "stream": False,
        }
        try:
            with httpx.Client(
                transport=self.transport,
                timeout=httpx.Timeout(timeout, connect=min(timeout, 10)),
            ) as client:
                response = client.post(
                    "https://api.deepseek.com/chat/completions",
                    headers={"Authorization": "Bearer " + self.settings.api_key.get_secret_value()},
                    json=body,
                )
                response.raise_for_status()
                result = response.json()
            choice = result["choices"][0]
            message = choice["message"]
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise LLMError("invalid_response")
            if choice.get("finish_reason") == "length":
                raise LLMError("incomplete_response")
            raw_usage = result.get("usage", {})
            usage = {k: v for k, v in raw_usage.items()
                     if k in ("prompt_tokens", "completion_tokens", "total_tokens")
                     and isinstance(v, int) and not isinstance(v, bool) and v >= 0}
            return message, usage
        except httpx.TimeoutException:
            raise LLMError("timeout") from None
        except httpx.HTTPStatusError as error:
            code = {401: "authentication_failed", 402: "insufficient_balance", 429: "rate_limited"}.get(
                error.response.status_code, "provider_unavailable")
            raise LLMError(code) from None
        except httpx.RequestError:
            raise LLMError("network_error") from None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise LLMError("invalid_response") from None
