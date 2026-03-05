FROM python:3.12-slim

# Metadata
LABEL org.opencontainers.image.title="distrotv-proxy"
LABEL org.opencontainers.image.description="DistroTV HLS proxy for Channels DVR and VLC"
LABEL org.opencontainers.image.source="https://github.com/YOUR_USERNAME/distrotv-proxy"
LABEL org.opencontainers.image.licenses="MIT"

# Don't write .pyc files, don't buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install deps first (better layer caching — only re-runs if requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app and templates
COPY app.py .
COPY templates/ templates/

# Create non-root user and data directory
# /data is chmod 777 so it remains writable even if a host volume is mounted
# with different ownership (e.g. root-owned bind mounts or named volumes on
# some Docker setups).  The container drops to appuser immediately after.
RUN useradd -r -s /bin/false appuser && \
    mkdir -p /data && \
    chmod 777 /data

USER appuser

# State file lives in /data (mount a volume here for persistence)
ENV STATE_FILE=/data/channel_state.json \
    PORT=8787

EXPOSE 8787

# Health check — give it 60s to warm up (startup probe takes ~20s)
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/health')"

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
