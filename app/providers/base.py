"""LLM provider adapters: one class per provider, a declarative SPEC, validate() and test().

Same pattern as channels: the admin page draws each provider's form from its SPEC, and a new
provider is a new class plus one line in the registry. Secrets (the API key) are write-only.
"""
from abc import ABC, abstractmethod

import requests

from app.spec import Field


class ProviderError(Exception):
    """A provider problem with a message safe to show an admin (never contains the key)."""


class ModelListUnavailable(ProviderError):
    """The endpoint exists but cannot list its models."""


def get_json(url: str, headers: dict, timeout: float = 15) -> dict:
    """GET and parse JSON, turning every failure into a safe ProviderError. The response body is
    never included in a message, because some providers echo part of the key in errors."""
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise ProviderError(f"Could not reach the provider ({type(e).__name__}). Check the endpoint and network.") from None
    if r.status_code in (401, 403):
        raise ProviderError(f"The provider rejected the API key (HTTP {r.status_code}).")
    if r.status_code == 402:
        raise ProviderError("The provider says the account has no credit left (HTTP 402).")
    if r.status_code == 404:
        raise ModelListUnavailable("This endpoint cannot list its models (HTTP 404).")
    if r.status_code == 429:
        raise ProviderError("The provider is rate limiting this key (HTTP 429). Try again shortly.")
    if not r.ok:
        raise ProviderError(f"The provider answered HTTP {r.status_code}.")
    try:
        return r.json()
    except ValueError:
        raise ProviderError("The provider answered with something that is not JSON.") from None


def clean_model_id(name: str) -> str:
    return name[len("models/"):] if name.startswith("models/") else name


API_KEY = Field("api_key", "API key", secret=True, required=True, help="Stored encrypted; never shown again.")


class ProviderAdapter(ABC):
    type: str
    title: str = ""
    warning: str = ""                      # shown next to the provider in the admin page
    SPEC: tuple[Field, ...] = (API_KEY,)

    def __init__(self, cfg: dict):
        self.cfg = cfg

    @classmethod
    def validate(cls, cfg: dict) -> list[str]:
        """Human-readable problems with a config; empty when it looks complete."""
        problems = []
        for f in cls.SPEC:
            v = cfg.get(f.name)
            if f.required and (v is None or v == "" or v == []):
                problems.append(f"{f.label} is required")
        return problems

    @abstractmethod
    def list_models(self) -> list[str]:
        """Models this key can use. Raises ProviderError with a safe message on failure."""

    @abstractmethod
    def build(self, model: str, timeout: float):
        """A LangChain chat model for `model`. No retries inside the SDK: our own fallback
        decides what to do about a failure, quickly."""
