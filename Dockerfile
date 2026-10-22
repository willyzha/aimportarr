FROM python:3.12-slim

LABEL maintainer="willyzha"
LABEL description="AImportarr - AI-powered media matching and automated importer for the *arr suite"

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

# Copy source code
COPY src/ /app/src/

# Default volume for configuration
VOLUME ["/config"]

# Run the service
CMD ["python", "-m", "src.main"]
