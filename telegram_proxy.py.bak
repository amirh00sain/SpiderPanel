# telegram_proxy.py — Lightweight MTProto Proxy Server for Spider Panel
# ══════════════════════════════════════════════════════════════════════════════
# Implements the Telegram MTProto obfuscated proxy protocol using asyncio.
# Each inbound gets its own MTProtoProxyServer instance on its internal port.
# Per-user secrets identify users; traffic is forwarded to Telegram servers.
# ══════════════════════════════════════════════════════════════════════════════

import asyncio
import hashlib
import logging
import secrets
import struct
from typing import Callable, Optional

logger = logging.getLogger("Spider-TelegramProxy")

# Telegram datacenter IP ranges (DC1-DC5)
TELEGRAM_DC_RANGES = [
    ("149.154.160.0", 20),   # 149.154.160.0/20
    ("91.108.4.0", 22),      # 91.108.4.0/22
    ("91.108.56.0", 22),     # 91.108.56.0/22
    ("149.154.164.0", 22),   # 149.154.164.0/22
    ("185.76.151.0", 24),    # 185.76.151.0/24
]

# Default Telegram DC addresses for routing
TELEGRAM_DCS = {
    1: ("149.154.175.50", 443),
    2: ("149.154.167.51", 443),
    3: ("149.154.175.100", 443),
    4: ("149.154.167.91", 443),
    5: ("149.154.173.131", 443),
}

# Obfuscated protocol tag: 0xefefefef
OBFUSCATED_TAG = b"\xef\xef\xef\xef"

# Secret length in the handshake
SECRET_LEN = 16

# Proxy secret prefix byte (0xEE = default proxy, 0xDD = test DC proxy)
SECRET_PREFIX_DEFAULT = 0xEE
SECRET_PREFIX_TEST = 0xDD

# Max initial handshake size
HANDSHAKE_MAX = 64

# I/O buffer size
BUF_SIZE = 64 * 1024

# Connection timeout (seconds)
CONNECT_TIMEOUT = 10.0


def derive_secret_from_uuid(config_uuid: str, salt: str = "spider-tg-proxy") -> str:
    """Derive a deterministic 16-byte hex secret from a user's config UUID.

    The secret is stable across regenerations — same UUID always produces
    the same secret. The format is compatible with t.me/proxy links:
    32 hex chars (16 bytes), prefixed with 0xEE for default proxy type.
    """
    h = hashlib.sha256(f"{salt}:{config_uuid}".encode()).digest()
    # First byte is 0xEE (default proxy type), next 15 bytes from hash
    secret_bytes = bytes([SECRET_PREFIX_DEFAULT]) + h[:15]
    return secret_bytes.hex()


def is_telegram_dc(ip: str) -> bool:
    """Check if an IP belongs to Telegram datacenter ranges."""
    try:
        parts = [int(p) for p in ip.split(".")]
        if len(parts) != 4:
            return False
        ip_int = (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]
        for base, prefix in TELEGRAM_DC_RANGES:
            base_parts = [int(p) for p in base.split(".")]
            base_int = (base_parts[0] << 24) | (base_parts[1] << 16) | (base_parts[2] << 8) | base_parts[3]
            mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
            if (ip_int & mask) == (base_int & mask):
                return True
    except Exception:
        pass
    return False


class MTProtoProxyServer:
    """Lightweight MTProto obfuscated proxy server.

    Handles the Telegram MTProto protocol handshake, extracts user secrets
    for identification, and forwards encrypted traffic to Telegram DCs.
    """

    def __init__(self, inbound_id: str, port: int, sni: str = "",
                 destination: str = "", server_name: str = ""):
        self.inbound_id = inbound_id
        self.port = port
        self.sni = sni
        self.destination = destination
        self.server_name = server_name

        # secrets_map: secret_hex -> {user_id, config_uuid, label, ...}
        self._secrets_map: dict = {}
        # user_id -> traffic bytes
        self._traffic: dict = {}
        # Active connections
        self._connections: dict = {}
        # Server socket
        self._server: Optional[asyncio.AbstractServer] = None
        self._running = False

        # Callback for traffic reporting: user_id, bytes
        self.on_traffic: Optional[Callable] = None
        # Callback for connection events
        self.on_connection: Optional[Callable] = None

    def update_secrets(self, secrets_map: dict):
        """Hot-reload user secrets without restarting the server."""
        self._secrets_map = dict(secrets_map)
        logger.info(f"[TG Proxy {self.inbound_id}] Secrets updated: {len(self._secrets_map)} users")

    def get_traffic(self) -> dict:
        """Return per-user traffic stats."""
        return dict(self._traffic)

    async def start(self):
        """Start the proxy server on the configured port."""
        if self._running:
            return
        try:
            self._server = await asyncio.start_server(
                self._handle_connection,
                "0.0.0.0",
                self.port,
                reuse_address=True,
            )
            self._running = True
            logger.info(f"[TG Proxy {self.inbound_id}] Started on port {self.port}")
        except Exception as e:
            logger.error(f"[TG Proxy {self.inbound_id}] Failed to start on port {self.port}: {e}")
            raise

    async def stop(self):
        """Gracefully stop the proxy server."""
        if not self._running:
            return
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        # Close all active connections
        for conn_id, conn in list(self._connections.items()):
            try:
                conn["writer"].close()
            except Exception:
                pass
        self._connections.clear()
        logger.info(f"[TG Proxy {self.inbound_id}] Stopped")

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle a new incoming MTProto proxy connection."""
        conn_id = secrets.token_urlsafe(6)
        peer = writer.get_extra_info("peername")
        client_ip = peer[0] if peer else "unknown"

        try:
            # Read the initial handshake (first 64 bytes)
            handshake = await asyncio.wait_for(reader.read(HANDSHAKE_MAX), timeout=CONNECT_TIMEOUT)
            if len(handshake) < 64:
                writer.close()
                return

            # Extract the secret (bytes 8-24, after random padding)
            secret_hex = handshake[8:24].hex()

            # Look up the user by secret
            user_info = self._secrets_map.get(secret_hex)
            if not user_info:
                logger.warning(f"[TG Proxy {self.inbound_id}] Unknown secret from {client_ip}")
                writer.close()
                return

            user_id = user_info.get("user_id", "unknown")
            config_uuid = user_info.get("config_uuid", "")

            logger.info(f"[TG Proxy {self.inbound_id}] Connection from {client_ip} user={user_info.get('label', user_id)}")

            # Determine the target Telegram DC
            # The DC ID is encoded in the handshake; for default proxy, use DC2
            dc_id = 2
            if self.destination:
                # Custom destination: parse host:port
                if ":" in self.destination:
                    target_host, target_port = self.destination.rsplit(":", 1)
                    target_port = int(target_port)
                else:
                    target_host, target_port = self.destination, 443
            else:
                target_host, target_port = TELEGRAM_DCS.get(dc_id, TELEGRAM_DCS[2])

            # Connect to the Telegram DC
            target_reader, target_writer = await asyncio.wait_for(
                asyncio.open_connection(target_host, target_port),
                timeout=CONNECT_TIMEOUT,
            )

            # Forward the handshake to the target
            target_writer.write(handshake)
            await target_writer.drain()

            # Register connection
            self._connections[conn_id] = {
                "user_id": user_id,
                "client_ip": client_ip,
                "target": f"{target_host}:{target_port}",
                "bytes_up": 0,
                "bytes_down": 0,
            }

            # Start bidirectional forwarding
            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(self._forward(reader, target_writer, conn_id, user_id, "up")),
                    asyncio.create_task(self._forward(target_reader, writer, conn_id, user_id, "down")),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"[TG Proxy {self.inbound_id}] Connection error: {e}")
        finally:
            # Report final traffic
            conn = self._connections.pop(conn_id, None)
            if conn and self.on_traffic:
                total = conn["bytes_up"] + conn["bytes_down"]
                if total > 0:
                    self._traffic[user_id] = self._traffic.get(user_id, 0) + total
                    try:
                        self.on_traffic(user_id, total)
                    except Exception:
                        pass
            # Close both sides
            try:
                writer.close()
            except Exception:
                pass
            try:
                target_writer.close()
            except Exception:
                pass

    async def _forward(self, src: asyncio.StreamReader, dst: asyncio.StreamWriter,
                       conn_id: str, user_id: str, direction: str):
        """Forward data between src and dst, tracking bytes."""
        try:
            while True:
                data = await src.read(BUF_SIZE)
                if not data:
                    break
                dst.write(data)
                if dst.transport.get_write_buffer_size() > 0:
                    await dst.drain()
                # Track traffic
                conn = self._connections.get(conn_id)
                if conn:
                    if direction == "up":
                        conn["bytes_up"] += len(data)
                    else:
                        conn["bytes_down"] += len(data)
        except (asyncio.CancelledError, Exception):
            pass
