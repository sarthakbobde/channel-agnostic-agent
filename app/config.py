import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    base_url: str = os.getenv("NINEROUTER_BASE_URL", "http://localhost:20128/v1")
    api_key: str = os.getenv("NINEROUTER_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "")
    models_dir: str = os.getenv("MODELS_DIR", "./models")                # downloaded embedding models
    docs_dir: str = os.getenv("DOCS_DIR", "./data/kb_files")             # uploaded originals, one folder per knowledge base
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "25"))
    chat_dir: str = os.getenv("CHAT_DIR", "./chats")
    chroma_path: str = os.getenv("CHROMA_PATH", "./chroma_db")
    collection: str = os.getenv("COLLECTION_NAME", "docs")
    admin_db_path: str = os.getenv("ADMIN_DB_PATH", "./admin_data/admin.db")
    secret_key: str = os.getenv("APP_SECRET_KEY", "")
    database_url: str = os.getenv("DATABASE_URL", "")      # overrides ADMIN_DB_PATH (e.g. PostgreSQL)
    chat_api_token: str = os.getenv("CHAT_API_TOKEN", "")
    web_ui_url: str = os.getenv("WEB_UI_URL", "http://localhost:8501")   # where password links for chat users point
    admin_email_domain: str = os.getenv("ADMIN_EMAIL_DOMAIN", "sysfore.com")
    admin_port: int = int(os.getenv("ADMIN_PORT", "0") or 0)             # 0 = admin shares the main port
    admin_host: str = os.getenv("ADMIN_HOST", "127.0.0.1")               # where the separate admin listener binds
    admin_require_https: bool = os.getenv("ADMIN_REQUIRE_HTTPS", "true").lower() in ("1", "true", "yes")
    admin_allow_remote: bool = os.getenv("ADMIN_ALLOW_REMOTE", "false").lower() in ("1", "true", "yes")
    admin_public_url: str = os.getenv("ADMIN_PUBLIC_URL", "")           # e.g. https://agent.example.com (sign-in return address)
    cors_origins: tuple = tuple(o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip())

    top_k: int = int(os.getenv("TOP_K", "5"))
    score_threshold: float = float(os.getenv("SCORE_THRESHOLD", "0.35"))
    chunk_size: int = int(os.getenv("CHUNK_SIZE", "900"))
    chunk_overlap: int = int(os.getenv("CHUNK_OVERLAP", "150"))
    memory_max_turns: int = int(os.getenv("MEMORY_MAX_TURNS", "6"))
    max_retention_days: int = int(os.getenv("MAX_RETENTION_DAYS", "365"))
    guardrails_enabled: bool = os.getenv("GUARDRAILS_ENABLED", "true").lower() in ("1", "true", "yes")   # first-run default
    prompt_max_chars: int = int(os.getenv("PROMPT_MAX_CHARS", "4000"))
    summarise_after_turns: int = int(os.getenv("SUMMARISE_AFTER_TURNS", "12"))


settings = Settings()