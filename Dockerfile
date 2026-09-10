FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential git curl ca-certificates libssl-dev zlib1g-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Build the official Telegram MTProxy binary.
RUN git clone --depth 1 https://github.com/TelegramMessenger/MTProxy.git /tmp/MTProxy \
    && make -C /tmp/MTProxy \
    && install -m 0755 /tmp/MTProxy/objs/bin/mtproto-proxy /usr/local/bin/mtproto-proxy \
    && rm -rf /tmp/MTProxy

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .
RUN python -m py_compile main.py telegram_proxy.py relay_vless.py shared.py pages.py

EXPOSE 8080
EXPOSE 443

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
