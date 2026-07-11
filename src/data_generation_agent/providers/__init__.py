"""Narrow model-provider boundaries shared by versioned pipeline tools."""

from .openai_compatible import (
    ModelGateway,
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
    OpenAICompatibleGateway,
)

__all__ = [
    "ModelGateway",
    "ModelGatewayError",
    "ModelRequest",
    "ModelResponse",
    "OpenAICompatibleGateway",
]
