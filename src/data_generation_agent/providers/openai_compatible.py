from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from data_generation_agent.tools.common import redact_secrets


class ModelGatewayError(RuntimeError):
    """A bounded model request failed or returned an invalid envelope."""


@dataclass(frozen=True)
class ModelRequest:
    system_prompt: str
    user_prompt: str
    model: str
    max_tokens: int
    timeout_seconds: int
    idempotency_key: str
    temperature: float = 0.0


@dataclass(frozen=True)
class ModelResponse:
    text: str
    response_id: str | None
    model: str


class ModelGateway(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse: ...


class OpenAICompatibleGateway:
    """One fixed OpenAI-compatible chat endpoint with no ambient capabilities."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        allow_insecure_http: bool = False,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url.strip())
        if parsed.scheme not in ({"https", "http"} if allow_insecure_http else {"https"}):
            raise ModelGatewayError("model gateway must use an approved HTTP scheme")
        if not parsed.netloc or parsed.username or parsed.password:
            raise ModelGatewayError("model gateway URL is invalid")
        if not isinstance(api_key, str) or len(api_key.strip()) < 8:
            raise ModelGatewayError("model gateway credential is missing")
        normalized = base_url.rstrip("/")
        self.endpoint = (
            normalized
            if normalized.endswith("/chat/completions")
            else normalized + "/chat/completions"
        )
        self.api_key = api_key.strip()

    def complete(self, request: ModelRequest) -> ModelResponse:
        if not request.model.strip():
            raise ModelGatewayError("model id must not be empty")
        if not 1 <= request.max_tokens <= 32768:
            raise ModelGatewayError("max_tokens is outside the bounded range")
        if not 1 <= request.timeout_seconds <= 600:
            raise ModelGatewayError("timeout_seconds is outside the bounded range")
        payload = json.dumps(
            {
                "model": request.model,
                "messages": [
                    {"role": "system", "content": request.system_prompt},
                    {"role": "user", "content": request.user_prompt},
                ],
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        outbound = urllib.request.Request(
            self.endpoint,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Idempotency-Key": request.idempotency_key,
            },
        )
        try:
            with urllib.request.urlopen(outbound, timeout=request.timeout_seconds) as response:
                body = response.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            detail = redact_secrets(str(exc), (self.api_key,))
            raise ModelGatewayError(f"model gateway request failed: {detail}") from exc
        try:
            envelope = json.loads(body.decode("utf-8"))
            choice = envelope["choices"][0]
            text = choice["message"]["content"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ModelGatewayError("model gateway returned an invalid response envelope") from exc
        if not isinstance(text, str) or not text.strip():
            raise ModelGatewayError("model gateway returned empty content")
        response_id = envelope.get("id")
        response_model = envelope.get("model", request.model)
        return ModelResponse(
            text=text.strip(),
            response_id=str(response_id) if response_id is not None else None,
            model=str(response_model),
        )
