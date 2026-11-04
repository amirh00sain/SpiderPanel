
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential \
       git \
       curl \
       ca-certificates \
       libssl-dev \
       zlib1g-dev \
       pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Detect the target architecture and build MTProxy where supported.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) \
        git clone --depth 1 \
          https://github.com/TelegramMessenger/MTProxy.git /tmp/MTProxy; \
        if ! make -C /tmp/MTProxy; then \
          make -C /tmp/MTProxy clean || true; \
          echo "ERROR: MTProxy compilation failed on AMD64."; \
          exit 1; \
        fi; \
        install -m 0755 \
          /tmp/MTProxy/objs/bin/mtproto-proxy \
          /usr/local/bin/mtproto-proxy; \
        rm -rf /tmp/MTProxy \
        ;; \
      arm64) \
        echo "ARM64 detected: upstream MTProxy build skipped."; \
        echo "The panel will build, but MTProxy is not available."; \
        ;; \
      *) \
        echo "Unsupported architecture: $arch"; \
        exit 1 \
        ;; \
    esac

# Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application source
COPY . .

# Fail early if the main Python modules contain syntax errors.
RUN python -m py_compile main.py vercel_relay.py

EXPOSE 8080
EXPOSE 443

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
