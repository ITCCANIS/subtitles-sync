FROM python:3.11-slim

# ffmpeg/ffprobe are required by ffsubsync and for reference extraction.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && ffsubsync --help | grep -q -- "--split-penalty"

COPY sync_core.py server.py /app/

CMD ["uvicorn", "server:app", "--host", "127.0.0.1", "--port", "8095"]
