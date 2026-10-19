"""First start after the upgrade: turn the .env model into a provider so nothing breaks.

If no provider exists yet and .env still names a model and key, create a provider "Default (from
.env)" (an OpenAI-compatible endpoint, which is what the prototype used), approve that type, and
make it the preferred model. From then on the key lives only in the encrypted store and the .env
line is ignored. Runs once; never blocks startup.
"""
import logging

from sqlalchemy import select

from app.config import settings
from app.db import LLMProvider, ModelSettings
from app.providers.store import ProviderStore

log = logging.getLogger(__name__)
SEED_NAME = "Default (from .env)"


def seed_from_env(store: ProviderStore) -> bool:
    if not settings.api_key or not settings.llm_model:
        return False
    with store.db.session() as s:
        if s.scalar(select(LLMProvider.id)) is not None or s.get(ModelSettings, 1) is not None:
            return False
    store.set_approved(sorted(set(store.approved()) | {"openai_compatible"}), "system")
    pid = store.create("openai_compatible", SEED_NAME,
                       {"api_key": settings.api_key, "endpoint": settings.base_url,
                        "manual_models": [settings.llm_model]}, [settings.llm_model], "system")
    store.set_status(pid, "Not tested", "Imported from .env. Use Test connection to check it.")
    store.set_model_settings({"provider_id": pid, "model": settings.llm_model}, None, 60, "system")
    log.warning("Imported the model from .env as provider %r. The API key is now stored encrypted; "
                "remove NINEROUTER_API_KEY from .env (it is ignored from now on).", SEED_NAME)
    return True


def run() -> None:
    """Called at startup."""
    try:
        from app import llm
        db, secrets = llm._dependencies()
        if secrets is not None:
            seed_from_env(ProviderStore(db, secrets))
    except Exception:
        log.exception("could not import the .env model as a provider")
