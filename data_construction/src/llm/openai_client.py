"""OpenAI-compatible LLM client for real API calls."""

from __future__ import annotations

import time
import os
from typing import TYPE_CHECKING

from .base import BaseLLMClient

if TYPE_CHECKING:
    from ..config import LLMRuntimeConfig

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[misc, assignment]


class OpenAILLMClient(BaseLLMClient):
    """
    LLM client using OpenAI-compatible HTTP API (OpenAI SDK, Azure, etc.).

    Supports per-task model selection via config (labeling_model, evidence_model, etc.).
    """

    def __init__(
        self,
        config: LLMRuntimeConfig | None = None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: int = 600,
        max_retries: int = 3,
    ) -> None:
        """
        Args:
            config: Full LLM config. If provided, overrides individual kwargs.
            api_key: API key (overrides config.api_key).
            base_url: API base URL (overrides config.base_url).
            model: Default model when none specified in generate().
            timeout_seconds: Request timeout.
            max_retries: Request retry count.
        """
        from ..config import LLMRuntimeConfig

        cfg = config or LLMRuntimeConfig()
        self._api_key = (
            api_key
            if api_key is not None
            else os.environ.get("OPENAI_API_KEY", cfg.api_key))
        self._base_url = (base_url or cfg.base_url).rstrip("/")
        self._timeout = timeout_seconds or cfg.timeout_seconds
        self._max_retries = max_retries or cfg.max_retries
        self._default_model = model or cfg.labeling_model
        self._labeling_model = cfg.labeling_model
        self._evidence_model = cfg.evidence_model
        self._writing_model = cfg.writing_model
        self._validation_model = cfg.validation_model
        self._null_generation_model = cfg.null_generation_model

        if OpenAI is None:
            raise ImportError(
                "openai package is required for OpenAILLMClient. "
                "Install with: pip install openai"
            )

        self._client = OpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            timeout=self._timeout,
            max_retries=self._max_retries,
        )

    def _resolve_model(self, model: str | None, task: str | None = None) -> str:
        if model:
            return model
        if task == "labeling":
            return self._labeling_model
        if task == "evidence":
            return self._evidence_model
        if task == "writing":
            return self._writing_model
        if task == "validation":
            return self._validation_model
        if task == "null_generation":
            return self._null_generation_model
        return self._default_model

    def generate(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 8192,
        task: str | None = None,
    ) -> str:
        """
        Call OpenAI-compatible chat completion API.

        Args:
            prompt: User message.
            system_prompt: Optional system instruction.
            model: Optional model override.
            temperature: Sampling temperature.
            max_tokens: Max output tokens.
            task: Task hint for model selection (labeling, evidence, etc.).

        Returns:
            Generated text.
        """
        resolved_model = self._resolve_model(model, task)

        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.chat.completions.create(
                    model=resolved_model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                content = response.choices[0].message.content
                return content or ""
            except Exception as e:
                if attempt < self._max_retries:
                    time.sleep(2 ** attempt)
                else:
                    raise RuntimeError(
                        f"LLM API failed after {self._max_retries + 1} attempts: {e}"
                    ) from e

        return ""
