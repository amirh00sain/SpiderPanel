from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import signal
import socket
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("Spider-TelegramProxy")
SECRET_RE = re.compile(r"^[0-9a-fA-F]{32}$")
BASE = Path(os.environ.get("SPIDER_DATA_DIR", "/data"))
TG_DIR = BASE / "telegram"
TG_DIR.mkdir(parents=True, exist_ok=True)
BIN = os.environ.get("MTPROTO_PROXY_BIN", "/usr/local/bin/mtproto-proxy")
STATS_BASE = int(os.environ.get("MTPROTO_STATS_PORT", "2398"))
WORKERS = max(1, int(os.environ.get("MTPROTO_WORKERS", "2")))
MAX_CONNECTIONS = max(1000, int(os.environ.get("MTPROTO_MAX_CONNECTIONS", "60000")))


def derive_secret_from_uuid(config_uuid: str, salt: str = "spider-tg-proxy") -> str:
    """Return the exact 16-byte / 32-hex client secret expected by official MTProxy."""
    return hashlib.sha256(f"{salt}:{config_uuid}".encode()).hexdigest()[:32]


def validate_secret(secret: str) -> str:
    secret = str(secret or "").strip().lower()
    # Railway sample uses plain 32-hex secrets. Do not add an implicit dd prefix.
    if not SECRET_RE.fullmatch(secret):
        raise ValueError("MTProxy secret must be exactly 32 hexadecimal characters")
    return secret


def is_docker_available() -> bool:
    return False


def run_docker_telegram_proxy(*args, **kwargs):
    return None


def stop_docker_telegram_proxy(*args, **kwargs):
    return None


def _railway_tcp_info() -> tuple[str, int, int]:
    domain = str(os.environ.get("RAILWAY_TCP_PROXY_DOMAIN") or "").strip()
    public_port = int(os.environ.get("RAILWAY_TCP_PROXY_PORT") or "0")
    app_port = int(os.environ.get("RAILWAY_TCP_APPLICATION_PORT") or "0")
    return domain, public_port, app_port


def _get_public_ip() -> str:
    # NAT-info must use the instance's real outbound/public IPv4.
    # Do NOT resolve RAILWAY_TCP_PROXY_DOMAIN here: that is the inbound TCP
    # proxy endpoint and is not necessarily the egress/NAT address used by
    # MTProxy when it connects to Telegram DCs.
    try:
        import urllib.request
        for url in ("https://api.ipify.org", "https://digitalresistance.dog/myIp"):
            try:
                with urllib.request.urlopen(url, timeout=5) as r:
                    ip = r.read().decode().strip()
                if ip:
                    return ip
            except Exception:
                continue
    except Exception:
        pass
    return ""


def _get_internal_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


async def _download_official_files() -> tuple[Path, Path]:
    """Download Telegram's proxy secret/config files, refreshing the config daily."""
    secret_file = TG_DIR / "proxy-secret"
    config_file = TG_DIR / "proxy-multi.conf"
    import urllib.request

    if not secret_file.exists() or secret_file.stat().st_size == 0:
        await asyncio.to_thread(urllib.request.urlretrieve, "https://core.telegram.org/getProxySecret", secret_file)
    if not config_file.exists() or (asyncio.get_running_loop().time() - config_file.stat().st_mtime) > 86400:
        await asyncio.to_thread(urllib.request.urlretrieve, "https://core.telegram.org/getProxyConfig", config_file)
    return secret_file, config_file


class MTProtoProxyServer:
    """Wrapper around the official Telegram MTProxy binary.

    Railway exposes TCP separately from the HTTP service. Therefore MTProxy listens
    on RAILWAY_TCP_APPLICATION_PORT while Uvicorn continues using PORT.
    """

    def __init__(self, inbound_id: str, port: int, sni: str = "", destination: str = "", server_name: str = ""):
        self.inbound_id = inbound_id
        self.railway_domain, self.railway_public_port, railway_app_port = _railway_tcp_info()
        # The inbound's Internal Port is authoritative for the MTProxy listener.
        # Railway's TCP application port must be configured to the same value.
        self.port = int(port)
        if railway_app_port and int(railway_app_port) != self.port:
            logger.warning(
                "[TG Proxy %s] Railway TCP application port (%s) differs from inbound Internal Port (%s); "
                "set RAILWAY_TCP_APPLICATION_PORT/TCP Proxy target to the Internal Port.",
                inbound_id, railway_app_port, self.port,
            )
        self.sni = self.destination = self.server_name = ""
        self._secrets_map: Dict[str, dict] = {}
        self._process: Optional[asyncio.subprocess.Process] = None
        self._running = False
        self._stdout_task: Optional[asyncio.Task] = None

    def update_secrets(self, secrets_map: dict):
        cleaned = {}
        for secret, info in (secrets_map or {}).items():
            try:
                cleaned[validate_secret(secret)] = dict(info or {})
            except Exception:
                logger.warning("Skipping invalid MTProxy secret for inbound %s", self.inbound_id)
        self._secrets_map = cleaned
        logger.info("[TG Proxy %s] Secrets updated: %d users", self.inbound_id, len(cleaned))

    def get_traffic(self) -> dict:
        return {}

    def _stats_port(self) -> int:
        # Keep stats local-only and deterministic per process.
        return STATS_BASE

    async def start(self):
        if self._running:
            return
        if not self._secrets_map:
            logger.info("[TG Proxy %s] no users/secrets yet; listener not started", self.inbound_id)
            return
        if not Path(BIN).exists():
            raise RuntimeError(f"official mtproto-proxy binary not found at {BIN}")

        secret_file, config_file = await _download_official_files()
        internal_ip = _get_internal_ip()
        public_ip = _get_public_ip()
        secret_args = []
        for secret in self._secrets_map:
            secret_args.extend(["-S", secret])

        # The exact structure is based on the official Telegram MTProxy runner:
        # -p = local stats port, -H = client-facing listener port.
        cmd = [
            BIN,
            "-p", str(self._stats_port()),
            "-H", str(self.port),
            "-M", str(WORKERS),
            "-C", str(MAX_CONNECTIONS),
            "--aes-pwd", str(secret_file),
            "-u", "nobody",
            str(config_file),
            "--allow-skip-dh",
        ]
        if internal_ip and public_ip:
            cmd += ["--nat-info", f"{internal_ip}:{public_ip}"]
        cmd += secret_args

        domain, public_port, _ = _railway_tcp_info()
        logger.info("[TG Proxy %s] Starting official MTProxy", self.inbound_id)
        logger.info("[TG Proxy %s] client listener: 0.0.0.0:%s", self.inbound_id, self.port)
        if domain and public_port:
            logger.info("[TG Proxy %s] Railway public endpoint: %s:%s", self.inbound_id, domain, public_port)
        logger.info("[TG Proxy %s] command: %s", self.inbound_id, " ".join(cmd[:-2]) + " <secrets>")

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._running = True
        self._stdout_task = asyncio.create_task(self._watch_output())
        await asyncio.sleep(0.8)
        if self._process and self._process.returncode is not None:
            rc = self._process.returncode
            self._process = None
            self._running = False
            raise RuntimeError(f"mtproto-proxy exited immediately with code {rc}")
        logger.info("[TG Proxy %s] MTProxy is listening on internal port %s", self.inbound_id, self.port)

    async def _watch_output(self):
        proc = self._process
        if not proc or not proc.stdout:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                logger.info("[TG Proxy %s] %s", self.inbound_id, line.decode(errors="replace").rstrip())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("MTProxy log watcher stopped: %s", exc)

    async def stop(self):
        self._running = False
        proc = self._process
        self._process = None
        if self._stdout_task:
            self._stdout_task.cancel()
            self._stdout_task = None
        if not proc:
            return
        if proc.returncode is None:
            try:
                proc.send_signal(signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass
        logger.info("[TG Proxy %s] stopped", self.inbound_id)

    async def restart(self):
        await self.stop()
        await self.start()


TGProxy = MTProtoProxyServer
