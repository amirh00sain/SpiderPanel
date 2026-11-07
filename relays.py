"""Built-in Trojan / VMess / Shadowsocks relays (TLS + WebSocket on port 443).

These follow exactly the same model as the managed ``پیش‌فرض TLS + WS`` VLESS
relay: Railway (or any TLS-terminating front) accepts the client's TLS+WS on
port 443 and hands the WebSocket to the panel's FastAPI app, which speaks the
protocol itself and opens the outbound connection through the normal
``proxy_connect`` pipeline (same quota, IP-limit, speed-limit and accounting
code as the VLESS relay).

Each relay owns one fixed inbound that is created at boot and can never be
edited or deleted:

    VMess        /ws/vmess/{uuid}
    Trojan       /ws/trojan/{uuid}
    Shadowsocks  /ws/shadowsocks/{uuid}

``{uuid}`` is the user's ``config_uuid`` (the same value the VLESS relay uses).
It is the VMess user id, and the Trojan / Shadowsocks password.

The module never imports ``main`` at import time (same pattern as ``tm.py``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import struct
import sys
import time
import uuid as _uuid
import zlib
from datetime import datetime
from urllib.parse import quote

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import WebSocket

# ── Constants ───────────────────────────────────────────────────────────────
RELAY_PUBLIC_PORT = 443
SS_METHOD = "aes-256-gcm"          # advertised in client links (chacha20 also accepted)
VMESS_TIME_WINDOW = 180            # seconds of allowed client clock skew
HANDSHAKE_TIMEOUT = 15.0

RELAYS: dict[str, dict] = {
    "vmess": {
        "id": "relay_vmess",
        "kind": "relay_vmess",
        "name": "پیش‌فرض VMess + WS",
        "path": "/ws/vmess/{uuid}",
        "label": "VMess",
    },
    "trojan": {
        "id": "relay_trojan",
        "kind": "relay_trojan",
        "name": "پیش‌فرض Trojan + WS",
        "path": "/ws/trojan/{uuid}",
        "label": "Trojan",
    },
    "shadowsocks": {
        "id": "relay_shadowsocks",
        "kind": "relay_shadowsocks",
        "name": "پیش‌فرض Shadowsocks + WS",
        "path": "/ws/shadowsocks/{uuid}",
        "label": "Shadowsocks",
    },
}
_KIND_TO_RELAY = {r["kind"]: k for k, r in RELAYS.items()}


def _m():
    """The already-loaded application module (never a second copy)."""
    mod = sys.modules.get("main")
    if mod is None:
        raise RuntimeError("main module is not initialized")
    return mod


# ── Inbound helpers ─────────────────────────────────────────────────────────
def relay_kind_of(inbound: dict | None) -> str | None:
    """'vmess' | 'trojan' | 'shadowsocks' when the inbound is one of ours."""
    if not inbound:
        return None
    return _KIND_TO_RELAY.get(str(inbound.get("_managed_kind") or ""))


def is_relay_inbound(inbound: dict | None) -> bool:
    return relay_kind_of(inbound) is not None


def find_relay_inbound_id(kind: str) -> str | None:
    main = _m()
    wanted = RELAYS[kind]["kind"]
    for iid, ib in main.INBOUNDS.items():
        if str(ib.get("_managed_kind") or "") == wanted:
            return iid
    return None


def _fixed_inbound(kind: str, domain: str, created_at: str | None = None) -> dict:
    spec = RELAYS[kind]
    return {
        "name": spec["name"],
        "_managed_kind": spec["kind"],
        "protocol": kind,
        "inbound_type": "transport",
        "system": True,        # never deletable (delete_inbound already honours this)
        "locked": True,        # never editable (raw/advanced + UI honour this)
        "enabled": True,
        "port": RELAY_PUBLIC_PORT,
        "network": "ws",
        "security": "tls",
        "domain": domain,
        "external_domain": "",
        "sni": "",
        "external_port": "",
        "fingerprint": "chrome",
        "reality_settings": {},
        "xhttp_settings": {},
        "ws_settings": {"path": spec["path"]},
        "created_at": created_at or datetime.now().isoformat(),
    }


async def ensure_relay_inbounds() -> list[str]:
    """Create the three relay inbounds once; re-assert their fixed values on
    every boot so they can never drift (path, port, transport are not editable)."""
    main = _m()
    created: list[str] = []
    async with main.INBOUNDS_LOCK:
        for kind, spec in RELAYS.items():
            domain = main._safe_host(main.SETTINGS.get("domain"), main.get_host())
            iid = find_relay_inbound_id(kind)
            if not iid:
                iid = spec["id"] if spec["id"] not in main.INBOUNDS else main.generate_short_id()
                main.INBOUNDS[iid] = _fixed_inbound(kind, domain)
                created.append(spec["name"])
                continue
            existing = main.INBOUNDS[iid]
            fixed = _fixed_inbound(kind, domain, existing.get("created_at"))
            # Everything except the live domain is forced back to the fixed value.
            fixed["domain"] = main._safe_host(existing.get("domain"), domain)
            existing.clear()
            existing.update(fixed)
    if created:
        for name in created:
            main.log_activity("inbound", f"اینباند {name} ساخته شد", "ok")
        asyncio.create_task(main.save_state())
    return created


# ── Client config generation ────────────────────────────────────────────────
def client_config(kind: str, config_uuid: str, host: str, port: str, tls_host: str, remark: str) -> str:
    """Build the share link for one relay. ``remark`` is already URL-quoted."""
    path = RELAYS[kind]["path"].replace("{uuid}", config_uuid)
    host_q = quote(tls_host, safe="")
    path_q = quote(path, safe="")
    authority = host
    if ":" in host and not host.startswith("["):
        authority = f"[{host}]"
    common = (f"security=tls&type=ws&host={host_q}&path={path_q}&sni={host_q}"
              f"&fp=chrome&alpn=http%2F1.1")
    if kind == "trojan":
        return f"trojan://{config_uuid}@{authority}:{port}?{common}#{remark}"
    if kind == "shadowsocks":
        userinfo = base64.urlsafe_b64encode(f"{SS_METHOD}:{config_uuid}".encode()).decode().rstrip("=")
        return f"ss://{userinfo}@{authority}:{port}?{common}#{remark}"
    # vmess: classic v2rayN base64-JSON share format
    from urllib.parse import unquote
    obj = {
        "v": "2", "ps": unquote(remark), "add": host, "port": str(port), "id": config_uuid,
        "aid": "0", "scy": "auto", "net": "ws", "type": "none",
        "host": tls_host, "path": path, "tls": "tls", "sni": tls_host,
        "alpn": "http/1.1", "fp": "chrome",
    }
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
    return "vmess://" + base64.b64encode(raw).decode()


# ══════════════════════════════════════════════════════════════════════════════
# Stream helpers
# ══════════════════════════════════════════════════════════════════════════════
class StreamClosed(Exception):
    """Client side of the WebSocket went away."""


class BadHandshake(Exception):
    """Authentication / framing failure — the connection is dropped silently."""


class WSStream:
    """Byte-stream view of a WebSocket (message boundaries are meaningless)."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.buf = bytearray()

    async def _fill(self) -> None:
        msg = await self.ws.receive()
        if msg["type"] == "websocket.disconnect":
            raise StreamClosed()
        data = msg.get("bytes") or (msg.get("text") or "").encode()
        if data:
            self.buf += data

    async def read_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            await self._fill()
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    async def read_some(self) -> bytes:
        while not self.buf:
            await self._fill()
        out = bytes(self.buf)
        self.buf.clear()
        return out


def _ip6(b: bytes) -> str:
    return socket.inet_ntop(socket.AF_INET6, b)


async def _read_socks_addr(read_exact) -> tuple[str, int]:
    """SOCKS5-style address (used by Trojan and Shadowsocks): atyp 1/3/4 + port."""
    atyp = (await read_exact(1))[0]
    if atyp == 1:
        address = socket.inet_ntoa(await read_exact(4))
    elif atyp == 3:
        n = (await read_exact(1))[0]
        address = (await read_exact(n)).decode("utf-8", errors="ignore")
    elif atyp == 4:
        address = _ip6(await read_exact(16))
    else:
        raise BadHandshake(f"unknown address type {atyp}")
    port = struct.unpack(">H", await read_exact(2))[0]
    if not address or not port:
        raise BadHandshake("empty destination")
    return address, port


# ══════════════════════════════════════════════════════════════════════════════
# Trojan
# ══════════════════════════════════════════════════════════════════════════════
class TrojanCodec:
    """hex(sha224(password)) CRLF cmd addr port CRLF payload — raw body after that.

    The password is the user's config_uuid (the path uuid)."""

    def __init__(self, uuid_str: str):
        self.expected = hashlib.sha224(uuid_str.encode()).hexdigest().encode()

    async def handshake(self, stream: WSStream):
        got = await stream.read_exact(56)
        if not hmac.compare_digest(got, self.expected):
            raise BadHandshake("trojan password mismatch")
        if await stream.read_exact(2) != b"\r\n":
            raise BadHandshake("trojan framing")
        cmd = (await stream.read_exact(1))[0]
        if cmd != 1:
            raise BadHandshake("trojan: only CONNECT is supported")
        address, port = await _read_socks_addr(stream.read_exact)
        if await stream.read_exact(2) != b"\r\n":
            raise BadHandshake("trojan framing")
        return address, port, b""   # any early payload stays in stream.buf

    def prefix(self) -> bytes:
        return b""

    async def read(self, stream: WSStream):
        return await stream.read_some()

    def write(self, data: bytes) -> bytes:
        return data

    def end(self) -> bytes:
        return b""


# ══════════════════════════════════════════════════════════════════════════════
# Shadowsocks (AEAD: aes-256-gcm / chacha20-ietf-poly1305)
# ══════════════════════════════════════════════════════════════════════════════
def _ss_master_key(password: str, length: int = 32) -> bytes:
    """EVP_BytesToKey(MD5, no salt) — the legacy Shadowsocks password→key rule."""
    out, prev = b"", b""
    pw = password.encode()
    while len(out) < length:
        prev = hashlib.md5(prev + pw).digest()
        out += prev
    return out[:length]


def _ss_subkey(master: bytes, salt: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA1(), length=32, salt=salt, info=b"ss-subkey").derive(master)


def _ss_nonce(counter: int) -> bytes:
    return counter.to_bytes(12, "little")


class ShadowsocksCodec:
    MAX_CHUNK = 0x3FFF

    def __init__(self, uuid_str: str):
        self.master = _ss_master_key(uuid_str)
        self.dec = None
        self.enc = None
        self.dec_n = 0
        self.enc_n = 0
        self.plain = bytearray()
        self.sent_salt = False

    # -- request direction ---------------------------------------------------
    async def _read_chunk(self, stream: WSStream) -> bytes:
        enc_len = await stream.read_exact(2 + 16)
        if self.dec is None:
            raise AssertionError("cipher not selected")
        try:
            raw = self.dec.decrypt(_ss_nonce(self.dec_n), enc_len, None)
        except InvalidTag:
            raise BadHandshake("shadowsocks: bad length tag")
        self.dec_n += 1
        size = struct.unpack(">H", raw)[0] & self.MAX_CHUNK
        enc_payload = await stream.read_exact(size + 16)
        try:
            data = self.dec.decrypt(_ss_nonce(self.dec_n), enc_payload, None)
        except InvalidTag:
            raise BadHandshake("shadowsocks: bad payload tag")
        self.dec_n += 1
        return data

    async def _plain_exact(self, stream: WSStream, n: int) -> bytes:
        while len(self.plain) < n:
            self.plain += await self._read_chunk(stream)
        out = bytes(self.plain[:n])
        del self.plain[:n]
        return out

    async def handshake(self, stream: WSStream):
        salt = await stream.read_exact(32)
        subkey = _ss_subkey(self.master, salt)
        # Same key size / nonce / tag for both ciphers → pick by trial on the
        # first (length) chunk, which authenticates the password at the same time.
        first = await stream.read_exact(2 + 16)
        chosen = None
        for factory in (AESGCM, ChaCha20Poly1305):
            cipher = factory(subkey)
            try:
                cipher.decrypt(_ss_nonce(0), first, None)
            except InvalidTag:
                continue
            chosen = cipher
            break
        if chosen is None:
            raise BadHandshake("shadowsocks password mismatch")
        self.dec = chosen
        self.enc_factory = type(chosen)
        stream.buf[:0] = first   # put the length chunk back and parse normally
        self.dec_n = 0
        address, port = await _read_socks_addr(lambda n: self._plain_exact(stream, n))
        rest = bytes(self.plain)
        self.plain.clear()
        return address, port, rest

    async def read(self, stream: WSStream):
        if self.plain:
            out = bytes(self.plain)
            self.plain.clear()
            return out
        try:
            return await self._read_chunk(stream)
        except StreamClosed:
            return None

    # -- response direction --------------------------------------------------
    def write(self, data: bytes) -> bytes:
        out = bytearray()
        if not self.sent_salt:
            salt = os.urandom(32)
            self.enc = self.enc_factory(_ss_subkey(self.master, salt))
            out += salt
            self.sent_salt = True
        for i in range(0, len(data), self.MAX_CHUNK):
            piece = data[i:i + self.MAX_CHUNK]
            out += self.enc.encrypt(_ss_nonce(self.enc_n), struct.pack(">H", len(piece)), None)
            self.enc_n += 1
            out += self.enc.encrypt(_ss_nonce(self.enc_n), piece, None)
            self.enc_n += 1
        return bytes(out)

    def prefix(self) -> bytes:
        return b""

    def end(self) -> bytes:
        return b""


# ══════════════════════════════════════════════════════════════════════════════
# VMess (AEAD header, alterId = 0)
# ══════════════════════════════════════════════════════════════════════════════
_VMESS_CMDKEY_SALT = b"c48619fe-8f02-49e0-b9e9-edf763e17e21"


def _hmac_custom(hash_fn, key: bytes, msg: bytes, block: int = 64) -> bytes:
    """HMAC whose 'hash' is an arbitrary one-shot function (VMess nested KDF)."""
    if len(key) > block:
        key = hash_fn(key)
    key = key.ljust(block, b"\x00")
    ipad = bytes(b ^ 0x36 for b in key)
    opad = bytes(b ^ 0x5C for b in key)
    return hash_fn(opad + hash_fn(ipad + msg))


def vmess_kdf(key: bytes, *path: bytes) -> bytes:
    def level(n: int):
        if n == 0:
            return lambda data: hmac.new(b"VMess AEAD KDF", data, hashlib.sha256).digest()
        parent = level(n - 1)
        k = path[n - 1]
        return lambda data: _hmac_custom(parent, k, data)
    return level(len(path))(key)


def vmess_kdf16(key: bytes, *path: bytes) -> bytes:
    return vmess_kdf(key, *path)[:16]


def _fnv1a32(data: bytes) -> int:
    h = 0x811C9DC5
    for b in data:
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def _chacha_key(key16: bytes) -> bytes:
    first = hashlib.md5(key16).digest()
    return first + hashlib.md5(first).digest()


class _Shake:
    """Lazy SHAKE128 stream (hashlib has no incremental squeeze)."""

    def __init__(self, seed: bytes):
        self.seed = seed
        self.size = 1024
        self.data = hashlib.shake_128(seed).digest(self.size)
        self.pos = 0

    def next16(self) -> int:
        if self.pos + 2 > self.size:
            self.size *= 2
            self.data = hashlib.shake_128(self.seed).digest(self.size)
        v = (self.data[self.pos] << 8) | self.data[self.pos + 1]
        self.pos += 2
        return v


class _ChunkCrypto:
    """One direction of the VMess body: AEAD payload + length/padding parsers."""

    TAG = 16

    def __init__(self, security: int, key16: bytes, iv: bytes, opt: int):
        self.opt = opt
        self.iv = iv
        self.count = 0
        if security == 3:
            self.aead = AESGCM(key16)
            self.len_aead = AESGCM(vmess_kdf16(key16, b"auth_len"))
        elif security == 4:
            self.aead = ChaCha20Poly1305(_chacha_key(key16))
            self.len_aead = ChaCha20Poly1305(_chacha_key(vmess_kdf16(key16, b"auth_len")))
        elif security == 5:
            self.aead = None
            self.len_aead = None
        else:
            raise BadHandshake(f"unsupported vmess security {security}")
        self.overhead = self.TAG if self.aead else 0
        self.masking = bool(opt & 0x04)
        self.padding = bool(opt & 0x08)
        self.auth_len = bool(opt & 0x10) and self.aead is not None
        self.shake = _Shake(iv) if (self.masking or self.padding) else None
        self.len_count = 0

    def _nonce(self, count: int) -> bytes:
        return struct.pack(">H", count & 0xFFFF) + self.iv[2:12]

    def _pad_len(self) -> int:
        return (self.shake.next16() % 64) if self.padding else 0

    # decode one chunk from `stream`; returns plaintext, or None on end-of-stream
    async def read_chunk(self, stream: WSStream):
        pad = self._pad_len()
        if self.auth_len:
            enc = await stream.read_exact(2 + self.TAG)
            try:
                raw = self.len_aead.decrypt(self._nonce(self.len_count), enc, None)
            except InvalidTag:
                raise BadHandshake("vmess: bad length tag")
            self.len_count += 1
            size = struct.unpack(">H", raw)[0] + self.overhead
        else:
            size = struct.unpack(">H", await stream.read_exact(2))[0]
            if self.masking:
                size ^= self.shake.next16()
        if size < pad + self.overhead:
            raise BadHandshake("vmess: bad chunk size")
        body = await stream.read_exact(size)
        data = body[:size - pad]
        if size - pad == self.overhead:      # empty chunk == end of stream
            return None
        if self.aead is None:
            return data
        try:
            plain = self.aead.decrypt(self._nonce(self.count), data, None)
        except InvalidTag:
            raise BadHandshake("vmess: bad payload tag")
        self.count += 1
        return plain

    def encode_chunk(self, data: bytes) -> bytes:
        pad = self._pad_len()
        ct = self.aead.encrypt(self._nonce(self.count), data, None) if self.aead else data
        self.count += 1
        size = len(ct) + pad
        if self.auth_len:
            head = self.len_aead.encrypt(self._nonce(self.len_count),
                                         struct.pack(">H", size - self.overhead), None)
            self.len_count += 1
        else:
            if self.masking:
                size ^= self.shake.next16()
            head = struct.pack(">H", size)
        return head + ct + (os.urandom(pad) if pad else b"")


class VMessCodec:
    MAX_CHUNK = 8000

    def __init__(self, uuid_str: str):
        self.cmd_key = hashlib.md5(_uuid.UUID(uuid_str).bytes + _VMESS_CMDKEY_SALT).digest()
        self.req: _ChunkCrypto | None = None
        self.resp: _ChunkCrypto | None = None
        self.chunked = True
        self.resp_header = b""

    async def handshake(self, stream: WSStream):
        ck = self.cmd_key
        auth_id = await stream.read_exact(16)
        enc_len = await stream.read_exact(18)
        nonce = await stream.read_exact(8)

        # 1) auth id: AES-128-ECB(time | rand | crc32)
        k = vmess_kdf16(ck, b"AES Auth ID Encryption")
        dec = Cipher(algorithms.AES(k), modes.ECB()).decryptor()
        pt = dec.update(auth_id) + dec.finalize()
        ts = struct.unpack(">q", pt[:8])[0]
        if (zlib.crc32(pt[:12]) & 0xFFFFFFFF) != struct.unpack(">I", pt[12:16])[0]:
            raise BadHandshake("vmess: bad auth id")
        if abs(time.time() - ts) > VMESS_TIME_WINDOW:
            raise BadHandshake("vmess: auth id outside time window")

        # 2) header length
        lk = vmess_kdf16(ck, b"VMess Header AEAD Key_Length", auth_id, nonce)
        ln = vmess_kdf(ck, b"VMess Header AEAD Nonce_Length", auth_id, nonce)[:12]
        try:
            hlen = struct.unpack(">H", AESGCM(lk).decrypt(ln, enc_len, auth_id))[0]
        except InvalidTag:
            raise BadHandshake("vmess: bad header length")

        # 3) header
        enc_hdr = await stream.read_exact(hlen + 16)
        hk = vmess_kdf16(ck, b"VMess Header AEAD Key", auth_id, nonce)
        hn = vmess_kdf(ck, b"VMess Header AEAD Nonce", auth_id, nonce)[:12]
        try:
            h = AESGCM(hk).decrypt(hn, enc_hdr, auth_id)
        except InvalidTag:
            raise BadHandshake("vmess: bad header")

        if len(h) < 41 or h[0] != 1:
            raise BadHandshake("vmess: bad header version")
        if (_fnv1a32(h[:-4]) & 0xFFFFFFFF) != struct.unpack(">I", h[-4:])[0]:
            raise BadHandshake("vmess: header checksum")
        body_iv, body_key = h[1:17], h[17:33]
        resp_v, opt = h[33], h[34]
        pad_len, security = h[35] >> 4, h[35] & 0x0F
        cmd = h[37]
        if cmd != 1:
            raise BadHandshake("vmess: only TCP is supported")
        port = struct.unpack(">H", h[38:40])[0]
        atyp = h[40]
        pos = 41
        if atyp == 1:
            address = socket.inet_ntoa(h[pos:pos + 4]); pos += 4
        elif atyp == 2:
            n = h[pos]; pos += 1
            address = h[pos:pos + n].decode("utf-8", errors="ignore"); pos += n
        elif atyp == 3:
            address = _ip6(h[pos:pos + 16]); pos += 16
        else:
            raise BadHandshake(f"vmess: unknown address type {atyp}")
        if not address or not port:
            raise BadHandshake("vmess: empty destination")

        self.chunked = bool(opt & 0x01)
        if security == 0:           # "auto"/legacy markers → treat as AES-128-GCM-less raw is unsafe
            raise BadHandshake("vmess: unsupported security")
        if self.chunked:
            self.req = _ChunkCrypto(security, body_key, body_iv, opt)
        elif security != 5:
            raise BadHandshake("vmess: unchunked stream needs security none")

        # Response side: key/iv are SHA-256 of the request ones (first 16 bytes).
        rk = hashlib.sha256(body_key).digest()[:16]
        ri = hashlib.sha256(body_iv).digest()[:16]
        if self.chunked:
            self.resp = _ChunkCrypto(security, rk, ri, opt)
        hdr = bytes([resp_v, 0, 0, 0])
        lk2 = vmess_kdf16(rk, b"AEAD Resp Header Len Key")
        ln2 = vmess_kdf(ri, b"AEAD Resp Header Len IV")[:12]
        hk2 = vmess_kdf16(rk, b"AEAD Resp Header Key")
        hn2 = vmess_kdf(ri, b"AEAD Resp Header IV")[:12]
        self.resp_header = (AESGCM(lk2).encrypt(ln2, struct.pack(">H", len(hdr)), None)
                            + AESGCM(hk2).encrypt(hn2, hdr, None))
        return address, port, b""

    def prefix(self) -> bytes:
        return self.resp_header

    async def read(self, stream: WSStream):
        if not self.chunked:
            return await stream.read_some()
        try:
            return await self.req.read_chunk(stream)
        except StreamClosed:
            return None

    def write(self, data: bytes) -> bytes:
        if not self.chunked:
            return data
        out = bytearray()
        for i in range(0, len(data), self.MAX_CHUNK):
            out += self.resp.encode_chunk(data[i:i + self.MAX_CHUNK])
        return bytes(out)

    def end(self) -> bytes:
        return self.resp.encode_chunk(b"") if self.chunked else b""


CODECS = {"vmess": VMessCodec, "trojan": TrojanCodec, "shadowsocks": ShadowsocksCodec}


# ══════════════════════════════════════════════════════════════════════════════
# Shared WebSocket entry (auth + accounting identical to the VLESS relay)
# ══════════════════════════════════════════════════════════════════════════════
def _is_valid_uuid(value: str) -> bool:
    try:
        _uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


async def relay_ws_entry(ws: WebSocket, uuid_str: str, kind: str) -> None:
    m = _m()
    await ws.accept()

    inbound_id = find_relay_inbound_id(kind)
    inbound = m.INBOUNDS.get(inbound_id) if inbound_id else None
    if not inbound or inbound.get("enabled") is False or not _is_valid_uuid(uuid_str):
        await ws.close(code=1008, reason="relay unavailable")
        return

    async with m.LINKS_LOCK:
        link = m.LINKS.get(uuid_str)
    relay_ids = list((link or {}).get("relay_inbound_ids") or [])
    if not (link and link.get("relay_enabled") and inbound_id in relay_ids):
        m.logger.warning(f"{kind} relay rejected uuid={uuid_str[:8]}…: not enabled for this inbound")
        await ws.close(code=1008, reason="relay unavailable for this inbound")
        return
    if not m.is_link_allowed(link):
        await ws.close(code=1008, reason="not authorized")
        return

    ip = m._ws_client_ip(ws)
    conn_id = secrets.token_urlsafe(6)
    label = RELAYS[kind]["label"]
    m.connections[conn_id] = {
        "uuid": uuid_str, "ip": ip, "transport": f"{kind}-ws",
        "connected_at": datetime.now().isoformat(), "bytes": 0,
    }
    m.log_activity("connection", f"اتصال جدید {label} از {ip} (کانفیگ {link.get('label', '?')})", "info")

    speed_limiter = None
    user_id = str(link.get("user_id") or "")
    if user_id:
        async with m.USERS_LOCK:
            linked_user = m.USERS.get(user_id) or {}
            try:
                mbps = max(0.0, float(linked_user.get("speed_limit_mbps") or 0))
            except (TypeError, ValueError):
                mbps = 0.0
        speed_limiter = m._user_speed_limiter(user_id, mbps)

    if not await m.enforce_ip_limit_for_link(uuid_str, ip):
        m.connections.pop(conn_id, None)
        m.log_activity("connection", f"رد اتصال {ip}: سقف اتصال همزمان پر شد", "warn")
        await ws.close(code=1008, reason="ip limit reached")
        return

    writer = None
    stream = WSStream(ws)
    codec = CODECS[kind](uuid_str)
    try:
        address, port, first_payload = await asyncio.wait_for(codec.handshake(stream), HANDSHAKE_TIMEOUT)
        m.stats["total_requests"] += 1
        m.logger.info(f"[{conn_id}] {kind} → {address}:{port}")

        reader, writer = await m.proxy_connect(uuid_str, address, port)
        m._tune_relay_socket(writer)

        head = codec.prefix()
        if head:
            await ws.send_bytes(head)
        if first_payload:
            if not await m.check_and_use(uuid_str, len(first_payload)):
                await ws.close(code=1008, reason="quota/disabled")
                return
            if speed_limiter:
                await speed_limiter.wait_for(len(first_payload))
            writer.write(first_payload)
            await writer.drain()

        async def client_to_target():
            try:
                while True:
                    data = await codec.read(stream)
                    if data is None:
                        break
                    if not data:
                        continue
                    if not await m.check_and_use(uuid_str, len(data)):
                        await ws.close(code=1008, reason="quota/disabled/unknown")
                        break
                    if speed_limiter:
                        await speed_limiter.wait_for(len(data))
                    m.connections[conn_id]["bytes"] += len(data)
                    writer.write(data)
                    if writer.transport.get_write_buffer_size() > m.RELAY_WRITE_HIGH_WATER:
                        await writer.drain()
            except (StreamClosed, BadHandshake):
                pass
            except Exception:
                pass
            finally:
                try:
                    writer.write_eof()
                except Exception:
                    pass

        async def target_to_client():
            try:
                while True:
                    data = await reader.read(m.RELAY_BUF_LOCAL)
                    if not data:
                        tail = codec.end()
                        if tail:
                            await ws.send_bytes(tail)
                        break
                    if not await m.check_and_use(uuid_str, len(data)):
                        await ws.close(code=1008, reason="quota/disabled/unknown")
                        break
                    if speed_limiter:
                        await speed_limiter.wait_for(len(data))
                    m.connections[conn_id]["bytes"] += len(data)
                    await ws.send_bytes(codec.write(data))
            except Exception:
                pass

        done, pending = await asyncio.wait(
            {asyncio.create_task(client_to_target()), asyncio.create_task(target_to_client())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        asyncio.create_task(m.save_state())

    except (StreamClosed, BadHandshake) as exc:
        if isinstance(exc, BadHandshake):
            m.logger.warning(f"{kind} relay [{conn_id}] rejected from {ip}: {exc}")
    except asyncio.TimeoutError:
        m.stats["total_errors"] += 1
    except Exception as exc:
        m.stats["total_errors"] += 1
        m.error_logs.append({"error": str(exc), "time": datetime.now().isoformat()})
        m.log_activity("error", f"خطای اتصال {label} {ip}: {exc}", "err")
    finally:
        if writer:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        m.connections.pop(conn_id, None)
        try:
            asyncio.create_task(m.release_ip_for_link(uuid_str, ip))
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass
