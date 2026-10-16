"""Concrete providers. Heavy SDK imports happen inside build() so the app starts fast."""
from app.providers.base import (API_KEY, ModelListUnavailable, ProviderAdapter, ProviderError, clean_model_id,
                                get_json)
from app.spec import Field

ENDPOINT_OPTIONAL = Field("endpoint", "Endpoint", help="Leave empty for the provider's standard address.")


class OpenAIStyle(ProviderAdapter):
    """Anything that speaks the OpenAI chat API: OpenAI, Grok, DeepSeek and custom endpoints."""
    default_base = ""
    SPEC = (API_KEY, ENDPOINT_OPTIONAL)

    def base_url(self) -> str:
        return (self.cfg.get("endpoint") or self.default_base).rstrip("/")

    def list_models(self) -> list[str]:
        manual = [m for m in (self.cfg.get("manual_models") or []) if m]
        try:
            data = get_json(self.base_url() + "/models", {"Authorization": f"Bearer {self.cfg['api_key']}"})
            ids = [clean_model_id(m.get("id") or m.get("name") or "") for m in (data.get("data") or data.get("models") or [])]
            ids = sorted({i for i in ids if i})
        except ModelListUnavailable:
            if not manual:
                raise ProviderError("This endpoint cannot list its models. Enter the model names under 'Models'.") from None
            return manual
        return ids or manual

    def build(self, model: str, timeout: float):
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, api_key=self.cfg["api_key"], base_url=self.base_url(),
                          temperature=0, timeout=timeout, max_retries=0)


class OpenAIProvider(OpenAIStyle):
    type, title, default_base = "openai", "OpenAI", "https://api.openai.com/v1"


class XAIProvider(OpenAIStyle):
    type, title, default_base = "xai", "Grok (xAI)", "https://api.x.ai/v1"
    warning = "Questions and document passages are sent to xAI's servers. Check that this is allowed for your data."


class DeepSeekProvider(OpenAIStyle):
    type, title, default_base = "deepseek", "DeepSeek", "https://api.deepseek.com/v1"
    warning = "Questions and document passages are sent to DeepSeek's servers. Check that this is allowed for your data."


class CompatibleProvider(OpenAIStyle):
    type, title = "openai_compatible", "OpenAI-compatible endpoint"
    SPEC = (
        API_KEY,
        Field("endpoint", "Endpoint", required=True, help="Base address, for example https://host/v1"),
        Field("manual_models", "Models", kind="list",
              help="Only needed when the endpoint cannot list its models. Comma-separated."),
    )


class AzureProvider(OpenAIStyle):
    type, title = "azure_openai", "Azure OpenAI"
    SPEC = (
        API_KEY,
        Field("endpoint", "Endpoint", required=True, help="https://<resource>.openai.azure.com"),
        Field("deployment", "Deployment name", required=True, help="Azure serves models by deployment."),
        Field("api_version", "API version", required=True, help="For example 2024-06-01"),
    )

    def build(self, model: str, timeout: float):
        from langchain_openai import AzureChatOpenAI
        return AzureChatOpenAI(azure_endpoint=self.cfg["endpoint"], azure_deployment=self.cfg["deployment"],
                               api_version=self.cfg["api_version"], api_key=self.cfg["api_key"],
                               temperature=0, timeout=timeout, max_retries=0)

    def list_models(self) -> list[str]:
        # Azure cannot list deployments with a key; prove the key and deployment with a one-token call.
        try:
            self.build(self.cfg["deployment"], 20).invoke("Reply with OK.", max_tokens=5)
        except Exception as e:
            raise ProviderError(f"Azure OpenAI refused the test call ({type(e).__name__}). "
                                "Check the endpoint, deployment, API version and key.") from None
        return [self.cfg["deployment"]]


class AnthropicProvider(ProviderAdapter):
    type, title = "anthropic", "Anthropic"

    def list_models(self) -> list[str]:
        data = get_json("https://api.anthropic.com/v1/models?limit=100",
                        {"x-api-key": self.cfg["api_key"], "anthropic-version": "2023-06-01"})
        return sorted({m["id"] for m in data.get("data", []) if m.get("id")})

    def build(self, model: str, timeout: float):
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model, api_key=self.cfg["api_key"], temperature=0, timeout=timeout,
                             max_retries=0)


class GoogleProvider(ProviderAdapter):
    type, title = "google", "Google (Gemini)"

    def list_models(self) -> list[str]:
        data = get_json("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                        {"x-goog-api-key": self.cfg["api_key"]})
        return sorted({clean_model_id(m["name"]) for m in data.get("models", [])
                       if "generateContent" in m.get("supportedGenerationMethods", [])})

    def build(self, model: str, timeout: float):
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model=model, google_api_key=self.cfg["api_key"], temperature=0,
                                      timeout=timeout, retries=0)
