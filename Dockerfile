# Paperless Claude Classifier – Dockerfile
# Zielplattform: Raspberry Pi 4 (ARM64)
# Base: python:3.11-slim (Debian Bookworm)

FROM python:3.11-slim AS base

# System-Dependencies für PyMuPDF und allgemeine Build-Tools
# libmupdf-dev + swig werden für PyMuPDF (fitz) auf ARM64 benötigt
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
        libgl1 \
    && rm -rf /var/lib/apt/lists/*

# Non-root User für Security
RUN groupadd --gid 1000 classifier \
    && useradd --uid 1000 --gid classifier --shell /bin/bash --create-home classifier

# Arbeitsverzeichnis
WORKDIR /app

# Dependencies zuerst kopieren (Docker Layer Caching)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Anwendungscode kopieren
COPY app/ ./app/

# Datenverzeichnis vorbereiten (wird als Volume gemountet)
RUN mkdir -p /app/data/logs && chown -R classifier:classifier /app/data

# Auf non-root User wechseln
USER classifier

# NiceGUI/Uvicorn Port
EXPOSE 8501

# Health-Check: prüft /health Endpoint alle 30 Sekunden
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8501/health || exit 1

# Einstiegspunkt
CMD ["python", "-m", "app.main"]
