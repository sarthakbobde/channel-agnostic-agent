# Use official Python runtime as base image
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy application code and requirements
COPY pyproject.toml .
COPY app/ ./app/
COPY channels.yaml .

# Install dependencies
RUN pip install --no-cache-dir -e .

# Create directories for persistent storage
RUN mkdir -p /app/chats /app/chroma_db /app/data/docs

# Expose port 8000 for API
EXPOSE 8000

# Health check
HEALTHCHECK CMD curl --fail http://localhost:8000/health || exit 1

# Run FastAPI app
CMD ["python", "-m", "uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
