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


def azure_endpoint(raw: str) -> str:
    """The resource address only: https://<resource>.openai.azure.com. People often paste the whole 'Target URI' from the Azure
    portal (with /openai/deployments/... and ?api-version=...), which makes every call fail, so anything after the host is dropped."""
    from urllib.parse import urlsplit
    raw = (raw or "").strip()
    if raw and "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    return f"{parts.scheme}://{parts.netloc}" if parts.netloc else raw.rstrip("/")


def azure_problem(e: Exception) -> str:
    """A short, safe explanation of a failed Azure call. Only the status and Azure's short error code are used, never its message
    (some messages echo part of the key)."""
    import httpx
    status = getattr(e, "status_code", None)
    body = getattr(e, "body", None)
    err = body.get("error", body) if isinstance(body, dict) else {}
    code = str((err or {}).get("code") or "").lower() if isinstance(err, dict) else ""
    text = str((err or {}).get("message") or "").lower() if isinstance(err, dict) else ""
    if isinstance(e, (httpx.ConnectError, httpx.UnsupportedProtocol, httpx.InvalidURL)) or type(e).__name__ == "APIConnectionError":
        return "Could not reach the endpoint. It should look like https://<resource>.openai.azure.com (no path after it); check it and the network."
    if isinstance(e, httpx.TimeoutException) or type(e).__name__ == "APITimeoutError":
        return "The endpoint did not answer in time. Check the address and that the resource is not behind a firewall."
    if status == 401 or code in ("401", "permissiondenied", "invalid_api_key"):
        return "Azure rejected the key (HTTP 401). Use a key of this same resource (Keys and Endpoint), and check the endpoint is its address."
    if status == 403:
        return "Azure refused access (HTTP 403). The key may be restricted by network rules or the resource may have public access switched off."
    if status == 404 or code in ("deploymentnotfound", "resourcenotfound"):
        return ("Azure answered 404" + (f" ({code})" if code and code != "404" else "") + ": it cannot find this deployment at this address. Check that "
                "(1) the DEPLOYMENT name is exact, (2) the deployment lives in the same Azure resource as the endpoint and the key, and "
                "(3) the API version is recent enough for the model: newer models such as gpt-4.1 need one like 2025-03-01-preview.")
    if status == 429:
        return "Azure is rate limiting this deployment (HTTP 429). Check its quota, or try again shortly."
    if status == 400 and ("api version" in text or "api-version" in text or code == "apiversionnotsupported"):
        return "Azure does not accept this API version (HTTP 400). Try a current one such as 2024-10-21 or 2025-03-01-preview."
    if status == 400 and "temperature" in text:
        return "This deployment does not accept a temperature setting (HTTP 400)."
    if status == 400 and code == "content_filter":
        return "Azure's content filter blocked the test message (HTTP 400). Check the deployment's content filter."
    if status:
        return f"Azure answered HTTP {status}" + (f" ({code})." if code else ".")
    return f"The test call failed ({type(e).__name__})."


class _TemperatureFallback:
    """Reasoning models (o-series, gpt-5) on Azure only accept the default temperature. Calls start with temperature 0 for steady
    answers; if Azure says the setting is not allowed, the same call is repeated without it, and later calls skip it."""

    def __init__(self, make):
        self._make = make
        self._model = make(0)

    @staticmethod
    def _rejects_temperature(e: Exception) -> bool:
        return getattr(e, "status_code", None) == 400 and "temperature" in str(getattr(e, "body", "") or e).lower()

    def invoke(self, *args, **kw):
        try:
            return self._model.invoke(*args, **kw)
        except Exception as e:                                  # noqa: BLE001
            if not self._rejects_temperature(e):
                raise
            self._model = self._make(None)
            return self._model.invoke(*args, **kw)

    def stream(self, *args, **kw):
        started = False
        try:
            for chunk in self._model.stream(*args, **kw):
                started = True
                yield chunk
        except Exception as e:                                  # noqa: BLE001
            if started or not self._rejects_temperature(e):
                raise
            self._model = self._make(None)
            yield from self._model.stream(*args, **kw)


class AzureProvider(OpenAIStyle):
    type, title = "azure_openai", "Azure OpenAI"
    SPEC = (
        API_KEY,
        Field("endpoint", "Endpoint", required=True, help="https://<resource>.openai.azure.com (the part after .com is ignored)"),
        Field("deployment", "Deployment name", required=True, help="The name you gave the deployment in Azure, not the model name."),
        Field("api_version", "API version", required=True, help="For example 2024-10-21"),
    )

    def _client(self, temperature, timeout: float):
        from langchain_openai import AzureChatOpenAI
        return AzureChatOpenAI(azure_endpoint=azure_endpoint(self.cfg["endpoint"]), azure_deployment=self.cfg["deployment"].strip(),
                               api_version=self.cfg["api_version"].strip(), api_key=self.cfg["api_key"],
                               temperature=temperature, timeout=timeout, max_retries=0)

    def build(self, model: str, timeout: float):
        return _TemperatureFallback(lambda temperature: self._client(temperature, timeout))

    PROBE_VERSIONS = ("2025-04-01-preview", "2025-03-01-preview", "2024-10-21")

    def _try(self, version: str | None = None) -> None:
        target = self if version is None else type(self)({**self.cfg, "api_version": version})
        target.build(target.cfg["deployment"], 20).invoke("Reply with OK.", max_tokens=5)

    def list_models(self) -> list[str]:
        # Azure cannot list deployments with a key; prove the endpoint, key, version and deployment with a one-token call.
        try:
            self._try()
        except Exception as e:                                  # noqa: BLE001
            if getattr(e, "status_code", None) == 404:           # a deployment that exists answers 404 on an API version too old for its model
                for v in self.PROBE_VERSIONS:
                    if v == self.cfg["api_version"].strip():
                        continue
                    try:
                        self._try(v)
                    except Exception:                           # noqa: BLE001
                        continue
                    raise ProviderError(f"Azure OpenAI test failed with API version {self.cfg['api_version'].strip()}, but this deployment answers on "
                                        f"{v}. Change the API version to {v}.") from None
            raise ProviderError("Azure OpenAI test failed: " + azure_problem(e)) from None
        return [self.cfg["deployment"].strip()]


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
