# Channel Agnostic Agent: API, admin page and channel connectors in one container.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# curl is used by the health check; build-essential covers packages without wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# All dependency versions are pinned in pyproject.toml.
# Heavy ML packages (LLM Guard, PyTorch, spaCy models) are added in the guardrails phase.
COPY pyproject.toml .
COPY app/ ./app/
COPY channels.yaml .
# The optional web chat (Chainlit) runs from the same image (a second container or Cloud Run service)
COPY webchat/ ./webchat/
RUN pip install -e .

# Language detection (fastText lid.176, about 1 MB, licence CC BY-SA 3.0), checked against its known hash. It lives outside
# /app/models so a volume mounted there does not hide it.
ENV LANGID_MODEL=/opt/langid/lid.176.ftz
RUN mkdir -p /opt/langid \
    && curl -fsSL -o /opt/langid/lid.176.ftz https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.ftz \
    && echo "8f3472cfe8738a7b6099e8e999c3cbfae0dcd15696aac7d7738a8039db603e83  /opt/langid/lid.176.ftz" | sha256sum -c -

# Persistent data lives in volumes: admin database, chat history, vector store, documents
RUN mkdir -p /app/admin_data /app/chats /app/chroma_db /app/data/docs /app/data/kb_files /app/models \
    && useradd --create-home --uid 10001 agent \
    && chown -R agent:agent /app
USER agent

# Bake the default embedding model (about 80 MB) into the image, so a fresh container never downloads it
RUN python -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction as F; F()(['warm up'])"

EXPOSE 8000 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl --fail http://localhost:8000/health || exit 1

# The database schema is migrated automatically at start (Alembic).
CMD ["python", "-m", "uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
