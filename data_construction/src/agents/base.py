from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar


InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


class BaseAgent(ABC, Generic[InputT, OutputT]):
    @abstractmethod
    def run(self, input: InputT) -> OutputT:
        """Run a typed agent step."""
        ...
