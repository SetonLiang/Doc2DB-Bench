from __future__ import annotations

from abc import ABC, abstractmethod


class BaseLLMClient(ABC):
    @abstractmethod
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
        Generate text from an LLM backend.

        Args:
            prompt: User message content.
            system_prompt: Optional system instruction.
            model: Optional model override (client may use default if None).
            temperature: Sampling temperature (0.0 = deterministic).
            max_tokens: Maximum tokens to generate.
            task: Optional task hint (e.g. "labeling", "evidence") for model selection.

        Returns:
            Generated text string.
        """
        ...
