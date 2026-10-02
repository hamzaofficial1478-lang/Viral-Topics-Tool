"""L — LLMProvider adapter over the Part-E llm module (one abstraction, reused)."""

from __future__ import annotations

from .. import llm as _llm
from .base import LLMProvider


class ConfiguredLLMProvider(LLMProvider):
    name = "llm"

    def available(self) -> bool:
        return _llm.available()

    def describe(self) -> str:
        return _llm.describe()

    def complete_json(self, prompt, schema, *, system=None, max_tokens=1200):
        return _llm.complete_json(prompt, schema, system=system, max_tokens=max_tokens)

    def check(self):
        return _llm.check()


def build_llm_provider() -> ConfiguredLLMProvider:
    return ConfiguredLLMProvider()
