FROM python:3.12-slim

LABEL maintainer="willyzha"
LABEL description="AI-powered queue companion & secure archive unpacker for Sonarr and Radarr"
LABEL org.opencontainers.image.title="AImportarr"
LABEL org.opencontainers.image.description="AI-powered queue companion & secure archive unpacker for Sonarr and Radarr — automatically resolves stuck imports, extracts archives in a sandbox, and blocklists downgrades or malware while preserving seeding."
LABEL org.opencontainers.image.source="https://github.com/willyzha/aimportarr"
LABEL org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AIMPORTARR_CONFIG=/config/config.yml

WORKDIR /app

# Install 7-Zip extraction tools
RUN apt-get update && \
    apt-get install -y --no-install-recommends 7zip p7zip-full unrar-free && \
    rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code and assets
COPY src/ /app/src/
COPY assets/ /app/assets/

# Default volume for configuration
VOLUME ["/config"]

# Expose Web UI & Metrics Dashboard
EXPOSE 9890

# Run the service
CMD ["python", "-m", "src.main"]
