import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    base_url: str = os.getenv("NINEROUTER_BASE_URL", "http://localhost:20128/v1")
    api_key: str = os.getenv("NINEROUTER_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "")
    chat_dir: str = os.getenv("CHAT_DIR", "./chats")
    chroma_path: str = os.getenv("CHROMA_PATH", "./chroma_db")
    collection: str = os.getenv("COLLECTION_NAME", "docs")
    admin_db_path: str = os.getenv("ADMIN_DB_PATH", "./admin_data/admin.db")
    secret_key: str = os.getenv("APP_SECRET_KEY", "")

    top_k: int = int(os.getenv("TOP_K", "5"))
    score_threshold: float = float(os.getenv("SCORE_THRESHOLD", "0.35"))
    chunk_size: int = int(os.getenv("CHUNK_SIZE", "900"))
    chunk_overlap: int = int(os.getenv("CHUNK_OVERLAP", "150"))
    memory_max_turns: int = int(os.getenv("MEMORY_MAX_TURNS", "6"))


settings = Settings()