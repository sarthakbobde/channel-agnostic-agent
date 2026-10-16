"""Provider registry. A new provider = a class in adapters.py plus one line here."""
from app.providers.adapters import (AnthropicProvider, AzureProvider, CompatibleProvider, DeepSeekProvider,
                                    GoogleProvider, OpenAIProvider, XAIProvider)
from app.providers.base import ProviderAdapter, ProviderError

REGISTRY: dict[str, type[ProviderAdapter]] = {
    cls.type: cls for cls in (AnthropicProvider, GoogleProvider, OpenAIProvider, AzureProvider,
                              XAIProvider, DeepSeekProvider, CompatibleProvider)
}


def adapter_class(type_: str) -> type[ProviderAdapter]:
    try:
        return REGISTRY[type_]
    except KeyError:
        raise ProviderError(f"Unknown provider type {type_!r}.") from None


__all__ = ["REGISTRY", "adapter_class", "ProviderAdapter", "ProviderError"]
