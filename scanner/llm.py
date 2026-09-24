"""Pluggable LLM provider, same design as ArcGuard's backend/llm.py so LLM_PROVIDER works the same way
in both projects. Only the explanation is swappable; the verdict is always deterministic.

To add a provider: an OpenAI-compatible HTTP API is one entry in PROVIDERS; anything else is a class
with a complete() method and a branch in get_provider().
"""
import os
from dataclasses import dataclass
from typing import Protocol

import requests
from dotenv import load_dotenv

load_dotenv()


class LLMError(RuntimeError):
    """The provider was unreachable, misconfigured, or errored."""


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(self, system: str, prompt: str, max_tokens: int) -> str: ...


@dataclass(frozen=True)
class ProviderSpec:
    key_env: str
    default_model: str
    base_url: str | None = None      # None = not OpenAI-compatible; use the vendor SDK (Anthropic)
    token_param: str = "max_tokens"  # OpenAI's newer models renamed it


# Defaults were current when written; pin your own with LLM_MODEL in .env.
PROVIDERS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec("ANTHROPIC_API_KEY", "claude-opus-5-5"),
    # Grok (xAI's model) and Groq (a host for open models) are different vendors.
    "grok": ProviderSpec("XAI_API_KEY", "grok-4", "https://api.x.ai/v1"),
    "groq": ProviderSpec("GROQ_API_KEY", "openai/gpt-oss-120b", "https://api.groq.com/openai/v1"),
    "gemini": ProviderSpec("GEMINI_API_KEY", "gemini-2.5-flash",
                           "https://generativelanguage.googleapis.com/v1beta/openai"),
    "openai": ProviderSpec("OPENAI_API_KEY", "gpt-5", "https://api.openai.com/v1", "max_completion_tokens"),
}


class AnthropicProvider:
    def __init__(self, model: str, api_key: str):
        import anthropic  # only needed when this provider is chosen
        self.name, self.model = "anthropic", model
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key)

    def complete(self, system: str, prompt: str, max_tokens: int) -> str:
        try:
            response = self._client.messages.create(
                model=self.model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": prompt}],
            )
        except self._anthropic.AnthropicError as exc:
            raise LLMError(f"Anthropic request failed: {exc}") from exc
        return "".join(b.text for b in response.content if b.type == "text").strip()


class OpenAICompatibleProvider:
    def __init__(self, name: str, model: str, api_key: str, spec: ProviderSpec):
        self.name, self.model = name, model
        self._api_key, self._spec = api_key, spec

    def complete(self, system: str, prompt: str, max_tokens: int) -> str:
        try:
            response = requests.post(
                f"{self._spec.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self.model, self._spec.token_param: max_tokens,
                      "messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": prompt}]},
                timeout=30,
            )
            response.raise_for_status()
            message = response.json()["choices"][0]["message"]
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"{self.name} request failed: {exc}") from exc
        # Reasoning models can return null content, or spend the whole budget before answering.
        text = (message.get("content") or "").strip()
        if not text:
            raise LLMError(f"{self.name} returned an empty response")
        return text


def get_provider(name: str | None = None, model: str | None = None) -> LLMProvider:
    name = (name or os.environ.get("LLM_PROVIDER") or "anthropic").lower()
    spec = PROVIDERS.get(name)
    if spec is None:
        raise LLMError(f"Unknown LLM_PROVIDER '{name}'. Options: {', '.join(PROVIDERS)}.")
    api_key = os.environ.get(spec.key_env, "")
    if not api_key:
        raise LLMError(f"LLM_PROVIDER is '{name}' but {spec.key_env} is not set.")
    model = model or os.environ.get("LLM_MODEL") or spec.default_model
    if spec.base_url is None:
        return AnthropicProvider(model, api_key)
    return OpenAICompatibleProvider(name, model, api_key, spec)
