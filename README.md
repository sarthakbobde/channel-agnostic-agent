# Channel Agnostic Agent

A knowledge-base question-answering assistant. One core answers questions from your documents
(with file and page citations) and is reachable from Slack, Microsoft Teams and Google Chat
through small per-platform adapters. Admins manage the channels and their credentials on a
web page, with no code changes or restarts.

| Read | For |
|---|---|
| [PROGRESS.md](PROGRESS.md) | What is built, how it works, open items |
| [ADMIN.md](ADMIN.md) | Setting up and using the admin page, security model |
| [TEAMS_SETUP.md](TEAMS_SETUP.md), [GCHAT_SETUP.md](GCHAT_SETUP.md) | Connecting Teams and Google Chat |
| `docs/` | Architecture and one-pager PDFs |

## Quick start (local, Windows)

```powershell
# 1. Python 3.11 environment (outside OneDrive: long paths break pip on Windows)
py -3.11 -m venv --without-pip $env:USERPROFILE\rag-venv
py -3.11 -m pip --python $env:USERPROFILE\rag-venv\Scripts\python.exe install -e .

# 2. Settings: copy .env.example to .env and fill in the LLM key and model, then add a master key
& $env:USERPROFILE\rag-venv\Scripts\python.exe -m app.secretstore generate   # paste into .env as APP_SECRET_KEY

# 3. Index your documents (data\docs) and start
.\start.ps1 -Ingest

# 4. Create the first admin (hidden password prompt), then open http://127.0.0.1:8000/admin
& $env:USERPROFILE\rag-venv\Scripts\python.exe -m app.admin.cli create-admin you@yourcompany.com
```

Channels (Slack, Teams, Google Chat) are added, tested and switched on in the admin page.
Slack uses Socket Mode and needs no public URL; Teams and Google Chat need an HTTPS address
(`.\start.ps1 -Tunnel`). The admin page only answers requests from the machine itself.

The sections below describe the Docker setup. Docker has **not** been updated for the admin
database (`APP_SECRET_KEY` and a volume for `admin_data/` are missing from `docker-compose.yml`)
and the Docker path as a whole is untested.

## Docker: Quick Start (3 commands)

```bash
# 1. Configure
cp .env.example .env
# Edit .env and add your NINEROUTER_API_KEY and LLM_MODEL

# 2. Deploy backend (in rag-assistant folder)
docker-compose up --build -d

# 3. Run Streamlit locally (in ui folder)
cd ../ui
streamlit run streamlit_app.py
```

- Backend API: http://localhost:8000
- Streamlit UI: http://localhost:8501

## How It Works

**Backend** (in Docker):
- FastAPI REST server on port 8000
- Handles RAG processing, vector search, conversation memory
- Data persists in `rag_chats/` and `rag_chroma_db/` volumes
- Location: `rag-assistant/` folder

**Frontend** (local, externalized):
- Streamlit runs on your machine on port 8501
- Calls backend API for chat responses
- No RAG processing locally
- Location: `../ui/` folder (separate)

## API Endpoints

```bash
# Health check
curl http://localhost:8000/health

# Send message
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "your question", "conv_id": "unique-id"}'

# List conversations
curl http://localhost:8000/conversations

# Get conversation history
curl http://localhost:8000/conversations/{conv_id}

# Delete conversation
curl -X DELETE http://localhost:8000/conversations/{conv_id}
```

## Configuration

Edit `.env` to customize:

```env
# Required
NINEROUTER_BASE_URL=http://localhost:20128/v1
NINEROUTER_API_KEY=sk-your-key-here
LLM_MODEL=testcombo

# Optional RAG tuning
TOP_K=5
SCORE_THRESHOLD=0.35
CHUNK_SIZE=900
CHUNK_OVERLAP=150
MEMORY_MAX_TURNS=6
```

## Docker Commands

```bash
# Start services
docker-compose up --build -d

# View logs
docker-compose logs -f rag

# Stop services
docker-compose stop

# Stop and remove containers
docker-compose down

# Stop and delete all data (careful!)
docker-compose down -v
```

## Files

- `Dockerfile` - Container image definition
- `docker-compose.yml` - Service orchestration
- `.dockerignore` - Excludes `ui/` folder from container
- `app/api.py` - FastAPI REST server
- `pyproject.toml` - Python dependencies (updated with FastAPI, Uvicorn)
- `.env.example` - Configuration template

## Troubleshooting

**Backend won't start?**
```bash
docker-compose logs rag
```

**API not responding?**
```bash
curl http://localhost:8000/health
```

**Port already in use?**
Edit `docker-compose.yml` and change the port mapping.

**Data lost after restart?**
Don't use `docker-compose down -v`. Use just `docker-compose down`.

## Deploy to Production

```bash
# Build image
docker build -t my-rag-assistant .

# Push to registry (AWS ECR example)
aws ecr get-login-password | docker login --username AWS --password-stdin [account].dkr.ecr.[region].amazonaws.com
docker tag my-rag-assistant:latest [account].dkr.ecr.[region].amazonaws.com/my-rag-assistant:latest
docker push [account].dkr.ecr.[region].amazonaws.com/my-rag-assistant:latest

# Deploy via AWS ECS, Azure Container Instances, Kubernetes, etc.
```

## Architecture

```
Local Machine          Docker Container
┌──────────────┐      ┌─────────────────────────┐
│  Streamlit   │      │  FastAPI Server         │
│  :8501       │─────→│  :8000                  │
└──────────────┘      │                         │
                      │  ┌─────────────────┐   │
                      │  │  RAG Engine     │   │
                      │  │  LangChain      │   │
                      │  └─────────────────┘   │
                      │                         │
                      │  ┌─────────────────┐   │
                      │  │  ChromaDB       │   │
                      │  │  Vector Search  │   │
                      │  └─────────────────┘   │
                      │                         │
                      │  Volumes:              │
                      │  - rag_chats/          │
                      │  - rag_chroma_db/      │
                      └─────────────────────────┘
```

## Why This Design

- **Minimal**: One Dockerfile, one docker-compose.yml
- **Clean**: UI excluded from container (kept local)
- **Scalable**: Multiple UIs can connect to one backend
- **Cloud-ready**: Deploy anywhere Docker runs
- **Simple**: No complex naming or multi-service setup
