"""Embedding models behind one interface (embed_documents / embed_query), chosen per knowledge base.

Adapters: the built-in local MiniLM (Chroma's, the default), a local multilingual model (e5-small through onnxruntime), OpenAI
(reusing a key under LLM Providers), Azure OpenAI and Cohere. Each has a declarative SPEC (the admin form is drawn from it), a
`validate()` and a `test()` that reports the vector size. Secrets are write-only: stored encrypted per knowledge base, never returned,
never in an error message.

Every call is recorded as usage (kind "embedding", the knowledge base, tokens and, when the model has a hand-entered price, cost).
A knowledge base's index remembers the model it was built with: queries are embedded with THAT model, so changing the model means
building a new index version (the existing rebuild), and the old index keeps answering until the switch.
"""
import logging
import threading
from abc import ABC, abstractmethod
from pathlib import Path

import httpx

from app.spec import Field

log = logging.getLogger(__name__)
KB_SECRET_OWNER = "kb-embedding"
TIMEOUT = 60


class EmbeddingError(RuntimeError):
    """A temporary problem (network, rate limit, server error): worth retrying."""


def _post(url: str, headers: dict, payload: dict, timeout: float = TIMEOUT) -> dict:
    """POST JSON and return the JSON answer. Never puts the response body or the key in an error message."""
    try:
        r = httpx.post(url, headers=headers, json=payload, timeout=timeout)
    except httpx.HTTPError as e:
        raise EmbeddingError(f"Could not reach the embedding service ({type(e).__name__}).") from None
    if r.status_code in (401, 403):
        raise ValueError(f"The embedding service rejected the key (HTTP {r.status_code}).")
    if r.status_code == 404:
        raise ValueError("The embedding service did not find that model or deployment (HTTP 404).")
    if r.status_code == 400:
        raise ValueError("The embedding service rejected the request (HTTP 400). Check the model and settings.")
    if r.status_code == 429:
        raise EmbeddingError("The embedding service is rate limiting this key (HTTP 429).")
    if r.status_code >= 500:
        raise EmbeddingError(f"The embedding service had a problem (HTTP {r.status_code}).")
    if not r.is_success:
        raise ValueError(f"The embedding service answered HTTP {r.status_code}.")
    try:
        return r.json()
    except ValueError:
        raise EmbeddingError("The embedding service answered with something that is not JSON.") from None


class EmbeddingModel(ABC):
    name: str
    title: str = ""
    builtin: bool = False                  # the store lets Chroma embed; vectors are identical to the existing index
    SPEC: tuple[Field, ...] = ()
    PRICES: dict = {}                      # model -> list price in USD per million tokens (approximate; check yours)
    warning: str = ""

    def __init__(self, config: dict | None = None, secrets: dict | None = None, resolver=None):
        self.config = dict(config or {})
        self.secrets = dict(secrets or {})
        self.resolver = resolver           # finds a key that lives elsewhere (OpenAI: under LLM Providers)
        self.usage = None                  # callable(tokens, count, query, ok, error) set by `build`

    # ---- configuration ------------------------------------------------------------------------------------------------
    @classmethod
    def validate(cls, config: dict, secrets: dict) -> list[str]:
        errors = []
        for f in cls.SPEC:
            v = (secrets if f.secret else config).get(f.name)
            if f.required and (v is None or v == ""):
                errors.append(f"{f.label} is required")
            if f.kind == "choice" and v not in (None, "") and f.choices and v not in f.choices:
                errors.append(f"{f.label} must be one of: {', '.join(f.choices)}")
        return errors

    def model_id(self) -> str:
        return self.config.get("model") or self.name

    def price_per_million(self) -> float | None:
        return self.PRICES.get(self.model_id())

    # ---- use -------------------------------------------------------------------------------------------------------------
    @abstractmethod
    def _embed(self, texts: list[str], query: bool) -> tuple[list[list[float]], int | None]:
        """-> (vectors, tokens used or None if the service does not say)"""

    def _run(self, texts: list[str], query: bool) -> list[list[float]]:
        try:
            vectors, tokens = self._embed(texts, query)
        except Exception as e:
            self._report(None, len(texts), query, False, type(e).__name__)
            raise
        self._report(tokens if tokens is not None else sum(len(t) for t in texts) // 4 + 1, len(texts), query, True, "")
        return vectors

    def _report(self, tokens, count, query, ok, error) -> None:
        if self.usage is not None:
            try:
                self.usage(tokens, count, query, ok, error)
            except Exception:
                log.warning("could not record embedding usage", exc_info=True)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._run(texts, False)

    def embed_query(self, text: str) -> list[float]:
        return self._run([text], True)[0]

    def test(self) -> tuple[bool, str]:
        """Embed a sample and report the vector size. The message never contains a key."""
        try:
            size = len(self._embed(["connection test"], True)[0][0])
        except Exception as e:
            msg = str(e) if isinstance(e, (ValueError, EmbeddingError)) else f"{type(e).__name__}"
            for v in self.secrets.values():
                if isinstance(v, str) and len(v) >= 4:
                    msg = msg.replace(v, "***")
            return False, msg
        return True, f"Works; vectors have {size} dimensions."


# ---- built-in local model (Chroma's) -----------------------------------------------------------------------------------
class ChromaDefaultEmbeddings(EmbeddingModel):
    name = "chroma-default"
    title = "Local MiniLM (built in)"
    builtin = True
    PRICES = {"chroma-default": 0.0}
    _fn = None
    _lock = threading.Lock()

    def _embed(self, texts, query):
        with self._lock:
            if ChromaDefaultEmbeddings._fn is None:
                from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
                ChromaDefaultEmbeddings._fn = DefaultEmbeddingFunction()
        return [list(map(float, v)) for v in self._fn(texts)], None


# ---- local multilingual model (e5-small, onnxruntime) -------------------------------------------------------------------
class LocalMultilingualEmbeddings(EmbeddingModel):
    name = "local-multilingual"
    title = "Local multilingual (e5-small)"
    PRICES = {"multilingual-e5-small": 0.0}
    SPEC = ()
    REPO = "intfloat/multilingual-e5-small"
    FILES = ("onnx/model_qint8_avx512_vnni.onnx", "tokenizer.json")
    MAX_LEN = 512
    _session = None
    _tokenizer = None
    _lock = threading.Lock()

    def model_id(self) -> str:
        return "multilingual-e5-small"

    @staticmethod
    def folder() -> Path:
        from app.config import settings
        return Path(settings.models_dir) / "multilingual-e5-small"

    @classmethod
    def available(cls) -> bool:
        return all((cls.folder() / f).exists() for f in cls.FILES)

    @classmethod
    def download(cls) -> None:
        """Fetch the model files (about 129 MB) from Hugging Face. Called only after an admin has agreed."""
        from huggingface_hub import hf_hub_download
        for f in cls.FILES + ("config.json",):
            hf_hub_download(cls.REPO, f, local_dir=str(cls.folder()))

    def _load(self):
        with self._lock:
            if LocalMultilingualEmbeddings._session is None:
                if not self.available():
                    raise ValueError("The multilingual model has not been downloaded yet. Download it from the Embedding model panel first.")
                import onnxruntime as ort
                from tokenizers import Tokenizer
                tok = Tokenizer.from_file(str(self.folder() / "tokenizer.json"))
                tok.enable_truncation(max_length=self.MAX_LEN)
                tok.enable_padding()
                LocalMultilingualEmbeddings._tokenizer = tok
                LocalMultilingualEmbeddings._session = ort.InferenceSession(str(self.folder() / self.FILES[0]),
                                                                           providers=["CPUExecutionProvider"])
        return self._session, self._tokenizer

    def _embed(self, texts, query):
        import numpy as np
        session, tok = self._load()
        prefix = "query: " if query else "passage: "                       # e5 models expect these prefixes
        out: list = []
        for i in range(0, len(texts), 16):
            enc = tok.encode_batch([prefix + t for t in texts[i:i + 16]])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if any(x.name == "token_type_ids" for x in session.get_inputs()):
                feeds["token_type_ids"] = np.zeros_like(ids)
            hidden = session.run(None, feeds)[0]
            pooled = (hidden * mask[..., None]).sum(axis=1) / mask.sum(axis=1, keepdims=True)    # mean pooling
            pooled = pooled / np.linalg.norm(pooled, axis=1, keepdims=True)
            out += [list(map(float, v)) for v in pooled]
        return out, None


# ---- API models ----------------------------------------------------------------------------------------------------------
class OpenAIEmbeddings(EmbeddingModel):
    name = "openai"
    title = "OpenAI"
    PRICES = {"text-embedding-3-small": 0.02, "text-embedding-3-large": 0.13, "text-embedding-ada-002": 0.10}
    SPEC = (
        Field("provider", "Key from LLM Providers", kind="choice", required=True,
              help="Uses the API key of an OpenAI or OpenAI-compatible provider you already added. The key is not copied."),
        Field("model", "Model", kind="choice", required=True, choices=tuple(PRICES), default="text-embedding-3-small"),
        Field("dimensions", "Vector size (optional)", kind="number", help="Only the text-embedding-3 models can return a smaller vector. Empty = the model's own size."),
    )
    warning = "Document passages and questions are sent to OpenAI's servers. Check that this is allowed for your data."

    def _embed(self, texts, query):
        found = self.resolver(self.config.get("provider")) if self.resolver else None
        if not found:
            raise ValueError("Choose a provider under LLM Providers that has an API key.")
        base = (found.get("endpoint") or "https://api.openai.com/v1").rstrip("/")
        payload = {"model": self.config.get("model") or "text-embedding-3-small", "input": texts}
        if self.config.get("dimensions"):
            payload["dimensions"] = int(self.config["dimensions"])
        data = _post(base + "/embeddings", {"Authorization": "Bearer " + found["api_key"]}, payload)
        items = sorted(data["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in items], (data.get("usage") or {}).get("total_tokens")


class AzureOpenAIEmbeddings(EmbeddingModel):
    name = "azure-openai"
    title = "Azure OpenAI"
    SPEC = (
        Field("endpoint", "Endpoint", required=True, help="For example https://my-resource.openai.azure.com"),
        Field("deployment", "Deployment name", required=True, help="The name of the embedding deployment in Azure."),
        Field("api_version", "API version", default="2024-02-01", help="Default 2024-02-01."),
        Field("api_key", "API key", secret=True, required=True, help="Stored encrypted; never shown again."),
    )
    warning = "Document passages and questions are sent to your Azure OpenAI resource."

    def model_id(self) -> str:
        return self.config.get("deployment") or "azure-embedding"

    def _embed(self, texts, query):
        url = (f"{self.config['endpoint'].rstrip('/')}/openai/deployments/{self.config['deployment']}/embeddings"
               f"?api-version={self.config.get('api_version') or '2024-02-01'}")
        data = _post(url, {"api-key": self.secrets.get("api_key", "")}, {"input": texts})
        items = sorted(data["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in items], (data.get("usage") or {}).get("total_tokens")


class CohereEmbeddings(EmbeddingModel):
    name = "cohere"
    title = "Cohere"
    PRICES = {"embed-english-v3.0": 0.10, "embed-multilingual-v3.0": 0.10, "embed-english-light-v3.0": 0.02, "embed-multilingual-light-v3.0": 0.02}
    SPEC = (
        Field("model", "Model", kind="choice", required=True, choices=tuple(PRICES), default="embed-multilingual-v3.0"),
        Field("api_key", "API key", secret=True, required=True, help="Stored encrypted; never shown again."),
    )
    warning = "Document passages and questions are sent to Cohere's servers. Check that this is allowed for your data."

    def _embed(self, texts, query):
        data = _post("https://api.cohere.com/v2/embed", {"Authorization": "Bearer " + self.secrets.get("api_key", "")},
                     {"model": self.config.get("model") or "embed-multilingual-v3.0", "texts": texts,
                      "input_type": "search_query" if query else "search_document", "embedding_types": ["float"]})
        tokens = ((data.get("meta") or {}).get("billed_units") or {}).get("input_tokens")
        return data["embeddings"]["float"], tokens


REGISTRY: dict[str, type[EmbeddingModel]] = {c.name: c for c in (
    ChromaDefaultEmbeddings, LocalMultilingualEmbeddings, OpenAIEmbeddings, AzureOpenAIEmbeddings, CohereEmbeddings)}
DEFAULT = "chroma-default"


def get(name: str = DEFAULT, config: dict | None = None, secrets: dict | None = None, resolver=None) -> EmbeddingModel:
    try:
        return REGISTRY[name](config, secrets, resolver)
    except KeyError:
        raise ValueError(f"unknown embedding model {name!r}") from None


# ---- per-knowledge-base wiring: secrets, usage and which model an index was built with ----------------------------------------
def _secret_store():
    from app.channels import get_store
    store = get_store()
    return store.secrets if store else None


def provider_resolver(db):
    """Find the key of an OpenAI-style provider by name, from LLM Providers."""
    def find(name):
        secrets = _secret_store()
        if not name or secrets is None:
            return None
        from app.providers.store import ProviderStore
        ps = ProviderStore(db, secrets)
        for p in ps.list_all():
            if p["name"] == name and p["type"] in ("openai", "openai_compatible"):
                found = ps.runtime(p["id"])
                return found[1] if found and found[1].get("api_key") else None
        return None
    return find


def save_kb_secrets(db, kb_id: int, values: dict, owner: str = KB_SECRET_OWNER) -> list[str]:
    """Store (encrypt) the embedding secrets of a knowledge base. An empty value keeps what is stored."""
    from sqlalchemy import select
    from app.db import Secret
    secrets = _secret_store()
    if values and secrets is None:
        raise ValueError("Secrets cannot be stored: APP_SECRET_KEY is not set on the server.")
    changed = []
    with db.session() as s:
        for name, val in values.items():
            if val in (None, ""):
                continue
            row = s.scalar(select(Secret).where(Secret.owner_type == owner, Secret.owner_id == str(kb_id), Secret.name == name))
            token = secrets.encrypt(str(val))
            if row is None:
                s.add(Secret(owner_type=owner, owner_id=str(kb_id), name=name, ciphertext=token))
            else:
                row.ciphertext = token
            changed.append(name)
    return changed


def load_kb_secrets(db, kb_id: int, owner: str = KB_SECRET_OWNER) -> dict:
    from sqlalchemy import select
    from app.db import Secret
    secrets = _secret_store()
    if secrets is None:
        return {}
    with db.session() as s:
        return {r.name: secrets.decrypt(r.ciphertext) for r in s.scalars(select(Secret).where(Secret.owner_type == owner, Secret.owner_id == str(kb_id)))}


def kb_secret_names(db, kb_id: int, owner: str = KB_SECRET_OWNER) -> list[str]:
    from sqlalchemy import select
    from app.db import Secret
    with db.session() as s:
        return sorted(s.scalars(select(Secret.name).where(Secret.owner_type == owner, Secret.owner_id == str(kb_id))))


def clear_kb_secrets(db, kb_id: int, owner: str = KB_SECRET_OWNER) -> None:
    from sqlalchemy import delete
    from app.db import Secret
    with db.session() as s:
        s.execute(delete(Secret).where(Secret.owner_type == owner, Secret.owner_id == str(kb_id)))


def build(name: str, config: dict | None, kb: dict | None = None, db=None, extra_secrets: dict | None = None,
          department: str | None = None) -> EmbeddingModel:
    """A ready embedding model for a knowledge base: the stored secrets, the provider key resolver and usage recording wired in."""
    from app.db import get_db
    db = db or get_db()
    kb_id = (kb or {}).get("id")
    if department is None and (kb or {}).get("department_id"):
        from app.knowledge import kbs
        department = kbs.department_name(db, kb["department_id"]) or None
    secrets = load_kb_secrets(db, kb_id) if kb_id else {}
    secrets.update({k: v for k, v in (extra_secrets or {}).items() if v})
    model = get(name, config, secrets, provider_resolver(db))
    dept = department if department is not None else None

    def record(tokens, count, query, ok, error):
        from app import identity, usage
        usage.record(db, provider=model.title, provider_type=model.name, model=model.model_id(),
                     purpose="embed-query" if query else "embed-doc",
                     department=(identity.current().department if query else None) or dept or None,
                     input_tokens=tokens, output_tokens=0 if ok else None, status="ok" if ok else "failed", error=error, kb_id=kb_id, kind="embedding",
                     cost=0.0 if (ok and model.price_per_million() == 0) else None)

    model.usage = record
    return model


def for_version(version: dict, kb: dict | None = None, db=None, department: str | None = None) -> EmbeddingModel:
    """The model a given index version was built with."""
    return build(version.get("embedding") or DEFAULT, version.get("embedding_config") or {}, kb or {"id": version.get("kb_id")}, db,
                 department=department)


def active_for(kb: dict, db=None) -> EmbeddingModel | None:
    """The model of the knowledge base's ACTIVE index version, or None when it uses the built-in model (Chroma embeds itself)."""
    from app.db import get_db
    from app.knowledge import versions
    try:
        v = versions.active(db or get_db(), kb["id"]) if kb.get("id") else None
    except Exception:
        return None
    if v is None or v.get("embedding") in (None, "", DEFAULT):
        return None
    return for_version(v, kb, db)
