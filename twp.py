"""Telegram Web Proxy (TWP) core: framing, MTProxy crypto, signing and relay.

SpiderPanel serves this protocol on the panel's OWN public TLS + WebSocket
entry point (Railway terminates TLS on 443 and forwards HTTPS/WSS to the
application on 8080), so this module never binds a socket of its own.

Wire flow, matching the reference implementation:

  1. ``GET /?bridge=<capability>``  → the bridge HTML page (capability is an
     HMAC of the user's proxy secret, so only a real link holder gets it).
  2. ``POST /api/v1/session``       → validates a short-lived bootstrap token
     plus a 9-byte HELLO frame, returns an 8-byte WELCOME frame and a
     43-character session token.
  3. ``WS  /api/v1/ws``             → subprotocol ``tproxy-v1.<session token>``
     carries the framed binary stream that this module relays to Telegram.

The MTProxy handshake inside each stream authenticates against the PER-USER
secret; the signing key is only ever used for bootstrap/session tokens.

This module deliberately imports nothing from ``main`` so it stays unit
testable; ``main`` injects the user lookup / authorisation callbacks.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import struct
import time
from pathlib import Path
from typing import Awaitable, Callable, Coroutine, Iterable, Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger("SpiderPanel.twp")

# ── Frame protocol ───────────────────────────────────────────────────────────
FRAME_OPEN = 0x01
FRAME_DATA = 0x02
FRAME_CLOSE = 0x03
FRAME_WINDOW = 0x04
FRAME_PING = 0x05
FRAME_PONG = 0x06
FRAME_HELLO = 0x10
FRAME_WELCOME = 0x11
FRAME_BYE = 0x1F

HEADER_SIZE = 8
MAX_PAYLOAD = 1024 * 1024
MAX_BATCH_FRAMES = 4096
INITIAL_WINDOW = 4 * 1024 * 1024
DATA_CHUNK = 64 * 1024
MAX_WS_MESSAGE = 2 * 1024 * 1024

SESSION_TTL_SECONDS = 300
MAX_STREAMS = 64

HANDSHAKE_TIMEOUT = 15.0
CONNECT_TIMEOUT = 10.0
WRITE_TIMEOUT = 30.0

# Bounds mirror the reference relay so a single session cannot pin memory.
LIMIT_INGRESS_BYTES = 8 * 1024 * 1024
LIMIT_INGRESS_ITEMS = 128
LIMIT_UPLINK_BYTES = 8 * 1024 * 1024
LIMIT_UPLINK_ITEMS = 8192
LIMIT_STREAM_BYTES = INITIAL_WINDOW + 1024 * 1024
LIMIT_STREAM_ITEMS = 4096
LIMIT_DOWNLINK_BYTES = 8 * 1024 * 1024
LIMIT_DOWNLINK_ITEMS = 8192
ITEM_COST = 256

WELCOME_FRAME = bytes((FRAME_WELCOME, 0, 0, 0, 0, 0, 0, 0))

TELEGRAM_DCS = {
    1: "149.154.175.50",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "149.154.171.5",
}
_MT_PROXY_TAGS = {0xEFEFEFEF, 0xEEEEEEEE, 0xDDDDDDDD}

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
CAPABILITY_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
PROXY_SECRET_RE = re.compile(r"^(?:[0-9a-f]{32}|dd[0-9a-f]{32})$")

SIGNING_KEY_ENV = "SPIDER_TWP_SIGNING_KEY"
SIGNING_KEY_FILENAME = "twp_signing.key"
_SIGNING_CONTEXT = b"spider-twp/session-signing/v1\n"
_SESSION_TOKEN_CONTEXT = b"spider-twp/session-token/v1\n"
_BRIDGE_CONTEXT = b"tdesktop-web-proxy-bridge-v1\n"

SigningKeyProvider = Callable[[], Optional[bytes]]
UserResolver = Callable[[str], Awaitable[Optional[dict]]]
BytesSink = Callable[[str, int, int], None]


# ── Encoding helpers ─────────────────────────────────────────────────────────
def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(str(value or "")) % 4)
    return base64.urlsafe_b64decode(str(value or "") + pad)


def constant_time_equal(a: bytes | str, b: bytes | str) -> bool:
    if isinstance(a, str):
        a = a.encode("utf-8")
    if isinstance(b, str):
        b = b.encode("utf-8")
    return hmac.compare_digest(a, b)


def _spawn(coro: Coroutine) -> Optional[asyncio.Task]:
    """Schedule a fire-and-forget coroutine from either sync or async context."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return None
    return loop.create_task(coro)


def encode_frame(frame_type: int, stream_id: int, payload: bytes = b"") -> bytes:
    if not 0 <= stream_id <= 0xFFFFFF:
        raise ValueError("invalid stream id")
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("invalid payload")
    header = struct.pack(">II", ((frame_type & 0xFF) << 24) | stream_id, len(payload))
    return header + payload


def parse_frames(data: bytes) -> list[tuple[int, int, bytes]]:
    """Split one WebSocket message into ``(type, stream_id, payload)`` frames."""
    frames: list[tuple[int, int, bytes]] = []
    offset = 0
    total = len(data)
    while offset < total:
        if len(frames) >= MAX_BATCH_FRAMES or total - offset < HEADER_SIZE:
            raise ValueError("bad batch")
        chunk = data[offset:offset + HEADER_SIZE]
        length = struct.unpack(">I", chunk[4:8])[0]
        if length > MAX_PAYLOAD or offset + HEADER_SIZE + length > total:
            raise ValueError("bad payload")
        stream_id = (chunk[1] << 16) | (chunk[2] << 8) | chunk[3]
        frames.append((chunk[0], stream_id, data[offset + HEADER_SIZE:offset + HEADER_SIZE + length]))
        offset += HEADER_SIZE + length
    if not frames:
        raise ValueError("empty batch")
    return frames


def is_hello(frames: Iterable[tuple[int, int, bytes]]) -> bool:
    items = list(frames)
    if len(items) != 1:
        return False
    frame_type, stream_id, payload = items[0]
    return frame_type == FRAME_HELLO and stream_id == 0 and payload == b"\x01"


def parse_proxy_secret(value) -> bytes:
    """Return the 16-byte inner MTProxy secret for a 32-hex (optionally dd) secret."""
    text = str(value or "").strip().lower()
    if not PROXY_SECRET_RE.fullmatch(text):
        raise ValueError("proxy secret must be 32 hex characters, optionally prefixed with dd")
    if text.startswith("dd"):
        text = text[2:]
    return bytes.fromhex(text)


# ── AES-CTR (NIST SP 800-38A counter: byte 15 is the least significant) ─────
class AesCtrStream:
    """Stateful AES-256-CTR keystream, matching the reference AesCtrStream."""

    __slots__ = ("_ctx",)

    def __init__(self, key: bytes, iv: bytes):
        if len(key) != 32 or len(iv) != 16:
            raise ValueError("bad AES material")
        self._ctx = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()

    def crypt(self, data: bytes) -> bytes:
        if not data:
            return b""
        return self._ctx.update(data)


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def accept_client_handshake(packet: bytes, secret: bytes) -> tuple[int, int, AesCtrStream, AesCtrStream]:
    """Validate a 64-byte client hello and derive the per-direction ciphers.

    Returns ``(dc_tag, dc_id, client_decrypt, client_encrypt)``.

    ``client_decrypt`` has already consumed the 64-byte hello, so uplink data
    continues the same counter. ``client_encrypt`` is fresh: the downlink
    direction is an independent stream, exactly as in the reference relay.
    """
    if len(packet) != 64:
        raise ValueError("bad handshake length")
    material = packet[8:56]
    reversed_material = material[::-1]

    client_decrypt = AesCtrStream(_sha256(material[0:32] + secret), material[32:48])
    client_encrypt = AesCtrStream(_sha256(reversed_material[0:32] + secret), reversed_material[32:48])

    plain = client_decrypt.crypt(packet)
    dc_tag = struct.unpack("<I", plain[56:60])[0]
    dc_id = struct.unpack("<h", plain[60:62])[0]
    if dc_tag not in _MT_PROXY_TAGS or abs(dc_id) not in TELEGRAM_DCS:
        raise ValueError("invalid MTProxy handshake")
    return dc_tag, dc_id, client_decrypt, client_encrypt


def _reserved_nonce(nonce: bytes) -> bool:
    if nonce[0] == 0xEF:
        return True
    word0 = struct.unpack("<I", nonce[0:4])[0]
    word1 = struct.unpack("<I", nonce[4:8])[0]
    if word0 in (0x44414548, 0x54534F50, 0x20544547, 0xEEEEEEEE, 0xDDDDDDDD) or word1 == 0:
        return True
    return False


def create_telegram_handshake(dc_tag: int, dc_id: int) -> tuple[bytes, AesCtrStream, AesCtrStream]:
    """Build the upstream 64-byte hello for a Telegram DC.

    Returns ``(wire, telegram_encrypt, telegram_decrypt)`` where ``wire`` is what
    must be written to the DC socket first.
    """
    while True:
        nonce = bytearray(secrets.token_bytes(64))
        if not _reserved_nonce(bytes(nonce)):
            break
    struct.pack_into("<I", nonce, 56, dc_tag)
    struct.pack_into("<h", nonce, 60, dc_id)
    nonce[62] = nonce[63] = 0

    material = bytes(nonce[8:56])
    reversed_material = material[::-1]

    telegram_encrypt = AesCtrStream(material[0:32], material[32:48])
    telegram_decrypt = AesCtrStream(reversed_material[0:32], reversed_material[32:48])

    encrypted = telegram_encrypt.crypt(bytes(nonce))
    wire = bytes(nonce[:56]) + encrypted[56:64]
    return wire, telegram_encrypt, telegram_decrypt


# ── Signing key ──────────────────────────────────────────────────────────────
class SigningKeyStore:
    """Resolve the HMAC signing key once and keep it stable for the process.

    Preference order:
      1. ``SPIDER_TWP_SIGNING_KEY`` — operators running several replicas must
         set the same value on every replica.
      2. A key persisted in the panel's data directory, created atomically with
         ``O_EXCL`` and 0600 permissions so concurrent starts cannot race.

    Returns ``None`` when neither is available, which callers surface as the
    ``not_ready`` inbound status instead of handing out unverifiable links.
    """

    def __init__(self, data_dir: Path | str, env: Optional[dict] = None):
        self._data_dir = Path(data_dir)
        self._env = env
        self._key: Optional[bytes] = None
        self._resolved = False
        self._error: str = ""

    @property
    def error(self) -> str:
        return self._error

    def _env_value(self) -> str:
        source = os.environ if self._env is None else self._env
        return str(source.get(SIGNING_KEY_ENV) or "").strip()

    def get(self) -> Optional[bytes]:
        if self._resolved:
            return self._key
        self._resolved = True

        env_key = self._env_value()
        if env_key:
            if len(env_key.encode("utf-8")) < 16:
                self._error = f"{SIGNING_KEY_ENV} must be at least 16 characters"
                logger.error("TWP signing key rejected: %s", self._error)
                return None
            self._key = env_key.encode("utf-8")
            self._error = ""
            return self._key

        path = self._data_dir / SIGNING_KEY_FILENAME
        try:
            existing = path.read_bytes()
        except FileNotFoundError:
            existing = b""
        except OSError as exc:
            self._error = f"cannot read the persisted signing key: {exc}"
            logger.error("TWP signing key read failed: %s", exc)
            return None

        if existing:
            if len(existing) < 16:
                self._error = "the persisted signing key is too short"
                logger.error("TWP signing key rejected: %s", self._error)
                return None
            self._key = existing
            self._error = ""
            return self._key

        generated = secrets.token_bytes(32)
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            # O_EXCL makes creation atomic: a losing racer re-reads the winner.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                existing = path.read_bytes()
                if len(existing) >= 16:
                    self._key = existing
                    self._error = ""
                    return self._key
            except OSError:
                pass
            self._error = "the persisted signing key is unreadable"
            return None
        except OSError as exc:
            self._error = f"cannot persist the signing key (set {SIGNING_KEY_ENV}): {exc}"
            logger.error("TWP signing key creation failed: %s", exc)
            return None

        try:
            os.write(fd, generated)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self._key = generated
        self._error = ""
        logger.info("TWP signing key initialised at %s", path)
        return self._key


def hmac_sign(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


# ── Tokens ───────────────────────────────────────────────────────────────────
def create_bootstrap_token(key: bytes, user_id: str, host: str, ttl: int = SESSION_TTL_SECONDS,
                           now: Optional[float] = None) -> str:
    """Issue a short-lived token that lets a user open a relay session."""
    issued = int(now if now is not None else time.time())
    payload = b64url(json.dumps(
        {"u": str(user_id), "h": str(host), "exp": issued + max(30, int(ttl)), "n": b64url(secrets.token_bytes(16))},
        separators=(",", ":"),
    ).encode("utf-8"))
    return f"{payload}.{b64url(hmac_sign(key, payload.encode('ascii')))}"


def verify_bootstrap_token(key: bytes, token: str, host: str, now: Optional[float] = None) -> Optional[str]:
    """Return the authorised user id, or ``None`` when the token is not usable."""
    if not token or token.count(".") != 1:
        return None
    payload, signature = token.split(".")
    if not payload or not signature:
        return None
    expected = b64url(hmac_sign(key, payload.encode("ascii")))
    if not constant_time_equal(signature, expected):
        return None
    try:
        data = json.loads(b64url_decode(payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    expiry = data.get("exp")
    if not isinstance(expiry, int) or expiry < int(now if now is not None else time.time()):
        return None
    if str(data.get("h") or "") != str(host or ""):
        return None
    user_id = str(data.get("u") or "")
    return user_id or None


def create_session_token(key: bytes, host: str) -> str:
    """Mint an opaque 43-character token bound to the canonical hostname."""
    nonce = secrets.token_bytes(16)
    mac = hmac_sign(key, _SESSION_TOKEN_CONTEXT + str(host).encode("utf-8") + b"\n" + nonce)
    return b64url(nonce + mac[:16])


def verify_session_token(key: bytes, host: str, token: str) -> bool:
    if not token or not TOKEN_RE.fullmatch(token):
        return False
    try:
        raw = b64url_decode(token)
    except (ValueError, binascii.Error, UnicodeEncodeError):
        return False
    if len(raw) != 32 or b64url(raw) != token:
        return False
    mac = hmac_sign(key, _SESSION_TOKEN_CONTEXT + str(host).encode("utf-8") + b"\n" + raw[:16])
    return constant_time_equal(raw[16:], mac[:16])


def compute_bridge_capability(secret: bytes, host: str) -> str:
    """Capability a link holder presents to fetch the bridge page."""
    return b64url(hmac_sign(secret, _BRIDGE_CONTEXT + str(host).encode("utf-8")))


def capability_matches(candidate: str, secret: bytes, host: str) -> bool:
    if not candidate or not CAPABILITY_RE.fullmatch(candidate):
        return False
    return constant_time_equal(candidate, compute_bridge_capability(secret, host))


# ── Session registry ─────────────────────────────────────────────────────────
class RelaySession:
    """One authenticated WebSocket session holding zero or more TCP streams."""

    def __init__(
        self,
        token: str,
        user_id: str,
        host: str,
        secret: bytes,
        *,
        send: Optional[Callable[[bytes], Awaitable[None]]] = None,
        on_bytes: Optional[BytesSink] = None,
        on_close: Optional[Callable[[str], Awaitable[None]]] = None,
        dc_connect: Optional[Callable[[str, int], Awaitable[tuple]]] = None,
        max_streams: int = MAX_STREAMS,
    ):
        self.token = token
        self.user_id = user_id
        self.host = host
        self.secret = secret
        self._send = send
        self._on_bytes = on_bytes
        self._on_close = on_close
        self._dc_connect = dc_connect or _open_dc_socket
        self.max_streams = max(1, int(max_streams))

        self.streams: dict[int, dict] = {}
        self.closed_ids: set[int] = set()
        self.closed = False
        self.created_at = time.time()
        self.expires_at = time.time() + SESSION_TTL_SECONDS

        self.ingress_bytes = 0
        self.ingress_items = 0
        self.uplink_bytes = 0
        self.uplink_items = 0
        self.downlink_bytes = 0
        self.downlink_items = 0
        self._downlink_waiters: set[asyncio.Future] = set()
        self._message_chain: Optional[asyncio.Task] = None
        self._send_lock = asyncio.Lock()
        self.uplink_total = 0
        self.downlink_total = 0

    # ── message pump ─────────────────────────────────────────────────────────
    async def on_ws_message(self, data: bytes) -> None:
        if self.closed:
            return
        length = len(data)
        if not length or length > MAX_WS_MESSAGE:
            await self.protocol_error("invalid_websocket_message")
            return
        cost = length + ITEM_COST
        if self.ingress_bytes + cost > LIMIT_INGRESS_BYTES or self.ingress_items >= LIMIT_INGRESS_ITEMS:
            await self.protocol_error("ingress_queue_limit")
            return
        self.ingress_bytes += cost
        self.ingress_items += 1
        try:
            await self._dispatch(data)
        except asyncio.CancelledError:
            raise
        except ValueError:
            await self.protocol_error("frame_parse_failed")
        finally:
            self.ingress_bytes -= cost
            self.ingress_items -= 1

    async def _dispatch(self, data: bytes) -> None:
        for frame_type, stream_id, payload in parse_frames(data):
            if self.closed:
                return
            if stream_id == 0:
                if frame_type != FRAME_PONG or len(payload) > 64:
                    await self.protocol_error("invalid_control_frame")
                    return
                continue
            if frame_type == FRAME_OPEN:
                self._open_stream(stream_id, payload)
            elif frame_type == FRAME_DATA:
                await self._queue_write(stream_id, payload)
            elif frame_type == FRAME_WINDOW:
                if not self._add_window(stream_id, payload):
                    return
            elif frame_type == FRAME_CLOSE:
                if payload:
                    await self.protocol_error("invalid_close")
                    return
                if stream_id not in self.streams and stream_id not in self.closed_ids:
                    await self.protocol_error("unknown_stream")
                    return
                await self.close_stream(stream_id, notify=False)
            else:
                await self.protocol_error("unknown_frame_type")
                return

    # ── stream lifecycle ─────────────────────────────────────────────────────
    def _open_stream(self, stream_id: int, payload: bytes) -> None:
        if (payload or stream_id in self.streams or stream_id in self.closed_ids):
            _spawn(self.protocol_error("invalid_open"))
            return
        if len(self.streams) >= self.max_streams:
            self.closed_ids.add(stream_id)
            _spawn(self._emit(FRAME_CLOSE, stream_id))
            return
        self.streams[stream_id] = {
            "id": stream_id,
            "handshake": b"",
            "handshake_bytes": 0,
            "writer": None,
            "reader": None,
            "reader_task": None,
            "drain_task": None,
            "queue": [],
            "receive_window": INITIAL_WINDOW,
            "send_credit": INITIAL_WINDOW,
            "closed": False,
            "abort": asyncio.Event(),
            "sent": [],
            "downlink_bytes": 0,
            "downlink_items": 0,
            "user_id": self.user_id,
        }

    async def _queue_write(self, stream_id: int, payload: bytes) -> None:
        stream = self.streams.get(stream_id)
        if stream is None:
            if stream_id in self.closed_ids and payload:
                return
            await self.protocol_error("unknown_stream")
            return
        length = len(payload)
        if not length or length > stream["receive_window"]:
            await self.protocol_error("invalid_data_window")
            return
        cost = length + HEADER_SIZE + ITEM_COST
        if (LIMIT_STREAM_BYTES < cost or stream["receive_window"] - length < 0):
            await self.protocol_error("invalid_data_window")
            return
        if (stream_queue_bytes(stream) + cost > LIMIT_STREAM_BYTES
                or len(stream["queue"]) >= LIMIT_STREAM_ITEMS
                or self.uplink_bytes + cost > LIMIT_UPLINK_BYTES
                or self.uplink_items >= LIMIT_UPLINK_ITEMS):
            await self.close_stream(stream_id, notify=True)
            return
        stream["receive_window"] -= length
        stream["queue"].append((bytes(payload), cost))
        self.uplink_bytes += cost
        self.uplink_items += 1
        if stream["drain_task"] is None or stream["drain_task"].done():
            stream["drain_task"] = asyncio.get_event_loop().create_task(self._drain(stream))

    async def _drain(self, stream: dict) -> None:
        stream_id = stream["id"]
        try:
            while not self.closed and not stream["closed"] and stream["queue"]:
                payload, cost = stream["queue"].pop(0)
                try:
                    await self._write_stream(stream, payload)
                finally:
                    self.uplink_bytes -= cost
                    self.uplink_items -= 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - relay must never take the app down
            logger.debug("TWP stream %s write failed: %s", stream_id, exc)
            await self.close_stream(stream_id, notify=True)
        finally:
            stream["drain_task"] = None

    async def _write_stream(self, stream: dict, payload: bytes) -> None:
        consumed = 0
        if stream["writer"] is None:
            take = min(64 - len(stream["handshake"]), len(payload))
            joined = stream["handshake"] + payload[:take]
            stream["handshake"] = joined
            stream["handshake_bytes"] += take
            stream["receive_window"] += take  # an unvalidated hello grants no credit
            if len(joined) < 64:
                self._grant(stream, take)
                stream["handshake"] = b""
                stream["handshake_bytes"] = 0
                return
            # Re-derive from the pristine 64 bytes: the accumulated buffer IS
            # the hello, so decrypt it exactly once.
            dc_tag, dc_id, client_decrypt, client_encrypt = accept_client_handshake(stream["handshake"], self.secret)
            wire, tg_encrypt, tg_decrypt = create_telegram_handshake(dc_tag, dc_id)

            reader, writer = await asyncio.wait_for(
                self._dc_connect(TELEGRAM_DCS[abs(dc_id)], 443), timeout=CONNECT_TIMEOUT)
            stream["reader"] = reader
            stream["writer"] = writer
            stream["client_decrypt"] = client_decrypt
            stream["client_encrypt"] = client_encrypt
            stream["tg_encrypt"] = tg_encrypt
            stream["tg_decrypt"] = tg_decrypt
            stream["dc_id"] = dc_id
            stream["handshake"] = b""
            stream["handshake_bytes"] = 0

            writer.write(wire)
            await asyncio.wait_for(writer.drain(), timeout=WRITE_TIMEOUT)

            stream["reader_task"] = asyncio.get_event_loop().create_task(self._read_stream(stream))
            consumed = take
            remainder = payload[take:]
            self._grant(stream, take)
            if not remainder:
                return
            payload = remainder

        for offset in range(0, len(payload), DATA_CHUNK):
            chunk = payload[offset:offset + DATA_CHUNK]
            plain = stream["client_decrypt"].crypt(chunk)
            encrypted = stream["tg_encrypt"].crypt(plain)
            stream["writer"].write(encrypted)
            await asyncio.wait_for(stream["writer"].drain(), timeout=WRITE_TIMEOUT)
            self.uplink_total += len(chunk)

        self._grant(stream, len(payload))
        if self._on_bytes is not None:
            self._on_bytes(self.user_id, self.uplink_total, 0)
            self.uplink_total = 0

    async def _read_stream(self, stream: dict) -> None:
        stream_id = stream["id"]
        reader = stream["reader"]
        try:
            while not self.closed and not stream["closed"]:
                if not await self._wait_downlink(stream):
                    break
                try:
                    data = await asyncio.wait_for(reader.read(DATA_CHUNK), timeout=WRITE_TIMEOUT)
                except (asyncio.TimeoutError, ConnectionError, OSError):
                    break
                if not data:
                    break
                if len(data) > MAX_WS_MESSAGE:
                    break
                size = len(data)
                stream["send_credit"] -= size
                cost = size + HEADER_SIZE + ITEM_COST
                stream["downlink_bytes"] += cost
                stream["downlink_items"] += 1
                self.downlink_bytes += cost
                self.downlink_items += 1

                plain = stream["tg_decrypt"].crypt(data)
                encrypted = stream["client_encrypt"].crypt(plain)
                await self._emit(FRAME_DATA, stream_id, encrypted)
                stream["sent"].append(size)
                self.downlink_total += size
                if self._on_bytes is not None:
                    self._on_bytes(self.user_id, 0, size)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("TWP stream %s read failed: %s", stream_id, exc)
        finally:
            await self.close_stream(stream_id, notify=True)

    def _grant(self, stream: dict, length: int) -> None:
        if not length or stream["closed"] or self.closed:
            return
        stream["receive_window"] += length
        asyncio.get_event_loop().create_task(
            self._emit(FRAME_WINDOW, stream["id"], struct.pack(">I", length)))

    def _add_window(self, stream_id: int, payload: bytes) -> bool:
        if len(payload) != 4:
            asyncio.get_event_loop().create_task(self.protocol_error("invalid_window"))
            return False
        amount = struct.unpack(">I", payload)[0]
        if not amount:
            asyncio.get_event_loop().create_task(self.protocol_error("invalid_window"))
            return False
        stream = self.streams.get(stream_id)
        if stream is None:
            return stream_id in self.closed_ids
        if amount > INITIAL_WINDOW - stream["send_credit"]:
            asyncio.get_event_loop().create_task(self.protocol_error("window_overflow"))
            return False
        stream["send_credit"] += amount
        remaining = amount
        while remaining and stream["sent"]:
            used = min(remaining, stream["sent"][0])
            stream["sent"][0] -= used
            remaining -= used
            if stream["sent"][0] <= 0:
                stream["sent"].pop(0)
        self._wake_downlink()
        return True

    def _downlink_room(self, stream: dict) -> int:
        if self.downlink_items >= LIMIT_DOWNLINK_ITEMS:
            return 0
        return max(0, min(
            DATA_CHUNK,
            stream["send_credit"],
            LIMIT_DOWNLINK_BYTES - self.downlink_bytes - HEADER_SIZE - ITEM_COST,
        ))

    async def _wait_downlink(self, stream: dict) -> bool:
        while not stream["closed"] and not self.closed and not self._downlink_room(stream):
            waiter: asyncio.Future = asyncio.get_event_loop().create_future()
            self._downlink_waiters.add(waiter)
            try:
                await waiter
            finally:
                self._downlink_waiters.discard(waiter)
        return not stream["closed"] and not self.closed

    def _wake_downlink(self) -> None:
        waiters = list(self._downlink_waiters)
        self._downlink_waiters.clear()
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def close_stream(self, stream_id: int, notify: bool) -> None:
        stream = self.streams.pop(stream_id, None)
        self.closed_ids.add(stream_id)
        if len(self.closed_ids) > 4096:
            self.closed_ids.pop()
        if stream is None:
            return
        stream["closed"] = True
        stream["abort"].set()
        for task in (stream.get("drain_task"), stream.get("reader_task")):
            if task is not None and not task.done():
                task.cancel()
        self.uplink_bytes -= sum(cost for _, cost in stream["queue"])
        self.uplink_items -= len(stream["queue"])
        stream["queue"] = []
        self.downlink_bytes -= stream["downlink_bytes"]
        self.downlink_items -= stream["downlink_items"]
        stream["downlink_bytes"] = stream["downlink_items"] = 0
        stream["sent"] = []
        self._wake_downlink()
        writer = stream.get("writer")
        if writer is not None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
        if notify and not self.closed:
            await self._emit(FRAME_CLOSE, stream_id)

    # ── outbound ─────────────────────────────────────────────────────────────
    async def _emit(self, frame_type: int, stream_id: int, payload: bytes = b"") -> None:
        if self.closed or self._send is None:
            return
        async with self._send_lock:
            try:
                await self._send(encode_frame(frame_type, stream_id, payload))
            except Exception as exc:  # noqa: BLE001
                logger.debug("TWP send failed: %s", exc)
                await self.shutdown("websocket_send_failed")

    async def protocol_error(self, reason: str = "protocol_error") -> None:
        logger.debug("TWP protocol error (%s) user=%s", reason, self.user_id)
        await self._emit(FRAME_BYE, 0)
        await self.shutdown(reason)

    async def shutdown(self, reason: str = "shutdown") -> None:
        if self.closed:
            return
        self.closed = True
        for stream_id in list(self.streams):
            await self.close_stream(stream_id, notify=False)
        self._wake_downlink()
        if self._on_close is not None:
            try:
                await self._on_close(reason)
            except Exception:  # noqa: BLE001
                pass


def stream_queue_bytes(stream: dict) -> int:
    return sum(cost for _, cost in stream["queue"])


async def _open_dc_socket(host: str, port: int):
    return await asyncio.open_connection(host, port)


class SessionRegistry:
    """In-memory session store keyed by the opaque session token.

    ``signing_key`` may be a :class:`SigningKeyStore` or a zero-argument
    callable returning the key bytes. A callable is preferred: the panel can
    then swap the store (environment override, restored state) without having
    to rebuild every registry that captured the old one.
    """

    def __init__(self, signing_key):
        self._signing = signing_key
        self._sessions: dict[str, RelaySession] = {}
        self._lock = asyncio.Lock()

    def _key(self) -> Optional[bytes]:
        if callable(self._signing):
            return self._signing()
        return self._signing.get()

    async def create(self, host: str, user_id: str, secret: bytes, **session_kwargs) -> Optional[RelaySession]:
        key = self._key()
        if not key:
            return None
        token = create_session_token(key, host)
        session = RelaySession(token, user_id, host, secret, **session_kwargs)
        async with self._lock:
            self._sessions[token] = session
        return session

    async def get(self, token: str) -> Optional[RelaySession]:
        async with self._lock:
            return self._sessions.get(token)

    async def drop(self, token: str, reason: str = "session_delete") -> Optional[RelaySession]:
        async with self._lock:
            session = self._sessions.pop(token, None)
        if session is not None:
            await session.shutdown(reason)
        return session

    async def purge_expired(self) -> int:
        now = time.time()
        async with self._lock:
            stale = [t for t, s in self._sessions.items() if s.closed or s.expires_at < now]
            sessions = [self._sessions.pop(t) for t in stale]
        for session in sessions:
            await session.shutdown("expired")
        return len(sessions)

    async def drop_user(self, user_id: str, reason: str = "user_unauthorized") -> int:
        """Kill every live session belonging to one user.

        Called whenever a user is disabled, expired, deleted, unassigned or
        otherwise loses authorisation, so access ends immediately rather than
        when the stream happens to close.
        """
        uid = str(user_id)
        async with self._lock:
            doomed = [t for t, s in self._sessions.items() if s.user_id == uid]
            sessions = [self._sessions.pop(t) for t in doomed]
        for session in sessions:
            await session.shutdown(reason)
        return len(sessions)

    def users(self) -> set:
        return {s.user_id for s in self._sessions.values()}

    def count(self) -> int:
        return len(self._sessions)

    async def shutdown_all(self, reason: str = "shutdown") -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await session.shutdown(reason)


# ── Bridge page ──────────────────────────────────────────────────────────────
_BRIDGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Web Proxy</title></head>
<body><main><h1>Connection bridge</h1><p>This page is opened by a compatible Telegram client.</p></main>
<script nonce="@@NONCE@@">
(()=>{'use strict';
const cfg=@@CONFIG@@;
let port=null, sessionToken='', socket=null, creating=false, closed=false;
const pending=[]; let pendingBytes=0;
const bootstrapAbort=new AbortController(); let connectTimer=null;
const fragment=new URLSearchParams(location.hash.slice(1));
const nonce=fragment.get('android')||'';
history.replaceState(null,'/','');
function status(state){try{port&&port.postMessage({t:'status',state})}catch{}}
function splitFrames(buffer){
 const input=new Uint8Array(buffer); let off=0,count=0,out=[];
 while(off<input.length){
  if(++count>4096||input.length-off<8)throw Error('bad frame');
  const view=new DataView(input.buffer,input.byteOffset+off,8); const len=view.getUint32(4);
  if(len>1048576||off+8+len>input.length)throw Error('bad frame');
  out.push(buffer.slice(off,off+8+len)); off+=8+len;
 }
 if(!out.length)throw Error('empty'); return out;
}
function sendToApp(buffer){for(const frame of splitFrames(buffer))port.postMessage(frame,[frame]);}
function fail(){if(closed)return;status('failed');close(false)}
async function createSession(hello){
 try{
  const response=await fetch(cfg.origin+'/api/v1/session',{method:'POST',headers:{Authorization:'Bearer '+cfg.bootstrap,'Content-Type':'application/octet-stream','X-Carrier-Mode':'websocket'},body:hello,mode:'same-origin',redirect:'error',cache:'no-store',credentials:'omit',referrerPolicy:'no-referrer',signal:bootstrapAbort.signal});
  if(!response.ok||response.headers.get('X-Carrier-Mode')!=='websocket')throw Error('session');
  sessionToken=response.headers.get('X-Session-Token')||'';
  if(!/^[A-Za-z0-9_-]{43}$/.test(sessionToken))throw Error('token');
  const welcome=await response.arrayBuffer();
  if(closed)return;
  if(welcome.byteLength!==8||new Uint8Array(welcome)[0]!==0x11||new Uint8Array(welcome).slice(1).some(v=>v!==0))throw Error('welcome');
  sendToApp(welcome);
  if(!closed)openSocket();
 }catch(e){fail()}
}
function openSocket(){
 const target=cfg.origin.replace(/^https:/,'wss:')+'/api/v1/ws';
 socket=new WebSocket(target,'tproxy-v1.'+sessionToken); socket.binaryType='arraybuffer';
 socket.onopen=()=>{
  if(closed)return; clearTimeout(connectTimer);connectTimer=null;status('connected');
  try{for(const data of pending){if(socket.bufferedAmount+data.byteLength>cfg.batchLimit)return fail();socket.send(data)}}catch{return fail()}
  pending.length=0;pendingBytes=0;
 };
 socket.onmessage=e=>{if(closed)return;if(!(e.data instanceof ArrayBuffer)||!e.data.byteLength||e.data.byteLength>cfg.batchLimit)return fail();try{sendToApp(e.data)}catch{return fail()}};
 socket.onerror=()=>{}; socket.onclose=()=>{if(!closed)fail()};
}
function fromApp(data){
 if(closed)return;
 if(data instanceof ArrayBuffer){
  if(!data.byteLength||data.byteLength>cfg.batchLimit)return fail();
  try{splitFrames(data)}catch{return fail()}
  if(!creating){creating=true;connectTimer=setTimeout(fail,cfg.connectTimeout);createSession(data);return}
  if(!sessionToken||!socket||socket.readyState!==WebSocket.OPEN){if(pending.length>=cfg.maxPendingItems||pendingBytes+data.byteLength>cfg.batchLimit)return fail();pending.push(data);pendingBytes+=data.byteLength;return}
  if(socket.bufferedAmount+data.byteLength>cfg.batchLimit)return fail();
  try{socket.send(data)}catch{fail()}
 }else if(data&&data.t==='close')close(true)
}
function activate(next){port=next;port.onmessage=e=>fromApp(e.data);port.start&&port.start();status('connecting')}
const native=globalThis.TelegramWebProxy;
if(/^[A-Za-z0-9_-]{43}$/.test(nonce)&&native&&typeof native.postMessage==='function'){
 const adapter={onmessage:null,start(){},close(){native.onmessage=null},postMessage(value){
  if(value instanceof ArrayBuffer){for(const frame of splitFrames(value))native.postMessage(frame)}
  else native.postMessage(JSON.stringify(value));
 }};
 native.onmessage=e=>{let d=e.data;if(typeof d==='string'){try{d=JSON.parse(d)}catch{return}}adapter.onmessage&&adapter.onmessage({data:d})};
 activate(adapter);native.postMessage(JSON.stringify({t:'tproxy-android-init',v:1,nonce}));
}
addEventListener('message',e=>{
 if(closed||port||e.source!==parent||!e.data||e.data.t!=='tproxy-init'||e.data.v!==1||e.ports.length!==1)return;
 try{const u=new URL(e.origin);if(u.protocol!=='http:'||u.hostname!=='127.0.0.1'||!u.port)return}catch{return}
 activate(e.ports[0]);
});
function close(notify){if(closed)return;closed=true;clearTimeout(connectTimer);connectTimer=null;bootstrapAbort.abort();pending.length=0;pendingBytes=0;try{socket&&socket.close()}catch{}if(notify&&sessionToken)fetch(cfg.origin+'/api/v1/session',{method:'DELETE',headers:{Authorization:'Bearer '+sessionToken},keepalive:true,mode:'same-origin',redirect:'error',cache:'no-store',credentials:'omit',referrerPolicy:'no-referrer'}).catch(()=>{});try{port&&port.close()}catch{}}
addEventListener('pagehide',()=>close(true),{once:true});
})();
</script></body></html>"""


def render_bridge_page(host: str, bootstrap: str, batch_limit: int = MAX_WS_MESSAGE) -> str:
    """Render the bridge HTML for a validated capability request."""
    safe_host = str(host or "").strip().lower().rstrip(".")
    config = json.dumps({
        "origin": f"https://{safe_host}",
        "bootstrap": str(bootstrap or ""),
        "batchLimit": int(batch_limit),
        "maxPendingItems": 1024,
        "connectTimeout": 15000,
    }, separators=(",", ":"))
    nonce = b64url(secrets.token_bytes(18))
    return (_BRIDGE_HTML
            .replace("@@NONCE@@", nonce)
            .replace("@@CONFIG@@", config))


def bridge_csp(host: str, nonce: str) -> str:
    """Content-Security-Policy for the bridge page (nonce must match the script)."""
    safe_host = re.sub(r"[^A-Za-z0-9.\-]", "", str(host or ""))
    safe_nonce = re.sub(r"[^A-Za-z0-9+/=_-]", "", str(nonce or ""))
    return (
        f"default-src 'none'; script-src 'nonce-{safe_nonce}'; "
        f"connect-src https://{safe_host} wss://{safe_host}; "
        "style-src 'none'; img-src 'none'; object-src 'none'; worker-src 'none'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors http://127.0.0.1:*; "
        "sandbox allow-same-origin allow-scripts"
    )
