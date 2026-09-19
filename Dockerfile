FROM python:3.11-slim

# Install system dependencies (ffmpeg is mandatory for ffsubsync)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python packages
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    ffsubsync

COPY server.py /app/server.py

CMD ["uvicorn", "server:app", "--host", "127.0.0.1", "--port", "8095"]
