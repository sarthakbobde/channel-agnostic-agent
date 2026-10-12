from functools import lru_cache
from langchain_openai import ChatOpenAI
from app.config import settings


@lru_cache(maxsize=1)
def get_chat_model() -> ChatOpenAI:
    if not settings.api_key or not settings.llm_model:
        raise RuntimeError("Set NINEROUTER_API_KEY and LLM_MODEL in .env")
    return ChatOpenAI(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.llm_model,
        temperature=0,
    )