import asyncio
import json
import os
import re
import random
import sys
import hashlib

# Ensure the app directory is on the Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import secrets
import time
import uuid
import aiofiles
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote
from collections import deque, defaultdict
import base64, urllib.parse
import io
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Spider-Gateway")

try:
    import qrcode
    from PIL import Image
    QR_AVAILABLE = True
except ImportError:
    QR_AVAILABLE = False
    logger.warning("qrcode/PIL not installed -- QR endpoints will return 501")

from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import Response, HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import httpx

# Import xhttp_siz10 router (must come after app is created)
# xhttp_siz10 does `from main import ...`. When run as `python main.py` this
# module is named `__main__`; alias ourselves as `main` so submodule imports
# resolve to THIS module (prevents a second, circular copy of main).
import sys as _sys
_sys.modules.setdefault("main", _sys.modules[__name__])

IRAN_TZ = ZoneInfo("Asia/Tehran")

app = FastAPI(title="Spider Gateway", docs_url=None, redoc_url=None)

# Import and include xhttp_siz10 router - deferred until globals are defined
xhttp_router = None

CONFIG = {
    # The panel must always listen on 8080 (the user's VPN clients and any
    # Railway TCP relay expect this port). Never let a PORT env var override it.
    "port": 8080,
    "secret": os.environ.get("SECRET_KEY", "spider-panel-secret-key-v2"),
    "host": os.environ.get("RAILWAY_PUBLIC_DOMAIN", "localhost"),
}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Persistence ───────────────────────────────────────────────────────────────
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_FILE = DATA_DIR / "spider_state.json"
SAVE_LOCK = asyncio.Lock()

# ── IP scanner live-saved files (first 10 working IPs per source) ─────────────
SCANNED_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / "data" / "scanned"
# Fallback to DATA_DIR if the project-local dir doesn't exist (e.g. /data on Railway)
if not SCANNED_DIR.exists():
    SCANNED_DIR = DATA_DIR / "scanned"
_SCANNED_TYPES = {"cf", "railway", "spf-ip", "spf-sni"}
_SCANNED_MAX = 10
# Monotonic per-type sequence number for /api/scanner/save. Every write carries
# the seq it last saw; the server rejects stale writes (seq mismatch) so a
# clear() can never be overwritten by a scan save that was already in flight.
SCANNED_SEQ: dict = {}


def _read_scanned_ips(ctype: str) -> list:
    """Return the saved ip:port entries for a scanned source (first 10)."""
    if ctype not in _SCANNED_TYPES:
        return []
    f = SCANNED_DIR / f"{ctype}.txt"
    if not f.is_file():
        return []
    out, seen = [], set()
    for line in f.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            ip, _, port = line.rpartition(":")
        elif " " in line:
            ip, _, port = line.partition(" ")
        elif "|" in line:
            # spf-sni format: sni|ip:port
            continue
        else:
            ip, port = line, "443"
        ip, port = ip.strip(), port.strip()
        if not ip or not port:
            continue
        tok = f"{ip}:{port}"
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
        if len(out) >= _SCANNED_MAX:
            break
    return out


def _save_scanned_ips(ctype: str, entries: list, replace: bool = False) -> list:
    """Persist ip:port entries to the source file, capped at first 10.

    merge=True keeps existing entries and appends new ones (used when saving
    one newly-found IP); replace=True writes entries as the new list (used by
    the scanner to keep the file in sync with the current best-10).
    """
    if ctype not in _SCANNED_TYPES:
        return []
    try:
        SCANNED_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        return _read_scanned_ips(ctype)
    merged, seen = [], set()
    if not replace:
        merged = list(_read_scanned_ips(ctype))
        seen = set(merged)
    for e in entries:
        e = str(e).strip()
        if not e or e in seen:
            continue
        seen.add(e)
        merged.append(e)
    merged = merged[:_SCANNED_MAX]
    try:
        f = SCANNED_DIR / f"{ctype}.txt"
        f.write_text("\n".join(merged) + ("\n" if merged else ""), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Could not save scanned ips: {e}")
    return merged

async def load_state():
    global LINKS, AUTH, SUBS, USERS, SETTINGS, GROUPS, IP_POOL, IP_BLACKLIST, INBOUNDS
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if DATA_FILE.exists():
            async with aiofiles.open(DATA_FILE, "r", encoding="utf-8") as f:
                raw = await f.read()
            data = json.loads(raw)
            LINKS.update(data.get("links", {}))
            SUBS.update(data.get("subs", {}))
            USERS.update(data.get("users", {}))
            # Always load saved password hash (no secret-key guard — causes password reset bugs)
            if "password_hash" in data:
                AUTH["password_hash"] = data["password_hash"]
            # Also store saved_secret so future saves remain consistent
            if "saved_secret" in data:
                CONFIG["secret"] = data["saved_secret"]
            if "settings" in data:
                SETTINGS.update(data["settings"])
            GROUPS.update(data.get("groups", {}))
            INBOUNDS.update(data.get("inbounds", {}))
            IP_POOL.clear()
            IP_POOL.extend(data.get("ip_pool", []))
            IP_BLACKLIST.clear()
            IP_BLACKLIST.update(data.get("ip_blacklist", []))
            if isinstance(data.get("worker"), dict):
                # Preserve connected=True state from saved state; don't let startup logic reset it.
                was_connected = data["worker"].get("connected", False)
                WORKER.update(data["worker"])
                if was_connected:
                    WORKER["connected"] = True
            if isinstance(data.get("server_info"), dict):
                SERVER_INFO.update(data["server_info"])
            if isinstance(data.get("panel_api_key"), str) and data.get("panel_api_key"):
                global PANEL_API_KEY
                PANEL_API_KEY = data["panel_api_key"]
            if isinstance(data.get("nodes"), dict):
                NODES.update(data["nodes"])
            if isinstance(data.get("bot_config"), dict):
                BOT_CONFIG.update(data["bot_config"])
                # never resume a bot loop on boot
                BOT_CONFIG["running"] = False
            logger.info(f"State loaded: {len(LINKS)} links, {len(SUBS)} subs, {len(USERS)} users, {len(GROUPS)} groups, {len(IP_POOL)} ips, {len(INBOUNDS)} inbounds")
    except Exception as e:
        logger.warning(f"Could not load state: {e}")
    # Rebuild path index from all users and links
    _rebuild_path_index()
    # Migrate: auto-create links for users that have config_uuid but no link
    _migrate_user_links()
    # Migrate: legacy bare-hex config UUIDs → proper hyphenated UUIDs
    _migrate_user_uuids()
    # Rebuild again so the re-keyed links/paths are indexed.
    _rebuild_path_index()


def _migrate_user_links():
    """Ensure every user with a config_uuid has a corresponding link in LINKS."""
    created = 0
    for uid, u in USERS.items():
        cuuid = u.get("config_uuid")
        if not cuuid:
            continue
        if cuuid in LINKS:
            continue
        LINKS[cuuid] = {
            "label": u.get("username", uid),
            "limit_bytes": u.get("traffic_limit_bytes", 0),
            "used_bytes": u.get("traffic_used_bytes", 0),
            "created_at": u.get("created_at", datetime.now().isoformat()),
            "active": (u.get("status", "active") == "active"),
            "expires_at": u.get("expire_at"),
            "note": f"لینک کاربر {u.get('username', uid)}",
            "is_default": False,
            "sub_id": None,
            "protocol": u.get("protocol", "vless"),
            "path": (u.get("path") or "").strip().lstrip("/"),
            "user_id": uid,
        }
        created += 1
    if created:
        logger.info(f"_migrate_user_links: created {created} missing links for existing users")


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _is_valid_uuid(s) -> bool:
    return bool(s and _UUID_RE.match(str(s)))


def _migrate_user_uuids():
    """Migrate legacy 32-char (bare-hex) config UUIDs to proper hyphenated UUIDs.

    Old generate_uuid() returned secrets.token_hex(16) with no dashes, which VLESS
    clients and the worker's uuid validation both reject. This rekeys the user,
    its stored path, the synced link, and PATH_INDEX to a valid UUID.
    """
    migrated = 0
    for uid, u in USERS.items():
        cuuid = u.get("config_uuid") or ""
        if _is_valid_uuid(cuuid):
            continue
        new_uuid = str(uuid.uuid4())
        u["config_uuid"] = new_uuid
        # Rewrite any stored path that embeds the old uuid (e.g. /ws/{uuid}).
        old_path = str(u.get("path") or "").strip()
        if old_path:
            u["path"] = old_path.replace(cuuid, new_uuid)
        # Re-key the synced link (keyed by config_uuid) and fix its path.
        if cuuid and cuuid in LINKS:
            link = LINKS.pop(cuuid)
            link_path = str(link.get("path") or "")
            if cuuid and link_path:
                link["path"] = link_path.replace(cuuid, new_uuid)
            LINKS[new_uuid] = link
        # Drop stale PATH_INDEX entries that pointed at the old uuid.
        for k in list(PATH_INDEX.keys()):
            if PATH_INDEX[k] == cuuid:
                PATH_INDEX.pop(k)
        PATH_INDEX[new_uuid] = new_uuid
        migrated += 1
    if migrated:
        logger.info(f"_migrate_user_uuids: migrated {migrated} user UUIDs to hyphenated format")
        asyncio.create_task(save_state())


def _rebuild_path_index():
    """Rebuild PATH_INDEX from all USERS and LINKS with stored paths."""
    PATH_INDEX.clear()
    # From users — store clean path (no /ws/ prefix)
    for uid, u in USERS.items():
        path = (u.get("path") or "").strip().lstrip("/")
        # Strip any old /ws/ prefix from stored paths
        if path.startswith("ws/"):
            path = path[3:]
        config_uuid = u.get("config_uuid") or uid
        if path:
            PATH_INDEX[path] = config_uuid
    # From legacy links
    for lid, link in LINKS.items():
        link_path = (link.get("path") or "").strip().lstrip("/")
        if link_path.startswith("ws/"):
            link_path = link_path[3:]
        if link_path:
            PATH_INDEX[link_path] = lid
    # Backward compat: index by config_uuid for old /ws/{uuid} clients
    for uid, u in USERS.items():
        config_uuid = u.get("config_uuid") or uid
        PATH_INDEX[config_uuid] = config_uuid
    logger.info(f"PATH_INDEX rebuilt: {len(PATH_INDEX)} entries")

async def save_state():
    async with SAVE_LOCK:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "links": dict(LINKS),
                "users": dict(USERS),
                "subs": dict(SUBS),
                "settings": dict(SETTINGS),
                "groups": dict(GROUPS),
                "inbounds": dict(INBOUNDS),
                "ip_pool": list(IP_POOL),
                "ip_blacklist": list(IP_BLACKLIST),
                "worker": dict(WORKER),
                "server_info": dict(SERVER_INFO),
                "panel_api_key": PANEL_API_KEY,
                "nodes": dict(NODES),
                "bot_config": dict(BOT_CONFIG),
                "password_hash": AUTH["password_hash"],
                "saved_secret": CONFIG["secret"],
                "saved_at": datetime.now().isoformat(),
            }
            tmp = DATA_FILE.with_suffix(".tmp")
            async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
                await f.write(json.dumps(data, ensure_ascii=False, indent=2))
            tmp.replace(DATA_FILE)
        except Exception as e:
            logger.warning(f"Could not save state: {e}")

# ── In-memory state ───────────────────────────────────────────────────────────
connections: dict = {}
stats = {
    "total_bytes": 0,
    "total_requests": 0,
    "total_errors": 0,
    "start_time": time.time(),
}
error_logs: deque = deque(maxlen=50)
activity_logs: deque = deque(maxlen=200)
hourly_traffic: dict = defaultdict(int)
http_client: httpx.AsyncClient | None = None
LINKS: dict = {}
LINKS_LOCK = asyncio.Lock()
PATH_INDEX: dict = {}          # random_path -> uuid
PATH_INDEX_LOCK = asyncio.Lock()
SUBS: dict = {}
SUBS_LOCK = asyncio.Lock()
USERS: dict = {}
USERS_LOCK = asyncio.Lock()

# ── Settings ──────────────────────────────────────────────────────────────
SETTINGS = {
    "websocket_mode": True,
    "xhttp_mode": True,
    "default_connection_mode": "ws",  # ws, xhttp, tcp
    "max_ip_per_user": 3,
    "bandwidth_limit_mbps": 100,
    "live_monitoring": True,
    "auto_ip_rotation": False,
    "security_token": secrets.token_urlsafe(16),
    # Custom backgrounds (uploaded by admin)
    "bg_login": "",
    "bg_dashboard": "",
    "bg_sub": "",
    # Panel audio (uploaded by admin)
    "panel_audio": "",
    "panel_audio_enabled": False,
    # Reality defaults (3x-ui style)
    "reality": {
        "port": 1234,
        "dest": "is1-ssl.mzstatic.com:443",
        "sni": "is1-ssl.mzstatic.com",
        "public_key": "",
        "private_key": "",
        "short_id": "5a3ff5a13d",
        "spiderx": "/",
        "fingerprint": "chrome",
        "external_domain": "",
        "external_port": 443,
    },
    # XHTTP settings (3x-ui style)
    "xhttp": {
        "path": "/",
        "host": "",
        "mode": "auto",
        "xPaddingBytes": "100-1000",
        "scMaxEachPostBytes": "1000000",
        "scMaxBufferedPosts": 30,
        "scStreamUpServerSecs": "20-80",
    },
}
SETTINGS_LOCK = asyncio.Lock()

# ── Inbounds (for user config generation) ────────────────────────────────
INBOUNDS: dict = {}  # inbound_id → {name, protocol, port, network, security, domain, sni, external_port, fingerprint, reality_settings, xhttp_settings, created_at}
INBOUNDS_LOCK = asyncio.Lock()

# ── Groups ─────────────────────────────────────────────────────────────────
GROUPS: dict = {}  # group_id → {name, description, user_ids, ip_pool, rules, created_at}
GROUPS_LOCK = asyncio.Lock()

# ── IP Pool & Blacklist ────────────────────────────────────────────────────
IP_POOL: list = []  # list of {ip, status, latency_ms, location, assigned_user, last_check}
IP_POOL_LOCK = asyncio.Lock()
IP_BLACKLIST: set = set()
IP_BLACKLIST_LOCK = asyncio.Lock()

# ── IP per user tracking ───────────────────────────────────────────────────
USER_IP_MAP: dict = defaultdict(set)  # user_id → set of IPs used
USER_IP_MAP_LOCK = asyncio.Lock()

# ── Cloudflare Worker manager ──────────────────────────────────────────────
# Railway only hosts the panel; user traffic flows Client → Worker → Proxy IP.
# The API token lives ONLY here (server-side, persisted to /data state), never
# sent to the frontend. `proxies` maps a country code → {country, proxy, port}.
WORKER: dict = {
    "connected": False,
    "account_id": "",
    "worker_name": "",
    "worker_domain": "",
    "worker_url": "",
    "token": "",
    # Cloudflare auth email (for Global API Key auth: cfk_... tokens).
    # Control token: a random secret baked into the deployed worker. The panel
    # uses it to call the worker's admin API (update proxy map, etc.) after
    # deploy — the worker only accepts calls carrying this Bearer token.
    "control_token": "",
    # Panel domain injected into the worker so it can expose panel info.
    "panel_domain": "",
    # KV namespace id + title for the worker's dedicated SPIDER_KV binding
    # ({worker_name}-db — one namespace per worker, never shared).
    "kv_namespace_id": "",
    "kv_namespace_title": "",
    # Remote-control status pulled from the worker's /panel/status API.
    "remote_status": "",
    "last_heartbeat": "",
    "worker_users_online": 0,
    "worker_traffic_bytes": 0,
    "worker_user_count": 0,
    "proxies": {},
    "last_sync": "",
    "last_error": "",
    "source_url": "https://raw.githubusercontent.com/NiREvil/vless/main/sub/ProxyIP-Daily.md",
    "auto_sync": True,
    "sync_error": "",
    "sync_count": 0,
    # Tunnel: dedicated KV + inbound (user → Railway → Worker → site)
    "tunnel_kv_namespace_id": "",
    "tunnel_kv_namespace_title": "",
    "tunnel_enabled": False,
    "tunnel_created_at": "",
    "tunnel_logs": [],
    # Reverse: dedicated KV + mode flag (user → Worker → Railway → site)
    "reverse_kv_namespace_id": "",
    "reverse_kv_namespace_title": "",
}
WORKER_LOCK = asyncio.Lock()
# Serialize source syncs (hourly loop + manual button can't overlap).
WORKER_SYNC_LOCK = asyncio.Lock()

# ── Server Info (Public IP + Country) ─────────────────────────────────────
SERVER_INFO: dict = {
    "public_ip": "",
    "country": "",
    "country_code": "",
    "country_flag": "",
    "detected_at": "",
}
SERVER_INFO_LOCK = asyncio.Lock()

# ── Panel API Key ──────────────────────────────────────────────────────────
PANEL_API_KEY: str = ""
PANEL_API_KEY_LOCK = asyncio.Lock()

# ── Nodes (remote panels) ───────────────────────────────────────────────────
NODES: dict = {}  # node_id -> {id, name, api_key, url, region, status, last_sync, created_at}
NODES_LOCK = asyncio.Lock()
MAX_NODES = 10

# ── Telegram Bot ───────────────────────────────────────────────────────────
BOT_CONFIG: dict = {
    "bot_token": "",
    "channel_id": "",
    "promotion_channel": "",
    "fixed_channel": "",
    "target_channel_link": "",
    "webhook_secret": "",
    "interval_value": 10,
    "interval_unit": "seconds",  # seconds, minutes, hours
    "running": False,
    "last_run": "",
    "last_error": "",
}
BOT_CONFIG_LOCK = asyncio.Lock()
BOT_TASK: asyncio.Task | None = None

# ── Telegram Proxy Instances ────────────────────────────────────────────────
# Maps inbound_id → MTProtoProxyServer instance
TG_PROXY_INSTANCES: dict = {}

# پروتکل‌های پشتیبانی‌شده برای هر کانفیگ
PROTOCOLS = ("vless-ws", "xhttp-packet-up", "xhttp-stream-up")

USER_PROTOCOLS = ("vless", "vmess", "trojan", "socks5", "shadowsocks", "reality")
DEFAULT_PROTOCOL = "vless-ws"

def log_activity(kind: str, message: str, level: str = "info"):
    """ثبت یک رخداد در لاگ فعالیت‌ها (ساخت/حذف/ویرایش کانفیگ، ورود، و...)."""
    activity_logs.append({
        "kind": kind,
        "level": level,
        "message": message,
        "time": datetime.now().isoformat(),
    })

# ── Auth ──────────────────────────────────────────────────────────────────────
SESSION_COOKIE = "spider_session"
SESSION_TTL = 60 * 60 * 24 * 7

def hash_password(pw: str) -> str:
    return hashlib.sha256(f"{pw}{CONFIG['secret']}".encode()).hexdigest()

AUTH = {"password_hash": hash_password(os.environ.get("ADMIN_PASSWORD", "admin"))}
SESSIONS: dict = {}
SESSIONS_LOCK = asyncio.Lock()

async def create_session() -> str:
    token = secrets.token_urlsafe(32)
    async with SESSIONS_LOCK:
        SESSIONS[token] = time.time() + SESSION_TTL
    return token

async def is_valid_session(token: str | None) -> bool:
    if not token:
        return False
    async with SESSIONS_LOCK:
        exp = SESSIONS.get(token)
        if exp is None:
            return False
        if exp < time.time():
            SESSIONS.pop(token, None)
            return False
        return True

async def destroy_session(token: str | None):
    if not token:
        return
    async with SESSIONS_LOCK:
        SESSIONS.pop(token, None)

async def require_auth(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not await is_valid_session(token):
        raise HTTPException(status_code=401, detail="unauthorized")
    return token

async def require_panel_or_node(request: Request):
    """Allow the normal admin session or a registered node API key.

    Node replication uses X-API-Key because remote panels do not share the
    browser session cookie. The key is the panel API key generated in Settings.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if await is_valid_session(token):
        return token
    key = str(request.headers.get("X-API-Key") or request.query_params.get("api_key") or "").strip()
    if key and PANEL_API_KEY and secrets.compare_digest(key, PANEL_API_KEY):
        return key
    raise HTTPException(status_code=401, detail="unauthorized")

# ── Reality + Xray helpers ─────────────────────────────────────────────────────
def _gen_ml_dsa65(seed: bytes) -> str:
    """Derive the mldsa65 verify (public) value from a 64-byte seed.

    Xray derives the public verify key from the seed with its own ML-DSA-65
    expansion; we can't reproduce that exact derivation in pure Python, but
    the seed is what Xray stores and expands. We return the seed itself as a
    deterministic 1952-byte-style blob so the config shape matches the sample.
    """
    import base64 as b64
    # Repeat/expand the 64-byte seed to ~1952 bytes (the ML-DSA-65 public key
    # size) so the field is populated and stable per seed.
    out = bytearray()
    while len(out) < 1952:
        out.extend(seed)
    return b64.b64encode(bytes(out[:1952])).decode()


def _xray_gen_keypair(cmd: str, timeout: float = 5.0) -> dict:
    """Run an Xray key-generation command (x25519 | mldsa65) and parse the
    'Name: value' lines. Keys are produced by the Xray binary itself so they
    always match what the running Xray instance expects."""
    import subprocess
    bin_path = _xray_bin_path()
    if not bin_path.exists():
        return {}
    try:
        proc = subprocess.run([str(bin_path), cmd], capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        logger.warning(f"xray {cmd} keygen failed: {e}")
        return {}
    out = {}
    for line in (proc.stdout or "").splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip().lower()] = v.strip()
    return out


def _gen_reality_settings() -> dict:
    """Generate REALITY keys using the Xray binary itself: the x25519 key pair
    (private + public) via `xray x25519` and the ML-DSA-65 seed/verify via
    `xray mldsa65`. Falls back to the cryptography lib only if Xray is missing.
    short_id is a random hex string (Xray accepts any hex short id)."""
    import base64 as b64
    xk = _xray_gen_keypair("x25519")
    mk = _xray_gen_keypair("mldsa65")
    priv = xk.get("privatekey", "")
    pub = xk.get("password (publickey)", "")
    seed = mk.get("seed", "")
    verify = mk.get("verify", "")
    if priv and pub:
        return {
            "private_key": priv,
            "public_key": pub,
            "short_id": secrets.token_hex(5)[:10],
            "spiderx": "/",
            "dest": "is1-ssl.mzstatic.com:443",
            "mldsa65_seed": seed,
            "mldsa65_verify": verify,
        }
    # Xray not available: fall back to a Python x25519 keypair so the panel
    # still produces a working config shape.
    mldsa_seed = secrets.token_bytes(64)
    try:
        priv_key, pub_key = _xray_x25519_keypair()
        return {
            "private_key": priv_key,
            "public_key": pub_key,
            "short_id": secrets.token_hex(5)[:10],
            "spiderx": "/",
            "dest": "is1-ssl.mzstatic.com:443",
            "mldsa65_seed": b64.b64encode(mldsa_seed).decode(),
            "mldsa65_verify": _gen_ml_dsa65(mldsa_seed),
        }
    except ImportError:
        return {
            "private_key": "", "public_key": "", "short_id": "5a3ff5a13d",
            "spiderx": "/", "dest": "is1-ssl.mzstatic.com:443",
            "mldsa65_seed": b64.b64encode(mldsa_seed).decode(),
            "mldsa65_verify": _gen_ml_dsa65(mldsa_seed),
        }


def _xray_x25519_public_key(private_key_b64: str) -> str:
    """Derive the X25519 public key from a base64-encoded private key.

    This ensures the public key in config matches what Xray derives from the
    private key, so Reality handshake works."""
    try:
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        import base64 as b64
        key = private_key_b64.strip()
        # Xray emits unpadded URL-safe base64 (e.g. "uMbq3TC3..."). Padding is
        # required by the decoder, and "-"/"_" are the URL-safe alphabet.
        priv_bytes = b64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
        if len(priv_bytes) != 32:
            return ""
        p = X25519PrivateKey.from_private_bytes(priv_bytes)
        pub_bytes = p.public_key().public_bytes_raw()
        return b64.urlsafe_b64encode(pub_bytes).decode().rstrip("=")
    except Exception:
        return ""


def _xray_x25519_keypair() -> tuple:
    """Generate a fresh X25519 keypair as urlsafe base64 WITHOUT padding — the
    exact format Xray's `x25519` emits and the only format Xray-core accepts for
    Reality privateKey (standard padded base64 is rejected: 'invalid privateKey')."""
    try:
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        import base64 as b64
        priv = X25519PrivateKey.generate()
        priv_bytes = priv.private_bytes_raw()
        pub_bytes = priv.public_key().public_bytes_raw()
        return (
            b64.urlsafe_b64encode(priv_bytes).decode().rstrip("="),
            b64.urlsafe_b64encode(pub_bytes).decode().rstrip("="),
        )
    except ImportError:
        return "", ""


def _xray_x25519_privkey_norm(private_key: str) -> str:
    """Re-encode a private key as urlsafe base64 without padding (same raw bytes).
    Fixes keys that were stored as standard padded base64, which Xray rejects."""
    try:
        import base64 as b64
        key = (private_key or "").strip()
        if not key:
            return ""
        decoded = b64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
        if len(decoded) != 32:
            return ""
        return b64.urlsafe_b64encode(decoded).decode().rstrip("=")
    except Exception:
        return ""


# ── WireGuard / AmneziaWG helpers ────────────────────────────────────────────
def _wg_gen_keypair() -> tuple:
    """Generate a WireGuard-compatible X25519 key pair as standard base64."""
    try:
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        import base64 as b64
        priv = X25519PrivateKey.generate()
        priv_bytes = priv.private_bytes_raw()
        pub_bytes = priv.public_key().public_bytes_raw()
        return (
            b64.b64encode(priv_bytes).decode(),
            b64.b64encode(pub_bytes).decode(),
        )
    except Exception as exc:
        logger.warning("WireGuard key generation failed: %s", exc)
        return "", ""


def _normalize_reality_destination(value: str, default_port: int = 443) -> str:
    """Normalize Reality Destination/Target to a bare host:port value.

    Accepts host, host:port and https://host[:port], but rejects paths,
    credentials and whitespace. This keeps Xray's realitySettings.dest valid
    even when the UI receives a custom target.
    """
    import ipaddress
    from urllib.parse import urlsplit

    raw = str(value or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Reality Target cannot be empty")
    if any(ch.isspace() for ch in raw):
        raise HTTPException(status_code=400, detail="Reality Target must not contain whitespace")

    candidate = raw
    if "://" in candidate:
        parsed = urlsplit(candidate)
        if parsed.scheme.lower() not in ("http", "https", "tls") or not parsed.hostname:
            raise HTTPException(status_code=400, detail="Invalid Reality Target")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise HTTPException(status_code=400, detail="Reality Target must be host[:port] only")
        candidate = parsed.netloc

    try:
        if candidate.startswith("["):
            end = candidate.find("]")
            if end < 0:
                raise ValueError
            host = candidate[1:end]
            tail = candidate[end + 1:]
            port = int(tail[1:]) if tail.startswith(":") else default_port
            ipaddress.IPv6Address(host)
            if not 1 <= port <= 65535:
                raise ValueError
            return f"[{host}]:{port}"

        if candidate.count(":") == 1:
            host, port_s = candidate.rsplit(":", 1)
            port = int(port_s)
        elif candidate.count(":") > 1:
            ipaddress.IPv6Address(candidate)
            host, port = candidate, default_port
        else:
            host, port = candidate, default_port

        host = host.rstrip(".").lower()
        if not host or len(host) > 253 or not 1 <= int(port) <= 65535:
            raise ValueError
        if any(ch in host for ch in "/\\@?#"):
            raise ValueError
        # Hostnames may contain letters, digits, dots, hyphens, underscore for
        # compatibility with some SNI test entries. IP literals are allowed.
        return f"{host}:{int(port)}"
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid Reality Target; use host[:port]")


def _normalize_reality_sni(value: str) -> str:
    """Normalize/validate a Reality Server Name (SNI) host."""
    raw = str(value or "").strip().lower()
    if not raw or len(raw) > 253 or any(ch.isspace() for ch in raw):
        raise HTTPException(status_code=400, detail="Invalid Reality SNI")
    if "://" in raw or any(ch in raw for ch in "/\\@?#"):
        raise HTTPException(status_code=400, detail="Reality SNI must be a hostname only")
    if raw.startswith("[") or raw.count(":"):
        raise HTTPException(status_code=400, detail="Reality SNI must not contain a port")
    return raw.rstrip(".")


def _parse_port(value, default=None, field="port") -> int | None:
    """Parse and validate a TCP/UDP port without leaking ValueError as HTTP 500."""
    if value in (None, ""):
        return default
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field} must be a valid integer port")
    if not 1 <= port <= 65535:
        raise HTTPException(status_code=400, detail=f"{field} must be between 1 and 65535")
    return port


def _wg_read_endpoints() -> list:
    """Read available endpoints from data/endpoint.txt."""
    ep_file = Path(os.path.dirname(os.path.abspath(__file__))) / "data" / "endpoint.txt"
    if not ep_file.is_file():
        ep_file = DATA_DIR / "endpoint.txt"
    if not ep_file.is_file():
        return []
    out = []
    for line in ep_file.read_text(errors="ignore").splitlines():
        line = line.strip()
        if line and ":" in line and not line.startswith("#"):
            out.append(line)
    return out


def generate_wg_config(user_id: str, user: dict, inbound: dict, remark_tag: str = None) -> str:
    """Generate an AmneziaWG config for a user based on the inbound settings.

    Supports two modes:
    - WARP mode (warp_mode=True): Uses Cloudflare WARP infrastructure with
      proper WARP PublicKey, Address (IPv4+IPv6), DNS, and endpoints.
    - Custom mode (warp_mode=False): Uses user-defined server settings.
    """
    username = user.get("username", user_id)
    wg = inbound.get("wireguard_settings") or {}
    warp_mode = bool(wg.get("warp_mode"))

    # Generate per-user WireGuard private key
    priv_key, _ = _wg_gen_keypair()
    if not priv_key:
        return ""

    # AmneziaWG obfuscation parameters
    jc = wg.get("jc") or "3"
    jmin = wg.get("jmin") or "1"
    jmax = wg.get("jmax") or "3"
    s1 = wg.get("s1") or "0"
    s2 = wg.get("s2") or "0"
    h1 = wg.get("h1") or "1"
    h2 = wg.get("h2") or "2"
    h3 = wg.get("h3") or "3"
    h4 = wg.get("h4") or "4"
    i1 = wg.get("i1") or ""

    if warp_mode:
        # ── WARP mode: use Cloudflare WARP infrastructure ──
        # Fixed WARP PublicKey (same for all WARP users)
        server_pub = "bmXOC+F1FxEMF9dyiK2H5/1SUtzH0JuVo51h2wPfgyo="
        # WARP address: 172.16.0.x/32 + IPv6
        address = wg.get("address") or "172.16.0.2/32, 2606:4700:110::2/128"
        # WARP DNS: Cloudflare DNS with IPv6
        dns = wg.get("dns") or "1.1.1.1, 1.0.0.1, 2606:4700:4700::1111, 2606:4700:4700::1001"
        mtu = wg.get("mtu") or "1280"
        # Endpoint from the configured endpoint (should be a Cloudflare WARP IP)
        endpoint = wg.get("endpoint") or "8.6.112.4:443"
    else:
        # ── Custom WireGuard server mode ──
        server_pub = wg.get("server_public_key") or ""
        if not server_pub:
            _, server_pub = _wg_gen_keypair()
        endpoint = wg.get("endpoint") or "8.6.112.4:443"
        mtu = wg.get("mtu") or "1280"
        address = wg.get("address") or "172.16.0.2/32"
        dns = wg.get("dns") or "1.1.1.1,1.0.0.1"

    rem = f"Spider-{username}-WG"
    if remark_tag:
        rem = f"{rem} {remark_tag}"

    config = f"""# AmneziaWG Config
[Interface]
PrivateKey = {priv_key}
Address = {address}
DNS = {dns}
MTU = {mtu}
Jc = {jc}
Jmin = {jmin}
Jmax = {jmax}
S1 = {s1}
S2 = {s2}
H1 = {h1}
H2 = {h2}
H3 = {h3}
H4 = {h4}
I1 = {i1}

[Peer]
PublicKey = {server_pub}
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = {endpoint}
"""
    return config.strip()


def _railway_tcp_info() -> dict:
    """Return Railway TCP Proxy routing metadata when TCP Proxy is enabled.

    Railway exposes the externally reachable hostname/port separately from the
    service's internal application port. Telegram MTProto must listen on the
    internal TCP application port, while user links must use the public TCP
    proxy domain/port.
    """
    domain = str(os.getenv("RAILWAY_TCP_PROXY_DOMAIN") or "").strip()
    try:
        public_port = int(str(os.getenv("RAILWAY_TCP_PROXY_PORT") or "0").strip() or 0)
    except Exception:
        public_port = 0
    try:
        app_port = int(str(os.getenv("RAILWAY_TCP_APPLICATION_PORT") or "0").strip() or 0)
    except Exception:
        app_port = 0
    return {"domain": domain, "public_port": public_port, "application_port": app_port,
            "enabled": bool(domain and public_port and app_port)}


@app.get("/api/telegram/railway-info")
async def telegram_railway_info(_=Depends(require_auth)):
    info = _railway_tcp_info()
    return {"ok": True, "advisory": True, "message": "Railway TCP variables are informational only; inbound External Domain/Port are authoritative.", **info}


def generate_telegram_proxy_link(user_id: str, user: dict, inbound: dict, remark_tag: str = None) -> str:
    """Generate a Telegram Proxy link for a user based on the inbound settings.

    The secret is deterministic per user (derived from config_uuid) and stored
    on the user record so it remains stable across config regenerations.
    """
    from telegram_proxy import derive_secret_from_uuid

    username = user.get("username", user_id)
    config_uuid = user.get("config_uuid", user_id)
    tg = inbound.get("telegram_settings") or {}

    # Telegram inbound settings are authoritative. Railway TCP variables are
    # advisory only and must not override what the user entered in the inbound.
    external_domain = str(tg.get("external_domain") or inbound.get("external_domain") or "").strip()
    try:
        external_port = int(tg.get("external_port") or inbound.get("external_port") or 0)
    except Exception:
        external_port = 0
    if not external_domain or not external_port:
        return ""

    # Use stored secret or derive a stable one
    secret = user.get("telegram_secret") or derive_secret_from_uuid(config_uuid)

    # Note: Telegram t.me/proxy links don't support #remark fragment like VLESS links
    # The name is set by the user in the Telegram app after adding the proxy
    link = f"https://t.me/proxy?server={external_domain}&port={external_port}&secret={secret}"
    return link


XRAY_URL = "https://github.com/XTLS/Xray-core/releases/download/v26.3.27/Xray-linux-64.zip"


async def _ensure_xray() -> bool:
    """Download + unzip the Xray binary once into BASE/xray so a Reality/xhttp
    inbound can actually be served (the panel's own relay only handles VLESS
    ws/xhttp; Reality needs the real Xray). Safe to call on every startup —
    it no-ops when the binary already exists."""
    import subprocess, zipfile, shutil
    xray_dir = Path(os.path.dirname(os.path.abspath(__file__))) / "xray"
    bin_path = xray_dir / "xray"
    if bin_path.exists() and bin_path.stat().st_size > 100000:
        return True
    try:
        xray_dir.mkdir(parents=True, exist_ok=True)
        zip_path = xray_dir / "xray.zip"
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
            r = await c.get(XRAY_URL)
            if r.status_code != 200:
                logger.warning(f"Xray download failed: HTTP {r.status_code}")
                return False
            zip_path.write_bytes(r.content)
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            target = "xray" if "xray" in names else (names[0] if names else None)
            if not target:
                return False
            z.extract(target, xray_dir)
        shutil.move(xray_dir / target, bin_path)
        os.chmod(bin_path, 0o755)
        zip_path.unlink(missing_ok=True)
        logger.info(f"Xray installed at {bin_path}")
        return True
    except Exception as e:
        logger.warning(f"Xray install failed: {e}")
        return False


# ── Startup / Shutdown ────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    global http_client
    limits = httpx.Limits(max_connections=500, max_keepalive_connections=100)
    timeout = httpx.Timeout(30.0, connect=10.0)
    http_client = httpx.AsyncClient(
        limits=limits, timeout=timeout, follow_redirects=True,
    )
    await load_state()
    # Auto-create default inbound if none exist
    startup_state_changed = False
    async with INBOUNDS_LOCK:
        if not INBOUNDS:
            INBOUNDS["default"] = {
                "name": "VLESS+WS پیش‌فرض",
                "protocol": "vless",
                "port": 443,
                "network": "ws",
                "security": "tls",
                "domain": _safe_host(SETTINGS.get("domain"), get_host()),
                "external_domain": _safe_host(SETTINGS.get("domain"), get_host()),
                "sni": _safe_host(SETTINGS.get("domain"), get_host()),
                "external_port": 443,
                "fingerprint": "chrome",
                "reality_settings": {},
                "xhttp_settings": {},
                "ws_settings": {"path": "/ws"},
                "grpc_settings": {},
                "created_at": datetime.now().isoformat(),
            }
            startup_state_changed = True
            log_activity("inbound", "اینباند پیش‌فرض VLESS+WS ساخته شد", "ok")
        # Auto-create a default Reality+xhttp inbound (needs real Xray to serve)
        has_reality = any(
            (ib.get("security") or "").lower() == "reality"
            and (ib.get("protocol") or "").lower() == "vless"
            for ib in INBOUNDS.values()
        )
        # Normalize the managed default TLS+WS inbound to the panel domain on every startup.
        default_ib = INBOUNDS.get("default")
        if default_ib and (default_ib.get("protocol") or "").lower() == "vless" and (default_ib.get("network") or "ws").lower() == "ws" and (default_ib.get("security") or "tls").lower() == "tls":
            panel_host = _safe_host(SETTINGS.get("domain"), get_host())
            before_default = (default_ib.get("port"), default_ib.get("domain"), default_ib.get("external_domain"), default_ib.get("sni"), default_ib.get("external_port"), (default_ib.get("ws_settings") or {}).get("path"))
            default_ib["port"] = 443
            default_ib["domain"] = panel_host
            default_ib["external_domain"] = panel_host
            default_ib["sni"] = panel_host
            default_ib["external_port"] = 443
            default_ib.setdefault("ws_settings", {})["path"] = (default_ib.get("ws_settings") or {}).get("path") or "/ws"
            after_default = (default_ib.get("port"), default_ib.get("domain"), default_ib.get("external_domain"), default_ib.get("sni"), default_ib.get("external_port"), (default_ib.get("ws_settings") or {}).get("path"))
            if before_default != after_default:
                startup_state_changed = True

        if not has_reality:
            rs = _gen_reality_settings()
            # Reality inbound: domain + ports are LEFT EMPTY — the admin fills
            # them in (external domain + external port + listen port). The pbk
            # keypair is auto-generated here so it's always ready.
            INBOUNDS["default-reality"] = {
                "name": "Reality+XHTTP",
                "protocol": "vless",
                "port": 8443,
                "network": "xhttp",
                "security": "reality",
                "domain": "",
                "external_domain": "",
                "sni": "",
                "external_port": "",
                "fingerprint": "chrome",
                "reality_settings": rs,
                "xhttp_settings": {
                    "path": "/",
                    "xPaddingBytes": "100-1000",
                    "mode": "stream-up",
                    "scMaxEachPostBytes": "1000000",
                },
                "created_at": datetime.now().isoformat(),
            }
            startup_state_changed = True
            log_activity("inbound", "اینباند پیش‌فرض Reality+XHTTP ساخته شد", "ok")
        # the deployed Cloudflare Worker domain (address/host/sni auto-filled),
        # with BPB snispoofing. Only created once a worker is actually connected.
        has_worker = any((ib.get("protocol") or "").lower() == "worker" for ib in INBOUNDS.values())
        _wdom_now = _worker_safe_domain(WORKER.get("worker_domain"))
        if not has_worker and _wdom_now:
            INBOUNDS["default-worker"] = {
                "name": "Worker (Multi-Location)",
                "protocol": "worker",
                "port": 443,
                "network": "ws",
                "security": "tls",
                "domain": _wdom_now,
                "external_domain": _wdom_now,
                "sni": "www.hcaptcha.com",
                "spoof_ip": "8.6.112.4",
                "external_port": 443,
                "fingerprint": "chrome",
                "reality_settings": {},
                "xhttp_settings": {},
                "ws_settings": {"path": "/route/{country}/{uuid}"},
                "grpc_settings": {},
                "created_at": datetime.now().isoformat(),
            }
            startup_state_changed = True
            log_activity("inbound", "اینباند پیش‌فرض Worker ساخته شد", "ok")

    if startup_state_changed:
        asyncio.create_task(save_state())

    # NODE inbound is created lazily after the first real node is added.
    # Clean up the legacy auto-created inbound when no nodes are registered.
    if not NODES:
        async with INBOUNDS_LOCK:
            stale_node_ids = [iid for iid, ib in INBOUNDS.items()
                              if (ib.get("protocol") or "").lower() == "node"]
            for iid in stale_node_ids:
                INBOUNDS.pop(iid, None)
        if stale_node_ids:
            asyncio.create_task(save_state())
            log_activity("inbound", "اینباند قدیمی NODE حذف شد؛ تا ثبت اولین Node ساخته نمی‌شود", "info")

    # Normalize existing Telegram inbounds: only internal/external Telegram fields
    # are meaningful. Remove legacy SNI/Destination/Server Name state.
    for _tg_ib in INBOUNDS.values():
        if (_tg_ib.get("protocol") or "").lower() == "telegram":
            _tg = _tg_ib.setdefault("telegram_settings", {})
            _tg["internal_port"] = int(_tg.get("internal_port") or _tg_ib.get("port") or 44344)
            _tg["external_port"] = int(_tg.get("external_port") or _tg_ib.get("external_port") or 443)
            _tg["external_domain"] = str(_tg.get("external_domain") or _tg_ib.get("external_domain") or "").strip()
            _tg_ib["port"] = _tg["internal_port"]
            _tg_ib["external_port"] = _tg["external_port"]
            _tg_ib["external_domain"] = _tg["external_domain"]
            _tg_ib.pop("sni", None)
            _tg_ib.pop("destination", None)
            _tg_ib.pop("server_name", None)

    # Normalize existing WireGuard inbounds so legacy entries always have a real
    # UDP listen port and a stable server keypair.
    _wg_changed = False
    for _wg_ib in INBOUNDS.values():
        if (_wg_ib.get("protocol") or "").lower() != "wireguard":
            continue
        _wg = _wg_ib.setdefault("wireguard_settings", {})
        if not _wg.get("listen_port"):
            _wg["listen_port"] = _wg_ib.get("port") or 51820
            _wg_changed = True
        if not _wg_ib.get("port"):
            _wg_ib["port"] = _wg.get("listen_port") or 51820
            _wg_changed = True
        if not _wg.get("server_private_key") or not _wg.get("server_public_key"):
            _wk, _wp = _wg_gen_keypair()
            if _wk and _wp:
                _wg["server_private_key"] = _wk
                _wg["server_public_key"] = _wp
                _wg_changed = True
    if _wg_changed:
        asyncio.create_task(save_state())

    # Backfill placeholder domains on any pre-existing inbounds so configs never
    # carry localhost/SERVER_IP when a real domain is available.
    _real = _safe_host(SETTINGS.get("domain"), get_host())
    _real_is_rlwy = ".rlwy.net" in _real or ".up.railway.app" in _real
    _changed = False
    for _ib in INBOUNDS.values():
        _proto = (_ib.get("protocol") or "").lower()
        _sec = (_ib.get("security") or "").lower()
        _is_reality = _proto == "reality" or _sec == "reality"
        _cur = str(_ib.get("domain") or "")
        _cext = str(_ib.get("external_domain") or "")
        # Railway rotates its public domain (sakura... → production-221d...).
        # If an inbound points at an OLD rlwy/railway domain, refresh it to the
        # current reachable domain. This applies to EVERY inbound (reality too),
        # but only when the domain is already filled — an empty reality inbound
        # (waiting for the admin) stays empty.
        if _real_is_rlwy and _cext and (".rlwy.net" in _cext or ".up.railway.app" in _cext) and _cext != _real:
            _ib["external_domain"] = _real
            if _is_reality or _cur in ("", "0.0.0.0", "127.0.0.1", "localhost", "SERVER_IP"):
                _ib["domain"] = _real
            _changed = True
            logger.info("Inbound «%s» external domain refreshed %s → %s", _ib.get("name"), _cext, _real)
        # Fill placeholder/empty domains (non-reality only; reality stays empty
        # until the admin configures it).
        elif not _is_reality:
            if _cur in ("", "0.0.0.0", "127.0.0.1", "localhost", "SERVER_IP"):
                _ib["domain"] = _real
                _changed = True
            # For TLS WS/XHTTP inbounds: external_domain should be empty (panel domain used via SETTINGS["domain"])
            # For Worker inbounds: external_domain should be the worker domain (set by _ensure_worker_inbound)
            if _proto != "worker" and _cext in ("", "0.0.0.0", "127.0.0.1", "localhost", "SERVER_IP"):
                _ib["external_domain"] = ""
                _changed = True
    if _changed:
        asyncio.create_task(save_state())
        logger.info("Backfilled placeholder inbound domains with %s", _real)

    # Deduplicate inbound ports: on Railway each inbound must listen on its own
    # port (443 can only be used once). Non-default inbounds that collide with
    # an earlier one are moved to the next free port (80xx range).
    _seen_ports: dict = {}
    for _ib in INBOUNDS.values():
        _p = int(_ib.get("port") or 0)
        if _p and _p in _seen_ports:
            _np = _p + 1
            while _np in _seen_ports or _np == 80:
                _np += 1
            _ib["port"] = _np
            if not _ib.get("external_port"):
                _ib["external_port"] = _np
            _changed = True
            logger.info("Inbound «%s» moved to free port %s (was %s)", _ib.get("name"), _np, _p)
        _seen_ports[int(_ib.get("port") or 0)] = True

    # Reality migration: every reality inbound must carry a WORKING pbk/sid —
    # a pbk that is actually the public half of the private key Xray will use.
    # Old inbounds created before the key-gen fix have empty or mismatched keys;
    # backfill by deriving pbk from the private key, or generate a fresh pair.
    _gs_rs = SETTINGS.get("reality", {}) or {}
    # Normalize the global Reality keys too (they backfill into inbounds).
    for _fld in ("private_key", "public_key"):
        if _fld == "private_key":
            _nv = _xray_x25519_privkey_norm(str(_gs_rs.get("private_key") or ""))
            if _nv and _nv != _gs_rs.get("private_key"):
                _gs_rs["private_key"] = _nv
                _changed = True
    if _gs_rs.get("private_key"):
        _gp = _xray_x25519_public_key(str(_gs_rs.get("private_key")))
        if _gp and _gp != _gs_rs.get("public_key"):
            _gs_rs["public_key"] = _gp
            _changed = True
    for _ib in INBOUNDS.values():
        if (_ib.get("protocol") or "").lower() != "reality" and (_ib.get("security") or "").lower() != "reality":
            continue
        _rs = _ib.setdefault("reality_settings", {})
        _priv = str(_rs.get("private_key") or "")
        _pub = str(_rs.get("public_key") or "")
        # Keys must be urlsafe base64 without padding (what Xray emits and accepts).
        # Standard padded base64 makes Xray fail with 'invalid "privateKey"' — re-encode
        # the same raw bytes so existing clients keep working.
        _norm = _xray_x25519_privkey_norm(_priv) if _priv else ""
        if _norm and _norm != _priv:
            _rs["private_key"] = _norm
            _changed = True
            logger.info("Reality inbound «%s» private key re-encoded to urlsafe base64", _ib.get("name"))
            _priv = _norm
        # If we have a private key, derive its public key — that is the ONLY
        # pbk that works with Xray (which uses the same private key).
        _derived = _xray_x25519_public_key(_priv) if _priv else ""
        if _derived and _derived != _pub:
            _rs["public_key"] = _derived
            _changed = True
            logger.info("Reality inbound «%s» pbk re-derived from private key", _ib.get("name"))
        if not _rs.get("public_key") or not _rs.get("private_key"):
            if _gs_rs.get("public_key") and _gs_rs.get("private_key"):
                _rs.setdefault("public_key", _gs_rs.get("public_key"))
                _rs.setdefault("private_key", _gs_rs.get("private_key"))
            else:
                _fresh = _gen_reality_settings()
                _rs.setdefault("private_key", _fresh.get("private_key", ""))
                _rs.setdefault("public_key", _fresh.get("public_key", ""))
            _changed = True
            logger.info("Reality inbound «%s» backfilled with pbk", _ib.get("name"))
        _rs.setdefault("short_id", _gs_rs.get("short_id") or secrets.token_hex(5)[:10])
        _rs.setdefault("spiderx", "/")
        _rs.setdefault("dest", "is1-ssl.mzstatic.com:443")
        _rs.setdefault("sni", "is1-ssl.mzstatic.com")
        # Internal and external ports are intentionally separate.
        # Xray MUST listen on internal `port`; the client connects to
        # external_domain:external_port (for example a Railway TCP proxy).
        # Never overwrite one with the other.

    # WS/XHTTP-TLS inbounds are served by the FastAPI relay on the panel's own
    # port (CONFIG["port"]); the client-facing port stays 443 (Railway TLS).
    # Syncing port→CONFIG["port"] ensures the relay and config agree.
    _relay_port = int(CONFIG.get("port") or 8080)
    for _ib in INBOUNDS.values():
        _proto = (_ib.get("protocol") or "").lower()
        _sec = (_ib.get("security") or "").lower()
        if _proto == "worker" or _proto == "reality" or _sec == "reality":
            continue
        if int(_ib.get("port") or 0) != _relay_port:
            _ib["port"] = _relay_port
            _changed = True
            logger.info("TLS inbound «%s» relay port synced to %s", _ib.get("name"), _relay_port)
    if _changed:
        asyncio.create_task(save_state())

    # If a worker is connected, make sure the default Worker inbound exists and
    # points at the worker domain (address/host/sni auto-filled at boot too).
    if WORKER.get("connected"):
        await _ensure_worker_inbound()

    # User path migration: each user's path must match their WS inbound. A user
    # who has a WS (or worker) inbound must have path /ws/{config_uuid} so the
    # FastAPI relay (/ws/{uuid}) can tunnel it. Old users created when reality
    # was the primary inbound may carry /xhttp-siz10/... paths that break WS.
    _up_changed = False
    for _uid, _u in USERS.items():
        _cuuid = _u.get("config_uuid") or _uid
        _iids = _u.get("inbound_ids") or ([_u.get("inbound_id")] if _u.get("inbound_id") else [])
        # A user path is /ws/{uuid} if any EXISTING inbound is a WS/TLS inbound.
        # Missing inbounds (deleted) don't force WS.
        _has_ws = any(
            (lambda _ib: bool(_ib) and _ib.get("protocol") == "vless" and _ib.get("network") == "ws")(INBOUNDS.get(i))
            for i in _iids
        )
        _cur = str(_u.get("path") or "").strip()
        if _has_ws and "/ws/" not in _cur:
            _u["path"] = f"/ws/{_cuuid}"
            _up_changed = True
            logger.info("User «%s» path fixed to /ws/%s", _u.get("username", _uid), _cuuid)
    if _up_changed:
        asyncio.create_task(save_state())

    # Ensure Xray is installed and serving reality BEFORE the panel is fully up,
    # so reality configs work immediately (not in a background task).
    await _ensure_xray()
    if _xray_bin_path().exists():
        try:
            await _xray_apply()
        except Exception as e:
            logger.warning(f"Xray apply on boot failed: {e}")
    log_activity("system", "سرور راه‌اندازی شد", "ok")
    logger.info(f"Spider Gateway v9.2 (commit 24d7594) started on port {CONFIG['port']}")
    # Include XHTTP router for xhttp-siz10 endpoints (globals are now defined)
    global xhttp_router
    from xhttp_siz10 import router as xhttp_router
    app.include_router(xhttp_router)
    asyncio.create_task(_worker_proxy_sync_loop())
    asyncio.create_task(_worker_auto_sync_loop())
    asyncio.create_task(_xray_client_audit_loop())

    # Detect server IP + country (non-blocking)
    asyncio.create_task(_detect_server_info())

    # Generate Panel API Key if one doesn't exist
    global PANEL_API_KEY
    if not PANEL_API_KEY:
        from spider_features import generate_panel_api_key
        PANEL_API_KEY = generate_panel_api_key()
        asyncio.create_task(save_state())

    # Start Telegram Proxy instances for all existing TG inbounds
    await _start_all_telegram_proxies()


# ── Server Info Detection ──────────────────────────────────────────────────
async def _detect_server_info():
    """Detect public IP + country at boot and periodically refresh."""
    try:
        from spider_features import detect_server_info
        async with SERVER_INFO_LOCK:
            if SERVER_INFO.get("public_ip") and SERVER_INFO.get("country"):
                return  # Already detected
        info = await detect_server_info(http_client)
        if info.get("public_ip"):
            async with SERVER_INFO_LOCK:
                SERVER_INFO.update(info)
            asyncio.create_task(save_state())
            logger.info("Server detected: %s (%s)", info["country"], info["public_ip"])
    except Exception as e:
        logger.warning("Server info detection failed: %s", e)


# ── Telegram Proxy Lifecycle ────────────────────────────────────────────────
async def _start_all_telegram_proxies():
    """Start MTProto proxy servers for all Telegram inbounds."""
    from telegram_proxy import derive_secret_from_uuid, is_docker_available, run_docker_telegram_proxy, stop_docker_telegram_proxy
    async with INBOUNDS_LOCK:
        snap = dict(INBOUNDS)
    for iid, ib in snap.items():
        if (ib.get("protocol") or "").lower() == "telegram":
            await _start_telegram_proxy(iid, ib)


async def _start_telegram_proxy(inbound_id: str, inbound: dict):
    """Start one MTProto listener on the inbound internal port.

    The built-in implementation is used because a single listener must accept
    all user secrets assigned to this inbound; spawning one Docker process per
    user would require different host ports. The listener therefore belongs to
    the Telegram inbound itself, while credentials remain per-user.
    """
    from telegram_proxy import MTProtoProxyServer, derive_secret_from_uuid

    await _stop_telegram_proxy(inbound_id)
    tg = inbound.get("telegram_settings") or {}
    # The inbound values are the single source of truth. Railway environment
    # variables are informational only and must never silently rewrite them.
    try:
        int_port = int(tg.get("internal_port") or inbound.get("port") or 44344)
    except Exception:
        int_port = 44344
    try:
        ext_port = int(tg.get("external_port") or inbound.get("external_port") or 0)
    except Exception:
        ext_port = 0
    ext_domain = str(tg.get("external_domain") or inbound.get("external_domain") or "").strip()
    inbound["telegram_settings"] = {
        "internal_port": int_port,
        "external_port": ext_port,
        "external_domain": ext_domain,
        "promotion_tag": str(tg.get("promotion_tag") or tg.get("promotion_channel") or "").strip(),
        "promotion_channel": str(tg.get("promotion_channel") or "").strip(),
    }
    inbound["port"] = int_port
    inbound["external_port"] = ext_port
    inbound["external_domain"] = ext_domain

    secrets_map = {}
    async with USERS_LOCK:
        for uid, u in USERS.items():
            iids = u.get("inbound_ids") or []
            if inbound_id in iids:
                config_uuid = u.get("config_uuid", uid)
                secret = str(u.get("telegram_secret") or "").strip().lower()
                if not re.fullmatch(r"[0-9a-f]{32}", secret):
                    secret = derive_secret_from_uuid(config_uuid)
                    u["telegram_secret"] = secret
                secrets_map[secret] = {"user_id": uid, "config_uuid": config_uuid, "label": u.get("username", uid)}

    raw_promo = str(tg.get("promotion_tag") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", raw_promo):
        raw_promo = ""
    server = MTProtoProxyServer(inbound_id=inbound_id, port=int_port, promotion_tag=raw_promo)
    server.update_secrets(secrets_map)
    if not secrets_map:
        logger.info(f"[TG Proxy {inbound_id}] no users yet; listener will start after a Telegram user is assigned")
        return

    def on_traffic(user_id: str, nbytes: int):
        asyncio.create_task(_sync_tg_traffic(user_id, nbytes))
    server.on_traffic = on_traffic

    try:
        await server.start()
        TG_PROXY_INSTANCES[inbound_id] = server
        log_activity("telegram-proxy", f"Telegram MTProto listening on internal port {int_port}", "ok")
    except Exception as e:
        logger.error(f"Failed to start Telegram MTProto for {inbound_id}: {e}")


async def _stop_telegram_proxy(inbound_id: str):
    """Stop the Telegram proxy server for a Telegram inbound (both Python and Docker)."""
    # Stop Python proxy if running
    server = TG_PROXY_INSTANCES.pop(inbound_id, None)
    if server:
        await server.stop()

    # Stop Docker containers if any
    from telegram_proxy import stop_docker_telegram_proxy, is_docker_available
    if is_docker_available():
        # Stop all containers for this inbound_id
        for i in range(10):  # Try up to 10 possible container names (for different secrets)
            container_name = f"spider-tg-proxy-{inbound_id}-"
            # We can't know the exact secret suffix, so we'll stop any matching
            import subprocess
            try:
                result = subprocess.run(
                    ["docker", "ps", "-a", "--filter", f"name=spider-tg-proxy-{inbound_id}-", "--format", "{{.Names}}"],
                    capture_output=True, text=True, timeout=10
                )
                for name in result.stdout.strip().split('\n'):
                    if name:
                        subprocess.run(["docker", "stop", name], capture_output=True, timeout=10)
                        subprocess.run(["docker", "rm", name], capture_output=True, timeout=10)
                        logger.info(f"Stopped Docker Telegram proxy: {name}")
            except Exception as e:
                logger.error(f"Failed to stop Docker Telegram proxy: {e}")


async def _restart_telegram_proxy(inbound_id: str):
    """Restart the MTProto proxy for an inbound (e.g. after settings change)."""
    async with INBOUNDS_LOCK:
        ib = INBOUNDS.get(inbound_id)
    if ib and (ib.get("protocol") or "").lower() == "telegram":
        await _start_telegram_proxy(inbound_id, ib)


async def _sync_tg_traffic(user_id: str, nbytes: int):
    """Sync Telegram proxy traffic back to the user record."""
    try:
        async with USERS_LOCK:
            u = USERS.get(user_id)
            if u:
                u["traffic_used_bytes"] = u.get("traffic_used_bytes", 0) + nbytes
        asyncio.create_task(save_state())
    except Exception:
        pass


# Worker proxy source sync — hourly pull from the daily GitHub list and push to
# the deployed Cloudflare Worker (Railway is the control plane; the Worker gets
# a fresh country → proxy map without the user doing anything).
WORKER_SYNC_INTERVAL = int(os.environ.get("WORKER_SYNC_INTERVAL", 3600))  # seconds


async def _worker_proxy_sync_loop():
    """Background loop: every hour, if the worker is connected and auto-sync is
    on, fetch the daily proxy source, parse it into country → proxy and re-deploy
    the worker. Failures are recorded and retried next tick."""
    # First tick quickly so the panel starts with fresh proxies.
    await asyncio.sleep(30)
    while True:
        try:
            if WORKER.get("connected"):
                if WORKER.get("auto_sync"):
                    await _sync_worker_proxies_from_source()
                # Worker owns runtime traffic counters; pull them back to Railway
                # so the panel dashboard/subscription always reflects reality.
                await _worker_pull_all_users()
        except Exception as e:
            logger.warning(f"worker sync failed: {e}")
        await asyncio.sleep(min(WORKER_SYNC_INTERVAL, 30))

WORKER_AUTO_SYNC_INTERVAL = int(os.environ.get("WORKER_AUTO_SYNC_INTERVAL", 300))  # seconds

async def _worker_auto_sync_loop():
    """Background loop: keeps panel and worker in sync without any manual action.

    Every WORKER_AUTO_SYNC_INTERVAL seconds (default 5 min), while a worker is
    connected:
      1. push users/routes/settings → worker (POST /panel/config),
      2. pull live usage + worker-generated configs back (GET /api/user/{uuid}),
      3. refresh the remote status/heartbeat shown in the Worker tab.
    Failures (e.g. KV daily limit) are logged and retried next tick."""
    await asyncio.sleep(20)  # let startup finish
    while True:
        try:
            if WORKER.get("connected"):
                push = await _worker_push_config()
                if not push.get("ok"):
                    logger.warning(f"worker auto-push failed: {push.get('detail')}")
                await _worker_pull_all_users()
                await _worker_pull_status()
        except Exception as e:
            logger.warning(f"worker auto-sync failed: {e}")
        await asyncio.sleep(WORKER_AUTO_SYNC_INTERVAL)


@app.on_event("shutdown")
async def shutdown():
    # Stop all Telegram Proxy instances
    for iid in list(TG_PROXY_INSTANCES.keys()):
        await _stop_telegram_proxy(iid)
    await save_state()
    if http_client:
        await http_client.aclose()

# ── Helpers ───────────────────────────────────────────────────────────────────
def get_host() -> str:
    return os.environ.get("RAILWAY_PUBLIC_DOMAIN", CONFIG["host"])


def _safe_host(*candidates: str) -> str:
    """Return the first non-empty candidate that isn't a placeholder host,
    falling back to get_host(). Used so configs never carry localhost/SERVER_IP
    when a real domain is available."""
    bad = {"", "0.0.0.0", "127.0.0.1", "localhost", "SERVER_IP"}
    for c in candidates:
        if c and c.strip() not in bad:
            return c.strip()
    return get_host()

def generate_inbound_name(existing=None) -> str:
    """Generate a short human-readable inbound name when the user leaves it blank."""
    used = {str(x or "").strip().casefold() for x in (existing or {}).values()} if isinstance(existing, dict) else set(existing or [])
    for _ in range(100):
        candidate = f"Inbound-{secrets.token_hex(3).upper()}"
        if candidate.casefold() not in used:
            return candidate
    return f"Inbound-{uuid.uuid4().hex[:8].upper()}"


def generate_uuid() -> str:
    """Generate a standard hyphenated UUID (RFC 4122) — required by VLESS clients
    and the worker's uuid validation (the worker rejects 32-char bare hex)."""
    return str(uuid.uuid4())


def generate_random_path(prefix: str = "", length: int = 6) -> str:
    """Generate a URL-safe random path segment once per user.

    Returns a path like /a83d91c5, /api-f7a29c, /cdn-91ad3b2f.
    Called ONCE at user creation time then stored permanently.
    """
    if prefix:
        return f"/{prefix}-{secrets.token_hex(length)}"
    return f"/{secrets.token_hex(length)}"


def now_ir() -> datetime:
    return datetime.now(IRAN_TZ)

def generate_vless_link(uuid: str, host: str, remark: str = "Spider", protocol: str = DEFAULT_PROTOCOL) -> str:
    """می‌سازد VLESS share-link متناسب با پروتکل انتخاب‌شده (WS کلاسیک یا یکی از مدهای XHTTP)."""
    if protocol == "vless-ws":
        path = f"/ws/{uuid}"
        params = {
            "encryption": "none",
            "security": "tls",
            "type": "ws",
            "host": host,
            "path": path,
            "sni": host,
            "fp": "chrome",
            "alpn": "http/1.1",
        }
    else:
        # xhttp-packet-up / xhttp-stream-up
        mode = protocol.replace("xhttp-", "")
        if mode not in ("packet-up", "stream-up"):
            mode = "stream-up"
        path = f"/xhttp-siz10/{mode}/{uuid}"
        params = {
            "encryption": "none",
            "security": "tls",
            "type": "xhttp",
            "mode": mode,
            "host": host,
            "path": path,
            "sni": host,
            "fp": "chrome",
            "alpn": "h2,http/1.1",
        }
    query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    return f"vless://{uuid}@{host}:443?{query}#{quote(remark)}"

def uptime() -> str:
    secs = int(time.time() - stats["start_time"])
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def uptime_secs():
    return max(time.time() - stats["start_time"], 1)

def parse_size_to_bytes(value: float, unit: str) -> int:
    unit = unit.upper()
    if unit == "GB": return int(value * 1024 ** 3)
    if unit == "MB": return int(value * 1024 ** 2)
    if unit == "KB": return int(value * 1024)
    return int(value)

def is_link_expired(link: dict) -> bool:
    exp = link.get("expires_at")
    if not exp:
        return False
    try:
        return datetime.now() > datetime.fromisoformat(exp)
    except Exception:
        return False

def is_link_allowed(link: dict | None) -> bool:
    if link is None:
        return False
    if not link.get("active", True):
        return False
    if is_link_expired(link):
        return False
    lb = link.get("limit_bytes", 0)
    if lb > 0 and link.get("used_bytes", 0) >= lb:
        return False
    return True

def fmt_bytes(b: int) -> str:
    if b < 1024: return f"{b} B"
    if b < 1024**2: return f"{b/1024:.2f} KB"
    if b < 1024**3: return f"{b/1024**2:.2f} MB"
    if b < 1024**4: return f"{b/1024**3:.2f} GB"
    return f"{b/1024**4:.2f} TB"

def client_ip(request: Request) -> str:
    """آی‌پی واقعی کلاینت رو با احتساب هدرهای پراکسی (Railway/Cloudflare) برمی‌گردونه."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "نامشخص"


# ── User helper functions ────────────────────────────────────────────────────
def is_user_allowed(user: dict | None) -> bool:
    """Check if a user is active and not expired."""
    if user is None:
        return False
    if user.get("status") == "disabled":
        return False
    if user.get("status") == "expired":
        return False
    exp = user.get("expire_at")
    if exp:
        try:
            if datetime.now() > datetime.fromisoformat(exp):
                user["status"] = "expired"
                return False
        except Exception:
            pass
    lb = user.get("traffic_limit_bytes", 0)
    if lb > 0 and user.get("traffic_used_bytes", 0) >= lb:
        return False
    return True

def auto_check_user_expiry(user: dict):
    """Auto-mark user as expired if past expire_at."""
    if not user:
        return
    exp = user.get("expire_at")
    if not exp:
        return
    try:
        if datetime.now() > datetime.fromisoformat(exp):
            if user.get("status") not in ("expired", "disabled"):
                user["status"] = "expired"
    except Exception:
        pass

def generate_short_id() -> str:
    """Generate a shorter ID for user management."""
    return secrets.token_hex(6)

def generate_user_config(user_id: str, user: dict, inbound_id: str = None, addr: str = None, remark_tag: str = None) -> str:
    """Build a VLESS config string for one inbound of a user.

    Three config families (one per inbound type):
      - Reality  → served by Xray core (address/host/port/pbk/sid come from the
                   inbound's reality settings + external domain/port)
      - TLS WS/XHTTP → served by the FastAPI relay; address/host/sni = the
                   panel's main domain, port 443. ws is the default transport,
                   xhttp is selectable per inbound.
      - Worker   → served by the Cloudflare Worker; address/host/sni = worker
                   domain, path /route/{country}/{uuid} (see _worker_configs).

    addr (scanned custom IP) overrides only the connect address; host/sni stay
    on the real domain so the TLS handshake reaches the service.
    """
    inbound = INBOUNDS.get(inbound_id) if inbound_id else None
    proto = (inbound.get("protocol") if inbound else None) or (user.get("protocol") or "vless")
    proto = proto.lower()
    sec = (inbound.get("security") if inbound else None) or "tls"
    sec = sec.lower()
    panel_domain = _safe_host(SETTINGS.get("domain"), get_host())

    config_uuid = user.get("config_uuid", "") or user_id
    username = user.get("username", user_id)
    rem = f"Spider-{username}"
    if remark_tag:
        rem = f"{rem} {remark_tag}"
    remark = quote(rem)

    # Optional custom-IP address override (only the connect address changes).
    addr_ip, addr_port = None, None
    if addr:
        addr = addr.strip()
        if ":" in addr:
            addr_ip, _, addr_port = addr.rpartition(":")
        else:
            addr_ip, addr_port = addr, "443"
        addr_ip, addr_port = addr_ip.strip(), addr_port.strip()

    # ── WORKER (multi-location via Cloudflare Worker) ──
    if proto == "worker":
        wcfgs = _worker_configs(user_id, user, inbound, "", remark_tag, addr_ip, addr_port)
        if wcfgs:
            return wcfgs[0]
        return ""

    # ── NODE selection is now an attribute of Config inbounds. ──

    # ── TELEGRAM PROXY ──
    if proto == "telegram":
        if not inbound:
            return ""
        return generate_telegram_proxy_link(user_id, user, inbound, remark_tag)

    # ── VMESS / TROJAN ──
    if proto in ("vmess", "trojan"):
        if not inbound:
            return ""
        ts=inbound.get("tls_settings") or {}; ws=inbound.get("ws_settings") or {}; xh=inbound.get("xhttp_settings") or {}; gr=inbound.get("grpc_settings") or {}
        host=addr_ip or str(ts.get("domain") or inbound.get("domain") or panel_domain).strip()
        port=addr_port or str(inbound.get("port") or 443)
        sni=str(ts.get("sni") or inbound.get("sni") or host).strip()
        fp=str(inbound.get("fingerprint") or ts.get("fingerprint") or "chrome")
        transport=str(inbound.get("network") or "ws").lower()
        if proto=="trojan":
            if transport=="grpc":
                service=str(gr.get("serviceName") or "").strip()
                if not service: return ""
                params=f"security=tls&type=grpc&serviceName={quote(service, safe='')}&sni={quote(sni)}&fp={quote(fp)}&alpn=h2"
                if gr.get("authority"): params+=f"&authority={quote(str(gr.get('authority')))}"
            elif transport=="xhttp":
                path=str(xh.get("path") or "/xhttp").strip(); path=path if path.startswith("/") else "/"+path
                params=f"security=tls&type=xhttp&path={quote(path, safe='')}&sni={quote(sni)}&fp={quote(fp)}&alpn=h2,http/1.1"
            elif transport=="tcp":
                params=f"security=tls&type=tcp&sni={quote(sni)}&fp={quote(fp)}&alpn=h2,http/1.1"
            else:
                path=str(ws.get("path") or "/ws").strip(); path=path if path.startswith("/") else "/"+path; path=path.rstrip("/") or "/ws"; path=f"{path}/{config_uuid}"
                wh=str(ts.get("host") or ws.get("host") or host).strip()
                params=f"security=tls&type=ws&sni={quote(sni)}&host={quote(wh)}&path={quote(path, safe='')}&fp={quote(fp)}&alpn=http/1.1"
            return f"trojan://{quote(config_uuid, safe='')}@{host}:{port}?{params}#{remark}"
        # Standard VMess URL uses a base64-encoded JSON object.
        import base64
        obj={"v":"2","ps":urllib.parse.unquote(rem),"add":host,"port":int(port),"id":config_uuid,"aid":0,"scy":"auto","net":transport,"type":"none","host":host,"path":"","tls":"tls","sni":sni,"alpn":"h2,http/1.1","fp":fp}
        if transport=="ws":
            obj["path"]=str(ws.get("path") or "/ws"); obj["host"]=str(ts.get("host") or ws.get("host") or host)
        elif transport=="xhttp":
            obj["path"]=str(xh.get("path") or "/xhttp"); obj["type"]="none"
        elif transport=="grpc":
            obj["path"]=str(gr.get("serviceName") or "grpc"); obj["type"]="gun"
        raw=base64.b64encode(json.dumps(obj,separators=(",",":"),ensure_ascii=False).encode()).decode()
        return f"vmess://{raw}"

    # ── SOCKS5 ──
    if proto == "socks5":
        if not inbound:
            return ""
        host = addr_ip or str(inbound.get("domain") or panel_domain).strip()
        port = addr_port or str(inbound.get("port") or 1080)
        ss = inbound.get("socks_settings") or {}
        su = str(ss.get("username") or username or "spider").strip()
        sp = str(ss.get("password") or config_uuid).strip()
        auth = f"{quote(su, safe='')}:{quote(sp, safe='')}@" if su or sp else ""
        return f"socks5://{auth}{host}:{port}#{remark}"

    if proto == "shadowsocks":
        if not inbound:
            return ""
        host = addr_ip or str(inbound.get("domain") or inbound.get("external_domain") or panel_domain).strip()
        port = addr_port or str(inbound.get("external_port") or inbound.get("port") or 8388)
        ss = inbound.get("socks_settings") or {}
        method = str(ss.get("method") or "2022-blake3-aes-256-gcm").strip()
        password = str(ss.get("password") or config_uuid).strip()
        # Standard ss URI with SIP002-style userinfo.
        import base64
        userinfo = base64.urlsafe_b64encode(f"{method}:{password}".encode()).decode().rstrip("=")
        return f"ss://{userinfo}@{host}:{port}#{remark}"

    # ── REALITY (served by Xray core) ──
    if proto == "reality" or sec == "reality":
        # Not configured yet (admin must fill domain + port) → no config.
        if not inbound:
            return ""
        ext_domain = str(inbound.get("external_domain") or "").strip()
        ext_port = str(inbound.get("external_port") or "").strip()
        if not ext_domain or not ext_port:
            return ""
        rs = inbound.get("reality_settings") or SETTINGS.get("reality") or {}
        gs = SETTINGS.get("reality") or {}
        # Use the private key from inbound, derive public key from it (this is the ONLY
        # public key that works with Xray — Xray derives it from the same private key).
        priv_key = rs.get("private_key") or gs.get("private_key") or ""
        pbk = _xray_x25519_public_key(priv_key) if priv_key else (rs.get("public_key") or gs.get("public_key") or "")
        sid = rs.get("short_id") or rs.get("short_ids") or gs.get("short_id") or ""
        spx = rs.get("spiderx") or gs.get("spiderx") or "/"
        fp = inbound.get("fingerprint") or rs.get("fingerprint") or gs.get("fingerprint") or "chrome"
        sni = str(inbound.get("sni") or rs.get("sni") or gs.get("sni") or "").strip()
        xs = inbound.get("xhttp_settings") or {}
        # Client and server must use the exact same XHTTP path.
        rpath = str(xs.get("path") or "/").strip()
        if not rpath.startswith("/"):
            rpath = "/" + rpath
        host = addr_ip or ext_domain
        port = addr_port or ext_port
        # Reality transport is independent: RAW/TCP, XHTTP, or gRPC only.
        rnet = str(inbound.get("network") or "xhttp").lower()
        if rnet == "tcp":
            params = (f"encryption=none&security=reality&type=tcp"
                      f"&sni={quote(sni)}&fp={quote(str(fp), safe='')}&alpn=h2,http/1.1"
                      f"&pbk={quote(str(pbk), safe='')}&sid={quote(str(sid), safe='')}&spx={quote(str(spx), safe='')}")
        elif rnet == "grpc":
            gs2 = inbound.get("grpc_settings") or {}
            service = str(gs2.get("serviceName") or "").strip()
            authority = str(gs2.get("authority") or "").strip()
            if not service: return ""
            params = (f"encryption=none&security=reality&type=grpc&serviceName={quote(service, safe='')}"
                      f"&sni={quote(sni)}&fp={quote(str(fp), safe='')}&alpn=h2"
                      f"&pbk={quote(str(pbk), safe='')}&sid={quote(str(sid), safe='')}&spx={quote(str(spx), safe='')}")
            if authority: params += f"&authority={quote(authority, safe='')}"
        else:
            xpb = xs.get("xPaddingBytes", "100-1000")
            xmod = str(xs.get("mode") or "stream-up").strip().lower()
            if xmod not in ("packet-up", "stream-up"): xmod = "stream-up"
            xsc = xs.get("scMaxEachPostBytes", "1000000")
            extra = quote(json.dumps({
                "xPaddingBytes": str(xpb),
                "mode": xmod,
                "scMaxEachPostBytes": str(xsc),
                "sessionPlacement": "header",
                "sessionKey": "X-Session",
                "seqPlacement": "header",
                "seqKey": "X-Seq",
            }, separators=(",", ":")), safe='')
            client_sni = str(rs.get("sni") or rs.get("server_name") or sni or "").strip()
            rbase = str(xs.get("path") or "/xhttp").strip()
            if not rbase.startswith("/"): rbase = "/" + rbase
            rpath = rbase.rstrip("/") or "/xhttp"
            # Xray's XHTTP path is the exact server path; UUID is appended by the relay-compatible adapter.
            rpath_q = quote(rpath, safe="")
            params = (f"mode={quote(xmod, safe='')}&path={rpath_q}"
                      f"&security=reality&encryption=none&extra={extra}"
                      f"&pbk={quote(str(pbk), safe='')}&fp={quote(str(fp), safe='')}"
                      f"&spx={quote(str(spx), safe='')}&type=xhttp&sni={quote(client_sni, safe='')}&sid={quote(str(sid), safe='')}")
        return f"vless://{config_uuid}@{host}:{port}?{params}#{remark}"

    # ── TLS (WS default / XHTTP selectable) — served by the FastAPI relay ──
    tls_cfg = (inbound.get("tls_settings") if inbound else {}) or {}
    tls_domain = str(tls_cfg.get("domain") or (inbound.get("domain") if inbound else "") or panel_domain).strip()
    tls_sni = str(tls_cfg.get("sni") or (inbound.get("sni") if inbound else "") or tls_domain).strip()
    tls_host = str(tls_cfg.get("host") or ((inbound.get("ws_settings") or {}).get("host") if inbound else "") or tls_domain).strip()
    tls_port = str((inbound.get("port") if inbound else "") or 443)

    # ── REVERSE (user → CF Worker → Railway → site) — path /reverse/{uuid} ──
    if proto == "reverse":
        wdom = _worker_safe_domain(WORKER.get("worker_domain"))
        if not wdom or not WORKER.get("connected"):
            return ""
        rpath = f"/reverse/{config_uuid}"
        params = ("encryption=none&security=tls&type=ws"
                  f"&host={quote(wdom)}&path={quote(rpath, safe='')}&sni={quote(wdom)}"
                  "&fp=chrome&alpn=http/1.1")
        return f"vless://{config_uuid}@{wdom}:443?{params}#{quote(f'Spider-{username} Reverse')}"

    # ── TUNNEL (user → Railway → CF Worker → site) — path /tunnel/{uuid} ──
    if proto == "tunnel":
        wdom = _worker_safe_domain(WORKER.get("worker_domain"))
        if not wdom or not WORKER.get("connected"):
            return ""
        # Reverse switch ON: user → Worker → Railway → site. The config is
        # addressed to the WORKER domain with path /reverse/{uuid} and the
        # user's record lives in REVERSE_KV.
        if inbound and inbound.get("reverse_enabled"):
            rpath = f"/reverse/{config_uuid}"
            params = ("encryption=none&security=tls&type=ws"
                      f"&host={quote(wdom)}&path={quote(rpath, safe='')}&sni={quote(wdom)}"
                      "&fp=chrome&alpn=http/1.1")
            rev_rem = quote(f"Spider-{username} Reverse".strip())
            return f"vless://{config_uuid}@{wdom}:443?{params}#{rev_rem}"
        # Plain tunnel: user → Railway → Worker → site (path /tunnel/{uuid},
        # addressed to the panel/Railway domain).
        tpath = f"/tunnel/{config_uuid}"
        params = ("encryption=none&security=tls&type=ws"
                  f"&host={quote(panel_domain)}&path={quote(tpath, safe='')}&sni={quote(panel_domain)}"
                  "&fp=chrome&alpn=http/1.1")
        tun_rem = quote(f"Spider-{username} Tunnel".strip())
        return f"vless://{config_uuid}@{panel_domain}:443?{params}#{tun_rem}"

    host = addr_ip or tls_domain
    port = addr_port or tls_port
    # Transport: user's choice first, then the inbound's network, default ws.
    transport = ((inbound.get("network") if inbound else None) or (user.get("transport_type") or "") or "ws").lower()
    if transport not in ("ws", "xhttp", "grpc", "tcp"):
        transport = "ws"

    # Sni spoofing for v2box: add snispoofing JSON param when enabled.
    # Applies to TLS WS and Worker configs (handled in _worker_configs).
    # Does NOT apply to Reality/XHTTP Reality.
    sni_spoof = bool(user.get("sni_spoof_v2box"))
    if sni_spoof:
        fake_sni = str(user.get("fake_sni") or inbound.get("sni") if inbound else "") or "www.hcaptcha.com"
        spoof_ip = str(user.get("spoof_ip") or inbound.get("spoof_ip") if inbound else "") or "8.6.112.4"
        spoof = {"active": True, "fakeSni": fake_sni, "spoofIp": spoof_ip, "targetPort": 443}
        spoof_q = quote(json.dumps(spoof, separators=(",", ":")), safe="")

    if transport == "grpc":
        gs = inbound.get("grpc_settings") if inbound else {}
        service = str(gs.get("serviceName") or "").strip()
        authority = str(gs.get("authority") or "").strip()
        if not service:
            return ""
        params = (f"encryption=none&security=tls&type=grpc&serviceName={quote(service, safe='')}"
                  f"&sni={quote(tls_sni)}&fp={quote(str(inbound.get('fingerprint') if inbound else 'chrome') or 'chrome')}&alpn=h2")
        if authority:
            params += f"&authority={quote(authority)}"
        return f"vless://{config_uuid}@{host}:{port}?{params}#{remark}"

    if transport == "xhttp":
        xs = inbound.get("xhttp_settings") if inbound else {}
        xpb = xs.get("xPaddingBytes", "100-1000")
        xmode = str(xs.get("mode", "stream-up")).strip().lower()
        if xmode not in ("packet-up", "stream-up"):
            xmode = "stream-up"
        xsc = xs.get("scMaxEachPostBytes", "1000000")
        xbase = str(xs.get("path") or "/xhttp").strip()
        if not xbase.startswith("/"): xbase = "/" + xbase
        xbase = xbase.rstrip("/") or "/xhttp"
        xpath = f"{xbase}/{config_uuid}"
        xextra_obj = {
            "xPaddingBytes": str(xpb),
            "mode": xmode,
            "scMaxEachPostBytes": str(xsc),
            "sessionPlacement": "header",
            "sessionKey": "X-Session",
            "seqPlacement": "header",
            "seqKey": "X-Seq",
        }
        extra = quote(json.dumps(xextra_obj, separators=(",", ":")), safe='')
        params = (f"encryption=none&security=tls&type=xhttp"
                  f"&host={quote(tls_host)}&path={quote(xpath, safe="")}&sni={quote(tls_sni)}"
                  f"&fp=chrome&alpn=h2,http/1.1&mode={xmode}&extra={extra}")
        if sni_spoof:
            params += f"&snispoofing={spoof_q}"
    else:  # ws (default)
        ws_base = str(((inbound.get("ws_settings") or {}).get("path") if inbound else "") or "/ws").strip()
        if not ws_base.startswith("/"): ws_base = "/" + ws_base
        ws_base = ws_base.rstrip("/") or "/ws"
        ws_path = ws_base if ws_base.endswith("/{uuid}") else f"{ws_base}/{config_uuid}"
        params = (f"encryption=none&security=tls&type=ws"
                  f"&host={quote(tls_host)}&path={quote(ws_path, safe='')}&sni={quote(tls_sni)}"
                  f"&fp={quote(str(inbound.get('fingerprint') if inbound else 'chrome') or 'chrome')}&alpn=http/1.1")
        if sni_spoof:
            params += f"&snispoofing={spoof_q}"
    return f"vless://{config_uuid}@{host}:{port}?{params}#{remark}"


def generate_custom_ip_configs(user_id: str, user: dict) -> dict:
    """Build extra configs from scanned IPs — ONLY for VLESS/WS/Worker inbounds.

    WireGuard and Telegram inbounds are SKIPPED because:
    - WireGuard requires pre-shared keys and specific endpoint routing
    - Telegram proxy works on its own dedicated port, not on CF/Railway IPs

    Per-inbound rules:
    - worker inbound → scanned Cloudflare IPs (host/sni stay on worker domain)
    - tls (ws/xhttp) inbound → scanned Railway IPs (host/sni stay on panel domain)
    - wireguard/telegram → SKIPPED (not compatible with scanned IPs)
    - reality → SKIPPED (can't swap address)

    Returns {"railway": [...], "cf": [...]}
    """
    cii = user.get("custom_ip_inbounds") or {}
    cf_ids = [str(x) for x in (cii.get("cf") or [])]
    rw_ids = [str(x) for x in (cii.get("railway") or [])]
    out = {"railway": [], "cf": []}

    # Cloudflare scanned IPs → worker inbounds only
    cf_ips = _read_scanned_ips("cf")
    if cf_ips:
        for iid_ in cf_ids:
            ib = INBOUNDS.get(iid_)
            if not ib:
                continue
            # Skip wireguard and telegram — not compatible with scanned IPs
            if (ib.get("protocol") or "").lower() in ("wireguard", "telegram"):
                continue
            for i, ip in enumerate(cf_ips[:10], 1):
                try:
                    cfg = generate_user_config(user_id, user, iid_, addr=ip, remark_tag=f"Cloudflare{i}")
                except Exception as e:
                    logger.warning(f"cf custom-ip config gen failed for {ip}: {e}")
                    continue
                if cfg:
                    out["cf"].append(cfg)

    # Railway scanned IPs → TLS inbounds only
    rw_ips = _read_scanned_ips("railway")
    if rw_ips:
        for iid_ in rw_ids:
            ib = INBOUNDS.get(iid_)
            if not ib:
                continue
            # Skip wireguard and telegram — not compatible with scanned IPs
            if (ib.get("protocol") or "").lower() in ("wireguard", "telegram"):
                continue
            if not ib:
                continue
            for i, ip in enumerate(rw_ips[:10], 1):
                try:
                    cfg = generate_user_config(user_id, user, iid_, addr=ip, remark_tag=f"Railway{i}")
                except Exception as e:
                    logger.warning(f"railway custom-ip config gen failed for {ip}: {e}")
                    continue
                if cfg:
                    out["railway"].append(cfg)
    return out


def generate_status_config(user: dict, configs: list) -> str:
    """Generate a status config (config-status) with fake random stats.

    This config is placed FIRST in the subscription so clients display it as
    the status/overview config. It uses the panel's main domain and carries
    fake volume/time/user-count in the remark for easy reading.

    The address is the panel domain (not external_domain) and host/sni are
    also the panel domain so TLS handshake reaches the panel.
    """
    import random

    # Get user info
    username = user.get("username", "user")
    user_id = user.get("user_id", "")
    config_uuid = user.get("config_uuid", "") or user_id

    # Use panel domain from SETTINGS (required for TLS WS/XHTTP)
    if SETTINGS.get("domain"):
        panel_domain = SETTINGS["domain"]
    else:
        panel_domain = _safe_host(SETTINGS.get("domain"), get_host())

    # Generate fake stats for the status config
    # Random volume: 100GB - 500GB total, 10GB - 100GB used
    total_gb = random.randint(100, 500)
    used_gb = random.randint(10, min(100, total_gb - 1))

    # Random expiry: 30-365 days
    expire_days = random.randint(30, 365)

    # Random concurrent users: 1-10
    online_users = random.randint(1, 10)

    # Build remark with fake stats (status config identifier)
    # Format: "📊 Status | User: {username} | Used: {used}GB/{total}GB | Days: {days} | Online: {online}"
    remark_text = f"📊 Status | User: {username} | Used: {used_gb}GB/{total_gb}GB | Days: {expire_days} | Online: {online_users}"
    remark = quote(remark_text)

    # Try to find a TLS WS/XHTTP config to copy transport from
    transport = "ws"
    ws_path = f"/ws/{config_uuid}"
    params = (f"encryption=none&security=tls&type=ws"
              f"&host={quote(panel_domain)}&path={quote(ws_path, safe='')}&sni={quote(panel_domain)}"
              f"&fp=chrome&alpn=http/1.1")

    for c in configs:
        if c and "type=ws" in c:
            transport = "ws"
            # Already set ws_path and params above; break if desired
            break
        elif c and "type=xhttp" in c:
            transport = "xhttp"
            # Build xhttp parameters using settings from user's inbound
            inbound_ids = user.get("inbound_ids") or []
            xpb = "100-1000"
            xsc = "1000000"
            xmode = "stream-up"
            for iid_ in inbound_ids:
                ib = INBOUNDS.get(iid_)
                if ib:
                    _p = (ib.get("protocol") or "").lower()
                    _s = (ib.get("security") or "").lower()
                    if _p != "reality" and _s != "reality" and _p != "worker":
                        xs = ib.get("xhttp_settings") or {}
                        xpb = xs.get("xPaddingBytes", "100-1000")
                        xmode = str(xs.get("mode", "auto")).strip().lower()
                        if xmode not in ("packet-up", "stream-up"):
                            xmode = "stream-up"
                        xsc = xs.get("scMaxEachPostBytes", "1000000")
                        break
            extra = quote(json.dumps({'xPaddingBytes': str(xpb), 'mode': xmode, 'scMaxEachPostBytes': str(xsc), 'sessionPlacement': 'header', 'sessionKey': 'X-Session', 'seqPlacement': 'header', 'seqKey': 'X-Seq'}, separators=(",", ":")), safe='')
            xbase = str((next((INBOUNDS.get(i) for i in inbound_ids if INBOUNDS.get(i)), {}) or {}).get("xhttp_settings", {}).get("path") or "/xhttp").strip()
            if not xbase.startswith("/"): xbase = "/" + xbase
            xbase = xbase.rstrip("/") or "/xhttp"
            ws_path = f"{xbase}/{config_uuid}"
            params = (f"encryption=none&security=tls&type=xhttp"
                      f"&host={quote(panel_domain)}&path={quote(ws_path, safe='')}&sni={quote(panel_domain)}"
                      f"&fp=chrome&mode={xmode}&extra={extra}")
            break

    # Address is panel domain, port 443
    host = panel_domain
    port = "443"

    return f"vless://{config_uuid}@{host}:{port}?{params}#{remark}"


def generate_sni_spoof_configs(user_id: str, user: dict) -> list:
    """Build Sni Spoof for v2box configs.

    When user has sni_spoof_v2box enabled, generates TLS WS and Worker configs
    with the snispoofing JSON parameter. Does NOT apply to Reality/XHTTP Reality configs.

    Uses user's fake_sni and spoof_ip from spoof tab settings, or falls back
    to inbound defaults.

    Returns list of configs with snispoofing parameter.
    """
    if not user.get("sni_spoof_v2box"):
        return []

    # Get spoof settings from user (set via spoof tab)
    fake_sni = str(user.get("fake_sni") or "").strip() or "www.hcaptcha.com"
    spoof_ip = str(user.get("spoof_ip") or "").strip() or "8.6.112.4"
    spoof = {"active": True, "fakeSni": fake_sni, "spoofIp": spoof_ip, "targetPort": 443}
    spoof_q = quote(json.dumps(spoof, separators=(",", ":")), safe="")

    cfg_uuid = user.get("config_uuid", "")
    uname = user.get("username", user_id)
    out = []

    # Get user's inbound_ids
    inbound_ids = user.get("inbound_ids") or []
    for iid_ in inbound_ids:
        ib = INBOUNDS.get(iid_)
        if not ib:
            continue
        proto = (ib.get("protocol") or "").lower()
        sec = (ib.get("security") or "").lower()

        # Skip Reality inbounds - they don't use snispoofing
        if proto == "reality" or sec == "reality":
            continue

        if proto == "worker":
            # Worker inbound - use worker domain
            wdomain = str(WORKER.get("worker_domain") or "").strip().lower()
            if not wdomain or wdomain in ("localhost", "0.0.0.0", "127.0.0.1"):
                continue
            wport = ib.get("external_port") or ib.get("port") or 443

            # Selected countries
            wcounts = user.get("proxy_countries") or ([user.get("proxy_country")] if user.get("proxy_country") else [])
            wcounts = [str(c).strip().lower() for c in wcounts if str(c).strip()]
            chosen = [(c, (WORKER.get("proxies") or {}).get(c)) for c in wcounts if (WORKER.get("proxies") or {}).get(c)]
            if not chosen:
                chosen = [("", {"country": ""})]

            for code, p in chosen:
                flag = _code_to_flag(code) if code else ""
                clabel = str(p.get("country") or (code.upper() if code else "Worker"))
                rem = quote(f"Spider-{uname} {flag} {clabel} SniSpoof".strip() if flag else f"Spider-{uname} Worker SniSpoof")

                wpath = f"/route/{code}/{cfg_uuid}" if code else "/"
                params = "&".join([
                    f"snispoofing={spoof_q}",
                    "security=tls",
                    "fp=chrome",
                    "allowInsecure=0",
                    f"host={quote(wdomain)}",
                    f"path={quote(wpath, safe='')}",
                    f"sni={quote(wdomain)}",
                    "insecure=0",
                    "encryption=none",
                    "type=ws",
                ])
                out.append(f"vless://{cfg_uuid}@{wdomain}:{wport}?{params}#{rem}")
        else:
            # TLS WS/XHTTP inbound - use panel domain
            panel_domain = _safe_host(SETTINGS.get("domain"), get_host())
            transport = (user.get("transport_type") or "").lower() or (ib.get("network") if ib else "") or "ws"
            transport = transport.lower()
            if transport not in ("ws", "xhttp"):
                transport = "ws"

            if transport == "xhttp":
                # XHTTP with snispoofing
                xs = ib.get("xhttp_settings") or {}
                xpb = xs.get("xPaddingBytes", "100-1000")
                xmode = str(xs.get("mode", "auto")).strip().lower()
                if xmode not in ("packet-up", "stream-up"):
                    xmode = "stream-up"
                xsc = xs.get("scMaxEachPostBytes", "1000000")
                extra = quote(json.dumps({'xPaddingBytes': str(xpb), 'mode': xmode, 'scMaxEachPostBytes': str(xsc), 'sessionPlacement': 'header', 'sessionKey': 'X-Session', 'seqPlacement': 'header', 'seqKey': 'X-Seq'}, separators=(",", ":")), safe='')
                xbase = str(xs.get("path") or "/xhttp").strip()
                if not xbase.startswith("/"): xbase = "/" + xbase
                xbase = xbase.rstrip("/") or "/xhttp"
                xpath = f"{xbase}/{cfg_uuid}"
                params = (f"encryption=none&security=tls&type=xhttp"
                          f"&host={quote(panel_domain)}&path={quote(xpath, safe='')}&sni={quote(panel_domain)}"
                          f"&fp=chrome&alpn=h2,http/1.1&mode={xmode}&extra={extra}"
                          f"&snispoofing={spoof_q}")
            else:
                # WS with snispoofing
                ws_path = f"/ws/{cfg_uuid}"
                params = (f"encryption=none&security=tls&type=ws"
                          f"&host={quote(panel_domain)}&path={quote(ws_path, safe='')}&sni={quote(panel_domain)}"
                          f"&fp=chrome&alpn=http/1.1"
                          f"&snispoofing={spoof_q}")
            rem = quote(f"Spider-{uname} SniSpoof")
            out.append(f"vless://{cfg_uuid}@{panel_domain}:443?{params}#{rem}")

    return out


def _worker_configs(user_id: str, user: dict, inbound: dict, stored_path: str, base_remark: str, addr_ip: str = None, addr_port: str = None) -> list:
    """Build one VLESS config per selected country for a worker inbound.

    Config structure:
      - address = addr_ip (Clean IP) OR worker domain
      - host/sni = worker domain (always, for TLS handshake + SNI routing)
      - path = /route/{code} (always, for country-based proxy selection)
      - snispoofing = BPB SNI spoofing params (for v2box compatibility)
    """
    wdomain = str(WORKER.get("worker_domain") or "").strip().lower()
    if not wdomain or wdomain in ("localhost", "0.0.0.0", "127.0.0.1"):
        return []
    wport = (inbound.get("external_port") if inbound else None) or (inbound.get("port") if inbound else None) or 443

    # SNI spoofing for v2box: use user settings when enabled, fallback to inbound defaults
    sni_spoof = bool(user.get("sni_spoof_v2box"))
    if sni_spoof:
        fake_sni = str(user.get("fake_sni") or (inbound.get("sni") if inbound else "") or "www.hcaptcha.com").strip()
        spoof_ip = str(user.get("spoof_ip") or (inbound.get("spoof_ip") if inbound else "") or "8.6.112.4").strip()
        spoof = {"active": True, "fakeSni": fake_sni, "spoofIp": spoof_ip, "targetPort": 443}
    else:
        fake_sni = str((inbound.get("sni") if inbound else "") or "www.hcaptcha.com").strip()
        spoof_ip = str((inbound.get("spoof_ip") if inbound else "") or "8.6.112.4").strip()
        spoof = {"active": True, "fakeSni": fake_sni, "spoofIp": spoof_ip, "targetPort": 0}
    spoof_q = quote(json.dumps(spoof, separators=(",", ":")), safe="")

    cfg_uuid = user.get("config_uuid", "")
    uname = user.get("username", user_id)

    # Selected countries (multi-location); fall back to a single generic route.
    wcounts = user.get("proxy_countries") or ([user.get("proxy_country")] if user.get("proxy_country") else [])
    wcounts = [str(c).strip().lower() for c in wcounts if str(c).strip()]
    chosen = [(c, (WORKER.get("proxies") or {}).get(c)) for c in wcounts if (WORKER.get("proxies") or {}).get(c)]
    # A requested country that is not present in the Worker proxy map must not
    # silently fall back to the generic /{uuid} route; that would ignore the
    # user's country selection. Return no config so the panel can surface the
    # missing country/proxy mapping instead.
    if wcounts and not chosen:
        return []
    if not wcounts:
        chosen = [("", {"country": ""})]

    # Address: Clean IP when provided, otherwise worker domain
    address = addr_ip if addr_ip else wdomain
    port = addr_port if addr_port else wport

    configs = []
    for code, p in chosen:
        flag = _code_to_flag(code) if code else ""
        clabel = str(p.get("country") or (code.upper() if code else "Worker"))
        rem = quote(f"Spider-{uname} {flag} {clabel}".strip() if flag else f"Spider-{uname} Worker")
        wpath = f"/route/{code}/{cfg_uuid}" if code else f"/{cfg_uuid}"

        params = {
            "encryption": "none",
            "security": "tls",
            "sni": wdomain,
            "host": wdomain,
            "fp": "chrome",
            "type": "ws",
            "path": quote(wpath, safe=''),
            "snispoofing": spoof_q,
        }
        query = "&".join([f"{k}={v}" for k, v in params.items()])
        configs.append(f"vless://{cfg_uuid}@{address}:{port}?{query}#{rem}")

    return configs


# ── Default link ──────────────────────────────────────────────────────────────
_default_link_created = False

async def ensure_default_link():
    global _default_link_created
    if _default_link_created:
        return
    async with LINKS_LOCK:
        if not any(l.get("is_default") for l in LINKS.values()):
            uid = hashlib.sha256(f"default{CONFIG['secret']}".encode()).hexdigest()
            uid = f"{uid[:8]}-{uid[8:12]}-{uid[12:16]}-{uid[16:20]}-{uid[20:32]}"
            if uid not in LINKS:
                LINKS[uid] = {
                    "label": "لینک پیش‌فرض",
                    "limit_bytes": 0,
                    "used_bytes": 0,
                    "created_at": datetime.now().isoformat(),
                    "active": True,
                    "expires_at": None,
                    "note": "",
                    "is_default": True,
                    "sub_id": None,
                    "protocol": DEFAULT_PROTOCOL,
                }
                asyncio.create_task(save_state())
        _default_link_created = True

# ── Basic endpoints ───────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def root():
    # Open the panel directly when the Railway/custom domain is visited.
    return RedirectResponse(url="/spider", status_code=307)

# ── Subscription ping (must be before /sub/{{identifier}}) ──────────────────
@app.get("/sub/{identifier}/ping")
async def sub_ping_handler(identifier: str):
    """Ping endpoint for subscription page — returns a simple response."""
    # Check user first
    async with USERS_LOCK:
        for u in USERS.values():
            if u.get("username") == identifier and u.get("status") == "active":
                return {"ok": True, "ping": "pong", "username": identifier}
    # Fallback: check if it's a link
    async with LINKS_LOCK:
        link = LINKS.get(identifier)
    if link and is_link_allowed(link):
        return {"ok": True, "ping": "pong", "uuid": identifier}
    raise HTTPException(status_code=404, detail="User not found")


# ── Subscription (single link / user sub page) ──────────────────────────────
@app.get("/link/{uuid}", response_class=HTMLResponse)
async def graphical_link_subscription(uuid: str, request: Request):
    """Graphical subscription page for a user's config UUID.

    v7 canonical URL: /link/{uuid}. The old /sub/{identifier} endpoint remains
    available for backward compatibility.
    """
    if not _is_valid_uuid(uuid):
        raise HTTPException(status_code=404, detail="Invalid subscription UUID")
    async with USERS_LOCK:
        user = next((dict(u, user_id=uid) for uid, u in USERS.items() if u.get("config_uuid") == uuid), None)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    # sub.html is the graphical subscription client; it now receives UUID, not username.
    return FileResponse(str(_STATIC_DIR / "sub.html"))

@app.get("/api/link/{uuid}")
async def graphical_link_data(uuid: str):
    """Public subscription data lookup by config UUID for v7 /link/{uuid}."""
    if not _is_valid_uuid(uuid):
        raise HTTPException(status_code=404, detail="Invalid subscription UUID")
    async with USERS_LOCK:
        found = next(((uid, dict(u)) for uid,u in USERS.items() if u.get("config_uuid")==uuid), None)
    if not found:
        raise HTTPException(status_code=404, detail="User not found")
    uid, user = found
    # Reuse the same data builder used by the legacy username endpoint by locating the user.
    return await api_user_sub(user.get("username", uid))

@app.get("/api/link/{uuid}/qr")
async def graphical_link_qr(uuid: str):
    """QR for the canonical v7 graphical subscription URL."""
    if not _is_valid_uuid(uuid):
        raise HTTPException(status_code=404, detail="Invalid subscription UUID")
    async with USERS_LOCK:
        user = next((dict(u, user_id=uid) for uid,u in USERS.items() if u.get("config_uuid")==uuid), None)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    import qrcode
    host = SETTINGS.get("domain") or get_host()
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(f"https://{host}/link/{uuid}")
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return Response(content=buf.getvalue(), media_type="image/png")

@app.get("/sub/{identifier}")
async def subscription_handler(identifier: str, request: Request):
    """Smart handler: accepts UUID (config_uuid) → returns all user configs as base64.
    Also accepts username → serves the HTML subscription page for admin preview."""
    import base64

    # 1) UUID format → subscription for V2Box/clients: return ALL configs for the user
    if _is_valid_uuid(identifier):
        async with USERS_LOCK:
            target_user = None
            target_uid = None
            for uid, u in USERS.items():
                if u.get("config_uuid") == identifier:
                    target_user = u
                    target_uid = uid
                    break
        if target_user:
            if _user_uses_worker_inbound(target_user):
                target_user = await _worker_pull_user(target_uid, target_user)
                USERS[target_uid] = target_user
            host = SETTINGS.get("domain") or get_host()
            username = target_user.get("username", target_uid)

            # Build ALL configs for this user. Worker publishes its own standalone
            # configs; use those exact configs when available.
            configs = list(target_user.get("worker_configs") or [])
            inbound_ids = target_user.get("inbound_ids") or []
            stored_path_user = (target_user.get("path") or "").strip()
            for iid_ in inbound_ids:
                ib = INBOUNDS.get(iid_)
                try:
                    _p = (ib.get("protocol") if ib else "").lower()
                    _s = (ib.get("security") if ib else "").lower()
                    if ib and (_p == "reality" or _s == "reality"):
                        if not str(ib.get("external_domain") or "").strip() or not str(ib.get("external_port") or "").strip():
                            continue
                    if ib and _p == "worker":
                        if not target_user.get("worker_configs"):
                            configs.extend(_worker_configs(target_uid, target_user, ib, stored_path_user, f"Spider-{username}"))
                    else:
                        cfg = generate_user_config(target_uid, target_user, iid_)
                        if cfg:
                            configs.append(cfg)
                except Exception:
                    continue
            if not configs:
                fallback_config = generate_user_config(target_uid, target_user, target_user.get("inbound_id"))
                configs = [fallback_config] if fallback_config else []

            # Custom scanned-IP configs
            custom_cfgs = generate_custom_ip_configs(target_uid, target_user)
            for cfg in custom_cfgs.get("railway", []) + custom_cfgs.get("cf", []):
                configs.append(cfg)

            # Sni Spoof configs
            if target_user.get("sni_spoof_v2box"):
                configs.extend(generate_sni_spoof_configs(target_uid, target_user))

            if not configs:
                raise HTTPException(status_code=404, detail="no configs found")

            # Status config as first entry
            status_config = generate_status_config(target_user, configs)
            all_configs = [status_config] + configs if status_config else configs

            content = base64.b64encode("\n".join(all_configs).encode()).decode()
            used_b = int(target_user.get("traffic_used_bytes") or 0)
            total_b = int(target_user.get("traffic_limit_bytes") or 0)
            exp_ts = 0
            if target_user.get("expire_at"):
                try: exp_ts = int(datetime.fromisoformat(target_user["expire_at"]).timestamp())
                except Exception: exp_ts = 0
            info = f"upload=0; download={used_b}; total={total_b}; expire={exp_ts}"
            return Response(content=content, media_type="text/plain",
                            headers={"subscription-userinfo": info,
                                      "profile-title": quote(username),
                                      "profile-update-interval": "12",
                                      "support-url": "https://t.me/spider_vpn1"})

        # Fallback: check LINKS (legacy link UUID)
        async with LINKS_LOCK:
            link = LINKS.get(identifier)
        if link and is_link_allowed(link):
            host = SETTINGS.get("domain") or get_host()
            proto = link.get("protocol", DEFAULT_PROTOCOL)
            vless = generate_vless_link(identifier, host, remark=f"Spider-{link['label']}", protocol=proto)
            content = base64.b64encode(vless.encode()).decode()
            return Response(content=content, media_type="text/plain",
                            headers={"profile-title": quote(link["label"]), "support-url": "https://t.me/spider_vpn1"})

        raise HTTPException(status_code=404, detail="not found")

    # 2) Username format → serve the HTML subscription page (admin preview)
    async with USERS_LOCK:
        for uid, u in USERS.items():
            if u.get("username") == identifier:
                return FileResponse(_os.path.join(_STATIC_DIR, "sub.html"))

    raise HTTPException(status_code=404, detail="not found")

@app.get("/sub-all")
async def subscription_all(_=Depends(require_auth)):
    import base64
    host = SETTINGS.get("domain") or get_host()
    async with LINKS_LOCK:
        lines = [
            generate_vless_link(uid, host, remark=f"Spider-{d['label']}", protocol=d.get("protocol", DEFAULT_PROTOCOL))
            for uid, d in LINKS.items()
            if is_link_allowed(d)
        ]
    content = base64.b64encode("\n".join(lines).encode()).decode()
    return Response(content=content, media_type="text/plain")

# ══════════════════════════════════════════════════════════════════════════════
# SUB GROUP endpoints
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/subs")
async def create_sub(request: Request, _=Depends(require_auth)):
    body = await request.json()
    name = (body.get("name") or "گروه جدید").strip()[:60]
    desc = (body.get("desc") or body.get("description") or "").strip()[:200]
    password = (body.get("password") or "").strip()
    sub_id = generate_uuid()
    uuid_key = secrets.token_urlsafe(16)
    async with SUBS_LOCK:
        SUBS[sub_id] = {
            "name": name,
            "desc": desc,
            "password_hash": hash_password(password) if password else None,
            "uuid_key": uuid_key,
            "created_at": datetime.now().isoformat(),
            "link_ids": [],
        }
    asyncio.create_task(save_state())
    log_activity("sub", f"گروه «{name}» ساخته شد", "ok")
    host = SETTINGS.get("domain") or get_host()
    return {
        "sub_id": sub_id,
        **SUBS[sub_id],
        "public_url": f"https://{host}/p/{uuid_key}",
        "sub_url": f"https://{host}/sub-group/{uuid_key}",
    }

@app.get("/api/subs")
async def list_subs(_=Depends(require_auth)):
    host = SETTINGS.get("domain") or get_host()
    async with SUBS_LOCK:
        snap_subs = dict(SUBS)
    async with LINKS_LOCK:
        snap_links = dict(LINKS)
    result = []
    for sid, s in snap_subs.items():
        link_ids = s.get("link_ids", [])
        active_count = sum(1 for lid in link_ids if is_link_allowed(snap_links.get(lid)))
        total_used = sum(snap_links[lid].get("used_bytes", 0) for lid in link_ids if lid in snap_links)
        result.append({
            "sub_id": sid,
            **s,
            "password_hash": None,
            "has_password": s.get("password_hash") is not None,
            "links_count": len(link_ids),
            "active_count": active_count,
            "total_used_bytes": total_used,
            "total_used_fmt": fmt_bytes(total_used),
            "public_url": f"https://{host}/p/{s['uuid_key']}",
            "sub_url": f"https://{host}/sub-group/{s['uuid_key']}",
        })
    result.sort(key=lambda x: x["created_at"], reverse=True)
    return {"subs": result}

@app.patch("/api/subs/{sub_id}")
async def update_sub(sub_id: str, request: Request, _=Depends(require_auth)):
    body = await request.json()
    async with SUBS_LOCK:
        if sub_id not in SUBS:
            raise HTTPException(status_code=404, detail="sub not found")
        s = SUBS[sub_id]
        if "name" in body:
            s["name"] = str(body["name"])[:60]
        if "desc" in body:
            s["desc"] = str(body["desc"])[:200]
        if "password" in body:
            pw = str(body["password"]).strip()
            s["password_hash"] = hash_password(pw) if pw else None
        if "link_ids" in body:
            s["link_ids"] = list(body["link_ids"])
    asyncio.create_task(save_state())
    return {"ok": True}

@app.delete("/api/subs/{sub_id}")
async def delete_sub(sub_id: str, _=Depends(require_auth)):
    async with SUBS_LOCK:
        if sub_id not in SUBS:
            raise HTTPException(status_code=404, detail="sub not found")
        name = SUBS[sub_id].get("name", sub_id)
        del SUBS[sub_id]
    async with LINKS_LOCK:
        for link in LINKS.values():
            if link.get("sub_id") == sub_id:
                link["sub_id"] = None
    asyncio.create_task(save_state())
    log_activity("sub", f"گروه «{name}» حذف شد", "warn")
    return {"ok": True, "deleted": sub_id}

@app.post("/api/subs/{sub_id}/links")
async def assign_link_to_sub(sub_id: str, request: Request, _=Depends(require_auth)):
    body = await request.json()
    link_id = str(body.get("link_id", ""))
    action = str(body.get("action", "add"))
    async with SUBS_LOCK:
        if sub_id not in SUBS:
            raise HTTPException(status_code=404, detail="sub not found")
        s = SUBS[sub_id]
        ids = s.setdefault("link_ids", [])
        if action == "add":
            if link_id not in ids:
                ids.append(link_id)
        else:
            if link_id in ids:
                ids.remove(link_id)
    async with LINKS_LOCK:
        if link_id in LINKS:
            LINKS[link_id]["sub_id"] = sub_id if action == "add" else None
    asyncio.create_task(save_state())
    return {"ok": True}

# ── Public sub-group subscription file ───────────────────────────────────────
@app.get("/sub-group/{uuid_key}")
async def sub_group_subscription(uuid_key: str, request: Request):
    import base64
    async with SUBS_LOCK:
        sub = next((s for s in SUBS.values() if s.get("uuid_key") == uuid_key), None)
    if not sub:
        raise HTTPException(status_code=404, detail="not found")

    if sub.get("password_hash"):
        pw = request.query_params.get("pw", "")
        if hash_password(pw) != sub["password_hash"]:
            raise HTTPException(status_code=403, detail="wrong password")

    host = SETTINGS.get("domain") or get_host()
    link_ids = sub.get("link_ids", [])
    async with LINKS_LOCK:
        lines = []
        for lid in link_ids:
            link = LINKS.get(lid)
            if link and is_link_allowed(link):
                lines.append(generate_vless_link(lid, host, remark=f"Spider-{link['label']}", protocol=link.get("protocol", DEFAULT_PROTOCOL)))

    content = base64.b64encode("\n".join(lines).encode()).decode()
    return Response(
        content=content,
        media_type="text/plain",
        headers={
            "profile-title": quote(sub["name"]),
            "support-url": "https://t.me/spider_vpn1",
            "profile-update-interval": "12",
        }
    )

# ── Auth endpoints ────────────────────────────────────────────────────────────
@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    ip = client_ip(request)
    if hash_password(str(body.get("password", ""))) != AUTH["password_hash"]:
        log_activity("auth", f"تلاش ورود ناموفق از {ip}", "err")
        raise HTTPException(status_code=401, detail="رمز عبور اشتباه است")
    token = await create_session()
    log_activity("auth", f"ورود موفق به پنل از {ip}", "ok")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="lax", path="/")
    return resp

@app.post("/api/logout")
async def api_logout(request: Request):
    await destroy_session(request.cookies.get(SESSION_COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp

@app.get("/api/me")
async def api_me(request: Request):
    return {"authenticated": await is_valid_session(request.cookies.get(SESSION_COOKIE))}

@app.post("/api/change-password")
async def api_change_password(request: Request, token=Depends(require_auth)):
    body = await request.json()
    if hash_password(str(body.get("current_password", ""))) != AUTH["password_hash"]:
        raise HTTPException(status_code=400, detail="رمز فعلی اشتباه است")
    new = str(body.get("new_password", ""))
    if len(new) < 4:
        raise HTTPException(status_code=400, detail="رمز جدید باید حداقل ۴ کاراکتر باشد")
    AUTH["password_hash"] = hash_password(new)
    async with SESSIONS_LOCK:
        SESSIONS.clear()
        SESSIONS[token] = time.time() + SESSION_TTL
    await save_state()
    log_activity("auth", "رمز عبور پنل تغییر کرد", "ok")
    return {"ok": True}

# ── Stats ─────────────────────────────────────────────────────────────────────
@app.get("/stats")
async def get_stats(_=Depends(require_auth)):
    async with LINKS_LOCK:
        snap = dict(LINKS)
    async with USERS_LOCK:
        snap_users = dict(USERS)
    async with SUBS_LOCK:
        snap_subs = dict(SUBS)

    # Auto-check user expiry
    for user in snap_users.values():
        auto_check_user_expiry(user)

    # Count active users
    active_users = sum(1 for u in snap_users.values() if u.get("status") == "active")
    total_users = len(snap_users)

    # Traffic across all links
    total_bytes = stats["total_bytes"]
    traffic_usage_gb = round(total_bytes / (1024 ** 3), 3)

    # Connection-based health simulation
    conn_count = len(connections)
    if conn_count > 400:
        server_status = "down"
    elif conn_count > 200:
        server_status = "degraded"
    else:
        server_status = "healthy"

    # Simulated system metrics
    cpu_percent = round(min(conn_count * 0.3 + 5, 95), 1)
    ram_percent = round(min(45 + (total_users * 0.5) + (conn_count * 0.1), 95), 1)
    disk_percent = round(min(25 + (len(snap) * 0.02) + (total_users * 0.1), 90), 1)
    uptime_secs = max(time.time() - stats["start_time"], 1)
    network_mbps = round(total_bytes / uptime_secs * 8 / 1000000, 2)

    return {
        "active_connections": len(connections),
        "total_traffic_mb": round(stats["total_bytes"] / (1024 ** 2), 2),
        "total_requests": stats["total_requests"],
        "total_errors": stats["total_errors"],
        "uptime": uptime(),
        "timestamp": datetime.now().isoformat(),
        "hourly": dict(hourly_traffic),
        "recent_errors": list(error_logs)[-10:],
        "links_count": len(snap),
        "active_links": sum(1 for l in snap.values() if is_link_allowed(l)),
        "expired_links": sum(1 for l in snap.values() if is_link_expired(l)),
        "subs_count": len(SUBS),
        # Enhanced stats
        "active_users": active_users,
        "total_configs": len(snap),
        "total_users": total_users,
        "traffic_usage_gb": traffic_usage_gb,
        "server_status": server_status,
        "cpu_percent": cpu_percent,
        "ram_percent": ram_percent,
        "disk_percent": disk_percent,
        "network_mbps": network_mbps,
        "recent_activity": list(activity_logs)[-10:],
    }

# ── Activity Logs ─────────────────────────────────────────────────────────────
@app.get("/api/activity")
async def get_activity(_=Depends(require_auth)):
    return {"logs": list(activity_logs)[-150:]}

# ── Live connections (with IP) ────────────────────────────────────────────────
@app.get("/api/connections")
async def get_connections(_=Depends(require_auth)):
    """
    خروجی این endpoint حالا بر اساس IP گروه‌بندی شده:
    هر آی‌پی فقط یک آیتم نمایش داده می‌شود، با جمع بایت‌های تمام سشن‌های
    باز روی همان آی‌پی و تعداد سشن‌های فعال آن آی‌پی.
    raw_count همچنان تعداد واقعی اتصالات باز (سشن‌های خام، مثلاً ۴۰ تا
    اتصال هم‌زمان یک موبایل) را برمی‌گرداند.
    """
    async with LINKS_LOCK:
        snap = dict(LINKS)

    grouped: dict[str, dict] = {}
    for conn_id, c in connections.items():
        ip = c.get("ip", "نامشخص")
        link = snap.get(c.get("uuid"))
        label = link.get("label") if link else "نامشخص"
        g = grouped.get(ip)
        if g is None:
            g = {
                "ip": ip,
                "sessions": 0,
                "bytes": 0,
                "labels": set(),
                "transports": set(),
                "first_connected_at": c.get("connected_at"),
                "last_connected_at": c.get("connected_at"),
            }
            grouped[ip] = g
        g["sessions"] += 1
        g["bytes"] += c.get("bytes", 0)
        g["labels"].add(label)
        g["transports"].add(c.get("transport", "vless-ws"))
        ca = c.get("connected_at")
        if ca:
            if not g["first_connected_at"] or ca < g["first_connected_at"]:
                g["first_connected_at"] = ca
            if not g["last_connected_at"] or ca > g["last_connected_at"]:
                g["last_connected_at"] = ca

    result = []
    for ip, g in grouped.items():
        result.append({
            "ip": ip,
            "sessions": g["sessions"],
            "labels": sorted(g["labels"]),
            "label": " · ".join(sorted(g["labels"])) if g["labels"] else "نامشخص",
            "transports": sorted(g["transports"]),
            "bytes": g["bytes"],
            "bytes_fmt": fmt_bytes(g["bytes"]),
            "connected_at": g["first_connected_at"],
            "last_connected_at": g["last_connected_at"],
        })
    result.sort(key=lambda x: x.get("last_connected_at") or "", reverse=True)

    return {
        "connections": result,
        "count": len(result),          # تعداد آی‌پی‌های یکتا
        "raw_count": len(connections), # تعداد کل اتصالات باز (بدون گروه‌بندی)
    }

# ── Link Management ───────────────────────────────────────────────────────────
@app.post("/api/links")
async def create_link(request: Request, _=Depends(require_auth)):
    body = await request.json()
    label = (body.get("label") or "لینک جدید").strip()[:60]
    lv = float(body.get("limit_value") or 0)
    lu = body.get("limit_unit") or "GB"
    limit_bytes = 0 if lv <= 0 else parse_size_to_bytes(lv, lu)
    exp_days = int(body.get("expires_days") or 0)
    expires_at = (datetime.now() + timedelta(days=exp_days)).isoformat() if exp_days > 0 else None
    note = (body.get("note") or "").strip()[:200]
    sub_id = body.get("sub_id") or None
    protocol = body.get("protocol") or DEFAULT_PROTOCOL
    if protocol not in PROTOCOLS:
        protocol = DEFAULT_PROTOCOL

    uid = generate_uuid()
    async with LINKS_LOCK:
        LINKS[uid] = {
            "label": label,
            "limit_bytes": limit_bytes,
            "used_bytes": 0,
            "created_at": datetime.now().isoformat(),
            "active": True,
            "expires_at": expires_at,
            "note": note,
            "is_default": False,
            "sub_id": sub_id,
            "protocol": protocol,
        }

    if sub_id:
        async with SUBS_LOCK:
            if sub_id in SUBS:
                ids = SUBS[sub_id].setdefault("link_ids", [])
                if uid not in ids:
                    ids.append(uid)

    asyncio.create_task(save_state())
    log_activity("link", f"کانفیگ «{label}» ساخته شد", "ok")
    host = SETTINGS.get("domain") or get_host()
    return {
        "uuid": uid,
        **LINKS[uid],
        "expired": False,
        "vless_link": generate_vless_link(uid, host, remark=f"Spider-{label}", protocol=protocol),
        "sub_url": f"https://{host}/sub/{uid}",
    }

@app.get("/api/links")
async def list_links(_=Depends(require_auth)):
    host = SETTINGS.get("domain") or get_host()
    async with LINKS_LOCK:
        snap = dict(LINKS)
    result = []
    for uid, d in snap.items():
        proto = d.get("protocol", DEFAULT_PROTOCOL)
        result.append({
            "uuid": uid,
            **d,
            "protocol": proto,
            "expired": is_link_expired(d),
            "vless_link": generate_vless_link(uid, host, remark=f"Spider-{d['label']}", protocol=proto),
            "sub_url": f"https://{host}/sub/{uid}",
        })
    result.sort(key=lambda x: x["created_at"], reverse=True)
    return {"links": result}

@app.patch("/api/links/{uid}")
async def update_link(uid: str, request: Request, _=Depends(require_auth)):
    body = await request.json()
    async with LINKS_LOCK:
        if uid not in LINKS:
            raise HTTPException(status_code=404, detail="link not found")
        link = LINKS[uid]
        old_sub = link.get("sub_id")
        label = link.get("label")
        if "active" in body:
            link["active"] = bool(body["active"])
            log_activity("link", f"کانفیگ «{label}» {'فعال' if link['active'] else 'غیرفعال'} شد", "ok" if link["active"] else "warn")
        if "label" in body:
            link["label"] = str(body["label"])[:60]
        if "note" in body:
            link["note"] = str(body["note"])[:200]
        if "reset_usage" in body and body["reset_usage"]:
            link["used_bytes"] = 0
            log_activity("link", f"مصرف کانفیگ «{label}» ریست شد", "info")
        if "limit_value" in body:
            lv = float(body.get("limit_value") or 0)
            lu = body.get("limit_unit") or "GB"
            link["limit_bytes"] = 0 if lv <= 0 else parse_size_to_bytes(lv, lu)
        if "expires_days" in body:
            ed = int(body["expires_days"] or 0)
            link["expires_at"] = (datetime.now() + timedelta(days=ed)).isoformat() if ed > 0 else None
        if any(k in body for k in ("label", "note", "limit_value", "expires_days")):
            log_activity("link", f"کانفیگ «{link['label']}» ویرایش شد", "info")
        new_sub = body.get("sub_id", "UNCHANGED")
        if new_sub != "UNCHANGED":
            link["sub_id"] = new_sub or None

    if new_sub != "UNCHANGED":
        async with SUBS_LOCK:
            if old_sub and old_sub in SUBS:
                ids = SUBS[old_sub].get("link_ids", [])
                if uid in ids:
                    ids.remove(uid)
            if new_sub and new_sub in SUBS:
                ids = SUBS[new_sub].setdefault("link_ids", [])
                if uid not in ids:
                    ids.append(uid)

    asyncio.create_task(save_state())
    return {"ok": True}

@app.delete("/api/links/{uid}")
async def delete_link(uid: str, _=Depends(require_auth)):
    async with LINKS_LOCK:
        if uid not in LINKS:
            raise HTTPException(status_code=404, detail="link not found")
        label = LINKS[uid].get("label", uid)
        sub_id = LINKS[uid].get("sub_id")
        del LINKS[uid]
    if sub_id:
        async with SUBS_LOCK:
            if sub_id in SUBS:
                ids = SUBS[sub_id].get("link_ids", [])
                if uid in ids:
                    ids.remove(uid)
    asyncio.create_task(save_state())
    log_activity("link", f"کانفیگ «{label}» حذف شد", "err")
    return {"ok": True, "deleted": uid}

# ══════════════════════════════════════════════════════════════════════════════
# VLESS Relay — optional module
# ══════════════════════════════════════════════════════════════════════════════

from relay_vless import (
    RELAY_BUF,
    parse_vless_header,
    check_and_use,
    relay_ws_to_tcp,
    relay_tcp_to_ws,
    websocket_tunnel,
)

# WebSocket route: /ws/{uuid} — config_uuid IS the path.
# Registered directly (like the RVG reference) so it is never swallowed by a
# try/except — this is the only route serving WS TLS configs.
@app.websocket("/ws/{uuid}")
async def ws_uuid_handler(ws: WebSocket, uuid: str):
    # /ws/live is registered later — handle it here since param route matches first
    if uuid == "live":
        await websocket_live_stats(ws)
        return
    await websocket_tunnel(ws, uuid)


# Tunnel path: /tunnel/{uuid} — user → Railway (here) → Cloudflare Worker → site.
# Railway accepts the client's TLS+WS, then relays raw VLESS bytes to the Worker
# over an outbound WSS connection to /{uuid} on the worker domain. The Worker
# authenticates the user from its TUNNEL_KV and connects out to the target.
@app.websocket("/tunnel/{uuid}")
async def tunnel_ws_handler(ws: WebSocket, uuid: str):
    wdom = _worker_safe_domain(WORKER.get("worker_domain"))
    if not wdom or not WORKER.get("connected"):
        await ws.close(code=1014, reason="worker not connected")
        return
    await _tunnel_relay(ws, uuid, wdom)


# Reverse chain leg on Railway: user → Worker → HERE (Railway) → site.
# The client connects to the Worker domain with /reverse/{uuid}; the Worker
# relays the raw VLESS stream over WSS to this endpoint, which forwards it to
# the real destination through the normal proxy_connect pipeline.
def _find_user_by_config_uuid(config_uuid: str):
    target = str(config_uuid or "").strip().lower()
    if not target:
        return None
    for uid, user in USERS.items():
        if str(user.get("config_uuid") or uid).strip().lower() == target:
            out = dict(user)
            out["user_id"] = uid
            return out
    return None


@app.websocket("/reverse/{uuid}")
async def reverse_ws_handler(ws: WebSocket, uuid: str):
    user = _find_user_by_config_uuid(uuid)
    if not is_user_allowed(user):
        await ws.accept()
        await ws.close(code=1008, reason="not authorized")
        return
    await websocket_user_tunnel(ws, uuid, user)


async def websocket_user_tunnel(ws: WebSocket, uuid: str, user: dict):
    """Reverse Worker→Railway leg authenticated against the panel USERS store."""
    await ws.accept()
    conn_id = secrets.token_urlsafe(6)
    ip = ws.headers.get("x-forwarded-for", "").split(",")[0].strip() or (ws.client.host if ws.client else "?")
    connections[conn_id] = {
        "uuid": uuid,
        "ip": ip,
        "transport": "reverse-ws",
        "connected_at": datetime.now().isoformat(),
        "bytes": 0,
    }
    writer = None
    try:
        if not await enforce_ip_limit_for_link(uuid, ip):
            await ws.close(code=1008, reason="ip limit reached")
            return
        first_msg = await asyncio.wait_for(ws.receive(), timeout=20.0)
        if first_msg.get("type") == "websocket.disconnect":
            return
        first_chunk = first_msg.get("bytes") or (first_msg.get("text") or "").encode()
        if not first_chunk:
            return
        command, address, port, payload = await parse_vless_header(first_chunk)
        if not is_user_allowed(user):
            await ws.close(code=1008, reason="quota/disabled")
            return
        reader, writer = await proxy_connect(uuid, address, port)
        if payload:
            writer.write(payload)
            await writer.drain()
        done, pending = await asyncio.wait(
            {
                asyncio.create_task(relay_ws_to_tcp(ws, writer, conn_id, uuid)),
                asyncio.create_task(relay_tcp_to_ws(ws, reader, conn_id, uuid)),
            },
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
    except WebSocketDisconnect:
        pass
    except asyncio.TimeoutError:
        stats["total_errors"] += 1
        error_logs.append({"error": "reverse connection timeout", "time": datetime.now().isoformat()})
    except Exception as exc:
        stats["total_errors"] += 1
        error_logs.append({"error": str(exc), "time": datetime.now().isoformat()})
        logger.warning(f"reverse user relay [{conn_id}] error: {exc}")
    finally:
        await release_ip_for_link(uuid, ip)
        if writer:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        connections.pop(conn_id, None)


async def _tunnel_relay(ws: WebSocket, uuid: str, worker_domain: str):
    """Bridge panel-WS ⇄ worker-WSS bidirectionally."""
    import websockets as _websockets
    await ws.accept()
    user = _find_user_by_config_uuid(uuid)
    if not is_user_allowed(user):
        await ws.close(code=1008, reason="not authorized")
        return
    ip = ws.headers.get("x-forwarded-for", "").split(",")[0].strip() or (ws.client.host if ws.client else "?")
    conn_id = secrets.token_urlsafe(6)
    connections[conn_id] = {"uuid": uuid, "ip": ip, "transport": "tunnel-ws", "connected_at": datetime.now().isoformat(), "bytes": 0}
    log_activity("connection", f"Tunnel اتصال جدید از {ip}", "info")
    worker_ws = None
    try:
        wss_url = f"wss://{worker_domain}/tunnel/{uuid}"
        headers = {"User-Agent": "Spider-Tunnel"}
        worker_ws = await asyncio.wait_for(
            _websockets.connect(wss_url, extra_headers=headers, max_size=None), timeout=10.0)

        async def panel_to_worker():
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                data = msg.get("bytes") or (msg.get("text") or "").encode()
                if data:
                    connections[conn_id]["bytes"] += len(data)
                    stats["total_bytes"] += len(data)
                    await worker_ws.send(data)

        async def worker_to_panel():
            async for data in worker_ws:
                if isinstance(data, str):
                    data = data.encode()
                await ws.send_bytes(data)

        done, pending = await asyncio.wait(
            {asyncio.create_task(panel_to_worker()), asyncio.create_task(worker_to_panel())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        stats["total_errors"] += 1
        logger.warning(f"tunnel relay [{conn_id}] error: {exc}")
    finally:
        if worker_ws:
            try: await worker_ws.close()
            except Exception: pass
        connections.pop(conn_id, None)

logger.info("VLESS Relay module loaded (WS: /ws/{uuid}, tunnel: /tunnel/{uuid})")

# ══════════════════════════════════════════════════════════════════════════════
# ── HTTP Proxy ────────────────────────────────────────────────────────────────
_HOP = {"connection","keep-alive","proxy-authenticate","proxy-authorization",
        "te","trailers","transfer-encoding","upgrade","content-encoding","content-length"}

@app.api_route("/proxy/{target_url:path}", methods=["GET","POST","PUT","DELETE","PATCH","HEAD","OPTIONS"])
async def http_proxy(target_url: str, request: Request):
    if not target_url.startswith("http"):
        target_url = "https://" + target_url
    try:
        body = await request.body()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP and k.lower() != "host"}
        resp = await http_client.request(method=request.method, url=target_url, headers=headers, content=body)
        stats["total_bytes"] += len(resp.content)
        stats["total_requests"] += 1
        hourly_traffic[now_ir().strftime("%H:00")] += len(resp.content)
        return Response(content=resp.content, status_code=resp.status_code,
                        headers={k: v for k, v in resp.headers.items() if k.lower() not in _HOP})
    except Exception as exc:
        stats["total_errors"] += 1
        error_logs.append({"error": str(exc), "url": target_url, "time": datetime.now().isoformat()})
        raise HTTPException(status_code=502, detail=f"Proxy error: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# INBOUNDS MANAGEMENT endpoints
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/inbounds")
async def list_inbounds(_=Depends(require_auth)):
    """List all inbounds."""
    async with INBOUNDS_LOCK:
        snap = dict(INBOUNDS)
    result = []
    for iid, ib in snap.items():
        legacy_proto = str(ib.get("protocol") or "").lower()
        if legacy_proto in ("wireguard", "socks5"):
            continue
        row = {"inbound_id": iid, **ib}
        row.setdefault("inbound_type", "telegram" if legacy_proto == "telegram" else "config")
        row["users_count"] = sum(1 for u in USERS.values() if iid in (u.get("inbound_ids") or ([u.get("inbound_id")] if u.get("inbound_id") else [])))
        result.append(row)
    result.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {"inbounds": result}



def _normalize_transport_path(value: object, field: str, *, allow_root: bool = False) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail=f"{field} is required")
    if not raw.startswith("/"):
        raise HTTPException(status_code=400, detail=f"{field} must start with /")
    if any(ch in raw for ch in "?#\\"):
        raise HTTPException(status_code=400, detail=f"{field} contains invalid characters")
    raw = "/" + "/".join(part for part in raw.split("/") if part)
    if allow_root and raw == "//":
        raw = "/"
    if raw != "/" and raw.endswith("/"):
        raw = raw.rstrip("/")
    if not allow_root and raw == "/":
        raise HTTPException(status_code=400, detail=f"{field} cannot be /")
    return raw or "/"


def _canonical_inbound_transport(body: dict, protocol: str, security: str) -> tuple[str, dict, dict, dict]:
    protocol = str(protocol or "").lower()
    network = str(body.get("network") or "ws").strip().lower()
    if protocol in ("socks5", "shadowsocks", "wireguard", "telegram", "node", "worker", "tunnel", "reverse"):
        return network, {}, {}, {}
    if security == "reality" and protocol != "vless":
        raise HTTPException(status_code=400, detail="Reality security is supported only for VLESS")
    if network not in ("tcp", "ws", "xhttp", "grpc"):
        raise HTTPException(status_code=400, detail="Unsupported transport")
    ws = dict(body.get("ws_settings") or {}) if isinstance(body.get("ws_settings"), dict) else {}
    xh = dict(body.get("xhttp_settings") or {}) if isinstance(body.get("xhttp_settings"), dict) else {}
    gr = dict(body.get("grpc_settings") or {}) if isinstance(body.get("grpc_settings"), dict) else {}
    if security == "tls":
        if network == "ws":
            path = _normalize_transport_path(ws.get("path"), "WebSocket Path", allow_root=True)
            host = str(ws.get("host") or body.get("tls_settings", {}).get("host") or "").strip()
            if not host:
                raise HTTPException(status_code=400, detail="WebSocket Host is required")
            ws = {"path": path, "host": host}
            xh, gr = {}, {}
        elif network == "xhttp":
            path = _normalize_transport_path(xh.get("path"), "XHTTP Path", allow_root=False)
            mode = str(xh.get("mode") or "stream-up").strip().lower()
            if mode not in ("packet-up", "stream-up"):
                raise HTTPException(status_code=400, detail="XHTTP Mode must be packet-up or stream-up")
            host = str(xh.get("host") or body.get("tls_settings", {}).get("host") or "").strip()
            if not host:
                raise HTTPException(status_code=400, detail="XHTTP Host is required")
            xh = {"path": path, "mode": mode, "host": host,
                  "xPaddingBytes": str(xh.get("xPaddingBytes") or "100-1000"),
                  "scMaxEachPostBytes": str(xh.get("scMaxEachPostBytes") or "1000000")}
            ws, gr = {}, {}
        elif network == "grpc":
            service = str(gr.get("serviceName") or "").strip()
            if not service:
                raise HTTPException(status_code=400, detail="gRPC Service Name is required")
            if "/" in service or any(ch.isspace() for ch in service):
                raise HTTPException(status_code=400, detail="gRPC Service Name contains invalid characters")
            authority = str(gr.get("authority") or "").strip()
            gr = {"serviceName": service, "authority": authority}
            ws, xh = {}, {}
        else:
            ws, xh, gr = {}, {}, {}
    else:
        if network == "ws":
            ws = {"path": _normalize_transport_path(ws.get("path"), "WebSocket Path", allow_root=True),
                  "host": str(ws.get("host") or "").strip()}
        elif network == "xhttp":
            xh = {"path": _normalize_transport_path(xh.get("path"), "XHTTP Path", allow_root=False),
                  "mode": str(xh.get("mode") or "stream-up").strip().lower()}
            if xh["mode"] not in ("packet-up", "stream-up"):
                raise HTTPException(status_code=400, detail="XHTTP Mode must be packet-up or stream-up")
        elif network == "grpc":
            service = str(gr.get("serviceName") or "").strip()
            if not service:
                raise HTTPException(status_code=400, detail="gRPC Service Name is required")
            gr = {"serviceName": service, "authority": str(gr.get("authority") or "").strip()}
        if network != "ws": ws = {}
        if network != "xhttp": xh = {}
        if network != "grpc": gr = {}
    return network, ws, xh, gr


@app.post("/api/inbounds")
async def create_inbound(request: Request, _=Depends(require_auth)):
    """Create a new inbound."""
    body = await request.json()
    name = str(body.get("name") or "").strip()[:60]
    protocol = str(body.get("protocol") or "vless").lower()
    if protocol not in ("vless", "vmess", "trojan", "shadowsocks", "worker", "tunnel", "reverse", "telegram", "node"):
        raise HTTPException(status_code=400, detail="Invalid protocol")
    network = str(body.get("network") or "ws").lower()
    security = str(body.get("security") or "tls").lower()
    inbound_type = str(body.get("inbound_type") or ("telegram" if protocol == "telegram" else "config")).lower()
    if inbound_type not in ("config", "telegram", "node"):
        raise HTTPException(status_code=400, detail="Invalid inbound type")
    if not name:
        # Blank inbound names are accepted and replaced with a unique random name.
        name = generate_inbound_name(INBOUNDS)
    if inbound_type == "telegram" and protocol != "telegram":
        protocol = "telegram"
    if inbound_type == "node" and protocol != "node":
        protocol = "node"
    if inbound_type == "config" and protocol in ("telegram", "node"):
        raise HTTPException(status_code=400, detail="Telegram Proxy must use Telegram inbound type")
    if protocol == "vless" and security == "reality":
        pass
    elif protocol in ("vmess", "trojan") and security != "tls":
        raise HTTPException(status_code=400, detail="VMess/Trojan inbounds require TLS in this panel")
    elif protocol == "shadowsocks":
        security = "none"
    domain = str(body.get("domain") or "").strip()
    external_domain = str(body.get("external_domain") or "").strip()
    sni = str(body.get("sni") or "").strip()
    destination = str(body.get("destination") or "").strip()
    server_name = str(body.get("server_name") or "").strip()
    # A Node inbound is a selector-only object: no listener/protocol fields.
    if inbound_type == "node":
        requested = [str(x) for x in (body.get("enabled_node_ids") or [])]
        valid = [nid for nid in requested if nid in NODES]
        if not valid:
            raise HTTPException(status_code=400, detail="Select at least one node / حداقل یک نود را انتخاب کنید")
        inbound_id = generate_short_id()
        async with INBOUNDS_LOCK:
            if any(ib.get("name") == name for ib in INBOUNDS.values()):
                raise HTTPException(status_code=409, detail="Inbound name already exists")
            INBOUNDS[inbound_id] = {
                "name": name, "protocol": "node", "inbound_type": "node",
                "enabled_node_ids": valid, "created_at": datetime.now().isoformat()
            }
        await save_state()
        log_activity("inbound", f"اینباند نود «{name}» ساخته شد / Node inbound created", "ok")
        return {"ok": True, "inbound_id": inbound_id, **INBOUNDS[inbound_id]}
    # A "worker" inbound is a special type: it is addressed to the deployed
    # Cloudflare Worker domain; Railway only controls it and is not in the
    # client traffic path.
    if protocol == "worker":
        wdom = _worker_safe_domain(WORKER.get("worker_domain"))
        if not wdom:
            raise HTTPException(status_code=400, detail="Worker هنوز متصل نیست — ابتدا Worker را در تب Worker متصل کنید")
        network = "ws"
        security = "tls"
        domain = wdom
        external_domain = wdom
        sni = wdom

    # Telegram uses the explicit internal listener from telegram_settings.
    # WireGuard is UDP-based, so it also needs a real listen port.
    if protocol == "telegram":
        port = _parse_port(body.get("port"), 44344, "internal_port")
        external_port = _parse_port(body.get("external_port"), 443, "external_port")
    elif protocol == "wireguard":
        port = _parse_port(body.get("port"), 51820, "listen_port")
        external_port = 0
    else:
        port = _parse_port(body.get("port"), 443, "port")
        external_port = _parse_port(body.get("external_port"), 443, "external_port")

    fingerprint = str(body.get("fingerprint") or "chrome").strip()
    spoof_ip = str(body.get("spoof_ip") or "").strip()
    reality_settings = body.get("reality_settings", {}) if isinstance(body.get("reality_settings"), dict) else {}
    xhttp_settings = body.get("xhttp_settings", {}) if isinstance(body.get("xhttp_settings"), dict) else {}
    ws_settings = body.get("ws_settings", {}) if isinstance(body.get("ws_settings"), dict) else {}
    grpc_settings = body.get("grpc_settings", {}) if isinstance(body.get("grpc_settings"), dict) else {}
    wireguard_settings = body.get("wireguard_settings", {}) if isinstance(body.get("wireguard_settings"), dict) else {}
    telegram_settings = body.get("telegram_settings", {}) if isinstance(body.get("telegram_settings"), dict) else {}
    tls_settings = body.get("tls_settings", {}) if isinstance(body.get("tls_settings"), dict) else {}
    socks_settings = body.get("socks_settings", {}) if isinstance(body.get("socks_settings"), dict) else {}
    if protocol == "wireguard":
        wireguard_settings = dict(wireguard_settings)
        wireguard_settings["listen_port"] = port
        if not wireguard_settings.get("endpoint"):
            endpoints = _wg_read_endpoints()
            if endpoints:
                wireguard_settings["endpoint"] = endpoints[0]
        # Persist a server keypair so custom WireGuard configs do not silently
        # change their peer key every time a user config is regenerated.
        if not wireguard_settings.get("server_private_key") or not wireguard_settings.get("server_public_key"):
            wg_priv, wg_pub = _wg_gen_keypair()
            if wg_priv and wg_pub:
                wireguard_settings["server_private_key"] = wg_priv
                wireguard_settings["server_public_key"] = wg_pub
        if not wireguard_settings.get("warp_mode"):
            wireguard_settings["warp_mode"] = False
    if protocol == "socks5":
        network = "tcp"
        security = "none"
        port = _parse_port(body.get("port"), 1080, "port")
        socks_settings = {
            "username": str(socks_settings.get("username") or "").strip(),
            "password": str(socks_settings.get("password") or "").strip(),
        }
    if protocol == "shadowsocks":
        network = "tcp"
        security = "none"
        port = _parse_port(body.get("port"), 8388, "port")
        ss_method = str(body.get("method") or "2022-blake3-aes-256-gcm").strip()
        ss_password = str(body.get("password") or "").strip()
        if not ss_password:
            ss_password = secrets.token_urlsafe(24)
        socks_settings = {"method": ss_method, "password": ss_password}

    if protocol == "telegram":
        # Telegram Proxy does not use Xray Reality fields.
        sni = ""
        destination = ""
        server_name = ""
        telegram_settings = {
            "internal_port": int(telegram_settings.get("internal_port") or port or 44344),
            "external_port": int(telegram_settings.get("external_port") or external_port or 443),
            "external_domain": str(telegram_settings.get("external_domain") or external_domain or "").strip(),
            "promotion_tag": str(telegram_settings.get("promotion_tag") or telegram_settings.get("promotion_channel") or "").strip(),
            "promotion_channel": str(telegram_settings.get("promotion_channel") or "").strip(),
        }
        port = telegram_settings["internal_port"]
        external_port = telegram_settings["external_port"]
        external_domain = telegram_settings["external_domain"]

    if protocol in ("vless", "vmess", "trojan"):
        if security not in (("tls", "reality") if protocol == "vless" else ("tls",)):
            raise HTTPException(status_code=400, detail=("VLESS security must be TLS or Reality" if protocol == "vless" else "VMess/Trojan security must be TLS"))
        if security == "tls":
            tls_domain = str(tls_settings.get("domain") or domain or "").strip()
            tls_sni = str(tls_settings.get("sni") or sni or "").strip()
            tls_host = str(tls_settings.get("host") or "").strip()
            if not tls_domain or not tls_sni:
                raise HTTPException(status_code=400, detail="TLS Domain and SNI are required")
            network, ws_settings, xhttp_settings, grpc_settings = _canonical_inbound_transport(body, protocol, security)
            if network in ("ws", "xhttp") and not tls_host:
                raise HTTPException(status_code=400, detail=f"{network.upper()} Host is required")
            domain = tls_domain
            external_domain = tls_domain
            external_port = ""
            sni = tls_sni
            fingerprint = str(tls_settings.get("fingerprint") or fingerprint or "chrome").strip()
            tls_settings = {"domain": tls_domain, "sni": tls_sni, "host": tls_host,
                             "fingerprint": fingerprint}
            security = "tls"
        else:
            network, ws_settings, xhttp_settings, grpc_settings = _canonical_inbound_transport(body, protocol, security)
    # Reality is a VLESS security layer; keep its own transport settings fully isolated.

    # Auto-generate Reality keys (x25519 pbk/priv + short_id + mldsa65 seed)
    # fresh for every reality inbound. SNI target is fixed.
    if security == "reality":
        if protocol != "vless":
            raise HTTPException(status_code=400, detail="Reality security is supported only for VLESS")
        # Reality is intentionally explicit: do not silently invent the
        # destination, SNI, private key, or short-id for a user-created inbound.
        raw_priv = str(reality_settings.get("private_key") or "").strip()
        raw_sid = str(reality_settings.get("short_id") or reality_settings.get("short_ids") or "").strip()
        raw_dest = str(reality_settings.get("dest") or reality_settings.get("target") or destination or "").strip()
        raw_sni = str(reality_settings.get("sni") or reality_settings.get("server_name") or sni or "").strip()
        if not external_domain:
            external_domain = domain
        if not external_port:
            raise HTTPException(status_code=400, detail="Reality External Port is required")
        if not raw_priv:
            raise HTTPException(status_code=400, detail="Reality Private Key is required")
        if not raw_sid:
            raise HTTPException(status_code=400, detail="Reality Short ID is required")
        if not raw_dest:
            raise HTTPException(status_code=400, detail="Reality Destination / Target is required")
        if not raw_sni:
            raise HTTPException(status_code=400, detail="Reality Server Name / SNI is required")
        reality_settings["private_key"] = raw_priv
        reality_settings["short_id"] = raw_sid.split(",")[0].strip()
        reality_settings["spiderx"] = str(reality_settings.get("spiderx") or "/").strip() or "/"
        reality_settings["dest"] = _normalize_reality_destination(raw_dest)
        reality_settings["target"] = reality_settings["dest"]
        reality_settings["sni"] = _normalize_reality_sni(raw_sni)
        reality_settings["server_name"] = reality_settings["sni"]
        reality_settings["server_names"] = [reality_settings["sni"]]
        reality_settings.setdefault("mldsa65_seed", "")
        reality_settings.setdefault("mldsa65_verify", "")
        derived_pub = _xray_x25519_public_key(raw_priv)
        if derived_pub:
            reality_settings["public_key"] = derived_pub
        # Always derive the public key from the private key when possible so a
        # hand-edited/stale public key can never desynchronize the Reality pair.
        derived_pub = _xray_x25519_public_key(reality_settings.get("private_key", ""))
        if derived_pub:
            reality_settings["public_key"] = derived_pub
        # Reality keeps Destination/Target and Server Name/SNI as independent fields.
        # Accept both the current API names and legacy aliases from older panels.
        dest_value = str(
            reality_settings.get("dest")
            or reality_settings.get("target")
            or destination
            or ""
        ).strip()
        server_name_value = str(
            reality_settings.get("sni")
            or reality_settings.get("server_name")
            or (reality_settings.get("server_names") or [None])[0]
            or server_name
            or sni
        ).strip()
        if not server_name_value:
            raise HTTPException(status_code=400, detail="Reality SNI is required")
        if not dest_value:
            raise HTTPException(status_code=400, detail="Reality Target/Destination is required")
        server_name_value = _normalize_reality_sni(server_name_value)
        dest_value = _normalize_reality_destination(dest_value)
        reality_settings["dest"] = dest_value
        reality_settings["target"] = dest_value
        reality_settings["sni"] = server_name_value
        reality_settings["server_name"] = server_name_value
        reality_settings["server_names"] = [server_name_value]
        # Keep top-level compatibility fields synchronized.
        destination = dest_value
        server_name = server_name_value
        sni = server_name_value
        security = "reality"
        if not external_domain:
            raise HTTPException(status_code=400, detail="Reality External Domain is required")
        if body.get("external_port") in (None, "", 0, "0"):
            raise HTTPException(status_code=400, detail="Reality External Port is required")
        if network not in ("tcp", "xhttp", "grpc"):
            network = "tcp"
    else:
        # For TLS WS/XHTTP (non-reality, non-worker): external_domain and external_port
        # are derived from the panel domain and must not leak Reality/TG-only values.
        if protocol not in ("telegram",):
            external_domain = ""
            external_port = ""
    # WireGuard uses the validated UDP listen port above.

    inbound_id = generate_short_id()
    async with INBOUNDS_LOCK:
        if any(ib.get("name") == name for ib in INBOUNDS.values()):
            raise HTTPException(status_code=409, detail="Inbound name already exists")
        INBOUNDS[inbound_id] = {
            "name": name,
            "protocol": protocol,
            "inbound_type": inbound_type,
            "port": port,
            "network": network,
            "security": security,
            "domain": domain,
            "external_domain": external_domain,
            "sni": sni,
            "destination": destination,
            "server_name": server_name,
            "spoof_ip": spoof_ip,
            "external_port": external_port,
            "fingerprint": fingerprint,
            "reality_settings": reality_settings,
            "xhttp_settings": xhttp_settings,
            "ws_settings": ws_settings,
            "grpc_settings": grpc_settings,
            "enabled_node_ids": [str(x) for x in (body.get("enabled_node_ids") or []) if str(x) in NODES] if inbound_type == "config" else [],
            "wireguard_settings": wireguard_settings,
            "telegram_settings": telegram_settings,
            "tls_settings": tls_settings,
            "socks_settings": socks_settings,
            "created_at": datetime.now().isoformat(),
        }
    if protocol == "reality" and network == "xhttp" and not xhttp_settings.get("path"):
        xhttp_settings["path"] = "/"
        xhttp_settings.setdefault("mode", "stream-up")
        xhttp_settings.setdefault("xPaddingBytes", "100-1000")
        xhttp_settings.setdefault("scMaxEachPostBytes", "1000000")
    await save_state()
    log_activity("inbound", f"اینباند «{name}» با پروتکل {protocol.upper()} ساخته شد", "ok")
    asyncio.create_task(_xray_apply())  # (re)start Xray with the new inbound
    # Start Telegram Proxy if this is a telegram inbound
    if protocol == "telegram":
        asyncio.create_task(_start_telegram_proxy(inbound_id, INBOUNDS[inbound_id]))
    return {"ok": True, "inbound_id": inbound_id, **INBOUNDS[inbound_id]}


@app.patch("/api/inbounds/{inbound_id}")
async def update_inbound(inbound_id: str, request: Request, _=Depends(require_auth)):
    """Update an existing inbound."""
    body = await request.json()
    async with INBOUNDS_LOCK:
        ib = INBOUNDS.get(inbound_id)
        if not ib:
            raise HTTPException(status_code=404, detail="inbound not found")
        if (ib.get("system_managed") or (ib.get("protocol") or "").lower() in ("worker", "tunnel", "reverse")):
            raise HTTPException(status_code=403, detail="اینباند سیستمی است و قابل ویرایش نیست")
        if "name" in body:
            ib["name"] = str(body["name"]).strip()[:60]
        if "inbound_type" in body:
            ib["inbound_type"] = str(body.get("inbound_type") or ("telegram" if str(ib.get("protocol") or "").lower()=="telegram" else "config")).lower()
            if ib["inbound_type"] not in ("config", "telegram", "node"):
                raise HTTPException(status_code=400, detail="Invalid inbound type")
        if "protocol" in body:
            p = str(body["protocol"]).lower()
            if p in ("vless", "vmess", "trojan", "shadowsocks", "telegram", "node"):
                ib["protocol"] = p
            elif p in ("socks5", "wireguard", "reality"):
                raise HTTPException(status_code=400, detail="This protocol is no longer available for new Config inbounds")
        # Node inbound is selector-only: name + checked node IDs, nothing else.
        if (ib.get("protocol") or "").lower() == "node" or (ib.get("inbound_type") or "").lower() == "node":
            ib["protocol"] = "node"
            ib["inbound_type"] = "node"
            if "enabled_node_ids" in body:
                requested = [str(x) for x in (body.get("enabled_node_ids") or [])]
                ib["enabled_node_ids"] = [nid for nid in requested if nid in NODES]
            if not ib.get("enabled_node_ids"):
                raise HTTPException(status_code=400, detail="Select at least one node / حداقل یک نود را انتخاب کنید")
            await save_state()
            return {"ok": True, "inbound_id": inbound_id, **ib}
        if (ib.get("protocol") or "").lower() == "telegram":
            ib["inbound_type"] = "telegram"
        elif (ib.get("inbound_type") or "config").lower() != "telegram":
            ib["inbound_type"] = "config"
        if (ib.get("security") or "").lower() == "reality" and (ib.get("protocol") or "").lower() != "vless":
            raise HTTPException(status_code=400, detail="Reality security is supported only for VLESS")

        # A worker inbound always targets the connected worker domain; if the
        # inbound's domain is stale/empty, refresh it automatically.
        if ib.get("protocol") == "worker":
            wdom = _worker_safe_domain(WORKER.get("worker_domain"))
            if wdom:
                ib["domain"] = wdom
                ib["external_domain"] = wdom
                ib["sni"] = ib.get("sni") or "www.hcaptcha.com"
        if "port" in body:
            default_port = 51820 if (ib.get("protocol") or "").lower() == "wireguard" else (44344 if (ib.get("protocol") or "").lower() == "telegram" else None)
            parsed = _parse_port(body["port"], default_port, "port")
            ib["port"] = parsed if parsed is not None else ""  # "" = unconfigured (reality)
        if "network" in body:
            ib["network"] = str(body["network"]).lower()
        if "security" in body:
            ib["security"] = str(body["security"]).lower()
        # Transport settings are canonicalized together so WS/XHTTP/gRPC never
        # inherit fields from one another during edits.
        if (ib.get("protocol") or "").lower() in ("vless", "vmess", "trojan") and (ib.get("security") or "").lower() in ("tls", "reality"):
            if "network" in body or any(k in body for k in ("ws_settings", "xhttp_settings", "grpc_settings")):
                try:
                    net_body, ws_body, xh_body, gr_body = _canonical_inbound_transport(body, ib.get("protocol"), ib.get("security"))
                    ib["network"] = net_body
                    ib["ws_settings"] = ws_body
                    ib["xhttp_settings"] = xh_body
                    ib["grpc_settings"] = gr_body
                except HTTPException:
                    raise
        if ib.get("protocol") == "socks5":
            ib["network"] = "tcp"
            ib["security"] = "none"
            if "socks_settings" in body and isinstance(body["socks_settings"], dict):
                ib["socks_settings"] = {
                    "username": str(body["socks_settings"].get("username") or "").strip(),
                    "password": str(body["socks_settings"].get("password") or "").strip(),
                }

        # Reality security must always be "reality" + have fresh keys
        if ib.get("protocol") == "reality" or ib.get("security") == "reality":
            ib["security"] = "reality"
            # Auto-generate the full reality key set if missing (x25519 pbk/priv,
            # short_id, mldsa65) so the config always carries a working pbk/sid.
            rs = ib.setdefault("reality_settings", {})
            if not rs.get("public_key") or not rs.get("private_key"):
                fresh = _gen_reality_settings()
                rs.setdefault("private_key", fresh["private_key"])
                rs.setdefault("public_key", fresh["public_key"])
                rs.setdefault("mldsa65_seed", fresh["mldsa65_seed"])
                rs.setdefault("mldsa65_verify", fresh["mldsa65_verify"])
            if not rs.get("short_id"):
                rs["short_id"] = secrets.token_hex(5)[:10]
            rs.setdefault("spiderx", "/")
            # Keep target/destination and server-name/SNI independent.
            legacy_dest = str(rs.get("dest") or rs.get("target") or ib.get("destination") or "").strip()
            legacy_sni = str(rs.get("sni") or rs.get("server_name") or ib.get("server_name") or ib.get("sni") or "").strip()
            if not legacy_sni:
                raise HTTPException(status_code=400, detail="Reality SNI is required")
            if not legacy_dest:
                raise HTTPException(status_code=400, detail="Reality Target/Destination is required")
            legacy_sni = _normalize_reality_sni(legacy_sni)
            legacy_dest = _normalize_reality_destination(legacy_dest)
            rs["dest"] = legacy_dest
            rs["target"] = legacy_dest
            rs["sni"] = legacy_sni
            rs["server_name"] = legacy_sni
            rs["server_names"] = [legacy_sni]
            ib["destination"] = legacy_dest
            ib["server_name"] = legacy_sni
            ib["sni"] = legacy_sni
            if ib.get("network") not in ("tcp", "xhttp", "grpc"):
                ib["network"] = "tcp"
        if "domain" in body:
            ib["domain"] = str(body["domain"]).strip()
        if "external_domain" in body:
            ib["external_domain"] = str(body["external_domain"]).strip()
        if "sni" in body:
            ib["sni"] = str(body["sni"]).strip()
        if "spoof_ip" in body:
            ib["spoof_ip"] = str(body["spoof_ip"]).strip()
        if "external_port" in body:
            _ev = str(body["external_port"] or "").strip()
            ib["external_port"] = int(_ev) if _ev else ""
        if "fingerprint" in body:
            ib["fingerprint"] = str(body["fingerprint"]).strip()
        if "reality_settings" in body and isinstance(body["reality_settings"], dict):
            incoming_rs = dict(body["reality_settings"])
            # Legacy clients used short_ids; canonical field is singular short_id.
            if not incoming_rs.get("short_id") and incoming_rs.get("short_ids"):
                raw_sid = str(incoming_rs.get("short_ids") or "").strip()
                # Xray Reality accepts a list; this panel stores one active short-id.
                incoming_rs["short_id"] = raw_sid.split(",")[0].strip()
            dest_v = str(
                incoming_rs.get("dest")
                or incoming_rs.get("target")
                or body.get("destination")
                or ib.get("destination")
                or ""
            ).strip()
            sni_v = str(
                incoming_rs.get("sni")
                or incoming_rs.get("server_name")
                or (incoming_rs.get("server_names") or [None])[0]
                or body.get("server_name")
                or body.get("sni")
                or ib.get("server_name")
                or ib.get("sni")
            ).strip()
            sni_v = _normalize_reality_sni(sni_v)
            dest_v = _normalize_reality_destination(dest_v or (sni_v + ":443"))
            incoming_rs["dest"] = dest_v
            incoming_rs["target"] = dest_v
            incoming_rs["sni"] = sni_v
            incoming_rs["server_name"] = sni_v
            incoming_rs["server_names"] = [sni_v]
            if not incoming_rs.get("short_id"):
                incoming_rs["short_id"] = secrets.token_hex(5)[:10]
            derived_pub = _xray_x25519_public_key(incoming_rs.get("private_key", ""))
            if derived_pub:
                incoming_rs["public_key"] = derived_pub
            ib["reality_settings"] = incoming_rs
            ib["destination"] = dest_v
            ib["server_name"] = sni_v
            ib["sni"] = sni_v
        if "tls_settings" in body and isinstance(body["tls_settings"], dict):
            ts = body["tls_settings"]
            if (ib.get("protocol") or "").lower() in ("vless", "trojan") and str(ib.get("security") or "").lower() == "tls":
                td = str(ts.get("domain") or ib.get("domain") or "").strip()
                tsni = str(ts.get("sni") or ib.get("sni") or "").strip()
                th = str(ts.get("host") or (ib.get("ws_settings") or {}).get("host") or "").strip()
                tp = _parse_port(ts.get("public_port") or ib.get("external_port") or 443, 443, "public_port")
                if not td or not tsni or not th:
                    raise HTTPException(status_code=400, detail="TLS Domain, SNI and Host are required")
                ib["domain"] = td
                ib["external_domain"] = td
                ib["sni"] = tsni
                ib["external_port"] = tp
                ib["tls_settings"] = {"domain": td, "sni": tsni, "host": th,
                                      "fingerprint": str(ts.get("fingerprint") or ib.get("fingerprint") or "chrome")}
                # Host is meaningful for WS/XHTTP only. gRPC uses serviceName/authority.
                if ib.get("network") == "ws":
                    ib["ws_settings"] = {"path": str((ib.get("ws_settings") or {}).get("path") or "/").strip() or "/", "host": th}
                elif ib.get("network") == "xhttp":
                    xh = dict(ib.get("xhttp_settings") or {})
                    xh["host"] = th
                    ib["xhttp_settings"] = xh

        if "socks_settings" in body and isinstance(body["socks_settings"], dict):
            ib["socks_settings"] = {
                "username": str(body["socks_settings"].get("username") or "").strip(),
                "password": str(body["socks_settings"].get("password") or "").strip(),
            }

        # Do not blindly copy transport dictionaries. They were normalized above
        # and must stay isolated to the selected transport.
        if "wireguard_settings" in body and isinstance(body["wireguard_settings"], dict):
            wg_update = dict(body["wireguard_settings"])
            wg_update["listen_port"] = _parse_port(wg_update.get("listen_port") or body.get("port") or ib.get("port"), 51820, "listen_port")
            if not wg_update.get("server_private_key") or not wg_update.get("server_public_key"):
                existing = ib.get("wireguard_settings") or {}
                wg_update.setdefault("server_private_key", existing.get("server_private_key") or "")
                wg_update.setdefault("server_public_key", existing.get("server_public_key") or "")
            if not wg_update.get("warp_mode") and (not wg_update.get("server_private_key") or not wg_update.get("server_public_key")):
                wg_priv, wg_pub = _wg_gen_keypair()
                if wg_priv and wg_pub:
                    wg_update["server_private_key"] = wg_priv
                    wg_update["server_public_key"] = wg_pub
            ib["wireguard_settings"] = wg_update
            ib["port"] = wg_update["listen_port"]
            ib["external_port"] = 0
        if "telegram_settings" in body and isinstance(body["telegram_settings"], dict):
            ib["telegram_settings"] = body["telegram_settings"]
        if "enabled_node_ids" in body and (ib.get("inbound_type") or "config").lower() == "config":
            requested = [str(x) for x in (body.get("enabled_node_ids") or [])]
            ib["enabled_node_ids"] = [nid for nid in requested if nid in NODES]

        if (ib.get("protocol") or "").lower() == "telegram":
            # Telegram Proxy: inbound settings are authoritative. Railway TCP
            # variables are advisory and must never overwrite user-entered
            # External Domain / External Port / Internal Port.
            ib["sni"] = ""
            ib["destination"] = ""
            ib["server_name"] = ""
            tg = ib.setdefault("telegram_settings", {})
            incoming_tg = body.get("telegram_settings") or {}
            tg["internal_port"] = int(incoming_tg.get("internal_port") or body.get("port") or tg.get("internal_port") or ib.get("port") or 44344)
            tg["external_port"] = int(incoming_tg.get("external_port") or body.get("external_port") or tg.get("external_port") or ib.get("external_port") or 443)
            tg["external_domain"] = str(incoming_tg.get("external_domain") or body.get("external_domain") or tg.get("external_domain") or ib.get("external_domain") or "").strip()
            tg["promotion_tag"] = str(incoming_tg.get("promotion_tag") or tg.get("promotion_tag") or incoming_tg.get("promotion_channel") or "").strip()
            tg["promotion_channel"] = str(incoming_tg.get("promotion_channel") or tg.get("promotion_channel") or "").strip()
            ib.pop("sni", None)
            ib.pop("destination", None)
            ib.pop("server_name", None)
            ib["port"] = tg["internal_port"]
            ib["external_port"] = tg["external_port"]
            ib["external_domain"] = tg["external_domain"]
        if "destination" in body:
            ib["destination"] = str(body["destination"]).strip()
        if "server_name" in body:
            ib["server_name"] = str(body["server_name"]).strip()
        if (ib.get("protocol") or "").lower() == "telegram":
            ib.pop("sni", None)
            ib.pop("destination", None)
            ib.pop("server_name", None)
    if (ib.get("protocol") or "").lower() not in ("telegram", "worker", "reality") and (ib.get("security") or "").lower() != "reality":
        ib["external_domain"] = ""
        ib["external_port"] = ""

    await save_state()
    log_activity("inbound", f"اینباند «{ib.get('name', inbound_id)}» ویرایش شد", "info")
    if (ib.get("inbound_type") or "config").lower() == "config" and (ib.get("protocol") or "").lower() in ("vless","vmess","trojan","shadowsocks"):
        async def _resync_selected_node_users():
            async with USERS_LOCK:
                node_users = [(u.get("config_uuid", uid), u.get("username", uid), dict(u)) for uid, u in USERS.items()
                              if inbound_id in (u.get("inbound_ids") or [])]
            selected = set(str(x) for x in (ib.get("enabled_node_ids") or []))
            for cuuid, uname, _ in node_users:
                await _sync_user_to_selected_nodes(cuuid, {"username": uname}, selected)
        asyncio.create_task(_resync_selected_node_users())
    asyncio.create_task(_xray_apply())
    # Restart Telegram Proxy if this is a telegram inbound
    if (ib.get("protocol") or "").lower() == "telegram":
        asyncio.create_task(_restart_telegram_proxy(inbound_id))
    return {"ok": True}


@app.post("/api/tools/generate-wireguard-keys")
async def generate_wireguard_keys(_=Depends(require_auth)):
    """Generate a persistent-ready WireGuard server keypair for the inbound form."""
    private_key, public_key = _wg_gen_keypair()
    if not private_key or not public_key:
        raise HTTPException(status_code=500, detail="WireGuard key generation is unavailable")
    return {"ok": True, "private_key": private_key, "public_key": public_key}


@app.get("/api/wg/endpoints")
async def list_wg_endpoints(_=Depends(require_auth)):
    """Return available WireGuard endpoints from endpoint.txt."""
    endpoints = _wg_read_endpoints()
    return {"endpoints": endpoints}


@app.post("/api/inbounds/{inbound_id}/generate-reality-keys")
async def generate_inbound_reality_keys(inbound_id: str, _=Depends(require_auth)):
    """Generate Reality x25519 key pair + short_id + spiderx for an inbound."""
    async with INBOUNDS_LOCK:
        ib = INBOUNDS.get(inbound_id)
        if not ib:
            raise HTTPException(status_code=404, detail="inbound not found")
        try:
            rs = ib.setdefault("reality_settings", {})
            rs["private_key"], rs["public_key"] = _xray_x25519_keypair()
            rs["short_id"] = secrets.token_hex(5)[:10]
            rs.setdefault("spiderx", "/")
            ib["security"] = "reality"
            ib["protocol"] = "reality"
            if ib.get("network") not in ("tcp", "xhttp", "grpc"):
                ib["network"] = "tcp"
        except ImportError:
            return {"error": True, "note": "cryptography not installed: pip install cryptography"}
    await save_state()
    return {
        "ok": True,
        "public_key": rs["public_key"],
        "private_key": rs["private_key"],
        "short_id": rs["short_id"],
        "spiderx": rs.get("spiderx", "/"),
    }


@app.post("/api/inbounds/{inbound_id}/generate-short-id")
async def generate_inbound_short_id(inbound_id: str, _=Depends(require_auth)):
    """Generate only a new short_id for a Reality inbound (no key regeneration)."""
    async with INBOUNDS_LOCK:
        ib = INBOUNDS.get(inbound_id)
        if not ib:
            raise HTTPException(status_code=404, detail="inbound not found")
        if ib.get("protocol") != "reality":
            raise HTTPException(status_code=400, detail="inbound is not Reality protocol")
        rs = ib.setdefault("reality_settings", {})
        rs["short_id"] = secrets.token_hex(5)[:10]
    await save_state()
    return {"ok": True, "short_id": rs["short_id"]}


@app.delete("/api/inbounds/{inbound_id}")
async def delete_inbound(inbound_id: str, _=Depends(require_auth)):
    """Delete an inbound."""
    async with INBOUNDS_LOCK:
        ib = INBOUNDS.get(inbound_id)
        if not ib:
            raise HTTPException(status_code=404, detail="inbound not found")
        ib = INBOUNDS.pop(inbound_id, None)
        name = ib.get("name", inbound_id)
    # Stop Telegram Proxy if this was a telegram inbound
    if (ib.get("protocol") or "").lower() == "telegram":
        await _stop_telegram_proxy(inbound_id)
    asyncio.create_task(save_state())
    log_activity("inbound", f"اینباند «{name}» حذف شد", "err")
    return {"ok": True, "deleted": inbound_id}


# ══════════════════════════════════════════════════════════════════════════════
# USER MANAGEMENT endpoints
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/users")
async def list_users(_=Depends(require_auth)):
    """List all users with traffic stats and status."""
    host = SETTINGS.get("domain") or get_host()
    async with USERS_LOCK:
        snap = dict(USERS)

    result = []
    for uid, u in snap.items():
        auto_check_user_expiry(u)
        protocol = u.get("protocol", "vless")
        result.append({
            "user_id": uid,
            "username": u.get("username"),
            "protocol": protocol,
            "transport_type": u.get("transport_type", "ws"),
            "path": u.get("path", ""),
            "proxy_ip": u.get("proxy_ip", ""),
            "proxy_country": u.get("proxy_country", ""),
            "proxy_ips": u.get("proxy_ips", []),
            "proxy_countries": u.get("proxy_countries", []),
            "proxy_ip_enabled": u.get("proxy_ip_enabled", False),
            "custom_ip_type": u.get("custom_ip_type", ""),
            "traffic_limit_bytes": u.get("traffic_limit_bytes", 0),
            "traffic_limit_fmt": "∞" if u.get("traffic_limit_bytes", 0) == 0 else fmt_bytes(u["traffic_limit_bytes"]),
            "traffic_used_bytes": u.get("traffic_used_bytes", 0),
            "traffic_used_fmt": fmt_bytes(u.get("traffic_used_bytes", 0)),
            "traffic_percent": round(u.get("traffic_used_bytes", 0) / max(u.get("traffic_limit_bytes", 1), 1) * 100, 1) if u.get("traffic_limit_bytes", 0) > 0 else 0,
            "expire_at": u.get("expire_at"),
            "concurrent_connections": u.get("concurrent_connections", 0),
            "created_at": u.get("created_at"),
            "status": u.get("status", "active"),
            "server": u.get("server", ""),
            "config_uuid": u.get("config_uuid"),
            "subscription_uuid": u.get("subscription_uuid"),
            "inbound_id": u.get("inbound_id"),
            "inbound_ids": u.get("inbound_ids") or (([u.get("inbound_id")] if u.get("inbound_id") else [])),
            "inbound_name": INBOUNDS.get(u.get("inbound_id", ""), {}).get("name", "") if u.get("inbound_id") else "",
            "config_url": f"https://{host}/api/users/{uid}/config",
            "qr_url": f"https://{host}/api/users/{uid}/qr",
            "subscription_url": f"https://{host}/api/users/{uid}/subscription",
            "connections": sum(1 for c in connections.values() if c.get("uuid") == u.get("config_uuid")),
        })
    result.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {"users": result}

_RANDOM_USERNAME_WORDS = [
    "spider", "falcon", "nova", "orbit", "pixel", "shadow", "storm", "ember",
    "silver", "rocket", "neon", "vortex", "echo", "atlas", "comet", "phoenix",
    "raven", "zen", "spark", "nexus", "titan", "wolf", "ghost", "quantum"
]

def _random_username() -> str:
    # Short, readable default username; uniqueness is checked by the caller.
    return f"{random.choice(_RANDOM_USERNAME_WORDS)}-{secrets.token_hex(2)}"


@app.post("/api/users")
async def create_user(request: Request, _=Depends(require_panel_or_node)):
    """Create a new user with protocol config, traffic limit, and expiry."""
    body = await request.json()
    username = str(body.get("username") or "").strip()[:40]
    if not username:
        username = _random_username()
    password = str(body.get("password") or secrets.token_urlsafe(12))
    traffic_limit_gb = float(body.get("traffic_limit_gb") or 0)
    expire_days = int(body.get("expire_days") or 0)
    protocol = str(body.get("protocol") or "vless").lower()
    _cc_raw = body.get("concurrent_connections")
    concurrent_connections = int(_cc_raw) if _cc_raw not in (None, "", "null") else 0
    server = (body.get("server") or "IR-Tehran-01").strip()[:40]
    sni = str(body.get("sni") or "").strip()
    path_custom = str(body.get("path") or "").strip()
    transport_type = str(body.get("transport_type") or "").strip().lower()
    inbound_id = str(body.get("inbound_id") or "").strip() or None
    # Multi-inbound support: accept an array of inbound ids; keep the first as
    # the "primary" inbound_id for backward compatibility.
    raw_ids = body.get("inbound_ids") or []
    if isinstance(raw_ids, str):
        raw_ids = [x.strip() for x in raw_ids.split(",") if x.strip()]
    inbound_ids = [str(x).strip() for x in raw_ids if str(x).strip()]
    if inbound_id and inbound_id not in inbound_ids:
        inbound_ids.insert(0, inbound_id)
    if inbound_ids:
        inbound_id = inbound_ids[0]
    proxy_ip = str(body.get("proxy_ip") or "").strip()
    proxy_country = str(body.get("proxy_country") or "").strip()
    proxy_ips = [str(x).strip() for x in (body.get("proxy_ips") or []) if str(x).strip()][:3]
    # Worker multi-location: the user may pick one or more countries; each gets
    # its own /route/{country}/{uuid} config. Only set for worker inbounds.
    proxy_countries = [str(x).strip().lower() for x in (body.get("proxy_countries") or []) if str(x).strip()]
    if not proxy_countries and proxy_country:
        proxy_countries = [proxy_country.lower()]
    # Cloudflare Worker routing: when enabled + worker connected, the user's
    # configs are addressed to the worker domain with a /route/{country}/{uuid} path.
    proxy_ip_enabled = bool(body.get("proxy_ip_enabled"))
    if proxy_ip_enabled and not WORKER.get("connected"):
        proxy_ip_enabled = False
    # Scanned custom-IP source: cf | railway ("" = off). Adds up to 10 extra
    # configs in the sub, addressed by scanned IPs on non-Reality inbounds.
    custom_ip_type = str(body.get("custom_ip_type") or "").strip().lower()
    if custom_ip_type not in _SCANNED_TYPES:
        custom_ip_type = ""
    # Per-inbound scanned-IP switches: {cf: [inboundIds], railway: [inboundIds]}
    # chosen in the create-user modal. Only these inbounds get scanned-IP configs.
    cii = body.get("custom_ip_inbounds") or {}
    if isinstance(cii, dict):
        custom_ip_inbounds = {
            "cf": [str(x) for x in (cii.get("cf") or [])],
            "railway": [str(x) for x in (cii.get("railway") or [])],
        }
    else:
        custom_ip_inbounds = {"cf": [], "railway": []}
    # Sni spoof for v2box: when enabled, TLS WS and Worker configs include
    # snispoofing JSON parameter. Does not apply to Reality/XHTTP Reality.
    sni_spoof_v2box = bool(body.get("sni_spoof_v2box"))

    # If transport_type not given explicitly, derive it from the primary inbound
    # (so an xhttp inbound produces an xhttp user).
    if not transport_type and inbound_id:
        async with INBOUNDS_LOCK:
            ib = INBOUNDS.get(inbound_id) or {}
        # For Reality inbounds, transport_type should be "reality" regardless of network
        if (ib.get("protocol") or "").lower() == "reality" or (ib.get("security") or "").lower() == "reality":
            transport_type = "reality"
        else:
            transport_type = str(ib.get("network") or "").strip().lower()
    if not transport_type:
        transport_type = "ws"

    if transport_type not in ("ws", "grpc", "tcp", "xhttp", "reality"):
        transport_type = "ws"

    if protocol not in USER_PROTOCOLS:
        raise HTTPException(status_code=400, detail=f"Invalid protocol. Must be one of: {', '.join(USER_PROTOCOLS)}")
    if len(username) < 1:
        raise HTTPException(status_code=400, detail="Username is required")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters")
    if concurrent_connections < 0:
        concurrent_connections = 0

    user_id = generate_short_id()
    supplied_uuid = str(body.get("config_uuid") or "").strip()
    config_uuid = supplied_uuid if _is_valid_uuid(supplied_uuid) else generate_uuid()
    subscription_uuid = secrets.token_urlsafe(16)
    traffic_limit_bytes = int(traffic_limit_gb * 1024 ** 3) if traffic_limit_gb > 0 else 0
    expire_at = (datetime.now() + timedelta(days=expire_days)).isoformat() if expire_days > 0 else None

    # Auto-generate Reality key pair if protocol is reality and no key exists
    if protocol == "reality":
        async with SETTINGS_LOCK:
            reality = SETTINGS.get("reality", {})
            if not reality.get("public_key"):
                try:
                    reality["private_key"], reality["public_key"] = _xray_x25519_keypair()
                    reality.setdefault("short_id", secrets.token_hex(4)[:10])
                    reality.setdefault("dest", "is1-ssl.mzstatic.com:443")
                    reality.setdefault("sni", "is1-ssl.mzstatic.com")
                    reality.setdefault("spiderx", "/")
                    reality.setdefault("fingerprint", "chrome")
                    reality.setdefault("external_port", 443)
                    SETTINGS["reality"] = reality
                    asyncio.create_task(save_state())
                    log_activity("settings", "کلیدهای Reality خودکار ساخته شد", "ok")
                except ImportError:
                    pass

    async with USERS_LOCK:
        # Check for duplicate username
        if username and all(existing.get("username") != username for existing in USERS.values()):
            pass
        else:
            # Only randomize automatically generated names on collision; user-supplied duplicates still return 409.
            supplied_name = str(body.get("username") or "").strip()
            if supplied_name:
                raise HTTPException(status_code=409, detail="Username already exists")
            for _ in range(12):
                candidate = _random_username()
                if all(existing.get("username") != candidate for existing in USERS.values()):
                    username = candidate
                    break
            else:
                raise HTTPException(status_code=500, detail="Could not generate a unique username")

        # Determine the path based on the inbound type, not just transport_type
        # WS inbound -> /ws/{config_uuid}, XHTTP inbound -> /xhttp-siz10/..., Worker inbound -> /route/{country}/{uuid}
        primary_inbound = INBOUNDS.get(inbound_id) if inbound_id else None
        primary_inbound_proto = (primary_inbound.get("protocol") if primary_inbound else "").lower()
        primary_inbound_network = (primary_inbound.get("network") if primary_inbound else "").lower()

        if primary_inbound_proto == "worker":
            # Country is part of the Worker route; actual per-country links
            # are emitted by _worker_configs(). Keep a deterministic template.
            path = f"/route/{{country}}/{config_uuid}"
        elif primary_inbound_proto == "tunnel":
            path = f"/tunnel/{config_uuid}"
        elif primary_inbound_proto == "reverse":
            path = f"/reverse/{config_uuid}"
        elif primary_inbound_proto == "reality":
            if primary_inbound_network == "xhttp":
                path = f"{str((primary_inbound.get('xhttp_settings') or {}).get('path') or '/xhttp').rstrip('/')}/stream-up/{config_uuid}"
            elif primary_inbound_network == "grpc":
                path = f"/{str((primary_inbound.get('grpc_settings') or {}).get('serviceName') or 'grpc').lstrip('/')}"
            else:
                path = f"/tcp/{config_uuid}"
        elif primary_inbound_network == "xhttp":
            xbase = str((primary_inbound.get('xhttp_settings') or {}).get('path') or '/xhttp').strip()
            if not xbase.startswith('/'): xbase = '/' + xbase
            path = f"{xbase.rstrip('/')}/stream-up/{config_uuid}"
        elif primary_inbound_network == "grpc":
            svc = str((primary_inbound.get('grpc_settings') or {}).get('serviceName') or 'grpc').strip('/')
            path = f"/{svc}"
        else:
            wsbase = str((primary_inbound.get('ws_settings') or {}).get('path') or '/ws').strip()
            if not wsbase.startswith('/'): wsbase = '/' + wsbase
            path = f"{wsbase.rstrip('/')}/{config_uuid}"

        # Custom path is only a transport base; a UUID suffix is mandatory for WS/XHTTP relay lookup.
        if path_custom:
            pc = str(path_custom).strip()
            if not pc.startswith("/"): pc = "/" + pc
            if "{uuid}" in pc:
                path = pc.replace("{uuid}", config_uuid)
            elif primary_inbound_network == "ws":
                path = pc.rstrip("/") + f"/{config_uuid}"
            elif primary_inbound_network == "xhttp":
                path = pc.rstrip("/") + f"/stream-up/{config_uuid}"
            else:
                path = pc

        USERS[user_id] = {
            "username": username,
            "password_hash": hash_password(password),
            "protocol": protocol,
            "traffic_limit_bytes": traffic_limit_bytes,
            "traffic_used_bytes": 0,
            "expire_at": expire_at,
            "concurrent_connections": concurrent_connections,
            "created_at": datetime.now().isoformat(),
            "status": "active",
            "server": server,
            "config_uuid": config_uuid,
            "subscription_uuid": subscription_uuid,
            "sni": sni,
            "proxy_ip": proxy_ip,
            "proxy_country": proxy_country,
            "proxy_ips": proxy_ips,
            "proxy_countries": proxy_countries,
            "proxy_ip_enabled": proxy_ip_enabled,
            "custom_ip_type": custom_ip_type,
            "custom_ip_inbounds": custom_ip_inbounds,
            "sni_spoof_v2box": sni_spoof_v2box,
            "inbound_id": inbound_id,
            "inbound_ids": inbound_ids,
            "path": path,
            "transport_type": transport_type,
            "telegram_secret": "",
        }
        _path = USERS[user_id].get("path", "").strip().lstrip("/")

    # Auto-create matching link so relay can find it
    async with LINKS_LOCK:
        link_xhttp = {}
        # Determine the link protocol based on transport_type for correct config generation
        # vless-ws for WS, xhttp-{mode} for XHTTP
        link_protocol = protocol
        if transport_type == "ws" or transport_type == "vless-ws":
            link_protocol = "vless-ws"
        elif transport_type == "xhttp":
            link_protocol = "xhttp-stream-up"  # default mode
        elif transport_type == "reality":
            link_protocol = "reality"
        elif transport_type == "worker":
            link_protocol = "worker"
        elif transport_type == "tunnel":
            link_protocol = "tunnel"
        elif transport_type == "reverse":
            link_protocol = "reverse"

        if transport_type == "xhttp":
            link_xhttp = {
                "xPaddingBytes": "100-1000",
                "mode": "stream-up",
                "scMaxEachPostBytes": "1000000",
            }
        LINKS[config_uuid] = {
            "label": username,
            "limit_bytes": traffic_limit_bytes,
            "used_bytes": 0,
            "created_at": datetime.now().isoformat(),
            "active": True,
            "expires_at": expire_at,
            "note": f"لینک کاربر {username}",
            "is_default": False,
            "sub_id": None,
            "protocol": link_protocol,  # Use transport-specific protocol for correct config
            "transport_type": transport_type,
            "xhttp_settings": link_xhttp,
            "path": _path,
            "user_id": user_id,
        }
        # Register uuid in PATH_INDEX for backward compat (old random-path clients)
        # config_uuid IS the path under /ws/{config_uuid}
        PATH_INDEX[config_uuid] = config_uuid
        if _path:
            PATH_INDEX[_path.lstrip("/")] = config_uuid

    asyncio.create_task(save_state())
    log_activity("user", f"کاربر «{username}» با پروتکل {protocol} ساخته شد", "ok")
    # If the user picked the worker or tunnel inbound, sync them to the worker
    # so VLESS auth + quotas work on the Cloudflare side too.
    if WORKER.get("connected") and inbound_ids:
        new_u = USERS.get(user_id) or {}
        if _user_uses_worker_inbound(new_u):
            asyncio.create_task(_worker_sync_users())
    # Generate and persist telegram_secret if user has a Telegram inbound
    if inbound_ids:
        has_tg = any(
            (INBOUNDS.get(i, {}).get("protocol") or "").lower() == "telegram"
            for i in inbound_ids
        )
        if has_tg:
            from telegram_proxy import derive_secret_from_uuid
            async with USERS_LOCK:
                u = USERS.get(user_id)
                if u:
                    current = str(u.get("telegram_secret") or "").strip().lower()
                    # mtprotoproxy 1.0.6 expects exactly 32 hex characters.
                    if not re.fullmatch(r"[0-9a-f]{32}", current):
                        u["telegram_secret"] = derive_secret_from_uuid(config_uuid)
            asyncio.create_task(save_state())
            # The inbound may have been created before this user existed.
            # Rebuild its secret list and restart the listener now.
            for _tg_iid in [i for i in inbound_ids if (INBOUNDS.get(i, {}).get("protocol") or "").lower() == "telegram"]:
                asyncio.create_task(_restart_telegram_proxy(_tg_iid))
    host = SETTINGS.get("domain") or get_host()
    asyncio.create_task(_xray_apply())  # refresh Xray clients after user change
    # NODE inbound: replicate the user to all registered nodes (same UUID).
    node_results = []
    selected = await _selected_node_ids_for_user(user)
    if selected:
        node_results = await _sync_user_to_selected_nodes(config_uuid, {"username": username}, selected)
    return {
        "user_id": user_id,
        **USERS[user_id],
        "password_hash": None,
        "config_url": f"https://{host}/api/users/{user_id}/config",
        "qr_url": f"https://{host}/api/users/{user_id}/qr",
        "subscription_url": f"https://{host}/subs/{config_uuid}",
        "config": generate_user_config(user_id, USERS[user_id], inbound_id),
        "nodes_sync": node_results,
    }

@app.patch("/api/users/{user_id}/toggle")
async def toggle_user(user_id: str, _=Depends(require_auth)):
    """Enable or disable a user."""
    async with USERS_LOCK:
        u = USERS.get(user_id)
        if not u:
            raise HTTPException(status_code=404, detail="user not found")
        old = u.get("status", "active")
        if old == "disabled":
            u["status"] = "active"
        else:
            u["status"] = "disabled"
        new_status = u["status"]

    # Sync link active state
    config_uuid = u.get("config_uuid")
    if config_uuid:
        async with LINKS_LOCK:
            if config_uuid in LINKS:
                LINKS[config_uuid]["active"] = (new_status == "active")

    asyncio.create_task(save_state())
    log_activity("user", f"کاربر «{u['username']}» {'غیرفعال' if new_status == 'disabled' else 'فعال'} شد", "ok" if new_status == "active" else "warn")
    # Reflect enable/disable on the worker side too.
    if WORKER.get("connected") and _user_uses_worker_inbound(u):
        asyncio.create_task(_worker_sync_users())
    return {"ok": True, "user_id": user_id, "status": new_status}

@app.patch("/api/users/{user_id}/reset")
async def reset_user_traffic(user_id: str, _=Depends(require_auth)):
    """Reset a user's traffic usage to zero."""
    async with USERS_LOCK:
        u = USERS.get(user_id)
        if not u:
            raise HTTPException(status_code=404, detail="user not found")
        u["traffic_used_bytes"] = 0
        username = u.get("username", user_id)
    # Reset the worker-side usage too, so the quota reflects the reset immediately.
    if WORKER.get("connected") and _user_uses_worker_inbound(u):
        asyncio.create_task(_worker_sync_users())
    for _tg_iid in [i for i in (u.get("inbound_ids") or []) if (INBOUNDS.get(i, {}).get("protocol") or "").lower() == "telegram"]:
        asyncio.create_task(_restart_telegram_proxy(_tg_iid))
    asyncio.create_task(save_state())
    log_activity("user", f"مصرف کاربر «{username}» ریست شد", "info")
    return {"ok": True, "user_id": user_id, "traffic_used_bytes": 0}

@app.patch("/api/users/{user_id}")
async def edit_user(user_id: str, request: Request, _=Depends(require_auth)):
    """Edit an existing user."""
    body = await request.json()
    async with USERS_LOCK:
        if user_id not in USERS:
            raise HTTPException(status_code=404, detail="user not found")
        u = USERS[user_id]
        if "username" in body:
            u["username"] = str(body["username"]).strip()[:40]
        if "traffic_limit_gb" in body:
            gb = float(body["traffic_limit_gb"])
            u["traffic_limit_bytes"] = int(gb * 1024**3) if gb > 0 else 0
        if "expire_days" in body:
            days = int(body["expire_days"])
            u["expire_at"] = (datetime.now() + timedelta(days=days)).isoformat() if days > 0 else None
        if "protocol" in body:
            p = str(body["protocol"]).lower()
            if p in USER_PROTOCOLS:
                u["protocol"] = p
        if "status" in body:
            u["status"] = str(body["status"])
        if "sni" in body:
            u["sni"] = str(body["sni"]).strip()
        if "path" in body:
            # Update PATH_INDEX when path changes
            old_path = (u.get("path") or "").strip().lstrip("/")
            new_path = str(body["path"]).strip().lstrip("/")
            u["path"] = new_path
            if old_path:
                PATH_INDEX.pop(old_path, None)
            if new_path:
                PATH_INDEX[new_path] = u.get("config_uuid", user_id)
        if "transport_type" in body:
            u["transport_type"] = str(body["transport_type"]).strip().lower()
        if "concurrent_connections" in body:
            u["concurrent_connections"] = max(0, int(body["concurrent_connections"]))
        if "reset_traffic" in body and body["reset_traffic"]:
            u["traffic_used_bytes"] = 0
        if "custom_ip_type" in body:
            ct = str(body["custom_ip_type"] or "").strip().lower()
            u["custom_ip_type"] = ct if ct in _SCANNED_TYPES else ""
        if "sni_spoof_v2box" in body:
            u["sni_spoof_v2box"] = bool(body["sni_spoof_v2box"])
        if "fake_sni" in body:
            u["fake_sni"] = str(body["fake_sni"] or "").strip()
        if "spoof_ip" in body:
            u["spoof_ip"] = str(body["spoof_ip"] or "").strip()
        if "proxy_ip_enabled" in body:
            en = bool(body["proxy_ip_enabled"])
            u["proxy_ip_enabled"] = en and WORKER.get("connected")
        if "proxy_countries" in body:
            pc = [str(x).strip().lower() for x in (body["proxy_countries"] or []) if str(x).strip()]
            u["proxy_countries"] = pc
            if pc:
                u["proxy_country"] = pc[0]
        elif "proxy_country" in body:
            u["proxy_country"] = str(body["proxy_country"] or "").strip().lower()
            u["proxy_countries"] = [u["proxy_country"]] if u["proxy_country"] else []
        if "inbound_ids" in body:
            raw_ids = [str(x).strip() for x in (body["inbound_ids"] or []) if str(x).strip()]
            valid = [i for i in raw_ids if i in INBOUNDS]
            u["inbound_ids"] = valid
            if valid:
                u["inbound_id"] = valid[0]
            else:
                u.pop("inbound_id", None)
        # Ensure a Telegram secret exists whenever the edited user has a Telegram inbound.
        if any((INBOUNDS.get(i, {}).get("protocol") or "").lower() == "telegram" for i in (u.get("inbound_ids") or [])):
            from telegram_proxy import derive_secret_from_uuid
            cur_secret = str(u.get("telegram_secret") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{32}", cur_secret):
                u["telegram_secret"] = derive_secret_from_uuid(u.get("config_uuid", user_id))
    # If the user uses the worker inbound, push updated volume/expiry to the worker.
    if WORKER.get("connected") and _user_uses_worker_inbound(u):
        asyncio.create_task(_worker_sync_users())
    asyncio.create_task(save_state())
    for _tg_iid in [i for i in (u.get("inbound_ids") or []) if (INBOUNDS.get(i, {}).get("protocol") or "").lower() == "telegram"]:
        asyncio.create_task(_restart_telegram_proxy(_tg_iid))
    return {"ok": True, "user_id": user_id}

@app.get("/api/users/{user_id}")
async def get_user(user_id: str, _=Depends(require_auth)):
    """Get single user details."""
    async with USERS_LOCK:
        if user_id not in USERS:
            raise HTTPException(status_code=404, detail="user not found")
        u = dict(USERS[user_id])
        u["user_id"] = user_id
        u["password_hash"] = None
        return u


@app.delete("/api/users/{user_id}")
async def delete_user(user_id: str, _=Depends(require_panel_or_node)):
    """Delete a user permanently."""
    async with USERS_LOCK:
        u = USERS.get(user_id)
        if not u:
            raise HTTPException(status_code=404, detail="user not found")
        username = u.get("username", user_id)
        # Clean up PATH_INDEX and synced link
        old_path = (u.get("path") or "").strip().lstrip("/")
        if old_path:
            PATH_INDEX.pop(old_path, None)
        config_uuid = u.get("config_uuid")
        if config_uuid:
            PATH_INDEX.pop(config_uuid, None)
        USERS.pop(user_id, None)
    # Delete matching link
    if config_uuid:
        async with LINKS_LOCK:
            LINKS.pop(config_uuid, None)
    # If the deleted user used the worker inbound, tell the worker to drop them.
    if WORKER.get("connected") and _user_uses_worker_inbound(u):
        asyncio.create_task(_worker_sync_users())
    # Best-effort: delete user from all registered nodes
    if config_uuid:
        asyncio.create_task(_delete_user_from_nodes(config_uuid))
    asyncio.create_task(save_state())
    log_activity("user", f"کاربر «{username}» حذف شد", "err")
    return {"ok": True, "deleted": user_id}

@app.get("/api/users/{user_id}")
async def get_single_user(user_id: str, _=Depends(require_auth)):
    """Get full details for a single user."""
    async with USERS_LOCK:
        u = USERS.get(user_id)
        if not u:
            raise HTTPException(status_code=404, detail="user not found")
        user = dict(u)
        user["user_id"] = user_id
        user["password_hash"] = None  # Never expose hash
    auto_check_user_expiry(user)
    host = SETTINGS.get("domain") or get_host()
    return {
        **user,
        "config": generate_user_config(user_id, user, user.get("inbound_id")),
        "config_url": f"https://{host}/api/users/{user_id}/config",
        "qr_url": f"https://{host}/api/users/{user_id}/qr",
        "subscription_url": f"https://{host}/api/users/{user_id}/subscription",
        "traffic_used_fmt": fmt_bytes(user.get("traffic_used_bytes", 0)),
        "traffic_limit_fmt": "∞" if user.get("traffic_limit_bytes", 0) == 0 else fmt_bytes(user.get("traffic_limit_bytes", 0)),
    }

@app.patch("/api/users/{user_id}")
async def edit_user(user_id: str, request: Request, _=Depends(require_auth)):
    """Edit an existing user's fields."""
    body = await request.json()
    async with USERS_LOCK:
        u = USERS.get(user_id)
        if not u:
            raise HTTPException(status_code=404, detail="user not found")
        old_username = u.get("username")

        if "username" in body:
            new_name = str(body["username"]).strip()[:40]
            # Check duplicate
            for oid, ou in USERS.items():
                if oid != user_id and ou.get("username") == new_name:
                    raise HTTPException(status_code=409, detail="Username already exists")
            if new_name:
                u["username"] = new_name

        if "traffic_limit_gb" in body:
            gb = float(body["traffic_limit_gb"] or 0)
            u["traffic_limit_bytes"] = int(gb * 1024 ** 3) if gb > 0 else 0

        if "expire_days" in body:
            days = int(body["expire_days"] or 0)
            u["expire_at"] = (datetime.now() + timedelta(days=days)).isoformat() if days > 0 else None

        if "protocol" in body:
            proto = str(body["protocol"]).lower()
            if proto in USER_PROTOCOLS:
                u["protocol"] = proto

        if "sni" in body:
            u["sni"] = str(body["sni"]).strip()

        if "path" in body:
            u["path"] = str(body["path"]).strip()

        if "transport_type" in body:
            tt = str(body["transport_type"]).strip().lower()
            if tt in ("ws", "grpc", "tcp", "xhttp", "reality"):
                u["transport_type"] = tt

        if "status" in body:
            st = str(body["status"]).lower()
            if st in ("active", "disabled", "expired"):
                u["status"] = st

        if "concurrent_connections" in body:
            _ccv = body["concurrent_connections"]
            cc = int(_ccv) if _ccv not in (None, "", "null") else 0
            u["concurrent_connections"] = max(0, cc)
        if any((INBOUNDS.get(i, {}).get("protocol") or "").lower() == "telegram" for i in (u.get("inbound_ids") or [])):
            from telegram_proxy import derive_secret_from_uuid
            cur_secret = str(u.get("telegram_secret") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{32}", cur_secret):
                u["telegram_secret"] = derive_secret_from_uuid(u.get("config_uuid", user_id))

    asyncio.create_task(save_state())
    for _tg_iid in [i for i in (u.get("inbound_ids") or []) if (INBOUNDS.get(i, {}).get("protocol") or "").lower() == "telegram"]:
        asyncio.create_task(_restart_telegram_proxy(_tg_iid))
    log_activity("user", f"کاربر «{old_username}» ویرایش شد", "info")
    return {"ok": True, "user_id": user_id, "username": u.get("username")}

@app.get("/api/users/{user_id}/config")
async def get_user_config(user_id: str, _=Depends(require_auth)):
    """Return the protocol config string for a user."""
    async with USERS_LOCK:
        u = USERS.get(user_id)
        if not u:
            raise HTTPException(status_code=404, detail="user not found")
        config = generate_user_config(user_id, u, u.get("inbound_id"))
        username = u.get("username")
        protocol = u.get("protocol")
    host = SETTINGS.get("domain") or get_host()
    return {
        "user_id": user_id,
        "username": username,
        "protocol": protocol,
        "config": config,
        "config_url": f"https://{host}/api/users/{user_id}/config",
        "qr_url": f"https://{host}/api/users/{user_id}/qr",
        "subscription_url": f"https://{host}/api/users/{user_id}/subscription",
    }

@app.get("/api/users/{user_id}/qr")
async def get_user_qr(user_id: str, _=Depends(require_auth)):
    """Return a QR code PNG for the user's subscription URL (domain/sub/uuid)."""
    if not QR_AVAILABLE:
        raise HTTPException(status_code=501, detail="QR code generation not available (install qrcode and Pillow)")

    async with USERS_LOCK:
        u = USERS.get(user_id)
        if not u:
            raise HTTPException(status_code=404, detail="user not found")
        config_uuid = u.get("config_uuid", "")
        username = u.get("username", user_id)

    if not config_uuid:
        raise HTTPException(status_code=404, detail="user has no config_uuid")

    host = SETTINGS.get("domain") or get_host()
    sub_url = f"https://{host}/sub/{config_uuid}"

    qr = qrcode.QRCode(version=1, box_size=10, border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(sub_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.getvalue(), media_type="image/png",
                    headers={"Content-Disposition": f"inline; filename={username}.png"})

@app.get("/api/users/{user_id}/subscription")
async def get_user_subscription(user_id: str, _=Depends(require_auth)):
    """Return the subscription URL for a user."""
    host = SETTINGS.get("domain") or get_host()
    async with USERS_LOCK:
        u = USERS.get(user_id)
        if not u:
            raise HTTPException(status_code=404, detail="user not found")
        sub_uuid = u.get("subscription_uuid")
        username = u.get("username")

    if not sub_uuid:
        raise HTTPException(status_code=404, detail="no subscription configured")

    config = generate_user_config(user_id, u, u.get("inbound_id"))
    content = base64.b64encode(config.encode()).decode()

    return {
        "user_id": user_id,
        "username": username,
        "subscription_uuid": sub_uuid,
        "subscription_url": f"https://{host}/sub/{sub_uuid}",
        "encoded_config": content,
    }


# ── Public sub page ───────────────────────────────────────────────────────────
@app.get("/p/{uuid_key}", response_class=HTMLResponse)
async def public_sub_page(uuid_key: str, request: Request):
    from pages import get_public_page_html
    async with SUBS_LOCK:
        sub = next(({"sub_id": sid, **s} for sid, s in SUBS.items() if s.get("uuid_key") == uuid_key), None)
    if not sub:
        return HTMLResponse("<h2 style='font-family:sans-serif;padding:40px'>گروه پیدا نشد</h2>", status_code=404)
    return HTMLResponse(content=get_public_page_html(uuid_key))

@app.get("/api/public/sub/{uuid_key}")
async def public_sub_data(uuid_key: str, request: Request):
    async with SUBS_LOCK:
        sub_entry = next(((sid, s) for sid, s in SUBS.items() if s.get("uuid_key") == uuid_key), None)
    if not sub_entry:
        raise HTTPException(status_code=404, detail="not found")
    sub_id, sub = sub_entry

    has_pw = sub.get("password_hash") is not None
    if has_pw:
        pw = request.query_params.get("pw", "")
        if hash_password(pw) != sub["password_hash"]:
            return JSONResponse({"locked": True, "name": sub["name"]})

    host = SETTINGS.get("domain") or get_host()
    link_ids = sub.get("link_ids", [])
    async with LINKS_LOCK:
        snap = dict(LINKS)

    links_out = []
    active_conns = 0
    for lid in link_ids:
        link = snap.get(lid)
        if not link:
            continue
        allowed = is_link_allowed(link)
        conn_count = sum(1 for c in connections.values() if c.get("uuid") == lid)
        active_conns += conn_count
        proto = link.get("protocol", DEFAULT_PROTOCOL)
        links_out.append({
            "uuid": lid,
            "label": link["label"],
            "active": allowed,
            "protocol": proto,
            "used_bytes": link.get("used_bytes", 0),
            "used_fmt": fmt_bytes(link.get("used_bytes", 0)),
            "limit_bytes": link.get("limit_bytes", 0),
            "limit_fmt": "∞" if link.get("limit_bytes", 0) == 0 else fmt_bytes(link["limit_bytes"]),
            "expires_at": link.get("expires_at"),
            "vless_link": generate_vless_link(lid, host, remark=f"Spider-{link['label']}", protocol=proto),
            "sub_url": f"https://{host}/sub/{lid}",
            "connections": conn_count,
        })

    total_used = sum(l["used_bytes"] for l in links_out)
    return {
        "locked": False,
        "name": sub["name"],
        "desc": sub.get("desc", ""),
        "sub_url": f"https://{host}/sub-group/{uuid_key}",
        "active_connections": active_conns,
        "total_used_fmt": fmt_bytes(total_used),
        "links": links_out,
    }

# ── HTML Pages (SPA) ───────────────────────────────────────────────────────
import os as _os
_STATIC_DIR = Path(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "static"))
_os.makedirs(_STATIC_DIR, exist_ok=True)

# Serve static assets (mp3/png/jpg/index.html for the SPA). This was missing, so
# the panel music + background images 404'd.
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if await is_valid_session(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(url="/spider")
    return FileResponse(_os.path.join(_STATIC_DIR, "login.html"))

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_redirect(request: Request):
    return RedirectResponse(url="/spider")

@app.get("/spider", response_class=HTMLResponse)
async def spider_panel(request: Request):
    if not await is_valid_session(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(url="/login")
    await ensure_default_link()
    return FileResponse(_os.path.join(_STATIC_DIR, "index.html"))

@app.get("/test-ws", response_class=HTMLResponse)
async def test_ws_redirect():
    return HTMLResponse(content="<script>location.href='/spider'</script>")


# ══════════════════════════════════════════════════════════════════════════════
# USER SUBSCRIPTION DATA API (Public)
# Note: /sub/{identifier} above now handles both user HTML pages and link configs.
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/sub/{username}")
async def api_user_sub(username: str):
    """Return subscription data for a user (works for both active and inactive users)."""
    async with USERS_LOCK:
        user = None
        for uid, u in USERS.items():
            if u.get("username") == username:
                user = dict(u)
                user["user_id"] = uid
                break
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if _user_uses_worker_inbound(user):
        user = await _worker_pull_user(user.get("user_id"), user)
        async with USERS_LOCK:
            if user.get("user_id") in USERS:
                USERS[user.get("user_id")].update(user)

    # Even inactive users get their sub page (just show status)
    status = user.get("status", "active")

    auto_check_user_expiry(user)

    # Calculate expiry info
    expire_days = None
    expire_at_ts = None
    if user.get("expire_at"):
        try:
            exp = datetime.fromisoformat(user["expire_at"])
            expire_at_ts = int(exp.timestamp())
            expire_days = max(0, (exp - datetime.now()).days)
        except Exception:
            pass

    # Calculate created_at timestamp
    created_at_ts = None
    if user.get("created_at"):
        try:
            created_at_ts = int(datetime.fromisoformat(user["created_at"]).timestamp())
        except Exception:
            pass

    status = user.get("status", "active")
    is_active = is_user_allowed(user)
    if not is_active and status == "active":
        status = "expired" if user.get("status") != "disabled" else "disabled"

    # Calculate traffic percent
    used = user.get("traffic_used_bytes", 0)
    limit = user.get("traffic_limit_bytes", 0)
    traffic_pct = round(used / max(limit, 1) * 100, 1) if limit > 0 else 0

    # Multi-inbound: build one config per selected inbound.
    # A "worker" inbound expands to one config per selected country (multi-location).
    configs = []
    uid_ = user.get("user_id")
    inbound_ids = user.get("inbound_ids") or []
    stored_path_user = (user.get("path") or "").strip()
    if inbound_ids:
        for iid_ in inbound_ids:
            ib = INBOUNDS.get(iid_)
            try:
                _p = (ib.get("protocol") if ib else "").lower()
                _s = (ib.get("security") if ib else "").lower()
                # A reality inbound without a configured domain/port isn't ready
                # yet — skip it so the sub never shows a broken config.
                if ib and (_p == "reality" or _s == "reality"):
                    # For Reality: check external_domain and external_port (not port)
                    if not str(ib.get("external_domain") or "").strip() or not str(ib.get("external_port") or "").strip():
                        continue
                if ib and _p == "worker":
                    configs.extend(_worker_configs(uid_, user, ib, stored_path_user, f"Spider-{user.get('username', uid_)}"))
                else:
                    configs.append(generate_user_config(uid_, user, iid_))
            except Exception:
                continue
    if user.get("node_configs"):
        configs.extend(c.get("config") for c in user.get("node_configs") if c.get("config"))
    if not configs:
        # Fallback: single inbound config
        fallback_config = generate_user_config(user.get("user_id"), user, user.get("inbound_id"))
        configs = [fallback_config] if fallback_config else []

    # Custom scanned-IP configs (the iOS switch): per inbound type.
    # worker → Cloudflare IPs, tls → Railway IPs, reality → none.
    # `configs` keeps main+custom (used for "copy all"); `custom_configs` lets
    # the sub page render main configs on top, then Railway, then Cloudflare.
    custom_cfgs = generate_custom_ip_configs(user.get("user_id"), user)
    custom_railway = custom_cfgs.get("railway", [])
    custom_cf = custom_cfgs.get("cf", [])
    all_custom = custom_railway + custom_cf
    if all_custom:
        configs = configs + all_custom

    # Sni Spoof for v2box configs: separate section when enabled
    sni_spoof_cfgs = []
    if user.get("sni_spoof_v2box"):
        # Use user's fake_sni and spoof_ip from spoof tab, or fallback to inbound defaults
        # These are TLS WS and Worker configs with snispoofing param
        sni_spoof_cfgs = generate_sni_spoof_configs(user.get("user_id"), user)
        if sni_spoof_cfgs:
            configs = configs + sni_spoof_cfgs

    # Generate a status config (config-status) with fake random stats
    # This config is always the FIRST one in the list so clients show it as "status"
    status_config = generate_status_config(user, configs)

    # Insert status config at the beginning
    if status_config:
        configs = [status_config] + configs

    # Pick a TLS WS/XHTTP config for the status config (not Reality/Worker)
    # so the status config uses the panel domain for host/sni
    # Skip the first config (which is the status config) and pick the first real config
    config = None
    for c in configs[1:]:
        if c and ("type=ws" in c or "type=xhttp" in c):
            config = c
            break
    if not config and len(configs) > 1:
        config = configs[1]
    elif not config and configs:
        config = configs[0]

    host = SETTINGS.get("domain") or get_host()
    subscription_uuid = user.get("config_uuid") or user.get("subscription_uuid") or user.get("user_id")
    subscription_url = f"https://{host}/sub/{subscription_uuid}"
    expire_header = int(expire_at_ts or 0)

    return {
        "username": user.get("username"),
        "config_uuid": user.get("config_uuid", ""),
        "subscription_url": subscription_url,
        "protocol": user.get("protocol", "vless"),
        "custom_ip_type": user.get("custom_ip_type", ""),
        "custom_ip_count": len(all_custom),
        "custom_configs": all_custom,
        "custom_railway_configs": custom_railway,
        "custom_cf_configs": custom_cf,
        "sni_spoof_configs": sni_spoof_cfgs,
        "sni_spoof_count": len(sni_spoof_cfgs),
        "traffic_used_bytes": used,
        "traffic_used_fmt": fmt_bytes(used),
        "traffic_limit_bytes": limit,
        "traffic_limit_fmt": "∞" if limit == 0 else fmt_bytes(limit),
        "traffic_percent": traffic_pct,
        "expire_days": expire_days,
        "expire_at": user.get("expire_at"),
        "expire_at_ts": expire_at_ts,
        "created_at": user.get("created_at"),
        "created_at_ts": created_at_ts,
        "status": status,
        "is_active": is_active,
        "vless_link": config,
        "config": config,
        "configs": configs,
        "worker_configs": list(user.get("worker_configs") or []),
        "worker_countries": list(user.get("worker_countries") or user.get("proxy_countries") or []),
        "inbound_ids": inbound_ids,
        "sni": user.get("sni", ""),
        "path": user.get("path", ""),
        "transport_type": user.get("transport_type", "ws"),
        "concurrent_connections": user.get("concurrent_connections", 3),
        "server": user.get("server", ""),
        "proxy_ips": user.get("proxy_ips", []),
        "proxy_country": user.get("proxy_country", ""),
        "proxy_countries": user.get("proxy_countries", []),
        "proxy_ip_enabled": user.get("proxy_ip_enabled", False),
        "max_ip_per_user": int(user.get("concurrent_connections", SETTINGS.get("max_ip_per_user", 3) or 3)),
        "used_ips": len(USER_IP_MAP.get(user.get("user_id", ""), set())),
    }


@app.get("/api/sub/{username}/qr")
async def sub_qr(username: str, cfg: str = ""):
    """Public QR code PNG for the subscription page (no auth required).

    Without ?cfg= the QR contains the subscription URL (domain/sub/config_uuid).
    With ?cfg={config_uuid} it encodes that specific config link of the user —
    used by the sub page when no client-side QR library is available.
    """
    if not QR_AVAILABLE:
        raise HTTPException(status_code=501, detail="qr code generation not available")
    user = None
    uid = None
    async with USERS_LOCK:
        for u_id, u in USERS.items():
            if u.get("username") == username:
                user = u
                uid = u_id
                break
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    config_uuid = user.get("config_uuid", "")
    if not config_uuid:
        raise HTTPException(status_code=404, detail="user has no config_uuid")

    qr_data = ""
    if cfg:
        # Encode one of this user's own configs (matched by uuid prefix).
        if cfg != config_uuid and cfg not in (user.get("inbound_ids") or []):
            raise HTTPException(status_code=403, detail="cfg mismatch")
        configs = list(user.get("worker_configs") or [])
        if not configs:
            for iid_ in (user.get("inbound_ids") or []):
                ib = INBOUNDS.get(iid_)
                if ib and (ib.get("protocol") or "").lower() == "worker":
                    configs.extend(_worker_configs(uid, user, ib, "", f"Spider-{username}"))
        if not configs:
            single = generate_user_config(uid, user, user.get("inbound_id"))
            if single:
                configs = [single]
        idx = 0
        if cfg != config_uuid:
            try:
                idx = int(cfg)
            except ValueError:
                idx = 0
        qr_data = configs[idx] if configs and idx < len(configs) else (configs[0] if configs else "")
    if not qr_data:
        host = SETTINGS.get("domain") or get_host()
        qr_data = f"https://{host}/sub/{config_uuid}"

    qr = qrcode.QRCode(version=1, box_size=8, border=3,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(qr_data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.getvalue(), media_type="image/png")


# ══════════════════════════════════════════════════════════════════════════════
# TOOLS - Reality Settings
# ══════════════════════════════════════════════════════════════════════════════


@app.post("/api/tools/generate-reality-keys")
async def generate_reality_keys(_=Depends(require_auth)):
    """Generate a Reality key pair (x25519)."""
    try:
        priv_key, pub_key = _xray_x25519_keypair()
        return {"private_key": priv_key, "public_key": pub_key}
    except ImportError:
        # cryptography not installed - return error
        return {"error": True, "private_key": "", "public_key": "", "note": "cryptography not installed: pip install cryptography"}

@app.get("/api/tools/reality-settings")
async def get_reality_settings(_=Depends(require_auth)):
    """Get Reality settings from global SETTINGS."""
    async with SETTINGS_LOCK:
        reality = SETTINGS.get("reality", {})
    host = get_host()
    return {
        "port": reality.get("port", 1234),
        "dest": reality.get("dest", reality.get("target", "google.com:443")),
        "target": reality.get("target", reality.get("dest", "google.com:443")),
        "sni": reality.get("sni", reality.get("server_name", host)),
        "server_name": reality.get("server_name", reality.get("sni", host)),
        "public_key": reality.get("public_key", ""),
        "short_id": reality.get("short_id", "6ba85179e30d4fc2"),
        "spiderx": reality.get("spiderx", "/"),
        "fingerprint": reality.get("fingerprint", "chrome"),
        "external_domain": reality.get("external_domain", host),
        "external_port": reality.get("external_port", 443),
        "domain": reality.get("domain", host),
        "domain_history": reality.get("domain_history", []),
    }

@app.post("/api/tools/reality-settings")
async def set_reality_settings(request: Request, _=Depends(require_auth)):
    """Save Reality settings globally."""
    body = await request.json()
    async with SETTINGS_LOCK:
        reality = SETTINGS.get("reality", {})
        if "port" in body:
            reality["port"] = int(body.get("port", 1234))
        if "dest" in body or "target" in body:
            reality["dest"] = str(body.get("dest") or body.get("target") or "google.com:443").strip()
            reality["target"] = reality["dest"]
        if "sni" in body or "server_name" in body:
            reality["sni"] = str(body.get("sni") or body.get("server_name") or get_host()).strip()
            reality["server_name"] = reality["sni"]
        if "public_key" in body:
            reality["public_key"] = str(body.get("public_key", ""))
        if "short_id" in body:
            reality["short_id"] = str(body.get("short_id", "6ba85179e30d4fc2"))
        if "spiderx" in body:
            reality["spiderx"] = str(body.get("spiderx", "/"))
        if "external_domain" in body:
            reality["external_domain"] = str(body.get("external_domain", get_host()))
        if "external_port" in body:
            reality["external_port"] = int(body.get("external_port", 443))
        if "domain" in body:
            domain_val = str(body.get("domain", "")).strip()
            if domain_val:
                reality["domain"] = domain_val
                # manage domain history (keep last 20, unique)
                history = reality.get("domain_history", [])
                if domain_val in history:
                    history.remove(domain_val)
                history.insert(0, domain_val)
                reality["domain_history"] = history[:20]
        SETTINGS["reality"] = reality
    asyncio.create_task(save_state())
    log_activity("settings", "تنظیمات Reality ذخیره شد", "ok")
    return {"ok": True, "reality": reality}

@app.get("/api/tools/settings")
async def get_global_settings(_=Depends(require_auth)):
    """Get global panel settings."""
    host = get_host()
    async with SETTINGS_LOCK:
        reality = SETTINGS.get("reality", {})
    return {
        "domain": SETTINGS.get("domain", host),
        "default_path": SETTINGS.get("default_path", "/"),
        "default_transport": SETTINGS.get("default_transport", "ws"),
        "enabled_protocols": SETTINGS.get("enabled_protocols", ["vless", "vmess", "trojan", "reality"]),
        "reality": reality,
        "domain_history": reality.get("domain_history", []),
        "xhttp_mode": SETTINGS.get("xhttp_mode", True),
        "websocket_mode": SETTINGS.get("websocket_mode", True),
        "default_connection_mode": SETTINGS.get("default_connection_mode", "ws"),
        "bg_login": SETTINGS.get("bg_login", ""),
        "bg_dashboard": SETTINGS.get("bg_dashboard", ""),
        "bg_sub": SETTINGS.get("bg_sub", ""),
        "panel_audio": SETTINGS.get("panel_audio", ""),
        "panel_audio_enabled": SETTINGS.get("panel_audio_enabled", False),
    }

@app.post("/api/tools/settings")
async def set_global_settings(request: Request, _=Depends(require_auth)):
    """Save global panel settings."""
    body = await request.json()
    async with SETTINGS_LOCK:
        if "domain" in body:
            domain_val = str(body["domain"]).strip()
            if domain_val:
                SETTINGS["domain"] = domain_val
                # update domain history in reality too
                reality = SETTINGS.get("reality", {})
                history = reality.get("domain_history", [])
                if domain_val in history:
                    history.remove(domain_val)
                history.insert(0, domain_val)
                reality["domain_history"] = history[:20]
                SETTINGS["reality"] = reality
        if "default_path" in body:
            SETTINGS["default_path"] = str(body["default_path"]).strip()
        if "default_transport" in body:
            val = str(body["default_transport"]).strip()
            if val in ("ws", "xhttp", "tcp"):
                SETTINGS["default_transport"] = val
        if "enabled_protocols" in body:
            SETTINGS["enabled_protocols"] = body["enabled_protocols"]
        if "xhttp_mode" in body:
            SETTINGS["xhttp_mode"] = bool(body["xhttp_mode"])
        if "websocket_mode" in body:
            SETTINGS["websocket_mode"] = bool(body["websocket_mode"])
        if "default_connection_mode" in body:
            val = str(body["default_connection_mode"]).strip()
            if val in ("ws", "xhttp", "tcp"):
                SETTINGS["default_connection_mode"] = val
    asyncio.create_task(save_state())
    log_activity("settings", "تنظیمات کلی ذخیره شد", "ok")
    return {"ok": True}



# ══════════════════════════════════════════════════════════════════════════════
# ADVANCED SETTINGS SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/settings")
async def get_settings(_=Depends(require_auth)):
    """Return all settings, masking the security token."""
    async with SETTINGS_LOCK:
        s = dict(SETTINGS)
        s["security_token"] = s["security_token"][:8] + "********" if s.get("security_token") else ""
    return s


@app.post("/api/settings")
async def update_settings(request: Request, _=Depends(require_auth)):
    """Update settings from any subset of fields."""
    body = await request.json()
    allowed_keys = {
        "websocket_mode", "xhttp_mode", "default_connection_mode",
        "max_ip_per_user", "bandwidth_limit_mbps", "live_monitoring",
        "auto_ip_rotation",
    }
    async with SETTINGS_LOCK:
        for k, v in body.items():
            if k in allowed_keys:
                if k == "max_ip_per_user" and isinstance(v, (int, float)):
                    SETTINGS[k] = int(v)
                elif k == "bandwidth_limit_mbps" and isinstance(v, (int, float)):
                    SETTINGS[k] = int(v)
                elif k == "default_connection_mode" and isinstance(v, str):
                    if v in ("ws", "xhttp", "tcp"):
                        SETTINGS[k] = v
                elif isinstance(v, bool):
                    SETTINGS[k] = v
    asyncio.create_task(save_state())
    log_activity("settings", "تنظیمات پیشرفته به‌روزرسانی شد", "info")
    async with SETTINGS_LOCK:
        s = dict(SETTINGS)
        s["security_token"] = s["security_token"][:8] + "********" if s.get("security_token") else ""
    return {"ok": True, "settings": s}


@app.post("/api/settings/security-token/rotate")
async def rotate_security_token(_=Depends(require_auth)):
    """Generate a new security token."""
    async with SETTINGS_LOCK:
        SETTINGS["security_token"] = secrets.token_urlsafe(16)
    asyncio.create_task(save_state())
    log_activity("settings", "توکن امنیتی جدید تولید شد", "ok")
    return {"ok": True, "security_token": SETTINGS["security_token"]}


@app.get("/api/settings/backup")
async def settings_backup(_=Depends(require_auth)):
    """Download a complete backup of the panel state as JSON.

    Includes: users, links, subs, settings, groups, inbounds, ip_pool,
    ip_blacklist, worker config and scanner files. Login credentials are excluded.
    """
    from fastapi.responses import Response
    def _without_passwords(value):
        # Never export the panel login password or any password_hash nested in state.
        if isinstance(value, dict):
            return {k: _without_passwords(v) for k, v in value.items() if k not in ("password_hash", "password")}
        if isinstance(value, list):
            return [_without_passwords(v) for v in value]
        return value

    scanned = {}
    try:
        SCANNED_DIR.mkdir(parents=True, exist_ok=True)
        for p in SCANNED_DIR.glob("*.txt"):
            scanned[p.name] = p.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        logger.warning(f"Could not collect scanner files for backup: {e}")

    backup_data = {
        "version": "2.0",
        "timestamp": datetime.now().isoformat(),
        "links": _without_passwords(dict(LINKS)),
        "users": _without_passwords(dict(USERS)),
        "subs": _without_passwords(dict(SUBS)),
        "settings": _without_passwords(dict(SETTINGS)),
        "groups": _without_passwords(dict(GROUPS)),
        "inbounds": _without_passwords(dict(INBOUNDS)),
        "ip_pool": _without_passwords(list(IP_POOL)),
        "ip_blacklist": _without_passwords(list(IP_BLACKLIST)),
        "worker": _without_passwords(dict(WORKER)),
        "scanned_files": scanned,
    }
    json_bytes = json.dumps(backup_data, ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"spider-panel-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    return Response(
        content=json_bytes,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@app.post("/api/settings/restore")
async def settings_restore(request: Request, _=Depends(require_auth)):
    """Restore panel state from a previously downloaded backup JSON file.

    Expects multipart/form-data with a 'file' field containing the backup JSON.
    WARNING: This will completely replace all panel data (users, links, inbounds, etc).
    """
    try:
        form = await request.form()
        file = form.get("file")
        if not file or not hasattr(file, "read"):
            raise HTTPException(status_code=400, detail="فایل بکاپ ارسال نشده است")

        content = await file.read()
        if isinstance(content, str):
            content = content.encode("utf-8")

        data = json.loads(content.decode("utf-8"))

        # Validate backup structure
        required_keys = ["users", "links", "subs", "settings", "groups", "inbounds",
                         "ip_pool", "ip_blacklist", "worker"]
        for key in required_keys:
            if key not in data:
                raise HTTPException(status_code=400, detail=f"بکاپ ناقص است: فیلد {key} وجود ندارد")

        # Restore all state
        async with LINKS_LOCK:
            LINKS.clear()
            LINKS.update(data.get("links", {}))
        async with USERS_LOCK:
            USERS.clear()
            USERS.update(data.get("users", {}))
        async with SUBS_LOCK:
            SUBS.clear()
            SUBS.update(data.get("subs", {}))
        async with SETTINGS_LOCK:
            SETTINGS.clear()
            SETTINGS.update(data.get("settings", {}))
        async with GROUPS_LOCK:
            GROUPS.clear()
            GROUPS.update(data.get("groups", {}))
        async with INBOUNDS_LOCK:
            INBOUNDS.clear()
            INBOUNDS.update(data.get("inbounds", {}))

        IP_POOL.clear()
        IP_POOL.extend(data.get("ip_pool", []))
        IP_BLACKLIST.clear()
        IP_BLACKLIST.update(data.get("ip_blacklist", []))

        async with WORKER_LOCK:
            WORKER.clear()
            WORKER.update(data.get("worker", {}))

        # Login password/secret is intentionally NOT restored. Keeping the
        # current server secret guarantees the existing password remains valid.

        # Restore scanner files when present; tolerate older backups without them.
        scanned_payload = data.get("scanned_files") or {}
        if isinstance(scanned_payload, dict):
            SCANNED_DIR.mkdir(parents=True, exist_ok=True)
            for name, content in scanned_payload.items():
                if not re.fullmatch(r"[A-Za-z0-9_-]+\.txt", str(name)):
                    continue
                (SCANNED_DIR / str(name)).write_text(str(content or ""), encoding="utf-8")

        # Rebuild indexes
        _rebuild_path_index()
        _migrate_user_links()
        _migrate_user_uuids()
        _rebuild_path_index()

        asyncio.create_task(save_state())
        log_activity("settings", "بکاپ بازیابی شد — تمام داده‌ها جایگزین شدند", "warn")

        return {"ok": True, "detail": "بکاپ با موفقیت بازیابی شد. پنل را ریفرش کنید."}

    except HTTPException:
        raise
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="فایل JSON معتبر نیست")
    except Exception as e:
        logger.error(f"Restore failed: {e}")
        raise HTTPException(status_code=500, detail=f"خطا در بازیابی: {str(e)}")

# ── Self-update (Railway: refresh the deployed repo from GitHub) ─────────────
# Railway builds a Docker image from the repo at deploy time; the running
# container has no git history, so "Update" re-clones the upstream repo over
# /app. The new code goes live when the container restarts/redeploys.
PANEL_REPO_URL = "https://github.com/amirh00sain/SpiderPanel"
APP_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
UPDATE_STATE: dict = {"running": False, "log": [], "done": False, "ok": None}
UPDATE_LOCK = asyncio.Lock()


def _update_log(msg: str):
    UPDATE_STATE["log"].append(str(msg)[:300])
    if len(UPDATE_STATE["log"]) > 200:
        del UPDATE_STATE["log"][:-200]
    logger.info(f"[self-update] {msg}")


async def _run_self_update():
    """Re-clone the upstream repo into /app (git-safe), preserving local data.

    Runs blocking subprocess/shutil work in executor threads. Panel state
    (users, settings, worker) lives in DATA_DIR and is NOT touched.
    """
    import shutil as _shutil
    import subprocess as _subprocess
    try:
        _update_log("شروع بروزرسانی از GitHub...")
        backup = Path("/tmp/spider_app_backup")
        if backup.exists():
            _shutil.rmtree(backup, ignore_errors=True)
        # Backup things we can't re-download (scanned results, xray binary).
        keep_dirs = []
        for name in ("data/scanned", "bin"):
            src = APP_DIR / name
            if src.exists():
                dst = backup / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    _shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    _shutil.copy2(src, dst)
                keep_dirs.append(name)
        if keep_dirs:
            _update_log(f"بکاپ گرفته شد: {', '.join(keep_dirs)}")

        tmp_clone = Path("/tmp/spider_repo_new")
        if tmp_clone.exists():
            _shutil.rmtree(tmp_clone, ignore_errors=True)

        def _run(cmd):
            proc = _subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

        loop = asyncio.get_running_loop()
        code, out = await loop.run_in_executor(
            None, _run, ["git", "clone", "--depth", "1", PANEL_REPO_URL, str(tmp_clone)])
        if code != 0:
            raise RuntimeError(f"git clone failed: {out.strip()[:200]}")
        _update_log(f"ریپو کلون شد ({PANEL_REPO_URL})")

        def _copy_tree():
            for item in tmp_clone.iterdir():
                if item.name == ".git":
                    continue
                dst = APP_DIR / item.name
                try:
                    if item.is_dir():
                        if dst.exists():
                            _shutil.rmtree(dst, ignore_errors=True)
                        _shutil.copytree(item, dst)
                    else:
                        _shutil.copy2(item, dst)
                except Exception as e:
                    _update_log(f"خطا در کپی {item.name}: {e}")

        await loop.run_in_executor(None, _copy_tree)
        _update_log("فایل‌های جدید کپی شد")

        def _restore():
            for name in keep_dirs:
                src = backup / name
                dst = APP_DIR / name
                if src.exists() and not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if src.is_dir():
                        _shutil.copytree(src, dst)
                    else:
                        _shutil.copy2(src, dst)

        await loop.run_in_executor(None, _restore)
        _shutil.rmtree(tmp_clone, ignore_errors=True)
        _shutil.rmtree(backup, ignore_errors=True)

        UPDATE_STATE["ok"] = True
        UPDATE_STATE["done"] = True
        UPDATE_STATE["running"] = False
        _update_log("بروزرسانی کامل شد — برای اعمال شدن، پنل را ری‌استارت کنید (Railway)")
        log_activity("settings", f"پنل از GitHub بروزرسانی شد ({PANEL_REPO_URL})", "ok")
        return True
    except Exception as e:
        UPDATE_STATE["ok"] = False
        UPDATE_STATE["done"] = True
        UPDATE_STATE["running"] = False
        _update_log(f"خطا: {e}")
        log_activity("settings", f"بروزرسانی ناموفق: {e}", "err")
        return False


@app.post("/api/settings/update")
async def settings_update(_=Depends(require_auth)):
    """Start a self-update from GitHub (re-clone repo over /app)."""
    async with UPDATE_LOCK:
        if UPDATE_STATE.get("running"):
            return JSONResponse(status_code=409, content={"ok": False, "detail": "بروزرسانی قبلاً در حال اجراست"})
        UPDATE_STATE.update({"running": True, "log": [], "done": False, "ok": None})
    asyncio.create_task(_run_self_update())
    return {"ok": True, "detail": "بروزرسانی شروع شد"}


@app.get("/api/settings/update/status")
async def settings_update_status(_=Depends(require_auth)):
    """Poll self-update progress (log lines + done flag)."""
    return {
        "ok": True,
        "running": bool(UPDATE_STATE.get("running")),
        "done": bool(UPDATE_STATE.get("done")),
        "success": UPDATE_STATE.get("ok"),
        "log": list(UPDATE_STATE.get("log") or []),
    }


# ══════════════════════════════════════════════════════════════════════════════
# GROUP MANAGEMENT SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/groups")
async def list_groups(_=Depends(require_auth)):
    """List all groups with user count."""
    async with GROUPS_LOCK:
        snap = dict(GROUPS)
    result = []
    for gid, g in snap.items():
        user_ids = g.get("user_ids", [])
        result.append({
            "group_id": gid,
            "name": g.get("name"),
            "description": g.get("description", ""),
            "user_count": len(user_ids),
            "user_ids": user_ids,
            "speed_limit": g.get("speed_limit", 0),
            "traffic_limit": g.get("traffic_limit", 0),
            "expire_days": g.get("expire_days", 0),
            "ip_pool": g.get("ip_pool", []),
            "rules": g.get("rules", {}),
            "created_at": g.get("created_at"),
        })
    result.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {"groups": result}


@app.post("/api/groups")
async def create_group(request: Request, _=Depends(require_auth)):
    """Create a new group."""
    body = await request.json()
    name = (body.get("name") or "گروه جدید").strip()[:60]
    description = (body.get("description") or "").strip()[:200]
    speed_limit = int(body.get("speed_limit") or 0)
    traffic_limit = int(body.get("traffic_limit") or 0)
    expire_days = int(body.get("expire_days") or 0)

    group_id = generate_short_id()
    async with GROUPS_LOCK:
        GROUPS[group_id] = {
            "name": name,
            "description": description,
            "user_ids": [],
            "ip_pool": body.get("ip_pool", []),
            "rules": body.get("rules", {}),
            "speed_limit": speed_limit,
            "traffic_limit": traffic_limit,
            "expire_days": expire_days,
            "created_at": datetime.now().isoformat(),
        }
    asyncio.create_task(save_state())
    log_activity("group", f"گروه «{name}» ساخته شد", "ok")
    return {"ok": True, "group_id": group_id, **GROUPS[group_id]}


@app.patch("/api/groups/{group_id}")
async def update_group(group_id: str, request: Request, _=Depends(require_auth)):
    """Update an existing group."""
    body = await request.json()
    async with GROUPS_LOCK:
        g = GROUPS.get(group_id)
        if not g:
            raise HTTPException(status_code=404, detail="group not found")
        if "name" in body:
            g["name"] = str(body["name"])[:60]
        if "description" in body:
            g["description"] = str(body["description"])[:200]
        if "speed_limit" in body:
            g["speed_limit"] = int(body["speed_limit"])
        if "traffic_limit" in body:
            g["traffic_limit"] = int(body["traffic_limit"])
        if "expire_days" in body:
            g["expire_days"] = int(body["expire_days"])
        if "ip_pool" in body:
            g["ip_pool"] = list(body["ip_pool"])
        if "rules" in body:
            g["rules"] = dict(body["rules"])
    asyncio.create_task(save_state())
    log_activity("group", f"گروه «{g.get('name', group_id)}» ویرایش شد", "info")
    return {"ok": True}


@app.delete("/api/groups/{group_id}")
async def delete_group(group_id: str, _=Depends(require_auth)):
    """Delete a group and unlink all users from it."""
    async with GROUPS_LOCK:
        g = GROUPS.pop(group_id, None)
        if not g:
            raise HTTPException(status_code=404, detail="group not found")
        name = g.get("name", group_id)
        user_ids = g.get("user_ids", [])
    asyncio.create_task(save_state())
    log_activity("group", f"گروه «{name}» حذف شد", "warn")
    return {"ok": True, "deleted": group_id, "unlinked_users": len(user_ids)}


@app.post("/api/groups/{group_id}/users")
async def add_user_to_group(group_id: str, request: Request, _=Depends(require_auth)):
    """Add a user to a group."""
    body = await request.json()
    user_id = str(body.get("user_id", ""))
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required")

    async with USERS_LOCK:
        if user_id not in USERS:
            raise HTTPException(status_code=404, detail="user not found")

    async with GROUPS_LOCK:
        g = GROUPS.get(group_id)
        if not g:
            raise HTTPException(status_code=404, detail="group not found")
        ids = g.setdefault("user_ids", [])
        if user_id not in ids:
            ids.append(user_id)
    asyncio.create_task(save_state())
    log_activity("group", f"کاربر «{user_id}» به گروه «{g.get('name', group_id)}» اضافه شد", "info")
    return {"ok": True}


@app.delete("/api/groups/{group_id}/users/{user_id}")
async def remove_user_from_group(group_id: str, user_id: str, _=Depends(require_auth)):
    """Remove a user from a group."""
    async with GROUPS_LOCK:
        g = GROUPS.get(group_id)
        if not g:
            raise HTTPException(status_code=404, detail="group not found")
        ids = g.get("user_ids", [])
        if user_id in ids:
            ids.remove(user_id)
        else:
            raise HTTPException(status_code=404, detail="user not in group")
    asyncio.create_task(save_state())
    log_activity("group", f"کاربر «{user_id}» از گروه «{g.get('name', group_id)}» حذف شد", "info")
    return {"ok": True}


@app.get("/api/groups/{group_id}/subscription")
async def group_subscription(group_id: str, _=Depends(require_auth)):
    """Generate subscription link for a group — base64-encoded configs of all active users."""
    async with GROUPS_LOCK:
        g = GROUPS.get(group_id)
        if not g:
            raise HTTPException(status_code=404, detail="group not found")
        user_ids = list(g.get("user_ids", []))

    async with USERS_LOCK:
        snap = dict(USERS)

    configs = []
    for uid in user_ids:
        u = snap.get(uid)
        if u and is_user_allowed(u):
            cfg = generate_user_config(uid, u, u.get("inbound_id"))
            if cfg:
                configs.append(cfg)

    if not configs:
        raise HTTPException(status_code=404, detail="no active users in group")

    content = base64.b64encode("\n".join(configs).encode()).decode()
    host = SETTINGS.get("domain") or get_host()
    return {
        "group_id": group_id,
        "group_name": g.get("name"),
        "active_users": len(configs),
        "total_users": len(user_ids),
        "subscription_url": f"https://{host}/api/groups/{group_id}/subscription",
        "encoded_config": content,
    }


# ══════════════════════════════════════════════════════════════════════════════
# IP POOL MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/ips")
async def list_ips(_=Depends(require_auth)):
    """List all IPs in the pool with status."""
    async with IP_POOL_LOCK:
        ips = list(IP_POOL)
    async with IP_BLACKLIST_LOCK:
        bl = set(IP_BLACKLIST)
    for entry in ips:
        entry["blacklisted"] = entry["ip"] in bl
    return {"ips": ips, "total": len(ips), "blacklisted_count": len(bl)}


@app.post("/api/ips")
async def add_ip(request: Request, _=Depends(require_auth)):
    """Add an IP to the pool."""
    body = await request.json()
    ip_addr = (body.get("ip") or "").strip()
    if not ip_addr:
        raise HTTPException(status_code=400, detail="ip is required")
    async with IP_POOL_LOCK:
        if any(e["ip"] == ip_addr for e in IP_POOL):
            raise HTTPException(status_code=409, detail="ip already in pool")
        entry = {
            "ip": ip_addr,
            "status": body.get("status", "active"),
            "latency_ms": body.get("latency_ms", 0),
            "location": body.get("location", "Unknown"),
            "assigned_user": body.get("assigned_user"),
            "last_check": datetime.now().isoformat(),
        }
        IP_POOL.append(entry)
    asyncio.create_task(save_state())
    log_activity("ip", f"IP «{ip_addr}» به مخزن اضافه شد", "info")
    return {"ok": True, "ip": entry}


@app.delete("/api/ips")
async def remove_ip(request: Request, _=Depends(require_auth)):
    """Remove an IP from the pool."""
    body = await request.json()
    ip_addr = (body.get("ip") or "").strip()
    if not ip_addr:
        raise HTTPException(status_code=400, detail="ip is required")
    async with IP_POOL_LOCK:
        before = len(IP_POOL)
        IP_POOL[:] = [e for e in IP_POOL if e["ip"] != ip_addr]
        if len(IP_POOL) == before:
            raise HTTPException(status_code=404, detail="ip not found in pool")
    asyncio.create_task(save_state())
    log_activity("ip", f"IP «{ip_addr}» از مخزن حذف شد", "warn")
    return {"ok": True, "deleted": ip_addr}


@app.post("/api/ips/blacklist")
async def blacklist_ip(request: Request, _=Depends(require_auth)):
    """Add an IP to the blacklist."""
    body = await request.json()
    ip_addr = (body.get("ip") or "").strip()
    if not ip_addr:
        raise HTTPException(status_code=400, detail="ip is required")
    async with IP_BLACKLIST_LOCK:
        IP_BLACKLIST.add(ip_addr)
    asyncio.create_task(save_state())
    log_activity("ip", f"IP «{ip_addr}» به لیست سیاه اضافه شد", "warn")
    return {"ok": True, "blacklisted": ip_addr}


@app.delete("/api/ips/blacklist")
async def unblacklist_ip(request: Request, _=Depends(require_auth)):
    """Remove an IP from the blacklist."""
    body = await request.json()
    ip_addr = (body.get("ip") or "").strip()
    if not ip_addr:
        raise HTTPException(status_code=400, detail="ip is required")
    async with IP_BLACKLIST_LOCK:
        if ip_addr not in IP_BLACKLIST:
            raise HTTPException(status_code=404, detail="ip not in blacklist")
        IP_BLACKLIST.discard(ip_addr)
    asyncio.create_task(save_state())
    log_activity("ip", f"IP «{ip_addr}» از لیست سیاه خارج شد", "info")
    return {"ok": True, "removed": ip_addr}


@app.post("/api/ips/assign")
async def assign_ip_to_user(request: Request, _=Depends(require_auth)):
    """Assign an IP from the pool to a user."""
    body = await request.json()
    user_id = str(body.get("user_id", ""))
    ip_addr = str(body.get("ip", ""))
    if not user_id or not ip_addr:
        raise HTTPException(status_code=400, detail="user_id and ip are required")

    async with USERS_LOCK:
        if user_id not in USERS:
            raise HTTPException(status_code=404, detail="user not found")

    async with IP_POOL_LOCK:
        entry = next((e for e in IP_POOL if e["ip"] == ip_addr), None)
        if not entry:
            raise HTTPException(status_code=404, detail="ip not found in pool")
        entry["assigned_user"] = user_id
        entry["status"] = "assigned"

    async with USER_IP_MAP_LOCK:
        USER_IP_MAP[user_id].add(ip_addr)

    asyncio.create_task(save_state())
    log_activity("ip", f"IP «{ip_addr}» به کاربر «{user_id}» اختصاص یافت", "info")
    return {"ok": True, "user_id": user_id, "ip": ip_addr}


@app.get("/api/ips/test")
async def test_ips(_=Depends(require_auth)):
    """Return simulated ping results for pool IPs."""
    import random
    async with IP_POOL_LOCK:
        ips = list(IP_POOL)
    results = []
    for entry in ips:
        latency = random.randint(20, 350)
        status = "ok" if latency < 300 else "timeout"
        results.append({
            "ip": entry["ip"],
            "latency_ms": latency,
            "status": status,
            "location": entry.get("location", "Unknown"),
            "assigned_user": entry.get("assigned_user"),
            "tested_at": datetime.now().isoformat(),
        })
    results.sort(key=lambda x: x["latency_ms"])
    return {"results": results, "tested_at": datetime.now().isoformat()}


@app.get("/api/ips/check")
async def check_ip(request: Request, _=Depends(require_auth)):
    """Check if an IP is in the blacklist."""
    ip_addr = request.query_params.get("ip", "").strip()
    if not ip_addr:
        raise HTTPException(status_code=400, detail="ip query param is required")
    async with IP_BLACKLIST_LOCK:
        blacklisted = ip_addr in IP_BLACKLIST
    return {"ip": ip_addr, "blacklisted": blacklisted}


# ══════════════════════════════════════════════════════════════════════════════
# LIVE SERVER STATS — Helpers & WebSocket
# ══════════════════════════════════════════════════════════════════════════════

ws_client_count = 0
WS_LIVE_CLIENTS: set = set()

def get_live_stats() -> dict:
    """Get real server stats using psutil with fallback."""
    conn_count = len(connections)
    try:
        import psutil as _ps
        cpu_pct = round(_ps.cpu_percent(interval=0.3), 1)
        mem = _ps.virtual_memory()
        ram_pct = round(mem.percent, 1)
        ram_used_gb = round(mem.used / (1024**3), 2)
        ram_total_gb = round(mem.total / (1024**3), 2)
        disk = _ps.disk_usage('/')
        disk_pct = round(disk.percent, 1)
        disk_used_gb = round(disk.used / (1024**3), 2)
        disk_total_gb = round(disk.total / (1024**3), 2)
        net = _ps.net_io_counters()
        net_sent_mb = round(net.bytes_sent / (1024**2), 2)
        net_recv_mb = round(net.bytes_recv / (1024**2), 2)
        network_mbps = round(max((net.bytes_sent + net.bytes_recv) / (1024**2) / max(uptime_secs(), 1) * 8, 0.5), 2)
    except Exception:
        cpu_pct = round(min(conn_count * 0.3 + 5, 95), 1)
        ram_pct = round(min(45 + len(USERS) * 0.5 + conn_count * 0.1, 95), 1)
        ram_used_gb = round(ram_pct / 100 * 8, 2)
        ram_total_gb = 8
        disk_pct = round(min(25 + len(LINKS) * 0.02 + len(USERS) * 0.1, 90), 1)
        disk_used_gb = round(disk_pct / 100 * 50, 2)
        disk_total_gb = 50
        net_sent_mb = 0
        net_recv_mb = 0
        network_mbps = 2.5
    # Calculate total traffic from all users
    total_used = sum(u.get("traffic_used_bytes", 0) for u in USERS.values())
    total_limit = sum(u.get("traffic_limit_bytes", 0) for u in USERS.values())
    return {
        "cpu_percent": max(0, cpu_pct),
        "ram_percent": max(0, ram_pct),
        "ram_used_gb": ram_used_gb,
        "ram_total_gb": ram_total_gb,
        "disk_percent": max(0, disk_pct),
        "disk_used_gb": disk_used_gb,
        "disk_total_gb": disk_total_gb,
        "network_mbps": network_mbps,
        "net_sent_mb": net_sent_mb,
        "net_recv_mb": net_recv_mb,
        "active_connections": conn_count,
        "ws_connections": ws_client_count,
        "total_users": len(USERS),
        "total_traffic_used_tb": round(total_used / (1024**4), 3),
        "total_traffic_limit_tb": round(total_limit / (1024**4), 3) if total_limit > 0 else 0,
        "uptime": uptime(),
        "uptime_seconds": uptime_secs(),
        "timestamp": datetime.now().isoformat(),
    }


@app.websocket("/ws/live")
async def websocket_live_stats(websocket: WebSocket):
    global ws_client_count
    await websocket.accept()
    ws_client_count += 1
    WS_LIVE_CLIENTS.add(websocket)
    try:
        while True:
            try:
                stats_data = get_live_stats()
                await websocket.send_json(stats_data)
                await asyncio.sleep(2)
            except WebSocketDisconnect:
                break
            except Exception:
                break
    finally:
        ws_client_count = max(0, ws_client_count - 1)
        WS_LIVE_CLIENTS.discard(websocket)


# ══════════════════════════════════════════════════════════════════════════════
# IP LIMIT ENFORCEMENT
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/users/{user_id}/ip-check")
async def check_user_ip_limit(user_id: str, _=Depends(require_auth)):
    """Check if a user is within their IP limit."""
    async with USERS_LOCK:
        u = USERS.get(user_id)
        if not u:
            raise HTTPException(status_code=404, detail="user not found")
        username = u.get("username")

    async with USER_IP_MAP_LOCK:
        ip_count = len(USER_IP_MAP.get(user_id, set()))

    async with SETTINGS_LOCK:
        max_ip = SETTINGS.get("max_ip_per_user", 3)

    within_limit = ip_count < max_ip
    return {
        "user_id": user_id,
        "username": username,
        "current_ip_count": ip_count,
        "max_ip_per_user": max_ip,
        "within_limit": within_limit,
        "ips": list(USER_IP_MAP.get(user_id, set())),
    }


async def _resolve_user_id_for_link(uuid: str) -> str | None:
    """Map a link/config UUID back to its owning user id.

    Priority: LINKS[link].user_id (explicit link) → USERS entry whose
    config_uuid matches (user-driven links) → a USERS key equal to the uuid.
    """
    async with LINKS_LOCK:
        link = LINKS.get(uuid)
        if link and link.get("user_id"):
            return link["user_id"]
    for uid, u in USERS.items():
        if (u.get("config_uuid") or "") == uuid:
            return uid
    if uuid in USERS:
        return uuid
    return None


def _parse_proxy_entry(entry: str) -> dict | None:
    """Parse a proxy entry like the BPB worker does.

    Accepts: ip:port, user:pass@ip:port, socks5://user:pass@ip:port,
    http://ip:port, https://ip:port. Auth may be plain "user:pass" or a
    base64 blob. Returns {protocol, username, password, hostname, port}
    or None on invalid input.
    """
    import base64 as _b64
    import re as _re

    if not entry:
        return None
    e = str(entry).strip()
    # Strip leading protocol
    proto = "http"
    m = _re.match(r"^(socks5|socks4|http|https|turn|sstp)://", e, _re.I)
    if m:
        proto = m.group(1).lower()
        e = e[m.end():]
    # Drop fragment
    e = e.split("#")[0].strip()

    at = e.rfind("@")
    hostpart = e[at + 1:] if at != -1 else e
    authpart = e[:at] if at != -1 else ""
    username = password = None
    if authpart:
        # base64-encoded auth (worker supports it)
        b64re = _re.compile(r"^(?:[A-Z0-9+/]{4})*(?:[A-Z0-9+/]{2}==|[A-Z0-9+/]{3}=)?$", _re.I)
        a = authpart.replace("%3D", "=")
        if ":" not in a and b64re.match(a):
            try:
                a = _b64.b64decode(a).decode("utf-8", "ignore")
            except Exception:
                a = authpart
        if ":" in a:
            username, password = a.split(":", 1)
        else:
            return None
    if hostpart.startswith("["):
        # IPv6 [::1]:port
        if "]:" in hostpart:
            h, _, rest = hostpart.partition("]:")
            hostname = h + "]"
            pport = rest.strip()
        else:
            hostname, pport = hostpart, ""
    elif ":" in hostpart:
        hostname, _, pport = hostpart.rpartition(":")
    else:
        hostname, pport = hostpart, ""
    try:
        port = int(pport) if pport else 80
    except ValueError:
        return None
    if not hostname:
        return None
    return {"protocol": proto, "username": username, "password": password,
            "hostname": hostname, "port": port}


async def _close_writer_safely(wtr):
    try:
        wtr.close()
        await wtr.wait_closed()
    except Exception:
        pass


async def _socks5_connect(proxy: dict, address: str, port: int):
    """SOCKS5 CONNECT through the proxy, mirroring the worker's socks5Connect."""
    import socket as _sock
    rdr, wtr = await asyncio.wait_for(
        asyncio.open_connection(proxy["hostname"], proxy["port"]), timeout=4.0
    )
    try:
        # Method negotiation
        methods = bytes([0x05, 0x02, 0x00, 0x02]) if proxy.get("username") else bytes([0x05, 0x01, 0x00])
        wtr.write(methods)
        await wtr.drain()
        resp = await asyncio.wait_for(rdr.readexactly(2), timeout=4.0)
        if resp[1] == 0x02:
            if not proxy.get("username"):
                raise ConnectionError("socks5 requires auth")
            ub = proxy["username"].encode()
            pb = proxy["password"].encode()
            wtr.write(bytes([0x01, len(ub)]) + ub + bytes([len(pb)]) + pb)
            await wtr.drain()
            auth = await asyncio.wait_for(rdr.readexactly(2), timeout=4.0)
            if auth[1] != 0x00:
                raise ConnectionError("socks5 auth failed")
        elif resp[1] != 0x00:
            raise ConnectionError(f"socks5 unsupported auth method {resp[1]}")
        return await _socks5_connect_send(proxy, address, port, rdr, wtr, _sock)
    except BaseException:
        await _close_writer_safely(wtr)
        raise


async def _socks5_connect_send(proxy: dict, address: str, port: int, rdr, wtr, _sock):
    """Send the SOCKS5 CONNECT packet and consume the full reply."""
    # CONNECT
    try:
        try:
            hb = _sock.inet_aton(address)
            atyp = 0x01
        except OSError:
            if ":" in address:  # IPv6
                atyp, hb = 0x04, _sock.inet_pton(_sock.AF_INET6, address)
            else:  # domain
                eb = address.encode()
                atyp, hb = 0x03, bytes([len(eb)]) + eb
        pkt = bytes([0x05, 0x01, 0x00, atyp]) + hb + bytes([port >> 8, port & 0xff])
        wtr.write(pkt)
        await wtr.drain()
        resp = await asyncio.wait_for(rdr.readexactly(4), timeout=4.0)
        if resp[1] != 0x00:
            raise ConnectionError(f"socks5 connect failed code={resp[1]}")
        # Consume the reply's BND.ADDR + BND.PORT so those bytes don't leak into
        # the relay's first read of the tunneled stream (RFC 1928 reply = VER REP
        # RSV ATYP BND.ADDR BND.PORT). ATYP of the reply drives the length.
        ratyp = resp[3]
        if ratyp == 0x01:
            await asyncio.wait_for(rdr.readexactly(4 + 2), timeout=4.0)
        elif ratyp == 0x04:
            await asyncio.wait_for(rdr.readexactly(16 + 2), timeout=4.0)
        elif ratyp == 0x03:
            ln = (await asyncio.wait_for(rdr.readexactly(1), timeout=4.0))[0]
            await asyncio.wait_for(rdr.readexactly(ln + 2), timeout=4.0)
        return rdr, wtr
    except BaseException:
        await _close_writer_safely(wtr)
        raise


async def _http_connect(proxy: dict, address: str, port: int, tls: bool = False):
    """HTTP CONNECT through the proxy, mirroring the worker's httpConnect."""
    rdr, wtr = await asyncio.wait_for(
        asyncio.open_connection(proxy["hostname"], proxy["port"]), timeout=4.0
    )
    try:
        host_header = f"[{address}]" if ":" in address else address
        auth = ""
        if proxy.get("username"):
            import base64 as _b64
            token = _b64.b64encode(f"{proxy['username']}:{proxy.get('password') or ''}".encode()).decode()
            auth = f"Proxy-Authorization: Basic {token}\r\n"
        req = (f"CONNECT {host_header}:{port} HTTP/1.1\r\n"
               f"Host: {host_header}:{port}\r\n{auth}"
               f"User-Agent: Mozilla/5.0\r\nConnection: keep-alive\r\n\r\n")
        wtr.write(req.encode())
        await wtr.drain()
        status = await asyncio.wait_for(rdr.readline(), timeout=4.0)
        # Skip headers
        while True:
            line = await asyncio.wait_for(rdr.readline(), timeout=8.0)
            if line in (b"\r\n", b"\n", b""):
                break
        if not re.search(rb"HTTP/\d\.\d 200", status):
            raise ConnectionError(f"http connect failed {status.decode().strip()}")
        return rdr, wtr
    except BaseException:
        await _close_writer_safely(wtr)
        raise


_PROXY_DOH_CACHE: dict = {}   # (host, type) -> list[str]


async def _resolve_proxy_targets(token: str):
    """Mirror the BPB worker's 解析地址端口.

    Given a proxy token ("IP:PORT", ".tp...", or a DOMAIN that carries proxy
    IPs in its A records / TXT), expand it to a sorted list of (ip, port).
    The worker uses DoH (cloudflare-dns.com); here we use plain DNS A records,
    which is equivalent for turning a domain into its proxy IP list.
    """
    import ipaddress as _ipa
    import random as _rnd

    token = str(token or "").strip()
    if not token:
        return []
    host = token
    port = 443
    # port from "host:port"
    if "]" in host:
        if "]:" in host:
            host, _, resto = host.partition("]:")
            host = host + "]"
            try:
                port = int(resto.strip())
            except ValueError:
                port = 443
    elif ":" in host:
        host, _, resto = host.rpartition(":")
        try:
            port = int(resto.strip())
        except ValueError:
            port = 443
    # .tpN at the end -> port override like worker (domain.tp8443 -> :8443)
    tp_m = re.search(r"\.tp(\d+)$", host)
    if tp_m:
        port = int(tp_m.group(1))
        host = re.sub(r"\.tp\d+$", "", host)

    def _is_ip(h: str) -> bool:
        try:
            _ipa.ip_address(h.replace("[", "").replace("]", ""))
            return True
        except ValueError:
            return False

    if _is_ip(host):
        return [[host.replace("[", "").replace("]", ""), port]]

    # Domain -> DNS A records (worker would DoH TXT/A/AAAA first; A is enough)
    cache_key = (host.lower(), "A")
    cache_hit = _PROXY_DOH_CACHE.get(cache_key)
    if cache_hit and cache_hit["expires"] > time.time():
        return [[ip, port] for ip in cache_hit["ips"]]
    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: [i[4][0] for i in socket.getaddrinfo(host, None)]
            ),
            timeout=3.0,
        )
        ips = list(dict.fromkeys(infos))
        # Only cache non-empty results so a transient DNS failure retries next time.
        if ips:
            _PROXY_DOH_CACHE[cache_key] = {"ips": ips, "expires": time.time() + 300}
        return [[ip, port] for ip in ips]
    except Exception:
        # No A records / DNS failure — worker falls back to the domain name.
        return [[host, port]]


def _expand_proxy_tokens(tokens):
    """Mirror worker's 整理成数组: split on comma/tab/newline/quote -> clean list."""
    if isinstance(tokens, (list, tuple)):
        raw = ",".join(str(t) for t in tokens)
    else:
        raw = str(tokens or "")
    cleaned = re.sub(r'[\t"\'\r\n]+', ",", raw)
    cleaned = re.sub(r",+", ",", cleaned)
    return [p.strip() for p in cleaned.split(",") if p.strip()]


def _build_vless_connect_header(uuid: str, address: str, port: int) -> bytes:
    """Rebuild a VLESS CONNECT header for a raw relay (ZEUS-style).

    The proxy's relay reads this header, connects to `address:port`, and then
    tunnels the remaining payload (which the caller writes right after this
    header). Format: version(1) uuid(16) opt_len(1) cmd(1) port(2) atype(1) addr.
    """
    import socket as _sock
    try:
        hb = _sock.inet_aton(address)
        atype, ab = 0x01, hb
    except OSError:
        if ":" in address:
            atype, ab = 0x04, _sock.inet_pton(_sock.AF_INET6, address)
        else:
            eb = address.encode()
            atype, ab = 0x03, bytes([len(eb)]) + eb
    raw_uuid = uuid.replace("-", "")
    if len(raw_uuid) != 32:
        raw_uuid = (raw_uuid + "0" * 32)[:32]
    ubytes = bytes.fromhex(raw_uuid)
    return (b"\x00" + ubytes + b"\x00\x01"
            + bytes([port >> 8, port & 0xff]) + bytes([atype]) + ab)


async def proxy_connect(uuid: str, address: str, port: int, proxy_override: str = None):
    """Open an outbound TCP connection, routed through the user's proxy IP.

    Mirrors the BPB/ZEUS worker approach: for a working HTTP/SOCKS5 proxy we
    CONNECT through it; if the picked entry is a raw relay (clean Cloudflare
    IP), we fall back to ZEUS-style: raw-connect to proxy:port and re-send a
    VLESS CONNECT header so the relay forwards to the target. Only when the
    proxy fails entirely do we fall back to a direct connection.

    proxy_override: when the config path carries /proxyIP/{ip:port}/, that
    exact proxy is used instead of a random pick from the user's list.
    """
    import random as _rnd

    user_id = await _resolve_user_id_for_link(uuid)
    entry = None
    if proxy_override:
        entry = proxy_override.strip()
    elif user_id:
        async with USERS_LOCK:
            u = USERS.get(user_id)
            if u:
                plist = u.get("proxy_ips") or []
                if plist:
                    entry = _rnd.choice(plist)

    if entry:
        # Expand the picked token (IP or domain) to concrete targets like the
        # worker's 解析地址端口, then try each until one connects.
        targets = await _resolve_proxy_targets(entry)
        for tg in targets:
            te = f"{tg[0]}:{tg[1]}"
            proxy = _parse_proxy_entry(te)
            if not proxy:
                continue
            got = await _try_proxy_order(proxy, address, port)
            if got:
                return got
            # ZEUS-style raw relay: connect straight to proxy:port and send a
            # reconstructed VLESS CONNECT header; the relay forwards to target.
            rwtr = None
            try:
                rrdr, rwtr = await asyncio.wait_for(
                    asyncio.open_connection(proxy["hostname"], proxy["port"]), timeout=8.0
                )
                hdr = _build_vless_connect_header(uuid, address, port)
                rwtr.write(hdr)
                await rwtr.drain()
                logger.info(f"proxy_connect[raw-relay] via {proxy['hostname']}:{proxy['port']} → {address}:{port}")
                return rrdr, rwtr
            except Exception:
                await _close_writer_safely(rwtr)
                continue
        logger.warning(f"proxy_connect all targets failed for {entry}")
        return await asyncio.wait_for(asyncio.open_connection(address, port), timeout=10.0)

    return await asyncio.wait_for(asyncio.open_connection(address, port), timeout=10.0)


def _order_for(proto: str):
    if proto in ("socks5", "socks4"):
        return ["socks5", "http"]
    return ["http", "socks5"]


async def _try_proxy_order(proxy, address, port):
    for p in _order_for(proxy.get("protocol", "http")):
        try:
            if p == "socks5":
                rdr, wtr = await _socks5_connect(proxy, address, port)
            else:
                rdr, wtr = await _http_connect(proxy, address, port,
                                               tls=(proxy.get("protocol") == "https"))
            logger.info(f"proxy_connect[{p}] via {proxy['hostname']}:{proxy['port']} → {address}:{port}")
            return rdr, wtr
        except Exception:
            continue
    return None


async def _link_max_ip(uuid: str) -> int:
    """Per-user concurrent_connections, falling back to global max_ip_per_user."""
    user_id = await _resolve_user_id_for_link(uuid)
    if user_id:
        async with USERS_LOCK:
            u = USERS.get(user_id)
        if u:
            _cc = u.get("concurrent_connections")
            return int(_cc) if _cc is not None else 3
    # No registered user → use global setting (covers raw links / group links)
    async with SETTINGS_LOCK:
        _mip = SETTINGS.get("max_ip_per_user")
        return int(_mip) if _mip is not None else 3

async def enforce_ip_limit_for_link(uuid: str, ip: str) -> bool:
    """Real per-user concurrent-IP limit enforcement.

    Called from the WS/XHTTP entrypoints with the actual client IP. Tracks the
    IP in USER_IP_MAP (the same store the dashboard's ip-check endpoint reads),
    so the panel shows real connected IPs instead of fake/manual assignments.
    Rejects the connection (returns False) when the user's IP count already
    reached the configured concurrent_connections / max_ip_per_user limit.

    NOTE: the relay modules import main lazily (avoiding circular imports), so
    they can call this function at connection time.
    """
    if not ip or ip in ("نامشخص", "unknown", "127.0.0.1"):
        return True

    max_ip = await _link_max_ip(uuid)
    if max_ip < 1:
        return True

    user_id = await _resolve_user_id_for_link(uuid)
    if not user_id:
        # No registered user → fall back to per-uuid tracking so the limit
        # still applies to raw links (default link, sub-group links, etc.)
        user_id = f"link:{uuid}"

    async with USER_IP_MAP_LOCK:
        ips = USER_IP_MAP[user_id]
        if ip in ips:
            # Same IP reconnecting → always allowed
            return True
        if len(ips) >= max_ip:
            return False
        ips.add(ip)
    asyncio.create_task(save_state())
    return True

async def release_ip_for_link(uuid: str, ip: str) -> None:
    """Remove a formerly-connected IP for a user/link.

    Called when a WS/XHTTP relay tears down so USER_IP_MAP reflects the *real*
    set of currently-connected IPs rather than stale historical assignments.
    """
    if not ip or ip in ("نامشخص", "unknown", "127.0.0.1"):
        return
    user_id = await _resolve_user_id_for_link(uuid)
    if not user_id:
        user_id = f"link:{uuid}"
    async with USER_IP_MAP_LOCK:
        s = USER_IP_MAP.get(user_id)
        if s:
            s.discard(ip)
    asyncio.create_task(save_state())


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN TOOLS
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/tools/config-generator")
async def config_generator(request: Request, _=Depends(require_auth)):
    """Generate a connection config string for given parameters."""
    body = await request.json()
    protocol = str(body.get("protocol", "vless")).lower()
    host = str(body.get("host", get_host())).strip()
    config_uuid = str(body.get("uuid") or generate_uuid())
    remark = str(body.get("remark", "Generated"))

    if protocol not in USER_PROTOCOLS:
        raise HTTPException(status_code=400, detail=f"Invalid protocol. Must be one of: {', '.join(USER_PROTOCOLS)}")

    # Build a temporary user-like dict for generate_user_config
    temp_user = {
        "protocol": protocol,
        "config_uuid": config_uuid,
        "username": remark,
    }
    # Override host temporarily
    original_host = CONFIG.get("host")
    CONFIG["host"] = host
    config = generate_user_config("temp", temp_user)
    if original_host:
        CONFIG["host"] = original_host

    return {
        "protocol": protocol,
        "host": host,
        "uuid": config_uuid,
        "remark": remark,
        "config": config,
        "generated_at": datetime.now().isoformat(),
    }


@app.post("/api/tools/ip-test")
async def ip_test(request: Request, _=Depends(require_auth)):
    """Simulated ping test for a given IP."""
    import random
    body = await request.json()
    ip_addr = str(body.get("ip", "")).strip()
    if not ip_addr:
        raise HTTPException(status_code=400, detail="ip is required")

    # Simple IP format check
    parts = ip_addr.split(".")
    valid_format = len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)
    if not valid_format:
        raise HTTPException(status_code=400, detail="invalid ip format")

    latency = random.randint(10, 400)
    status = "reachable" if latency < 350 else "unreachable"

    # Check blacklist
    async with IP_BLACKLIST_LOCK:
        blacklisted = ip_addr in IP_BLACKLIST

    return {
        "ip": ip_addr,
        "latency_ms": latency,
        "status": status,
        "blacklisted": blacklisted,
        "tested_at": datetime.now().isoformat(),
    }


@app.get("/api/tools/stress-test")
async def stress_test(_=Depends(require_auth)):
    """Simulated server load stats."""
    import random
    conn_count = len(connections)
    load_factor = min(conn_count / 500, 1.0) * 100
    return {
        "timestamp": datetime.now().isoformat(),
        "load_percent": round(load_factor, 1),
        "active_connections": conn_count,
        "max_theoretical_connections": 500,
        "cpu_percent": round(min(conn_count * 0.35 + random.uniform(2, 8), 95), 1),
        "ram_percent": round(min(50 + conn_count * 0.08 + random.uniform(1, 5), 95), 1),
        "disk_iops": random.randint(100, 2000),
        "network_mbps": round(random.uniform(2, 80), 2),
        "requests_per_second": stats.get("total_requests", 0) / max(time.time() - stats["start_time"], 1),
        "status": "healthy" if load_factor < 70 else ("degraded" if load_factor < 90 else "critical"),
    }


@app.post("/api/tools/bulk-create")
async def bulk_create_users(request: Request, _=Depends(require_auth)):
    """Create multiple users at once based on a template."""
    body = await request.json()
    count = int(body.get("count", 1))
    if count < 1:
        raise HTTPException(status_code=400, detail="count must be at least 1")
    if count > 100:
        raise HTTPException(status_code=400, detail="count cannot exceed 100")

    template = body.get("template", {})
    base_username = str(template.get("username_prefix", "bulk")).strip()[:20]
    protocol = str(template.get("protocol", "vless")).lower()
    traffic_limit_gb = float(template.get("traffic_limit_gb") or 0)
    expire_days = int(template.get("expire_days") or 0)
    concurrent = int(template.get("concurrent_connections") or 3)
    server = str(template.get("server", "IR-Tehran-01")).strip()[:40]

    if protocol not in USER_PROTOCOLS:
        raise HTTPException(status_code=400, detail=f"Invalid protocol: {protocol}")

    created = []
    async with USERS_LOCK:
        for i in range(count):
            user_id = generate_short_id()
            username = f"{base_username}{i + 1}"
            # Avoid duplicates: append random suffix if needed
            if any(u.get("username") == username for u in USERS.values()):
                username = f"{base_username}{i + 1}_{secrets.token_hex(3)}"
            config_uuid = generate_uuid()
            traffic_limit_bytes = int(traffic_limit_gb * 1024 ** 3) if traffic_limit_gb > 0 else 0
            expire_at = (datetime.now() + timedelta(days=expire_days)).isoformat() if expire_days > 0 else None
            USERS[user_id] = {
                "username": username,
                "password_hash": hash_password(secrets.token_urlsafe(8)),
                "protocol": protocol,
                "traffic_limit_bytes": traffic_limit_bytes,
                "traffic_used_bytes": 0,
                "expire_at": expire_at,
                "concurrent_connections": concurrent,
                "created_at": datetime.now().isoformat(),
                "status": "active",
                "server": server,
                "config_uuid": config_uuid,
                "subscription_uuid": secrets.token_urlsafe(16),
            }
            created.append({"user_id": user_id, "username": username})

    asyncio.create_task(save_state())
    log_activity("user", f"{count} کاربر به‌صورت انبوه ساخته شد", "ok")
    return {"ok": True, "created_count": len(created), "users": created}


# ══════════════════════════════════════════════════════════════════════════════
# SERVER RESOURCES (neon bars)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/server/resources")
async def server_resources(_=Depends(require_auth)):
    """Return live CPU, RAM, Disk, uptime for neon status bars."""
    try:
        import psutil
        return {
            "cpu_percent": psutil.cpu_percent(interval=0.3),
            "cpu_count": psutil.cpu_count(),
            "ram_percent": psutil.virtual_memory().percent,
            "ram_total_gb": round(psutil.virtual_memory().total / 1024**3, 1),
            "ram_used_gb": round(psutil.virtual_memory().used / 1024**3, 1),
            "disk_percent": psutil.disk_usage("/").percent,
            "disk_total_gb": round(psutil.disk_usage("/").total / 1024**3, 1),
            "net_sent_mb": round(psutil.net_io_counters().bytes_sent / 1024**2, 1),
            "net_recv_mb": round(psutil.net_io_counters().bytes_recv / 1024**2, 1),
            "uptime_seconds": int(time.time() - stats.get("start_time", time.time())),
        }
    except ImportError:
        return {"error": "psutil not installed", "cpu_percent": 0, "ram_percent": 0, "disk_percent": 0}


# ══════════════════════════════════════════════════════════════════════════════
# XRAY CORE CONFIG GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

def generate_xray_server_config(inbound_id: str = None) -> dict:
    """
    Generate a complete Xray-core server config.json based on inbound settings.
    Returns a dict that can be saved as config.json for Xray core.
    """
    inbound = None
    if inbound_id:
        inbound = INBOUNDS.get(inbound_id)
    
    host = SETTINGS.get("domain") or get_host()
    xray_config = {
        "log": {"loglevel": "warning"},
        "inbounds": [],
        "outbounds": [{"protocol": "freedom", "tag": "direct"}],
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": []
        }
    }
    
    if not inbound:
        # Generate for all inbounds
        for iid, ib in INBOUNDS.items():
            _add_inbound_to_xray(xray_config, ib, iid, host)
    else:
        _add_inbound_to_xray(xray_config, inbound, inbound_id, host)
    
    return xray_config


def _add_inbound_to_xray(cfg: dict, ib: dict, iid: str, host: str):
    """Add a single inbound to an Xray config dict.

    Only REALITY inbounds are served by Xray: WS/XHTTP TLS inbounds are handled
    by the FastAPI relay (Railway terminates TLS on the public port), and the
    Worker inbound is handled by the Cloudflare Worker. Adding TLS inbounds with
    a fake /etc/xray/cert.pem made Xray fail on Railway (no cert file), which
    took down Reality too.
    """
    protocol = ib.get("protocol", "vless")
    security = ib.get("security", "tls")
    if protocol == "shadowsocks":
        _raw_port = str(ib.get("port") or "").strip()
        if not _raw_port: return
        ss = ib.get("socks_settings") or {}
        cfg["inbounds"].append({
            "tag": f"inbound-{iid}", "listen": "0.0.0.0", "port": int(_raw_port),
            "protocol": "shadowsocks",
            "settings": {"method": str(ss.get("method") or "2022-blake3-aes-256-gcm"), "password": str(ss.get("password") or ""), "network": "tcp,udp"}
        })
        return
    is_reality = protocol == "reality" or security == "reality"
    if not is_reality:
        return  # WS/XHTTP-TLS + worker inbounds are NOT Xray's job
    # A reality inbound without a configured port is not ready yet — skip it
    # so Xray doesn't start on a wrong/default port.
    _raw_port = str(ib.get("port") or "").strip()
    if not _raw_port:
        return
    # Xray listens on the INTERNAL port; the external port is the Railway TCP
    # proxy port that forwards to it (client config uses external_port).
    port = int(_raw_port)
    network = ib.get("network", "ws")
    domain = ib.get("domain", host)
    sni_val = ib.get("sni", domain)
    fingerprint = ib.get("fingerprint", "chrome")
    rs = ib.get("reality_settings", {}) if (protocol == "reality" or security == "reality") else {}
    ws_settings = ib.get("ws_settings", {})
    xh_settings = ib.get("xhttp_settings", {})
    grpc_settings = ib.get("grpc_settings", {})
    
    inbound_obj = {
        "tag": f"inbound-{iid}",
        "listen": "0.0.0.0",
        "port": port,
        # Xray has no "reality" protocol id — Reality is a security layer on top
        # of VLESS, so reality inbounds must declare protocol "vless".
        "protocol": "vless" if protocol == "reality" else protocol,
        "settings": {"clients": [], "decryption": "none"},
        "streamSettings": {}
    }

    if protocol == "shadowsocks":
        ss = ib.get("socks_settings") or {}
        method = str(ss.get("method") or "2022-blake3-aes-256-gcm")
        password = str(ss.get("password") or "")
        inbound_obj["protocol"] = "shadowsocks"
        inbound_obj["settings"] = {"method": method, "password": password, "network": "tcp,udp"}

    # Protocol-specific client settings — use REAL user UUIDs that picked this
    # inbound so they can actually connect through Xray. (Reality is a VLESS
    # client too, so it also carries uuid clients.)
    # Only users that are currently allowed (active + not expired + quota left)
    # are served — expired/disabled/quota-exceeded users are dropped so Xray
    # rejects their connections (real expiry/volume enforcement for Reality).
    if protocol in ("vless", "reality", "vmess", "trojan"):
        client_ids = set()
        for u in USERS.values():
            uids = u.get("inbound_ids") or ([u.get("inbound_id")] if u.get("inbound_id") else [])
            if iid in uids and u.get("config_uuid") and is_user_allowed(u):
                client_ids.add(u["config_uuid"])
        if not client_ids:
            client_ids.add(generate_uuid())
        clients = []
        for uid in client_ids:
            client = {"id": uid}
            if protocol in ("vless", "reality"):
                client["flow"] = ""
            elif protocol == "vmess":
                client["alterId"] = 0
            elif protocol == "trojan":
                # Persistently derive the Trojan credential from the user's
                # config UUID so regeneration cannot invalidate existing links.
                client = {"password": uid}
            clients.append(client)
        inbound_obj["settings"]["clients"] = clients
    elif protocol == "socks5":
        accounts = []
        ss = ib.get("socks_settings") or {}
        username = str(ss.get("username") or "").strip()
        password = str(ss.get("password") or "").strip()
        # Prefer explicit inbound credentials. When omitted, create a stable
        # credential from the assigned user's config UUID.
        for u in USERS.values():
            uids = u.get("inbound_ids") or ([u.get("inbound_id")] if u.get("inbound_id") else [])
            if iid in uids and u.get("config_uuid") and is_user_allowed(u):
                accounts.append({"user": username or str(u.get("username") or "spider"),
                                 "pass": password or str(u.get("config_uuid"))})
        inbound_obj["settings"] = {
            "auth": "password" if accounts else "noauth",
            "accounts": accounts,
            "udp": True
        }

    # Transport / Stream settings
    if protocol == "reality" or security == "reality":
        # The Reality target is authoritative from this inbound's own
        # SNI/Destination/Server Names. Do not silently replace it with a
        # hard-coded target, otherwise the client SNI and Xray destination can
        # describe different TLS targets.
        rs_sni = _normalize_reality_sni(str(rs.get("sni") or ib.get("sni") or "").strip())
        rs_dest = _normalize_reality_destination(str(rs.get("dest") or rs.get("target") or "").strip())
        rs_server_names = rs.get("server_names") or rs.get("serverNames") or [rs_sni]
        if isinstance(rs_server_names, str):
            rs_server_names = [x.strip() for x in rs_server_names.split(",") if x.strip()]
        if not rs_server_names:
            rs_server_names = [rs_sni]
        inbound_obj["streamSettings"] = {
            "network": network if network in ("tcp", "xhttp", "grpc") else "tcp",
            "security": "reality",
            "realitySettings": {
                "show": False,
                "dest": rs_dest,
                "xver": 0,
                "serverNames": rs_server_names,
                "privateKey": rs.get("private_key", ""),
                "shortIds": [str(rs.get("short_id") or rs.get("short_ids") or "5a3ff5a13d").split(",")[0].strip()],
                "spiderX": rs.get("spiderx", "/"),
                "mldsa65Seed": rs.get("mldsa65_seed", ""),
                "settings": {
                    "publicKey": rs.get("public_key", ""),
                    "privateKey": rs.get("private_key", ""),
                    "fingerprint": fingerprint,
                    "serverName": rs_server_names[0],
                    "spiderX": rs.get("spiderx", "/"),
                    "mldsa65Verify": rs.get("mldsa65_verify", ""),
                },
            }
        }
        if network == "xhttp":
            _xpath = str(xh_settings.get("path") or "/").strip()
            if not _xpath.startswith("/"):
                _xpath = "/" + _xpath
            inbound_obj["streamSettings"]["xhttpSettings"] = {
                "path": _xpath,
                "host": xh_settings.get("host", domain),
                "mode": (xh_settings.get("mode") if xh_settings.get("mode") in ("packet-up", "stream-up") else "stream-up"),
                "xPaddingBytes": xh_settings.get("xPaddingBytes", "100-1000"),
                "scMaxEachPostBytes": xh_settings.get("scMaxEachPostBytes", "1000000"),
                "scMaxBufferedPosts": xh_settings.get("scMaxBufferedPosts", 30),
                "scStreamUpServerSecs": xh_settings.get("scStreamUpServerSecs", "20-80"),
                "sessionPlacement": xh_settings.get("sessionPlacement", "header"),
                "sessionKey": xh_settings.get("sessionKey", "X-Session"),
                "seqPlacement": xh_settings.get("seqPlacement", "header"),
                "seqKey": xh_settings.get("seqKey", "X-Seq"),
            }
    elif security == "tls":
        inbound_obj["streamSettings"] = {
            "network": network,
            "security": "tls",
            "tlsSettings": {
                "certificates": [{
                    "certificateFile": "/etc/xray/cert.pem",
                    "keyFile": "/etc/xray/key.pem"
                }]
            }
        }
        if network == "ws":
            inbound_obj["streamSettings"]["wsSettings"] = {
                "path": ws_settings.get("path", "/"),
                "headers": {"Host": ws_settings.get("host", domain)}
            }
        elif network == "grpc":
            inbound_obj["streamSettings"]["grpcSettings"] = {
                "serviceName": grpc_settings.get("serviceName", "")
            }
        elif network == "xhttp":
            inbound_obj["streamSettings"]["xhttpSettings"] = {
                "path": xh_settings.get("path", "/"),
                "host": xh_settings.get("host", domain),
                "mode": xh_settings.get("mode", "auto"),
                "xPaddingBytes": xh_settings.get("xPaddingBytes", "100-1000"),
                "scMaxEachPostBytes": xh_settings.get("scMaxEachPostBytes", "1000000"),
            }
    else:
        # No TLS (raw)
        inbound_obj["streamSettings"] = {"network": network}
        if network == "ws":
            inbound_obj["streamSettings"]["wsSettings"] = {"path": ws_settings.get("path", "/")}
    
    # Add sniffing
    inbound_obj["sniffing"] = {
        "enabled": True,
        "destOverride": ["http", "tls", "quic"]
    }
    
    cfg["inbounds"].append(inbound_obj)


# ── Xray process manager ───────────────────────────────────────────────────────
_xray_proc: asyncio.subprocess.Process | None = None
_xray_restart_lock = asyncio.Lock()
# Set of user config_uuids the last _xray_apply() served on reality inbounds.
# The audit loop re-applies Xray when this set changes (user expires / disabled /
# quota exhausted over time), so Reality connections are actually cut.
_xray_last_served: set = set()


def _expected_xray_client_uuids() -> set:
    """Real users Xray should currently serve on reality inbounds."""
    out = set()
    for iid, ib in INBOUNDS.items():
        is_reality = ((ib.get("protocol") or "").lower() == "reality"
                      or (ib.get("security") or "").lower() == "reality")
        if not is_reality:
            continue
        for u in USERS.values():
            uids = u.get("inbound_ids") or ([u.get("inbound_id")] if u.get("inbound_id") else [])
            if iid in uids and u.get("config_uuid") and is_user_allowed(u):
                out.add(u["config_uuid"])
    return out


async def _xray_client_audit_loop():
    """Periodically drop Reality users who expired/ran out of quota/disabled.

    Xray enforces nothing itself; the panel cuts Reality access by regenerating
    the config without the disallowed UUIDs and restarting Xray.
    """
    global _xray_last_served
    await asyncio.sleep(45)
    while True:
        try:
            async with USERS_LOCK:
                for u in USERS.values():
                    auto_check_user_expiry(u)
            expected = _expected_xray_client_uuids()
            if expected != _xray_last_served:
                await _xray_apply()
        except Exception as e:
            logger.warning(f"xray client audit failed: {e}")
        await asyncio.sleep(60)


def _xray_bin_path() -> Path:
    return Path(os.path.dirname(os.path.abspath(__file__))) / "xray" / "xray"


async def _xray_start(config: dict) -> bool:
    """Write config.json and start the Xray subprocess (or restart if running)."""
    global _xray_proc
    bin_path = _xray_bin_path()
    if not bin_path.exists():
        logger.warning("xray binary missing; skipping xray start")
        return False
    # No reality inbounds configured yet → don't run Xray with an empty config
    # (it would fail to bind any listener). Stop any running instance.
    if not (config.get("inbounds") or []):
        if _xray_proc and _xray_proc.returncode is None:
            try:
                _xray_proc.terminate()
            except Exception:
                pass
        return False
    async with _xray_restart_lock:
        # Stop existing
        if _xray_proc and _xray_proc.returncode is None:
            try:
                _xray_proc.terminate()
                await asyncio.wait_for(_xray_proc.wait(), timeout=3)
            except Exception:
                try:
                    _xray_proc.kill()
                except Exception:
                    pass
        cfg_path = bin_path.parent / "config.json"
        try:
            cfg_path.write_text(json.dumps(config, indent=2, ensure_ascii=False))
        except Exception as e:
            logger.warning(f"xray config write failed: {e}")
            return False
        try:
            log_path = bin_path.parent / "xray-runtime.log"
            log_fp = open(log_path, "ab", buffering=0)
            _xray_proc = await asyncio.create_subprocess_exec(
                str(bin_path), "-c", str(cfg_path),
                stdout=log_fp,
                stderr=asyncio.subprocess.STDOUT,
            )
            await asyncio.sleep(0.8)
            if _xray_proc.returncode is not None:
                try:
                    tail = log_path.read_text(errors="ignore")[-5000:]
                except Exception:
                    tail = ""
                logger.error(f"Xray exited immediately (code={_xray_proc.returncode}). {tail}")
                return False
            logger.info(f"Xray started (pid={_xray_proc.pid}) on configured Reality ports")
            return True
        except Exception as e:
            logger.warning(f"xray start failed: {e}")
            return False


async def _xray_apply():
    """Regenerate config for all inbounds and (re)start Xray with it."""
    global _xray_last_served
    config = generate_xray_server_config()
    await _xray_start(config)
    _xray_last_served = _expected_xray_client_uuids()


@app.post("/api/tools/generate-xray-config")
async def gen_xray_server_config(request: Request, _=Depends(require_auth)):
    """Generate a complete Xray-core server config.json for all or specific inbounds."""
    body = await request.json()
    inbound_id = body.get("inbound_id") or None
    
    try:
        config = generate_xray_server_config(inbound_id)
        return {
            "ok": True,
            "config": config,
            "config_json": json.dumps(config, indent=2, ensure_ascii=False),
            "inbounds_count": len(config["inbounds"]),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/tools/generate-xray-keys")
async def gen_xray_keys(_=Depends(require_auth)):
    """Generate all Xray-related keys: Reality x25519 keypair, UUID, shortId."""
    result = {
        "uuid": generate_uuid(),
        "short_id": secrets.token_hex(5)[:10],
    }
    try:
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        priv = X25519PrivateKey.generate()
        priv_bytes = priv.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pub_bytes = priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        import base64 as b64
        result["private_key"] = b64.b64encode(priv_bytes).decode()
        result["public_key"] = b64.b64encode(pub_bytes).decode()
    except ImportError:
        result["private_key"] = ""
        result["public_key"] = ""
        result["note"] = "cryptography not installed"
    return result


# ══════════════════════════════════════════════════════════════════════════════
# SERVER STATS (HTTP polling)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/server/stats")
async def server_stats_http(_=Depends(require_auth)):
    """One-shot HTTP response with live server stats (for polling clients)."""
    return get_live_stats()


# ── Static files mount (MUST be after all routes) ──
# ── Static files mount (MUST be after all routes) ──


# ══════════════════════════════════════════════════════════════════════════════
# FILE UPLOADS - Backgrounds, Audio, Custom Assets
# ══════════════════════════════════════════════════════════════════════════════

UPLOAD_DIR = _os.path.join(_STATIC_DIR, "uploads")
_os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/jpg", "image/webp", "image/gif"}
ALLOWED_AUDIO_TYPES = {"audio/mpeg", "audio/mp3", "audio/wav", "audio/ogg", "audio/webm"}


@app.post("/api/upload/background")
async def upload_background(request: Request, _=Depends(require_auth)):
    """Upload a custom background image for login, dashboard, or sub page."""
    form = await request.form()
    file = form.get("file")
    bg_type = str(form.get("type") or "login").lower()  # login, dashboard, sub
    
    if not file:
        raise HTTPException(status_code=400, detail="No file uploaded")
    
    content_type = file.content_type or ""
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid file type: {content_type}. Allowed: jpg, png, webp, gif")
    
    # Save file
    ext = file.filename.split(".")[-1] if "." in (file.filename or "") else "jpg"
    safe_name = f"bg_{bg_type}.{ext}"
    file_path = _os.path.join(UPLOAD_DIR, safe_name)
    
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:  # 10MB max
        raise HTTPException(status_code=400, detail="File too large (max 10MB)")
    
    _os.makedirs(_os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "wb") as f:
        f.write(content)
    
    # Update settings
    bg_key = f"bg_{bg_type}"
    async with SETTINGS_LOCK:
        SETTINGS[bg_key] = f"/static/uploads/{safe_name}?t={int(time.time())}"
    
    await save_state()
    log_activity("settings", f"Background {bg_type} uploaded", "ok")
    return {"ok": True, "url": SETTINGS[bg_key], "type": bg_type}


@app.post("/api/upload/audio")
async def upload_audio(request: Request, _=Depends(require_auth)):
    """Upload a custom audio/music file for the panel."""
    form = await request.form()
    file = form.get("file")
    
    if not file:
        raise HTTPException(status_code=400, detail="No file uploaded")
    
    content_type = file.content_type or ""
    if content_type not in ALLOWED_AUDIO_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid file type: {content_type}. Allowed: mp3, wav, ogg")
    
    ext = file.filename.split(".")[-1] if "." in (file.filename or "") else "mp3"
    safe_name = f"panel_audio.{ext}"
    file_path = _os.path.join(UPLOAD_DIR, safe_name)
    
    content = await file.read()
    if len(content) > 50 * 1024 * 1024:  # 50MB max
        raise HTTPException(status_code=400, detail="File too large (max 50MB)")
    
    _os.makedirs(_os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "wb") as f:
        f.write(content)
    
    # Update settings
    async with SETTINGS_LOCK:
        SETTINGS["panel_audio"] = f"/static/uploads/{safe_name}?t={int(time.time())}"
        SETTINGS["panel_audio_enabled"] = True
    
    await save_state()
    log_activity("settings", "Panel audio uploaded", "ok")
    return {"ok": True, "url": SETTINGS["panel_audio"]}


@app.post("/api/settings/background/remove")
async def remove_background(request: Request, _=Depends(require_auth)):
    """Remove a custom background."""
    body = await request.json()
    bg_type = str(body.get("type") or "login").lower()
    bg_key = f"bg_{bg_type}"
    async with SETTINGS_LOCK:
        SETTINGS.pop(bg_key, None)
    await save_state()
    return {"ok": True, "removed": bg_type}


@app.post("/api/settings/audio/remove")
async def remove_audio(_=Depends(require_auth)):
    """Remove panel audio."""
    async with SETTINGS_LOCK:
        SETTINGS["panel_audio"] = ""
        SETTINGS["panel_audio_enabled"] = False
    await save_state()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# IP SCANNER - Railway IPs, Ping Tests, Current IP
# ══════════════════════════════════════════════════════════════════════════════

RAILWAY_REGIONS = [
    {"name": "us-west1 (Oregon)", "host": "us-west1.railway.app"},
    {"name": "us-east4 (Virginia)", "host": "us-east4.railway.app"},
    {"name": "us-central1 (Iowa)", "host": "us-central1.railway.app"},
    {"name": "europe-west4 (Netherlands)", "host": "europe-west4.railway.app"},
    {"name": "europe-west1 (Belgium)", "host": "europe-west1.railway.app"},
    {"name": "asia-southeast1 (Singapore)", "host": "asia-southeast1.railway.app"},
    {"name": "asia-east1 (Taiwan)", "host": "asia-east1.railway.app"},
    {"name": "asia-northeast1 (Tokyo)", "host": "asia-northeast1.railway.app"},
    {"name": "australia-southeast1 (Sydney)", "host": "australia-southeast1.railway.app"},
    {"name": "southamerica-east1 (Sao Paulo)", "host": "southamerica-east1.railway.app"},
]

FAMOUS_SITES = [
    {"name": "Google", "host": "google.com"},
    {"name": "Cloudflare", "host": "cloudflare.com"},
    {"name": "GitHub", "host": "github.com"},
    {"name": "YouTube", "host": "youtube.com"},
    {"name": "Amazon", "host": "amazon.com"},
    {"name": "Wikipedia", "host": "wikipedia.org"},
    {"name": "Microsoft", "host": "microsoft.com"},
    {"name": "Twitter/X", "host": "twitter.com"},
    {"name": "Instagram", "host": "instagram.com"},
    {"name": "Telegram", "host": "telegram.org"},
]


import subprocess
import platform


@app.get("/api/tools/my-ip")
async def get_my_ip(_=Depends(require_auth)):
    """Get the server's current public IP."""
    ips = {}
    # Try multiple services
    for service, url in [
        ("ipify", "https://api.ipify.org?format=json"),
        ("icanhazip", "https://icanhazip.com"),
        ("ipinfo", "https://ipinfo.io/json"),
    ]:
        try:
            async with http_client as client:
                resp = await client.get(url, timeout=5)
                if resp.status_code == 200:
                    body = resp.text.strip()
                    ips[service] = body
        except Exception:
            ips[service] = None
    
    # Try Railway metadata
    railway_ip = None
    try:
        if os.environ.get("RAILWAY_STATIC_URL"):
            railway_ip = os.environ.get("RAILWAY_STATIC_URL")
    except Exception:
        pass
    
    return {
        "ips": ips,
        "railway_url": railway_ip,
        "local_hostname": platform.node(),
    }


@app.get("/api/tools/ping-sites")
async def ping_famous_sites(_=Depends(require_auth)):
    """Ping famous websites and return latency results."""
    results = []
    for site in FAMOUS_SITES:
        latency = None
        status = "error"
        try:
            system = platform.system().lower()
            if system == "windows":
                cmd = ["ping", "-n", "1", "-w", "3000", site["host"]]
            else:
                cmd = ["ping", "-c", "1", "-W", "3", site["host"]]
            
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            
            if proc.returncode == 0:
                output = stdout.decode(errors="ignore")
                # Extract time from ping output
                import re as _re
                if system == "windows":
                    match = _re.search(r"time[=<](\d+)ms", output)
                else:
                    match = _re.search(r"time=(\d+\.?\d*)\s*ms", output)
                if match:
                    latency = float(match.group(1))
                    status = "ok" if latency < 200 else ("slow" if latency < 500 else "very-slow")
                else:
                    status = "no-response"
            else:
                status = "unreachable"
        except asyncio.TimeoutError:
            status = "timeout"
        except Exception:
            status = "error"
        
        results.append({
            "name": site["name"],
            "host": site["host"],
            "latency_ms": latency,
            "status": status,
        })
    return {"sites": results}


@app.get("/api/tools/scan-railway-ips")
async def scan_railway_ips(_=Depends(require_auth)):
    """Ping Railway region endpoints (NOT Cloudflare) to test connectivity."""
    results = []
    for region in RAILWAY_REGIONS:
        latency = None
        status = "error"
        try:
            system = platform.system().lower()
            if system == "windows":
                cmd = ["ping", "-n", "1", "-w", "3000", region["host"]]
            else:
                cmd = ["ping", "-c", "1", "-W", "3", region["host"]]
            
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            
            if proc.returncode == 0:
                output = stdout.decode(errors="ignore")
                import re as _re
                if system == "windows":
                    match = _re.search(r"time[=<](\d+)ms", output)
                else:
                    match = _re.search(r"time=(\d+\.?\d*)\s*ms", output)
                if match:
                    latency = float(match.group(1))
                    status = "ok" if latency < 200 else ("slow" if latency < 500 else "very-slow")
                else:
                    status = "no-response"
            else:
                status = "unreachable"
        except asyncio.TimeoutError:
            status = "timeout"
        except Exception:
            status = "error"
        
        results.append({
            "region": region["name"],
            "host": region["host"],
            "latency_ms": latency,
            "status": status,
        })
    return {"regions": results}


# ══════════════════════════════════════════════════════════════════════════════
# CLOUDFLARE WORKER MANAGER — multi-location proxy via Cloudflare Workers
# Traffic: Client → Worker Domain → Cloudflare Worker → Selected Proxy IP → Internet
# Railway only hosts the panel/API; it is NOT in the VPN data path.
# ══════════════════════════════════════════════════════════════════════════════

CF_API = "https://api.cloudflare.com/client/v4"
CF_TOKEN_LINK = (
    "https://dash.cloudflare.com/profile/api-tokens?permissionGroupKeys="
    "%5B%7B%22key%22%3A%22workers_scripts%22%2C%22type%22%3A%22edit%22%7D%2C"
    "%7B%22key%22%3A%22workers_kv_storage%22%2C%22type%22%3A%22edit%22%7D%2C"
    "%7B%22key%22%3A%22workers_routes%22%2C%22type%22%3A%22edit%22%7D%5D"
    "&accountId=*&zoneId=all&name=SpiderPanel-Token"
)

# Worker script deployed to the user's Cloudflare account lives in the project
# at worker/_worker.js (NOT under /static, so it is never served to the web).
# The proxy map is injected at deploy time by replacing __PROXIES_JSON__, so
# adding/removing a country re-deploys the worker (see /api/worker/sync).
CF_WORKER_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / "worker"
CF_WORKER_TEMPLATE = CF_WORKER_DIR / "_worker.js"


def _worker_script() -> str:
    """Return the worker template source, or raise if the file is missing."""
    if not CF_WORKER_TEMPLATE.is_file():
        raise FileNotFoundError(
            f"worker template not found: {CF_WORKER_TEMPLATE} "
            "(create worker/_worker.js in the project repo)"
        )
    return CF_WORKER_TEMPLATE.read_text(encoding="utf-8")


def _is_cf_gak(token: str) -> bool:
    """True if token is a Cloudflare Global API Key (panel cfk_/cf_ prefix).

    The full prefixed token is sent as-is to Cloudflare's X-Auth-Key header;
    the cfk_ prefix is part of the accepted key format.
    """
    t = str(token or "").strip()
    return t.startswith("cfk_") or t.startswith("cf_") or bool(re.fullmatch(r"[a-f0-9]{37}", t, re.IGNORECASE))

def _cf_auth_token(token: str) -> str:
    """Return the token value to send to Cloudflare as-is (no stripping)."""
    return str(token or "").strip()

async def _cf_api(method: str, path: str, token: str, payload: dict = None, email: str = ""):
    """Call the Cloudflare API v4. Returns (status_code, json).

    token is either a Bearer token (modern) or a Global API Key (cfk_...). When
    the token looks like a Global API Key, we authenticate with X-Auth-Email +
    X-Auth-Key instead of Authorization: Bearer.
    """
    token = str(token or "").strip()
    email = str(email or "").strip()
    headers = {"Content-Type": "application/json", "User-Agent": "Spider-Panel"}
    # Cloudflare Global API Key (cfk_/cf_ prefix or 37-char hex) → Global Key
    # auth (X-Auth-Email + X-Auth-Key). Modern Bearer tokens → Authorization.
    # Only a real GAK is sent via X-Auth-Key; a Bearer token always uses Bearer
    # auth even if an email happens to be on file (filling email must not turn a
    # valid API token into a rejected X-Auth-Key).
    _is_gak = _is_cf_gak(token)
    if _is_gak:
        headers["X-Auth-Key"] = token
        headers["X-Auth-Email"] = email or os.environ.get("CF_EMAIL", "")
    else:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=40) as client:
        try:
            r = await client.request(method, f"{CF_API}{path}", headers=headers, json=payload)
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, {}
        except Exception as e:
            return 0, {"errors": [{"message": str(e)}]}


def _worker_safe_domain(raw: str) -> str:
    raw = str(raw or "").strip().lower()
    raw = re.sub(r"^https?://", "", raw).rstrip("/")
    if not raw or raw in ("localhost", "0.0.0.0", "127.0.0.1"):
        return ""
    return raw


def _worker_public() -> dict:
    """Snapshot of worker state with the API token stripped."""
    return {
        "connected": WORKER.get("connected", False),
        "account_id": WORKER.get("account_id", ""),
        "worker_name": WORKER.get("worker_name", ""),
        "worker_domain": WORKER.get("worker_domain", ""),
        "worker_url": WORKER.get("worker_url", ""),
        "panel_domain": WORKER.get("panel_domain", ""),
        "kv_namespace_id": WORKER.get("kv_namespace_id", ""),
        "kv_namespace_title": WORKER.get("kv_namespace_title", ""),
        "remote_status": WORKER.get("remote_status", ""),
        "last_heartbeat": WORKER.get("last_heartbeat", ""),
        "worker_users_online": int(WORKER.get("worker_users_online") or 0),
        "worker_traffic_bytes": int(WORKER.get("worker_traffic_bytes") or 0),
        "worker_user_count": int(WORKER.get("worker_user_count") or 0),
        "last_sync": WORKER.get("last_sync", ""),
        "last_error": WORKER.get("last_error", ""),
        "source_url": WORKER.get("source_url", ""),
        "auto_sync": bool(WORKER.get("auto_sync", True)),
        "sync_error": WORKER.get("sync_error", ""),
        "sync_count": int(WORKER.get("sync_count", 0)),
        "token_link": CF_TOKEN_LINK,
        "tunnel_enabled": bool(WORKER.get("tunnel_enabled", False)),
        "tunnel_kv_namespace_id": WORKER.get("tunnel_kv_namespace_id", ""),
        "tunnel_kv_namespace_title": WORKER.get("tunnel_kv_namespace_title", ""),
        "reverse_kv_namespace_id": WORKER.get("reverse_kv_namespace_id", ""),
        "reverse_kv_namespace_title": WORKER.get("reverse_kv_namespace_title", ""),
        "fixed_inbounds": [
            {"id": iid, "name": ib.get("name", iid), "protocol": ib.get("protocol", ""),
             "domain": ib.get("domain", ""), "path": (ib.get("ws_settings") or {}).get("path", ""),
             "system_managed": bool(ib.get("system_managed"))}
            for iid, ib in INBOUNDS.items()
            if (ib.get("protocol") or "").lower() in ("worker", "tunnel", "reverse")
        ],
        "proxies": [
            {"code": code, **dict(p)}
            for code, p in sorted((WORKER.get("proxies") or {}).items())
        ],
    }


async def _ensure_worker_kv() -> str | None:
    """Find or create the dedicated KV namespace for THIS worker ({worker}-db).

    Every deployed worker gets its own private KV namespace so multiple
    workers never share user/proxy state. The id is persisted in WORKER state;
    on reconnect the namespace is looked up by title and reused.
    """
    acct = str(WORKER.get("account_id") or "")
    cf_token = str(WORKER.get("token") or "")
    if not acct or not cf_token:
        return None
    existing = str(WORKER.get("kv_namespace_id") or "")
    if existing:
        return existing
    wname = str(WORKER.get("worker_name") or "").strip()
    kv_title = f"{wname}-db" if wname else "spider-worker-kv"
    # List existing namespaces, reuse ours if a previous deploy created it.
    code, data = await _cf_api("GET", f"/accounts/{acct}/storage/kv/namespaces", cf_token, email="")
    if code == 200:
        for ns in (data.get("result") or []):
            if ns.get("title") == kv_title:
                async with WORKER_LOCK:
                    WORKER["kv_namespace_id"] = ns.get("id")
                    WORKER["kv_namespace_title"] = kv_title
                asyncio.create_task(save_state())
                return ns.get("id")
    # Create a fresh dedicated namespace for this worker.
    code, data = await _cf_api(
        "POST", f"/accounts/{acct}/storage/kv/namespaces",
        cf_token, {"title": kv_title}, email="",
    )
    if code == 200 and data.get("result"):
        nid = data["result"].get("id")
        async with WORKER_LOCK:
            WORKER["kv_namespace_id"] = nid
            WORKER["kv_namespace_title"] = kv_title
        asyncio.create_task(save_state())
        return nid
    return None


async def _ensure_tunnel_kv() -> str | None:
    """Find or create the TUNNEL's own KV namespace ({worker}-tunnel-db).

    The tunnel keeps its state separate from the main worker KV. The name is
    derived from the worker's KV title so the pairing is always obvious.
    """
    acct = str(WORKER.get("account_id") or "")
    cf_token = str(WORKER.get("token") or "")
    if not acct or not cf_token:
        return None
    existing = str(WORKER.get("tunnel_kv_namespace_id") or "")
    if existing:
        return existing
    wname = str(WORKER.get("worker_name") or "").strip()
    base = f"{wname}-db" if wname else "spider-worker-kv"
    kv_title = f"{base}-tunnel"  # e.g. spider-a1b2c3-db-tunnel
    code, data = await _cf_api("GET", f"/accounts/{acct}/storage/kv/namespaces", cf_token, email="")
    if code == 200:
        for ns in (data.get("result") or []):
            if ns.get("title") == kv_title:
                async with WORKER_LOCK:
                    WORKER["tunnel_kv_namespace_id"] = ns.get("id")
                    WORKER["tunnel_kv_namespace_title"] = kv_title
                asyncio.create_task(save_state())
                return ns.get("id")
    code, data = await _cf_api(
        "POST", f"/accounts/{acct}/storage/kv/namespaces",
        cf_token, {"title": kv_title}, email="",
    )
    if code == 200 and data.get("result"):
        nid = data["result"].get("id")
        async with WORKER_LOCK:
            WORKER["tunnel_kv_namespace_id"] = nid
            WORKER["tunnel_kv_namespace_title"] = kv_title
        asyncio.create_task(save_state())
        return nid
    return None


async def _ensure_reverse_kv() -> str | None:
    """Find or create the REVERSE's own KV namespace ({worker}-db-reverse).

    Reverse mode: user → Worker → Railway → site. Its state (users, usage)
    lives in a third dedicated namespace so tunnel/reverse/worker never share
    keys and can never interfere with each other.
    """
    acct = str(WORKER.get("account_id") or "")
    cf_token = str(WORKER.get("token") or "")
    if not acct or not cf_token:
        return None
    existing = str(WORKER.get("reverse_kv_namespace_id") or "")
    if existing:
        return existing
    wname = str(WORKER.get("worker_name") or "").strip()
    base = f"{wname}-db" if wname else "spider-worker-kv"
    kv_title = f"{base}-reverse"  # e.g. spider-a1b2c3-db-reverse
    code, data = await _cf_api("GET", f"/accounts/{acct}/storage/kv/namespaces", cf_token, email="")
    if code == 200:
        for ns in (data.get("result") or []):
            if ns.get("title") == kv_title:
                async with WORKER_LOCK:
                    WORKER["reverse_kv_namespace_id"] = ns.get("id")
                    WORKER["reverse_kv_namespace_title"] = kv_title
                asyncio.create_task(save_state())
                return ns.get("id")
    code, data = await _cf_api(
        "POST", f"/accounts/{acct}/storage/kv/namespaces",
        cf_token, {"title": kv_title}, email="",
    )
    if code == 200 and data.get("result"):
        nid = data["result"].get("id")
        async with WORKER_LOCK:
            WORKER["reverse_kv_namespace_id"] = nid
            WORKER["reverse_kv_namespace_title"] = kv_title
        asyncio.create_task(save_state())
        return nid
    return None


async def _worker_deploy() -> tuple:
    """Deploy (or re-deploy) the VLESS worker script.

    The template is read from worker/_worker.js inside the project repo (not the
    state file), so updates to the worker are shipped with a normal git deploy.
    The panel domain + a control token are injected at deploy time so the panel
    can control the worker via its admin API (Bearer token protected). Users and
    the proxy pool live in the worker's KV namespace (SPIDER_KV) — the namespace
    is created/attached as a binding at deploy time.
    """
    try:
        template = _worker_script()
    except Exception as e:
        return 0, {"errors": [{"message": str(e)}]}
    # Ensure a control token exists (generate once, persist).
    ctrl = str(WORKER.get("control_token") or "")
    if not ctrl:
        ctrl = secrets.token_urlsafe(24)
        async with WORKER_LOCK:
            WORKER["control_token"] = ctrl
        asyncio.create_task(save_state())
    panel_domain = _safe_host(SETTINGS.get("domain"), get_host())
    async with WORKER_LOCK:
        WORKER["panel_domain"] = panel_domain
    worker_domain = _worker_safe_domain(WORKER.get("worker_domain"))
    script = (template
              .replace("__PANEL_DOMAIN__", json.dumps(panel_domain))
              .replace("__WORKER_DOMAIN__", json.dumps(worker_domain))
              .replace("__PANEL_TOKEN__", json.dumps(ctrl)))
    cf_token = str(WORKER.get("token") or "")
    if not cf_token:
        return 0, {"errors": [{"message": "Cloudflare API token is missing (worker not connected properly)"}]}
    kv_id = await _ensure_worker_kv()
    tunnel_kv_id = await _ensure_tunnel_kv() if WORKER.get("tunnel_enabled") else None
    reverse_kv_id = await _ensure_reverse_kv() if WORKER.get("tunnel_enabled") else None
    email = ""
    # Use Global API Key auth (X-Auth-Email + X-Auth-Key) when the token is a
    # real GAK (cfk_/cf_ prefix or 37-char hex); otherwise a Bearer token always
    # goes through Authorization even if an email is on file.
    _is_gak = _is_cf_gak(cf_token)
    if _is_gak:
        auth_headers = {"X-Auth-Email": email, "X-Auth-Key": cf_token}
    else:
        auth_headers = {"Authorization": f"Bearer {cf_token}"}
    try:
        # ESM module worker: multipart upload using the `files` form field so
        # Cloudflare parses it as a module-syntax worker (Content-Type must be
        # application/javascript+module), with main_module metadata and the KV
        # namespace bindings (SPIDER_KV for users/routes, TUNNEL_KV +
        # REVERSE_KV when the tunnel/reverse is enabled — three fully separate
        # namespaces). The template uses the global connect()
        # Socket API for outbound TCP, so no fetcher binding is needed.
        wname = WORKER.get("worker_name", "") or ""
        bindings = [{"name": "SPIDER_KV", "namespace_id": kv_id, "type": "kv_namespace"}]
        if tunnel_kv_id:
            bindings.append({"name": "TUNNEL_KV", "namespace_id": tunnel_kv_id, "type": "kv_namespace"})
        if reverse_kv_id:
            bindings.append({"name": "REVERSE_KV", "namespace_id": reverse_kv_id, "type": "kv_namespace"})
        meta = json.dumps({
            "main_module": "worker.js",
            "compatibility_date": "2025-01-01",
            "bindings": bindings,
        })
        boundary = "----SpiderPanel" + secrets.token_hex(8)
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="metadata"\r\n'
            "Content-Type: application/json\r\n\r\n"
            f"{meta}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files"; filename="worker.js"\r\n'
            "Content-Type: application/javascript+module\r\n\r\n"
            f"{script}\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.put(
                f"{CF_API}/accounts/{WORKER.get('account_id','')}/workers/scripts/{wname}",
                headers={**auth_headers, "Content-Type": f"multipart/form-data; boundary={boundary}"},
                content=body,
            )
        try:
            deploy_json = r.json()
        except Exception:
            deploy_json = {}
        return r.status_code, deploy_json
    except Exception as e:
        return 0, {"errors": [{"message": str(e)}]}


async def _worker_enable_workers_dev() -> dict:
    acct=str(WORKER.get("account_id") or ""); name=str(WORKER.get("worker_name") or ""); token=str(WORKER.get("token") or "")
    if not acct or not name or not token: return {"ok":False,"detail":"worker credentials missing"}
    code,data=await _cf_api("POST",f"/accounts/{acct}/workers/scripts/{name}/subdomain",token,{"enabled":True,"previews_enabled":False},email="")
    return {"ok":code==200 and bool(data.get("success")),"status":code,"data":data}


async def _worker_sync_users() -> dict:
    """Synchronize users to the correct isolated Worker KV namespaces."""
    domain = str(WORKER.get("worker_domain") or "").strip().lower()
    ctrl = str(WORKER.get("control_token") or "")
    if not domain or not ctrl or domain in ("localhost", "0.0.0.0", "127.0.0.1"):
        return {"ok": False, "detail": "worker not connected / no control token"}
    counts = {"worker": 0, "tunnel": 0, "reverse": 0}
    async with USERS_LOCK:
        users_snap = [(uid, dict(u)) for uid, u in USERS.items()]
    async with INBOUNDS_LOCK:
        ib_snap = dict(INBOUNDS)

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        for uid, u in users_snap:
            iids = u.get("inbound_ids") or ([u.get("inbound_id")] if u.get("inbound_id") else [])
            scopes = set()
            for iid in iids:
                proto = ((ib_snap.get(iid) or {}).get("protocol") or "").lower()
                if proto == "worker": scopes.add("worker")
                elif proto == "tunnel":
                    scopes.add("tunnel")
                elif proto == "reverse":
                    scopes.add("reverse")
            if not scopes:
                continue
            cuuid = u.get("config_uuid") or uid
            deadline = 0
            if u.get("expire_at"):
                try: deadline = int(datetime.fromisoformat(u["expire_at"]).timestamp())
                except Exception: deadline = 0
            payload = {
                "uuid": cuuid,
                "remark": u.get("username", uid),
                "limit_bytes": int(u.get("traffic_limit_bytes") or 0),
                "expire": deadline,
                "used_bytes": int(u.get("traffic_used_bytes") or 0),
                "proxy_ip": str(u.get("proxy_ip") or ""),
                "concurrent_connections": int(u.get("concurrent_connections") or 0),
                "countries": list(u.get("proxy_countries") or ([u.get("proxy_country")] if u.get("proxy_country") else [])),
            }
            for scope in sorted(scopes):
                try:
                    target = "/api/users" if scope == "worker" else f"/api/{scope}/users"
                    headers = {"Authorization": f"Bearer {ctrl}"}
                    if (u.get("status") or "active") != "active":
                        r = await client.delete(f"https://{domain}{target}/{cuuid}", headers=headers)
                    else:
                        r = await client.post(f"https://{domain}{target}", headers=headers, json=payload)
                    if r.status_code in (200, 201, 204):
                        counts[scope] += 1
                except Exception as e:
                    logger.warning("worker %s user sync failed for %s: %s", scope, uid, e)
    asyncio.create_task(save_state())
    return {"ok": True, "count": sum(counts.values()), "counts": counts}


async def _worker_pull_user(uid: str, u: dict) -> dict:
    """Pull usage/expiry/country and worker-generated config metadata back from Worker."""
    domain = str(WORKER.get("worker_domain") or "").strip().lower()
    ctrl = str(WORKER.get("control_token") or "")
    cuuid = u.get("config_uuid") or uid
    if not domain or not ctrl or not cuuid:
        return u
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(f"https://{domain}/api/user/{cuuid}", headers={"Authorization": f"Bearer {ctrl}"})
        if r.status_code != 200:
            return u
        data = r.json().get("user") or {}
        if "used_bytes" in data:
            u["traffic_used_bytes"] = int(data.get("used_bytes") or 0)
        if data.get("expire"):
            u["worker_expire"] = int(data.get("expire"))
        if isinstance(data.get("countries"), list):
            u["worker_countries"] = list(data.get("countries"))
        if isinstance(data.get("configs"), list):
            u["worker_configs"] = list(data.get("configs"))
        return u
    except Exception as e:
        logger.warning(f"worker pull failed for {uid}: {e}")
        return u

async def _worker_pull_all_users() -> int:
    count = 0
    async with USERS_LOCK:
        targets = [(uid, u) for uid, u in USERS.items() if _user_uses_worker_inbound(u)]
    for uid, u in targets:
        before = u.get("traffic_used_bytes")
        out = await _worker_pull_user(uid, u)
        if out is not u:
            async with USERS_LOCK:
                USERS[uid].update(out)
        if out.get("traffic_used_bytes") != before or out.get("worker_configs") is not None:
            count += 1
    if count:
        asyncio.create_task(save_state())
    return count


def _user_uses_worker_inbound(u: dict) -> bool:
    """True if the user references an inbound served by the Cloudflare Worker
    (worker routes, tunnel/reverse chains) — those users need quota/expiry
    synced to the worker KV."""
    iids = u.get("inbound_ids") or ([u.get("inbound_id")] if u.get("inbound_id") else [])
    return any((INBOUNDS.get(iid) or {}).get("protocol") in ("worker", "tunnel", "reverse") for iid in iids)


async def _ensure_worker_inbound() -> bool:
    """Create or refresh the default Worker inbound to match the connected
    worker domain. Called after a worker connects/deploys so the worker inbound
    always points address/host/sni at the current worker domain."""
    wdom = _worker_safe_domain(WORKER.get("worker_domain"))
    if not wdom:
        return False
    changed = False
    async with INBOUNDS_LOCK:
        wid = next((i for i, ib in INBOUNDS.items() if (ib.get("protocol") or "").lower() == "worker"), None)
        if wid:
            ib = INBOUNDS[wid]
            if (ib.get("domain") or "") != wdom or (ib.get("external_domain") or "") != wdom:
                ib["domain"] = wdom
                ib["external_domain"] = wdom
                changed = True
        else:
            INBOUNDS["default-worker"] = {
                "name": "Worker (Multi-Location)",
                "protocol": "worker",
                "port": 443,
                "network": "ws",
                "security": "tls",
                "domain": wdom,
                "external_domain": wdom,
                "sni": "www.hcaptcha.com",
                "spoof_ip": "8.6.112.4",
                "external_port": 443,
                "fingerprint": "chrome",
                "reality_settings": {},
                "xhttp_settings": {},
                "ws_settings": {"path": "/route/{country}/{uuid}"},
                "grpc_settings": {},
                "created_at": datetime.now().isoformat(),
            }
            changed = True
    if changed:
        asyncio.create_task(save_state())
    return True


async def _worker_push_config() -> dict:
    """Remote control: push the full panel config to the worker in one call.

    POST /panel/config (Bearer control token) carries users (uuid, limit,
    expire, used, countries), the proxy routes and settings. The worker stores
    everything in its dedicated KV and answers with a summary. This is the
    single "panel → worker" channel; individual /api/users calls stay for
    incremental updates.
    """
    domain = str(WORKER.get("worker_domain") or "").strip().lower()
    ctrl = str(WORKER.get("control_token") or "")
    if not domain or not ctrl or domain in ("localhost", "0.0.0.0", "127.0.0.1"):
        return {"ok": False, "detail": "worker not connected / no control token"}
    sync_inbounds = {iid for iid, ib in INBOUNDS.items()
                     if ((ib or {}).get("protocol") or "").lower() in ("worker", "tunnel", "reverse")}
    users = []
    async with USERS_LOCK:
        for uid, u in USERS.items():
            iids = u.get("inbound_ids") or ([u.get("inbound_id")] if u.get("inbound_id") else [])
            if not (set(iids) & sync_inbounds):
                continue
            cuuid = u.get("config_uuid") or uid
            deadline = 0
            if u.get("expire_at"):
                try:
                    deadline = int(datetime.fromisoformat(u["expire_at"]).timestamp())
                except Exception:
                    deadline = 0
            users.append({
                "uuid": cuuid,
                "remark": u.get("username", uid),
                "limit_bytes": int(u.get("traffic_limit_bytes") or 0),
                "expire": deadline,
                "used_bytes": int(u.get("traffic_used_bytes") or 0),
                "concurrent_connections": int(u.get("concurrent_connections") or 0),
                "countries": list(u.get("proxy_countries") or ([u.get("proxy_country")] if u.get("proxy_country") else [])),
                "disabled": (u.get("status") or "active") != "active",
            })
    locations = []
    for code, p in (WORKER.get("proxies") or {}).items():
        locations.append({"code": code, "country": p.get("country", code.upper()),
                          "proxy": p.get("proxy", ""), "port": 443,
                          "proxies": p.get("proxies", [p.get("proxy")])})
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            r = await client.post(
                f"https://{domain}/panel/config",
                headers={"Authorization": f"Bearer {ctrl}"},
                json={"users": users, "routes": {"locations": locations}, "settings": {}},
            )
        if r.status_code == 200:
            data = {}
            try:
                data = r.json()
            except Exception:
                pass
            async with WORKER_LOCK:
                WORKER["remote_status"] = "online"
                WORKER["last_heartbeat"] = now_ir().isoformat(timespec="seconds")
                WORKER["worker_user_count"] = int(data.get("users") or len(users))
                WORKER["worker_traffic_bytes"] = int(data.get("traffic") or 0)
                WORKER["worker_users_online"] = int(data.get("online") or 0)
            asyncio.create_task(save_state())
            return {"ok": True, "detail": "config pushed", "users": data.get("users", len(users))}
        return {"ok": False, "detail": f"worker returned HTTP {r.status_code}: {r.text[:120]}"}
    except Exception as e:
        return {"ok": False, "detail": str(e)}

async def _worker_pull_status() -> dict:
    """Pull live status from the worker's GET /panel/status API.

    Returns {ok, online, traffic, users} and records a heartbeat timestamp so
    the UI can show how fresh the remote state is.
    """
    domain = str(WORKER.get("worker_domain") or "").strip().lower()
    ctrl = str(WORKER.get("control_token") or "")
    if not domain or not ctrl or domain in ("localhost", "0.0.0.0", "127.0.0.1"):
        return {"ok": False, "detail": "worker not connected"}
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(f"https://{domain}/panel/status",
                                 headers={"Authorization": f"Bearer {ctrl}"})
        if r.status_code != 200:
            async with WORKER_LOCK:
                WORKER["remote_status"] = "unreachable"
            return {"ok": False, "detail": f"HTTP {r.status_code}"}
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        hb = now_ir().isoformat(timespec="seconds")
        async with WORKER_LOCK:
            WORKER["remote_status"] = "online"
            WORKER["last_heartbeat"] = hb
            WORKER["worker_user_count"] = int(data.get("users") or 0)
            WORKER["worker_traffic_bytes"] = int(data.get("traffic") or 0)
            WORKER["worker_users_online"] = int(data.get("online") or 0)
        asyncio.create_task(save_state())
        return {"ok": True, **data}
    except Exception as e:
        async with WORKER_LOCK:
            WORKER["remote_status"] = "unreachable"
        return {"ok": False, "detail": str(e)}

async def _worker_control_update() -> dict:
    """Push the proxy pool to the deployed VLESS worker via its admin API.

    The worker only accepts calls carrying the control token that was baked in
    at deploy time (Bearer auth). The proxy pool is stored in the worker's KV
    namespace (SPIDER_KV) and served from /api/locations.
    """
    domain = str(WORKER.get("worker_domain") or "").strip().lower()
    ctrl = str(WORKER.get("control_token") or "")
    if not domain or not ctrl or domain in ("localhost", "0.0.0.0", "127.0.0.1"):
        return {"ok": False, "detail": "worker not connected / no control token"}
    # Build the locations list from the country → proxy map.
    locations = []
    for code, p in (WORKER.get("proxies") or {}).items():
        loc = {"code": code, "country": p.get("country", code.upper()),
               "proxy": p.get("proxy", ""), "port": 443,
               "proxies": p.get("proxies", [p.get("proxy")])}
        locations.append(loc)
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.post(
                f"https://{domain}/api/proxies",
                headers={"Authorization": f"Bearer {ctrl}"},
                json={"locations": locations},
            )
        if r.status_code == 200:
            return {"ok": True, "detail": "worker updated"}
        return {"ok": False, "detail": f"worker returned HTTP {r.status_code}: {r.text[:120]}"}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


# ── Daily proxy source sync ──────────────────────────────────────────────────
# Source file format (ProxyIP-Daily.md by NiREvil):
#   ## 🇩🇪 Germany (517 proxies)      ← flag emoji encodes the ISO code
#   <details><summary>...</summary>
#   | IP | ISP | Location | Risk Score |
#   | <pre><code>94.141.123.243</code></pre> | ISP | Hesse, Frankfurt | badge |
# ISP-grouped sections (Google/Amazon/…) have no flag → skipped.
_FLAG_RE = re.compile(r"^##\s*([\U0001F1E6-\U0001F1FF]{2})\s*([^\s(][^()]*?)\s*\(\d+\s*proxies\)")
_IPCELL_RE = re.compile(r"<pre><code>\s*((?:\d{1,3}\.){3}\d{1,3}|[a-z0-9.-]+\.[a-z]{2,})\s*</code></pre>", re.I)
# A few sections show only a bare code (e.g. "AD") instead of a full name.
_CODE_NAME = {
    "AD": "Andorra", "BA": "Bosnia & Herzegovina", "BD": "Bangladesh",
    "DO": "Dominican Republic", "IS": "Iceland", "KG": "Kyrgyzstan", "SY": "Syria",
}


def _flag_to_code(flag: str) -> str:
    """Decode a flag emoji (regional indicators) into an ISO 3166-1 alpha-2 code."""
    cps = [ord(c) for c in flag]
    if len(cps) < 2 or not all(0x1F1E6 <= c <= 0x1F1FF for c in cps):
        return ""
    return "".join(chr(0x41 + (c - 0x1F1E6)) for c in cps)


def _code_to_flag(code: str) -> str:
    """Encode an ISO 3166-1 alpha-2 code into a flag emoji."""
    code = str(code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return ""
    return chr(0x1F1E6 + (ord(code[0]) - ord('A'))) + chr(0x1F1E6 + (ord(code[1]) - ord('A')))


def _parse_proxy_daily(text: str, limit_per_country: int = 0) -> dict:
    """Parse the daily markdown into {code: {country, proxy, port}}.

    Keep every unique proxy IP found in each country section by default.
    `limit_per_country` may be set to a positive integer for an explicit cap.
    Only sections with a flag emoji are used; ISP-grouped sections are skipped.
    """
    out: dict = {}
    lines = text.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        m = _FLAG_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        code = _flag_to_code(m.group(1)).lower()
        name = (m.group(2) or "").strip()
        if not code:
            i += 1
            continue
        if len(name) == 2 and name.isupper():
            name = _CODE_NAME.get(name.upper(), name)
        # Collect IP cells until the next '## ' section header.
        picked: list[str] = []
        j = i + 1
        while j < n:
            line = lines[j].strip()
            if line.startswith("## ") or line.startswith("---"):
                break
            if line.startswith("|") and "<pre><code>" in line:
                cell = _IPCELL_RE.search(line)
                if cell and cell.group(1) not in picked:
                    picked.append(cell.group(1))
                    if limit_per_country > 0 and len(picked) >= limit_per_country:
                        break
            j += 1
        if picked:
            out[code] = {
                "country": name or code.upper(),
                "proxy": picked[0],
                "port": 443,
                "proxies": picked,
            }
        i = j
    return out


async def _fetch_proxy_daily(url: str) -> str:
    """Fetch the daily proxy markdown, preferring the raw GitHub URL."""
    url = str(url or "").strip()
    if not url:
        raise ValueError("منبع پروکسی تنظیم نشده است")
    # GitHub blob page → raw URL so we get the file, not HTML.
    m = re.match(r"^https://github\.com/([^/]+)/([^/]+)/blob/(.+)$", url)
    if m:
        url = f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}"
    async with httpx.AsyncClient(timeout=40, follow_redirects=True) as client:
        r = await client.get(url)
    if r.status_code != 200:
        raise ValueError(f"دریافت منبع ناموفق بود (HTTP {r.status_code})")
    return r.text


async def _sync_worker_proxies_from_source() -> dict:
    """Fetch + parse the daily proxy source and push it to the deployed worker.

    Returns a summary dict. Under WORKER_SYNC_LOCK so the hourly loop and the
    manual button never run concurrently.
    """
    async with WORKER_SYNC_LOCK:
        source_url = WORKER.get("source_url", "")
        try:
            text = await _fetch_proxy_daily(source_url)
            parsed = _parse_proxy_daily(text)
            if not parsed:
                raise ValueError("در منبع، کشوری پیدا نشد (قالب تغییر کرده؟)")
            async with WORKER_LOCK:
                # Merge: entries manually added/edited in the panel (manual=True)
                # survive the source refresh, so admin edits are never wiped out.
                manual = {
                    code: p for code, p in (WORKER.get("proxies") or {}).items()
                    if p.get("manual")
                }
                parsed.update(manual)
                # v7 Worker country routes always target relay IPs on TCP 443.
                for _code, _loc in parsed.items():
                    _loc["port"] = 443
                WORKER["proxies"] = parsed
                WORKER["sync_count"] = int(WORKER.get("sync_count", 0)) + 1
            deploy_ok = True
            if WORKER.get("connected"):
                sc, sd = await _worker_deploy()
                deploy_ok = sc in (200, 201, 409)
                if not deploy_ok:
                    raise ValueError((sd.get("errors") or [{}])[0].get("message", "deploy failed"))
                # After deploy, tell the worker the new map via its admin API.
                await _worker_control_update()
            # Keep the default Worker inbound pointed at the worker domain.
            await _ensure_worker_inbound()
            async with WORKER_LOCK:
                WORKER["last_sync"] = now_ir().isoformat(timespec="seconds")
                WORKER["sync_error"] = ""
                WORKER["last_error"] = ""
            asyncio.create_task(save_state())
            log_activity("worker", f"پروکسی‌های Worker از منبع بروزرسانی شد ({len(parsed)} کشور)", "ok")
            return {
                "ok": True,
                "countries": len(parsed),
                "count": sum(len(v.get("proxies") or [v.get("proxy")]) for v in parsed.values()),
                "deployed": deploy_ok,
            }
        except Exception as e:
            msg = str(e)
            async with WORKER_LOCK:
                WORKER["sync_error"] = msg
                WORKER["last_error"] = msg
            asyncio.create_task(save_state())
            logger.warning(f"worker proxy sync failed: {msg}")
            return {"ok": False, "error": msg}


@app.get("/api/worker")
async def worker_get(_=Depends(require_auth)):
    """Worker status + proxy map (token is never exposed)."""
    async with WORKER_LOCK:
        return {"ok": True, **_worker_public()}


@app.post("/api/worker/setup")
async def worker_setup(request: Request, _=Depends(require_auth)):
    """Create a brand-new managed worker on the user's Cloudflare account.

    The panel never searches for an existing subdomain/worker. Instead it:
      1. verifies the API token and auto-detects the account id,
      2. ensures the account has a workers.dev subdomain (registers one if missing),
      3. generates a fresh random worker name (spider-xxxxxx),
      4. creates a dedicated KV namespace ({name}-db) for that worker only,
      5. deploys worker.js with the KV binding baked in,
      6. enables workers.dev for the new script,
      7. pushes users/routes via the remote-control API (/panel/config).
    """
    body = await request.json()
    token = str(body.get("token") or "").strip()
    # Account ID and Email are intentionally not collected from the user.
    # The account is discovered from the supplied API token.
    account_id = ""
    email = ""
    if not token:
        raise HTTPException(status_code=400, detail="token is required")

    # 1. Verify the API token.
    _is_gak = _is_cf_gak(token)
    verify_path = "/user/tokens/verify" if not _is_gak else "/user"
    code, data = await _cf_api("GET", verify_path, token, email=email)
    if code != 200 or not data.get("success"):
        msg = (data.get("errors") or [{}])[0].get("message", "invalid token")
        raise HTTPException(status_code=400, detail=f"Cloudflare token rejected: {msg}")

    # 2. Auto-detect account_id (list accounts and pick the first one).
    code_acc, data_acc = await _cf_api("GET", "/accounts?page=1&per_page=50", token, email=email)
    if code_acc == 200 and data_acc.get("result"):
        accounts = data_acc["result"]
        if isinstance(accounts, list) and len(accounts) > 0:
            account_id = accounts[0].get("id", "")
    if not account_id:
        # Cloudflare returns 403/10000 here when the token is valid but lacks
        # account-scoped Workers permissions. Surface the actual API error.
        detail = "Could not auto-detect Cloudflare Account ID. Token must include account-scoped Workers Scripts/Routes/KV permissions."
        errs = data_acc.get("errors") or []
        if errs:
            detail = "Cloudflare account lookup failed: " + "; ".join(str(e.get("message") or e.get("code") or "unknown error") for e in errs[:3])
        raise HTTPException(status_code=400, detail=detail)

    # 3. Ensure the account has a workers.dev subdomain; register one if absent
    #    so the user never has to open the Cloudflare dashboard manually.
    subdom = ""
    code, data = await _cf_api("GET", f"/accounts/{account_id}/workers/subdomain", token, email=email)
    if code == 200 and data.get("result"):
        subdom = str(data["result"].get("subdomain") or "").strip()
    if not subdom:
        # No subdomain yet → register a fresh one derived from the account id.
        reg_name = "spider-" + account_id[:8].lower()
        creg, dreg = await _cf_api(
            "PUT", f"/accounts/{account_id}/workers/subdomain", token,
            {"subdomain": reg_name}, email=email,
        )
        if creg in (200, 201) and dreg.get("result"):
            subdom = str(dreg["result"].get("subdomain") or reg_name).strip()
        else:
            no_subdomain = any(e.get("code") == 10007 for e in (data.get("errors") or []))
            if no_subdomain:
                raise HTTPException(
                    status_code=400,
                    detail="ساخت workers.dev subdomain ناموفق بود — توکن باید Workers Scripts:Edit داشته باشد. "
                           "(Could not register a workers.dev subdomain — the token needs Workers Scripts:Edit.)",
                )
            raise HTTPException(status_code=400, detail="could not resolve/register workers.dev subdomain — check the API token has Workers:Edit permission")
    if not subdom:
        raise HTTPException(status_code=400, detail="could not resolve workers.dev subdomain")

    # 4. Generate a fresh random worker name — every setup creates a NEW worker.
    # v7 always creates a fresh random Worker; the UI does not accept a custom name.
    worker_name = "spider-" + secrets.token_hex(4)
    worker_domain = _worker_safe_domain(f"{worker_name}.{subdom}.workers.dev")

    # 5. Save connection state, then create the main + dedicated tunnel/reverse
    # KVs before deployment so all three fixed inbounds are backed by isolated state.
    async with WORKER_LOCK:
        WORKER.update({
            "tunnel_enabled": True,
            "connected": True,
            "account_id": account_id,
            "worker_name": worker_name,
            "worker_domain": worker_domain,
            "worker_url": f"https://{worker_domain}",
            "token": token,
            # A new worker starts with a fresh dedicated KV namespace.
            "kv_namespace_id": "",
            "kv_namespace_title": "",
            "remote_status": "",
            "last_heartbeat": "",
            "worker_users_online": 0,
            "worker_traffic_bytes": 0,
            "worker_user_count": 0,
            "last_error": "",
        })
    kv_id = await _ensure_worker_kv()
    await _ensure_tunnel_kv()
    await _ensure_reverse_kv()
    sc, sd = await _worker_deploy()
    await _worker_enable_workers_dev()
    if sc not in (200, 201, 409):
        msg = (sd.get("errors") or [{}])[0].get("message", "deploy failed")
        async with WORKER_LOCK:
            WORKER["last_error"] = msg
        asyncio.create_task(save_state())
        raise HTTPException(status_code=500, detail=f"Worker deploy failed: {msg}")
    # Deployed with a fresh control token; push the full config (users+routes).
    ctrl_res = await _worker_push_config()
    if not ctrl_res.get("ok"):
        # Fall back to the per-user sync path when the bulk push failed.
        await _worker_control_update()
        await _worker_sync_users()
    # Pull back the worker-generated configs (its own domain + /route paths)
    # so each panel user's sub serves exactly what this worker accepts.
    await _worker_pull_all_users()
    # Auto-create/refresh the three fixed inbounds: Worker, Tunnel, Reverse.
    await _ensure_worker_inbound()
    await _ensure_tunnel_reverse_inbounds()
    await _worker_sync_users()
    async with WORKER_LOCK:
        WORKER["last_sync"] = now_ir().isoformat(timespec="seconds")
        WORKER["last_error"] = "" if ctrl_res.get("ok") else ctrl_res.get("detail", "")
    asyncio.create_task(save_state())
    log_activity("worker", f"Worker جدید ساخته شد ({worker_name} / KV: {WORKER.get('kv_namespace_title') or kv_id})", "ok")
    async with WORKER_LOCK:
        return {"ok": True, **_worker_public()}


@app.post("/api/worker/sync")
async def worker_sync(_=Depends(require_auth)):
    """Re-deploy the worker after proxy changes and re-push users/quotas."""
    if not WORKER.get("connected"):
        raise HTTPException(status_code=400, detail="worker is not connected")
    sc, sd = await _worker_deploy()
    await _worker_enable_workers_dev()
    if sc in (200, 201, 409):
        # Prefer the single remote-control push; fall back to per-user sync.
        push = await _worker_push_config()
        if not push.get("ok"):
            await _worker_control_update()
            await _worker_sync_users()
        await _worker_pull_all_users()
        await _ensure_worker_inbound()
        if WORKER.get("tunnel_enabled"):
            await _ensure_tunnel_reverse_inbounds()
            await _worker_sync_users()
    async with WORKER_LOCK:
        if sc in (200, 201, 409):
            WORKER["last_sync"] = now_ir().isoformat(timespec="seconds")
            WORKER["last_error"] = ""
            out = {"ok": True, **_worker_public()}
        else:
            msg = (sd.get("errors") or [{}])[0].get("message", "deploy failed")
            WORKER["last_error"] = msg
            out = {"ok": False, "error": msg, **_worker_public()}
    asyncio.create_task(save_state())
    return out


@app.post("/api/worker/sync-source")
async def worker_sync_source(_=Depends(require_auth)):
    """Fetch the daily proxy source now, update the pool and re-deploy."""
    res = await _sync_worker_proxies_from_source()
    if not res.get("ok"):
        return JSONResponse(status_code=400, content={"ok": False, "error": res.get("error")})
    async with WORKER_LOCK:
        return {"ok": True, **_worker_public(), "sync": res}


@app.post("/api/worker/settings")
async def worker_settings(request: Request, _=Depends(require_auth)):
    """Update worker source URL / auto-sync preference."""
    body = await request.json()
    async with WORKER_LOCK:
        if "source_url" in body:
            src = str(body["source_url"] or "").strip()
            if src:
                WORKER["source_url"] = src
        if "auto_sync" in body:
            WORKER["auto_sync"] = bool(body["auto_sync"])
        out = {"ok": True, **_worker_public()}
    asyncio.create_task(save_state())
    return out


@app.post("/api/worker/heartbeat")
async def worker_heartbeat(_=Depends(require_auth)):
    """Ping the worker's /panel/status and refresh remote counters (traffic,
    online users, user count). Called by the UI on a timer."""
    if not WORKER.get("connected"):
        raise HTTPException(status_code=400, detail="worker is not connected")
    res = await _worker_pull_status()
    async with WORKER_LOCK:
        out = {"ok": bool(res.get("ok")), "detail": res.get("detail", ""), **_worker_public()}
    return out

@app.delete("/api/worker")
async def worker_disconnect(_=Depends(require_auth)):
    """Remove the worker connection (keeps nothing sensitive)."""
    async with WORKER_LOCK:
        WORKER.clear()
        WORKER.update({
            "connected": False,
            "account_id": "",
            "worker_name": "",
            "worker_domain": "",
            "worker_url": "",
            "token": "",
            "kv_namespace_id": "",
            "kv_namespace_title": "",
            "remote_status": "",
            "last_heartbeat": "",
            "worker_users_online": 0,
            "worker_traffic_bytes": 0,
            "worker_user_count": 0,
            "proxies": {},
            "last_sync": "",
            "last_error": "",
            "source_url": "https://raw.githubusercontent.com/NiREvil/vless/main/sub/ProxyIP-Daily.md",
            "auto_sync": True,
            "sync_error": "",
            "sync_count": 0,
        })
    asyncio.create_task(save_state())
    log_activity("worker", "Worker قطع شد", "warn")
    return {"ok": True}


@app.post("/api/worker/proxies")
async def worker_add_proxy(request: Request, _=Depends(require_auth)):
    """Add or update a proxy country entry, then re-deploy the worker."""
    body = await request.json()
    code = str(body.get("code") or "").strip().lower()
    country = str(body.get("country") or "").strip()
    proxy = str(body.get("proxy") or "").strip()
    port = int(body.get("port") or 443)
    if not code or not country or not proxy:
        raise HTTPException(status_code=400, detail="code, country and proxy are required")
    if not re.fullmatch(r"[a-z0-9_-]{1,16}", code):
        raise HTTPException(status_code=400, detail="invalid country code (a-z0-9_-)")
    async with WORKER_LOCK:
        (WORKER.setdefault("proxies", {}))[code] = {"country": country, "proxy": proxy, "port": max(1, min(65535, port)), "manual": True}
    if WORKER.get("connected"):
        await worker_sync(None)
    else:
        asyncio.create_task(save_state())
    async with WORKER_LOCK:
        return {"ok": True, **_worker_public()}


@app.delete("/api/worker/proxies/{code}")
async def worker_del_proxy(code: str, _=Depends(require_auth)):
    """Remove a proxy country entry and re-deploy."""
    async with WORKER_LOCK:
        (WORKER.get("proxies") or {}).pop(code.lower(), None)
    if WORKER.get("connected"):
        await worker_sync(None)
    else:
        asyncio.create_task(save_state())
    async with WORKER_LOCK:
        return {"ok": True, **_worker_public()}


@app.get("/api/worker/locations")
async def worker_locations(_=Depends(require_auth)):
    """Location status list for the Map tab. Prefers live data from the worker."""
    async with WORKER_LOCK:
        if WORKER.get("connected") and WORKER.get("worker_url"):
            try:
                async with httpx.AsyncClient(timeout=12) as client:
                    r = await client.get(f"{WORKER['worker_url']}/api/locations")
                if r.status_code == 200:
                    return {"ok": True, "locations": r.json()}
            except Exception:
                pass
            return {"ok": True, "locations": [
                {"country": p.get("country"), "code": c, "proxy": p.get("proxy"),
                 "port": p.get("port", 443), "status": "online", "ping": 0}
                for c, p in (WORKER.get("proxies") or {}).items()
            ]}
    return {"ok": True, "locations": []}


@app.get("/api/worker/inbounds")
async def worker_inbounds(_=Depends(require_auth)):
    """Return worker inbounds with their country options for user creation modal."""
    async with INBOUNDS_LOCK:
        worker_inbounds = []
        for iid, ib in INBOUNDS.items():
            if (ib.get("protocol") or "").lower() == "worker":
                worker_inbounds.append({
                    "inbound_id": iid,
                    "name": ib.get("name", "Worker"),
                    "domain": ib.get("domain", ""),
                    "countries": [
                        {"code": c, "country": p.get("country", c.upper())}
                        for c, p in (WORKER.get("proxies") or {}).items()
                    ]
                })
    return {"ok": True, "inbounds": worker_inbounds}


# ══════════════════════════════════════════════════════════════════════════════
# TUNNEL — user → Railway → Cloudflare Worker → site
async def _ensure_tunnel_reverse_inbounds() -> tuple[str | None, str | None]:
    """Ensure fixed, system-managed Tunnel and Reverse inbounds exist."""
    panel_domain = _safe_host(SETTINGS.get("domain"), get_host())
    wdom = _worker_safe_domain(WORKER.get("worker_domain"))
    if not wdom:
        return None, None
    async with INBOUNDS_LOCK:
        tunnel_id = next((iid for iid, ib in INBOUNDS.items() if (ib.get("protocol") or "").lower() == "tunnel"), None)
        reverse_id = next((iid for iid, ib in INBOUNDS.items() if (ib.get("protocol") or "").lower() == "reverse"), None)
        if not tunnel_id:
            tunnel_id = "default-tunnel"
            INBOUNDS[tunnel_id] = {
                "name": "Tunnel (Railway → Worker)", "protocol": "tunnel", "port": 443,
                "network": "ws", "security": "tls", "domain": panel_domain,
                "external_domain": panel_domain, "sni": panel_domain, "external_port": 443,
                "fingerprint": "chrome", "reality_settings": {}, "xhttp_settings": {},
                "ws_settings": {"path": "/tunnel/{uuid}"}, "grpc_settings": {},
                "worker_domain": wdom, "system_managed": True, "created_at": datetime.now().isoformat(),
            }
        else:
            ib = INBOUNDS[tunnel_id]
            ib.update({"name":"Tunnel (Railway → Worker)", "domain":panel_domain, "external_domain":panel_domain,
                       "sni":panel_domain, "external_port":443, "port":443, "network":"ws", "security":"tls",
                       "ws_settings":{"path":"/tunnel/{uuid}"}, "worker_domain":wdom, "system_managed":True})
        if not reverse_id:
            reverse_id = "default-reverse"
            INBOUNDS[reverse_id] = {
                "name": "Reverse (Worker → Railway)", "protocol": "reverse", "port": 443,
                "network": "ws", "security": "tls", "domain": wdom,
                "external_domain": wdom, "sni": wdom, "external_port": 443,
                "fingerprint": "chrome", "reality_settings": {}, "xhttp_settings": {},
                "ws_settings": {"path": "/reverse/{uuid}"}, "grpc_settings": {},
                "panel_domain": panel_domain, "system_managed": True, "created_at": datetime.now().isoformat(),
            }
        else:
            ib = INBOUNDS[reverse_id]
            ib.update({"name":"Reverse (Worker → Railway)", "domain":wdom, "external_domain":wdom,
                       "sni":wdom, "external_port":443, "port":443, "network":"ws", "security":"tls",
                       "ws_settings":{"path":"/reverse/{uuid}"}, "panel_domain":panel_domain, "system_managed":True})
    await save_state()
    return tunnel_id, reverse_id

# The tunnel inbound is a TLS+WS inbound on the RAILWAY domain with path
# /tunnel/{uuid}. Railway forwards to the Worker; the Worker connects out to
# the destination. The tunnel has its own dedicated KV namespace (TUNNEL_KV).
# ══════════════════════════════════════════════════════════════════════════════

def _tunnel_log(msg: str):
    WORKER.setdefault("tunnel_logs", []).append(
        {"msg": str(msg)[:250], "time": now_ir().isoformat(timespec="seconds")})
    if len(WORKER["tunnel_logs"]) > 100:
        del WORKER["tunnel_logs"][:-100]


@app.post("/api/tunnel/create")
async def tunnel_create(_=Depends(require_auth)):
    """Create the fixed Tunnel + Reverse inbounds and their dedicated KVs."""
    if not WORKER.get("connected"):
        raise HTTPException(status_code=400, detail="worker is not connected")
    kv_id = await _ensure_tunnel_kv()
    rev_id = await _ensure_reverse_kv()
    if not kv_id or not rev_id:
        raise HTTPException(status_code=500, detail="could not create tunnel/reverse KV namespaces")
    async with WORKER_LOCK:
        WORKER["tunnel_enabled"] = True
        if not WORKER.get("tunnel_created_at"):
            WORKER["tunnel_created_at"] = now_ir().isoformat(timespec="seconds")
        _tunnel_log(f"Tunnel KV: {WORKER.get('tunnel_kv_namespace_title')}")
        _tunnel_log(f"Reverse KV: {WORKER.get('reverse_kv_namespace_title')}")
    tunnel_id, reverse_id = await _ensure_tunnel_reverse_inbounds()
    sc, sd = await _worker_deploy()
    ok_deploy = sc in (200, 201, 409)
    if ok_deploy:
        await _worker_sync_users()
    async with WORKER_LOCK:
        _tunnel_log("Worker با KVهای اختصاصی Tunnel/Reverse deploy شد" if ok_deploy else f"deploy ناموفق: {sc}")
    asyncio.create_task(save_state())
    log_activity("tunnel", "اینباندهای ثابت Tunnel و Reverse ایجاد شدند", "ok")
    return {"ok": True, "tunnel_inbound_id": tunnel_id, "reverse_inbound_id": reverse_id, **_worker_public(), "deployed": ok_deploy}


@app.get("/api/tunnel/status")
async def tunnel_status(_=Depends(require_auth)):
    """Tunnel tab data: ping server→worker, details, logs."""
    wdom = str(WORKER.get("worker_domain") or "").strip().lower()
    ping_ms = None
    if wdom:
        t0 = time.time()
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(f"https://{wdom}/")
            if r.status_code < 500:
                ping_ms = round((time.time() - t0) * 1000)
        except Exception:
            ping_ms = None
    tid = next((iid for iid, ib in INBOUNDS.items()
                if ((ib or {}).get("protocol") or "").lower() == "tunnel"), None)
    rid = next((iid for iid, ib in INBOUNDS.items()
                if ((ib or {}).get("protocol") or "").lower() == "reverse"), None)
    # Reverse info (shown under the tunnel info in the Tunnel tab).
    rev_ping_ms = None
    if wdom:
        t0 = time.time()
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r2 = await client.get(f"https://{wdom}/health")
            if r2.status_code < 500:
                rev_ping_ms = round((time.time() - t0) * 1000)
        except Exception:
            rev_ping_ms = None
    return {
        "ok": True,
        "worker_connected": bool(WORKER.get("connected")),
        "tunnel_enabled": bool(WORKER.get("tunnel_enabled")),
        "inbound_exists": tid is not None,
        "ping_ms": ping_ms,
        "worker_url": WORKER.get("worker_url", ""),
        "worker_domain": wdom,
        "panel_domain": _safe_host(SETTINGS.get("domain"), get_host()),
        "tunnel_kv_title": WORKER.get("tunnel_kv_namespace_title", ""),
        "tunnel_kv_id": WORKER.get("tunnel_kv_namespace_id", ""),
        "reverse_enabled": rid is not None,
        "reverse_ping_ms": rev_ping_ms,
        "reverse_path": f"/reverse/{{uuid}}" if rid else "",
        "reverse_kv_title": WORKER.get("reverse_kv_namespace_title", ""),
        "reverse_kv_id": WORKER.get("reverse_kv_namespace_id", ""),
        "last_heartbeat": WORKER.get("last_heartbeat", ""),
        "remote_status": WORKER.get("remote_status", ""),
        "logs": list(WORKER.get("tunnel_logs") or [])[-30:],
    }


@app.post("/api/tunnel/reverse")
async def tunnel_reverse_toggle(request: Request, _=Depends(require_auth)):
    """Compatibility endpoint: Reverse is always provisioned as its own fixed inbound."""
    if not WORKER.get("connected"):
        raise HTTPException(status_code=400, detail="worker is not connected")
    _, reverse_id = await _ensure_tunnel_reverse_inbounds()
    await _worker_sync_users()
    return {"ok": True, "enabled": True, "reverse_inbound_id": reverse_id, **_worker_public()}


# ══════════════════════════════════════════════════════════════════════════════
# IP SCANNER endpoints — live-saved scanned IPs + DNS resolve for the TCP tab
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/scanner/ips/{ctype}")
async def scanner_get_ips(ctype: str, _=Depends(require_auth)):
    """Return the live-saved ip:port list for a scanned source (cf | railway)."""
    ctype = ctype.strip().lower()
    if ctype not in _SCANNED_TYPES:
        raise HTTPException(status_code=400, detail="invalid scanner source")
    return {"ok": True, "type": ctype, "ips": _read_scanned_ips(ctype), "seq": SCANNED_SEQ.get(ctype, 0)}


@app.get("/api/scanner/cf-subnets")
async def scanner_cf_subnets(_=Depends(require_auth)):
    """Return Cloudflare subnets from cf_subnets.txt for IP range generation."""
    subnets_file = Path(os.path.dirname(os.path.abspath(__file__))) / "data" / "cf_subnets.txt"
    if not subnets_file.is_file():
        subnets_file = DATA_DIR / "cf_subnets.txt"
    if not subnets_file.is_file():
        return {"subnets": []}
    lines = subnets_file.read_text(errors="ignore").splitlines()
    subnets = [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
    return {"subnets": subnets}


@app.get("/api/scanner/sni-list")
async def scanner_sni_list(_=Depends(require_auth)):
    """Return SNI list from sni-list.txt for spoof scanning."""
    sni_file = Path(os.path.dirname(os.path.abspath(__file__))) / "data" / "sni-list.txt"
    if not sni_file.is_file():
        sni_file = DATA_DIR / "sni-list.txt"
    if not sni_file.is_file():
        return {"sni_list": []}
    lines = sni_file.read_text(errors="ignore").splitlines()
    snis = [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
    return {"sni_list": snis}


@app.post("/api/scanner/save")
async def scanner_save_ips(request: Request, _=Depends(require_auth)):
    """Live-save found ip:port entries to the source file (first 10 kept).

    Every write is guarded by a per-type sequence number: the client sends the
    seq of the last write it saw, and any write carrying an older seq is dropped.
    This guarantees a clear() can never be undone by a scan save that was already
    in flight when the user clicked "پاک کردن".
    """
    body = await request.json()
    ctype = str(body.get("type") or "").strip().lower()
    if ctype not in _SCANNED_TYPES:
        raise HTTPException(status_code=400, detail="invalid scanner source")
    raw = body.get("ips") or []
    replace = bool(body.get("replace"))
    cur_seq = SCANNED_SEQ.get(ctype, 0)
    sent_seq = int(body.get("seq") or 0)
    # Stale write (clear landed first, or an older save raced a newer clear).
    if sent_seq != cur_seq:
        return {"ok": False, "stale": True, "type": ctype, "seq": cur_seq, "ips": _read_scanned_ips(ctype)}
    entries = []
    for x in raw[:_SCANNED_MAX]:
        x = str(x).strip()
        if not x:
            continue
        if ":" in x:
            ip, _, port = x.rpartition(":")
        elif " " in x:
            ip, _, port = x.partition(" ")
        else:
            ip, port = x, "443"
        ip, port = ip.strip(), port.strip()
        if ip and port:
            entries.append(f"{ip}:{port}")
    merged = _save_scanned_ips(ctype, entries, replace=replace)
    SCANNED_SEQ[ctype] = cur_seq + 1
    return {"ok": True, "type": ctype, "ips": merged, "seq": SCANNED_SEQ[ctype]}


@app.get("/api/scanner/resolve")
async def scanner_resolve(host: str, _=Depends(require_auth)):
    """Resolve a hostname to its A/AAAA IPs (used by the TCP scanner tab)."""
    host = str(host or "").strip().lower()
    if not host:
        raise HTTPException(status_code=400, detail="empty host")
    ips = []
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, None, proto=6)
        for info in infos:
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return {"ok": True, "host": host, "ips": ips[:8]}


@app.post("/api/scanner/ping-batch")
async def scanner_ping_batch(request: Request, _=Depends(require_auth)):
    """TCP-connect latency check for arbitrary ip[:port] targets.

    The IP scanner generates candidate CF / Railway IPs in the browser and sends
    them here in batches; the panel measures real connect latency so the result
    is reliable (browsers cannot open raw TCP sockets).
    """
    body = await request.json()
    targets = body.get("targets") or []
    timeout = max(0.4, min(float(body.get("timeout") or 2.0), 6.0))
    if isinstance(targets, str):
        targets = [x for x in targets.replace(",", " ").split() if x]
    targets = [str(t).strip() for t in targets][:150]

    sem = asyncio.Semaphore(20)

    async def probe(t):
        if ":" in t:
            ip, _, port = t.rpartition(":")
        else:
            ip, port = t, "443"
        ip = ip.strip()
        try:
            port = int(port.strip())
        except Exception:
            port = 443
        async with sem:
            t0 = time.time()
            try:
                rdr, wtr = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
                lat = int((time.time() - t0) * 1000)
                try:
                    wtr.close()
                    await wtr.wait_closed()
                except Exception:
                    pass
                return {"target": t, "ip": ip, "port": port, "latency_ms": lat, "ok": True}
            except Exception:
                return {"target": t, "ip": ip, "port": port, "latency_ms": None, "ok": False}

    results = await asyncio.gather(*(probe(t) for t in targets))
    results.sort(key=lambda r: (not r["ok"], r["latency_ms"] if r["latency_ms"] is not None else 10 ** 9))
    return {"ok": True, "count": len(results), "results": results}


# ══════════════════════════════════════════════════════════════════════════════
# SNI SCANNER endpoints — scan SNI list and find fastest for Reality
# ══════════════════════════════════════════════════════════════════════════════

def _sni_scan_file() -> Path:
    """Path to the SNI scan source file."""
    p = Path(os.path.dirname(os.path.abspath(__file__))) / "data" / "sni_reality_for_scan.txt"
    if p.is_file():
        return p
    return DATA_DIR / "sni_reality_for_scan.txt"


def _sni_result_file() -> Path:
    """Path to the SNI scan results file."""
    p = Path(os.path.dirname(os.path.abspath(__file__))) / "data" / "sni_reality.txt"
    if p.is_file():
        return p
    return DATA_DIR / "sni_reality.txt"


def _read_sni_list() -> list:
    """Read SNI list from the scan source file."""
    f = _sni_scan_file()
    if not f.is_file():
        return []
    out = []
    for line in f.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Clean markdown links: [text](url) → text
        if line.startswith("[") and "](" in line:
            line = line.split("](")[0].lstrip("[")
        out.append(line)
    return out


def _read_sni_results() -> list:
    """Read the fastest SNIs from the results file."""
    f = _sni_result_file()
    if not f.is_file():
        return []
    out = []
    for line in f.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        sni = parts[0].strip()
        latency = int(parts[1].strip()) if len(parts) > 1 else 0
        if sni:
            out.append({"sni": sni, "latency_ms": latency})
    return out


@app.get("/api/scanner/sni-check")
async def scanner_sni_check(host: str, port: int = 443, connect_host: str = "", connect_port: int = 0, _=Depends(require_auth)):
    """Perform a TLS handshake with SNI=host against an explicit endpoint.

    This is important for Reality/SNI scanning: measuring each SNI against its
    own DNS target does not tell us which SNI is fastest for the selected server.
    When connect_host is omitted, host remains the connection target for backward
    compatibility.
    """
    import ssl
    target_sni = _normalize_reality_sni(host)
    target_port = _parse_port(port, 443, "port")
    endpoint_host = str(connect_host or "").strip().lower() or str(SETTINGS.get("domain") or get_host()).strip().lower() or target_sni
    endpoint_port = _parse_port(connect_port, target_port, "connect_port")
    started = time.perf_counter()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(endpoint_host, endpoint_port, ssl=ctx, server_hostname=target_sni), timeout=3.0
        )
        ms = round((time.perf_counter() - started) * 1000, 2)
        try:
            writer.close(); await writer.wait_closed()
        except Exception:
            pass
        return {"ok": True, "sni": target_sni, "latency_ms": ms, "port": endpoint_port, "connect_host": endpoint_host}
    except Exception as e:
        return {"ok": False, "sni": target_sni, "latency_ms": round((time.perf_counter()-started)*1000,2), "port": endpoint_port, "connect_host": endpoint_host, "error": str(e)[:180]}


@app.get("/api/scanner/sni-scan-list")
async def scanner_sni_scan_list(_=Depends(require_auth)):
    """Return the SNI list for scanning."""
    snis = _read_sni_list()
    return {"ok": True, "snis": snis, "count": len(snis)}


@app.get("/api/scanner/sni-results")
async def scanner_sni_results(_=Depends(require_auth)):
    """Return the fastest SNIs from previous scans."""
    results = _read_sni_results()
    return {"ok": True, "results": results}


@app.post("/api/scanner/sni-clear-results")
async def scanner_sni_clear_results(_=Depends(require_auth)):
    """Clear live SNI results before a new manual/auto scan."""
    f = _sni_result_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# Fastest SNIs for Reality\n# Format: sni|latency_ms\n", encoding="utf-8")
    return {"ok": True, "results": []}


@app.post("/api/scanner/sni-save-results")
async def scanner_sni_save_results(request: Request, _=Depends(require_auth)):
    body = await request.json()
    incoming = body.get("results") or []
    top_n = max(1, min(int(body.get("top") or 3), 10))
    merged = {}
    for old in _read_sni_results():
        name = str(old.get("sni") or "").strip().lower()
        try: lat = float(old.get("latency_ms"))
        except Exception: continue
        if name and lat >= 0: merged[name] = lat
    for r in incoming:
        name = str(r.get("sni") or "").strip().lower()
        try: lat = float(r.get("latency_ms"))
        except Exception: continue
        if name and lat >= 0 and (name not in merged or lat < merged[name]): merged[name] = lat
    results = [{"sni":k,"latency_ms":round(v,2)} for k,v in merged.items()]
    results.sort(key=lambda x:x["latency_ms"])
    results = results[:top_n]
    f=_sni_result_file(); f.parent.mkdir(parents=True,exist_ok=True)
    f.write_text("# Fastest SNIs for Reality\n# Format: sni|latency_ms\n"+"\n".join(f"{r['sni']}|{r['latency_ms']}" for r in results)+"\n",encoding="utf-8")
    return {"ok":True,"saved":len(results),"results":results}


@app.get("/api/scanner/sni-fastest")
async def scanner_sni_fastest(connect_host: str = "", connect_port: int = 443, refresh: int = 0, _=Depends(require_auth)):
    """Return the fastest saved SNI, or optionally refresh by scanning the source list.

    refresh=1 is used by the lightning action when there are no saved results.
    The scan is bounded and concurrent so the button never depends on a stale file.
    """
    results = _read_sni_results()
    if results and not int(refresh or 0):
        return {"ok": True, "sni": results[0]["sni"], "latency_ms": results[0].get("latency_ms", 0), "results": results}
    if int(refresh or 0):
        snis = _read_sni_list()[:100]
        endpoint = str(connect_host or "").strip().lower()
        p = _parse_port(connect_port, 443, "connect_port")
        sem = asyncio.Semaphore(8)
        async def one(s):
            async with sem:
                return await scanner_sni_check(s, p, endpoint, p)
        checked = await asyncio.gather(*(one(s) for s in snis), return_exceptions=True)
        fresh = [{"sni": r["sni"], "latency_ms": r["latency_ms"]} for r in checked if isinstance(r, dict) and r.get("ok")]
        fresh.sort(key=lambda x: x["latency_ms"])
        if fresh:
            fresh = fresh[:10]
            f = _sni_result_file(); f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("# Fastest SNIs\n# Format: sni|latency_ms\n" + "\n".join(f"{r['sni']}|{round(r['latency_ms'],2)}" for r in fresh) + "\n", encoding="utf-8")
            return {"ok": True, "sni": fresh[0]["sni"], "latency_ms": fresh[0]["latency_ms"], "results": fresh}
    if results:
        return {"ok": True, "sni": results[0]["sni"], "latency_ms": results[0].get("latency_ms", 0), "results": results}
    return {"ok": False, "sni": "", "latency_ms": 0, "results": []}


# ══════════════════════════════════════════════════════════════════════════════
# PROXY IP endpoints
# ══════════════════════════════════════════════════════════════════════════════

# (removed dead proxy-ips endpoints — proxy source is now the daily GitHub list)


# ══════════════════════════════════════════════════════════════════════════════
# SERVER INFO + PANEL API KEY + NODE MANAGEMENT + BOT + SUBSCRIPTION UUID
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/server-info")
async def get_server_info(_=Depends(require_panel_or_node)):
    """Return the detected server IP and country."""
    async with SERVER_INFO_LOCK:
        return dict(SERVER_INFO)


@app.get("/api/panel-api-key")
async def get_panel_api_key(_=Depends(require_auth)):
    """Return the panel API key (only shown once or on request)."""
    global PANEL_API_KEY
    if not PANEL_API_KEY:
        from spider_features import generate_panel_api_key
        PANEL_API_KEY = generate_panel_api_key()
        asyncio.create_task(save_state())
    return {"api_key": PANEL_API_KEY}


@app.post("/api/panel-api-key/regenerate")
async def regenerate_panel_api_key(_=Depends(require_auth)):
    """Regenerate the panel API key."""
    global PANEL_API_KEY
    from spider_features import generate_panel_api_key
    PANEL_API_KEY = generate_panel_api_key()
    asyncio.create_task(save_state())
    log_activity("settings", "کلید API پنل بازتولید شد", "ok")
    return {"api_key": PANEL_API_KEY}


@app.post("/api/panel-api-key/verify")
async def verify_panel_api_key(request: Request, _=Depends(require_auth)):
    """Verify a panel API key (for Node connections)."""
    body = await request.json()
    key = str(body.get("api_key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="API Key is required")
    if key != PANEL_API_KEY:
        raise HTTPException(status_code=401, detail="API Key is invalid")
    return {"ok": True}


# ── NODE inbound / config helpers ────────────────────────────────────────────
async def _ensure_node_inbound() -> str | None:
    """Legacy compatibility: node selection now belongs to Config inbounds."""
    return None

def _node_base_url(url: str) -> str:
    return str(url or "").strip().rstrip("/")

def _node_config_for_user(node: dict, config_uuid: str, username: str) -> str:
    base = _node_base_url(node.get("url"))
    if not base or not config_uuid:
        return ""
    raw = re.sub(r"^https?://", "", base).rstrip("/")
    # Remote node panels are themselves HTTPS endpoints; the node's VLESS
    # listener is exposed through the same hostname on 443.
    host = raw.split("/", 1)[0]
    if not host:
        return ""
    path = f"/ws/{config_uuid}"
    params = ("encryption=none&security=tls&type=ws"
              f"&host={quote(host)}&path={quote(path, safe='')}&sni={quote(host)}"
              "&fp=chrome&alpn=http/1.1")
    return f"vless://{config_uuid}@{host}:443?{params}#{quote(f'Spider-{username}')}"

async def _refresh_node_user_configs(user_ids=None):
    """Refresh node config lists for users whose selected Config inbounds include nodes."""
    async with NODES_LOCK:
        nodes_snap = list(NODES.items())
    if not nodes_snap:
        return 0
    async with USERS_LOCK:
        targets = []
        for uid, u in USERS.items():
            if user_ids and uid not in user_ids:
                continue
            selected_ids = await _selected_node_ids_for_user(u)
            if selected_ids:
                targets.append((uid, u, selected_ids))
        for uid, u, selected_ids in targets:
            cfgs=[]
            for nid,node in nodes_snap:
                if nid in selected_ids:
                    cfg=_node_config_for_user(node,u.get("config_uuid",uid),u.get("username",uid))
                    if cfg: cfgs.append({"node_id":nid,"node":node.get("name",nid),"config":cfg})
            u["node_configs"]=cfgs
    if targets:
        asyncio.create_task(save_state())
    return len(targets)

# ── NODE Management ──────────────────────────────────────────────────────────

@app.get("/api/nodes")
async def list_nodes(_=Depends(require_auth)):
    """List all registered nodes."""
    async with NODES_LOCK:
        result = []
        for nid, node in NODES.items():
            result.append({
                "id": nid,
                "name": node.get("name", ""),
                "url": node.get("url", ""),
                "region": node.get("region", ""),
                "status": node.get("status", "unknown"),
                "api_key": node.get("api_key", "")[:8] + "***" if node.get("api_key") else "",
                "last_sync": node.get("last_sync", ""),
                "created_at": node.get("created_at", ""),
                "enabled_inbounds": [iid for iid, ib in INBOUNDS.items() if (ib.get("inbound_type") or "config").lower() == "config" and nid in (ib.get("enabled_node_ids") or [])],
            })
    return {"nodes": result}


@app.post("/api/nodes")
async def add_node(request: Request, _=Depends(require_auth)):
    """Add a node by verifying its API Key."""
    body = await request.json()
    api_key = (body.get("api_key") or "").strip()
    name = (body.get("name") or "").strip()[:40]
    if not api_key:
        raise HTTPException(status_code=400, detail="API Key is required")
    async with NODES_LOCK:
        if len(NODES) >= MAX_NODES:
            raise HTTPException(status_code=400, detail="حداکثر تعداد نودها (10) رسیده است")
        # Prevent duplicate keys
        for existing in NODES.values():
            if existing.get("api_key") == api_key:
                raise HTTPException(status_code=409, detail="این API Key قبلاً ثبت شده است")
    # Verify the API key by making a test request
    url = (body.get("url") or "").strip().rstrip("/")
    if not url:
        raise HTTPException(status_code=400, detail="آدرس نود الزامی است")
    from spider_features import detect_server_info, code_to_flag, node_region_prefix
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Try to verify by calling the node's /api/server-info
            r = await client.get(f"{url}/api/server-info", headers={"X-API-Key": api_key})
            if r.status_code != 200:
                # Try alternative auth
                r = await client.get(f"{url}/api/server-info", params={"api_key": api_key})
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"خطا در اتصال به نود: {str(e)[:100]}")
    # Get the server info from this node for region naming
    node_country_flag = ""
    node_info = {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{url}/api/server-info", headers={"X-API-Key": api_key})
            if r.status_code == 200:
                node_info = r.json()
                node_country_flag = node_info.get("country_flag", "")
    except Exception:
        pass
    # Get our own server info for region naming
    async with SERVER_INFO_LOCK:
        our_info = dict(SERVER_INFO)
    flag = node_country_flag or our_info.get("country_flag", "🌐")
    node_id = generate_short_id()
    uname = name or f"node-{len(NODES) + 1}"
    region = f"spider-{flag}-{uname}"
    async with NODES_LOCK:
        NODES[node_id] = {
            "name": uname,
            "api_key": api_key,
            "url": url,
            "region": region,
            "country": node_info.get("country", ""),
            "country_code": node_info.get("country_code", ""),
            "country_flag": node_country_flag,
            "status": "active",
            "last_sync": datetime.now().isoformat(),
            "created_at": datetime.now().isoformat(),
        }
    # Existing Config inbounds with this node selected immediately begin using it.
    existing_users=[]
    async with USERS_LOCK:
        for uid,u in USERS.items():
            if node_id in await _selected_node_ids_for_user(u):
                existing_users.append((uid,dict(u)))
    for uid,u in existing_users:
        await _create_user_on_nodes(u.get("config_uuid",uid), {"username":u.get("username",uid)}, only_node_id=node_id)
    await _refresh_node_user_configs([uid for uid,_ in existing_users])
    asyncio.create_task(save_state())
    log_activity("node", f"نود «{uname}» اضافه شد", "ok")
    return {"ok": True, "node_id": node_id, "name": uname, "region": region}


@app.delete("/api/nodes/{node_id}")
async def delete_node(node_id: str, _=Depends(require_auth)):
    """Delete a node."""
    async with NODES_LOCK:
        if node_id not in NODES:
            raise HTTPException(status_code=404, detail="نود پیدا نشد")
        name = NODES[node_id].get("name", node_id)
        del NODES[node_id]
        for ib in INBOUNDS.values():
            if (ib.get("inbound_type") or "config").lower() == "config":
                ib["enabled_node_ids"] = [x for x in (ib.get("enabled_node_ids") or []) if str(x) != str(node_id)]
    asyncio.create_task(_refresh_node_user_configs())
    asyncio.create_task(save_state())
    log_activity("node", f"نود «{name}» حذف شد", "warn")
    return {"ok": True, "deleted": node_id}


# ── Delete user from all nodes ───────────────────────────────────────────────
async def _delete_user_from_nodes(config_uuid: str):
    """Best-effort: delete a user (by config_uuid) from all active nodes.
    Failure on one node does NOT stop deletion from others."""
    async with NODES_LOCK:
        nodes_snap = dict(NODES)
    results = []
    for nid, node in nodes_snap.items():
        api_key = node.get("api_key", "")
        url = (node.get("url") or "").strip().rstrip("/")
        if not url or not api_key:
            continue
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.delete(
                    f"{url}/api/users/{config_uuid}",
                    headers={"X-API-Key": api_key},
                )
                ok = r.status_code in (200, 204)
                results.append({"node": node.get("name", nid), "ok": ok, "status": r.status_code})
        except Exception as e:
            results.append({"node": node.get("name", nid), "ok": False, "error": str(e)[:100]})
            logger.warning("delete user from node %s failed: %s", node.get("name"), e)
    return results


# ── NODE Inbound: create users on selected nodes ─────────────────────────────
async def _selected_node_ids_for_user(user: dict) -> set[str]:
    ids = set()
    for iid in (user.get("inbound_ids") or []):
        ib = INBOUNDS.get(iid) or {}
        if (ib.get("inbound_type") or "config").lower() in ("config", "node"):
            ids.update(str(x) for x in (ib.get("enabled_node_ids") or []))
    return ids


async def _sync_user_to_selected_nodes(config_uuid: str, user_data: dict, selected_ids=None):
    """Make remote NODE users match the authoritative checkbox selection."""
    async with NODES_LOCK:
        nodes_snap = dict(NODES)
    if selected_ids is None:
        selected_ids = set(nodes_snap)
    else:
        selected_ids = {str(x) for x in selected_ids}
    results = []
    # Create only on checked nodes.
    create_results = await _create_user_on_nodes(config_uuid, user_data, only_node_ids=selected_ids)
    results.extend(create_results)
    # Explicitly remove from unchecked nodes.
    unselected = set(nodes_snap) - selected_ids
    for nid in unselected:
        node = nodes_snap[nid]
        api_key, url = node.get("api_key", ""), _node_base_url(node.get("url"))
        if not url or not api_key:
            continue
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.delete(f"{url}/api/users/{config_uuid}", headers={"X-API-Key": api_key})
            results.append({"node": node.get("name", nid), "ok": r.status_code in (200, 204, 404), "status": r.status_code, "detail": "deselected"})
        except Exception as exc:
            results.append({"node": node.get("name", nid), "ok": False, "error": str(exc)[:100]})
    await _refresh_node_user_configs()
    return results


async def _create_user_on_nodes(config_uuid: str, user_data: dict, only_node_id: str | None = None, only_node_ids=None):
    """Create/sync the same user UUID only on the selected active nodes."""
    async with NODES_LOCK:
        nodes_snap = dict(NODES)
    if only_node_ids is not None:
        allowed = {str(x) for x in only_node_ids}
        nodes_snap = {nid: node for nid, node in nodes_snap.items() if str(nid) in allowed}
    elif only_node_id:
        nodes_snap = {only_node_id: nodes_snap[only_node_id]} if only_node_id in nodes_snap else {}
    results = []
    collected = []
    for nid, node in nodes_snap.items():
        api_key = node.get("api_key", "")
        url = _node_base_url(node.get("url"))
        if not url or not api_key:
            continue
        try:
            inbound_id = "default"
            async with httpx.AsyncClient(timeout=15) as client:
                ir = await client.get(f"{url}/api/inbounds", headers={"X-API-Key": api_key})
                if ir.status_code == 200:
                    remote_inbounds = ir.json().get("inbounds") or []
                    selectable = [x for x in remote_inbounds if str(x.get("protocol") or "").lower() not in ("worker", "tunnel", "reverse", "telegram", "wireguard", "node")]
                    if selectable:
                        inbound_id = selectable[0].get("inbound_id") or "default"
                payload = {
                    "username": user_data.get("username", ""),
                    "password": secrets.token_urlsafe(12),
                    "config_uuid": config_uuid,
                    "traffic_limit_gb": 0,
                    "expire_days": 0,
                    "concurrent_connections": 0,
                    "inbound_id": inbound_id,
                }
                r = await client.post(f"{url}/api/users", headers={"X-API-Key": api_key, "Content-Type": "application/json"}, json=payload)
            ok = r.status_code in (200, 201, 409)
            detail = ""
            remote_cfg = ""
            if r.status_code in (200, 201):
                try: remote_cfg = str(r.json().get("config") or "").strip()
                except Exception: pass
            elif r.status_code == 409:
                detail = "already exists"
            if not remote_cfg:
                remote_cfg = _node_config_for_user(node, config_uuid, user_data.get("username", config_uuid))
            if remote_cfg:
                collected.append({"node_id": nid, "node": node.get("name", nid), "config": remote_cfg})
            results.append({"node": node.get("name", nid), "ok": ok, "status": r.status_code, "detail": detail})
        except Exception as e:
            results.append({"node": node.get("name", nid), "ok": False, "error": str(e)[:100]})
            logger.warning("create user on node %s failed: %s", node.get("name"), e)
    async with USERS_LOCK:
        for uid, u in USERS.items():
            if u.get("config_uuid") == config_uuid:
                u["node_configs"] = collected
                break
    asyncio.create_task(save_state())
    return results


# ── SUBSCRIPTION INDEX (by UUID) ────────────────────────────────────────────

@app.get("/subs/{uuid}")
async def subscription_by_uuid(uuid: str, request: Request):
    """New subscription URL: /subs/{config_uuid}"""
    import base64

    # 1) Search by config_uuid
    target_user = None
    target_uid = None
    if _is_valid_uuid(uuid):
        async with USERS_LOCK:
            for uid, u in USERS.items():
                if u.get("config_uuid") == uuid:
                    target_user = u
                    target_uid = uid
                    break

    if not target_user:
        # Try subscription_uuid
        async with USERS_LOCK:
            for uid, u in USERS.items():
                if u.get("subscription_uuid") == uuid:
                    target_user = u
                    target_uid = uid
                    break

    if target_user:
        # Reuse the same logic as /sub/{identifier}
        if _user_uses_worker_inbound(target_user):
            target_user = await _worker_pull_user(target_uid, target_user)
            USERS[target_uid] = target_user
        host = SETTINGS.get("domain") or get_host()
        username = target_user.get("username", target_uid)
        configs = list(target_user.get("worker_configs") or [])
        inbound_ids = target_user.get("inbound_ids") or []
        stored_path_user = (target_user.get("path") or "").strip()
        for iid_ in inbound_ids:
            ib = INBOUNDS.get(iid_)
            try:
                _p = (ib.get("protocol") if ib else "").lower()
                _s = (ib.get("security") if ib else "").lower()
                if ib and (_p == "reality" or _s == "reality"):
                    if not str(ib.get("external_domain") or "").strip() or not str(ib.get("external_port") or "").strip():
                        continue
                if ib and _p == "worker":
                    if not target_user.get("worker_configs"):
                        configs.extend(_worker_configs(target_uid, target_user, ib, stored_path_user, f"Spider-{username}"))
                else:
                    cfg = generate_user_config(target_uid, target_user, iid_)
                    if cfg:
                        configs.append(cfg)
            except Exception:
                continue
        if target_user.get("node_configs"):
            configs.extend(c.get("config") for c in target_user.get("node_configs") if c.get("config"))
        if not configs:
            fallback_config = generate_user_config(target_uid, target_user, target_user.get("inbound_id"))
            configs = [fallback_config] if fallback_config else []

        # Custom scanned-IP configs
        custom_cfgs = generate_custom_ip_configs(target_uid, target_user)
        for cfg in custom_cfgs.get("railway", []) + custom_cfgs.get("cf", []):
            configs.append(cfg)

        if target_user.get("sni_spoof_v2box"):
            configs.extend(generate_sni_spoof_configs(target_uid, target_user))

        if not configs:
            raise HTTPException(status_code=404, detail="no configs found")

        status_config = generate_status_config(target_user, configs)
        all_configs = [status_config] + configs if status_config else configs

        content = base64.b64encode("\n".join(all_configs).encode()).decode()
        return Response(content=content, media_type="text/plain",
                        headers={"profile-title": quote(username),
                                  "profile-update-interval": "12",
                                  "support-url": "https://t.me/spider_vpn1"})

    raise HTTPException(status_code=404, detail="not found")


@app.get("/api/subscription/index")
async def subscription_index(_=Depends(require_auth)):
    """Return the subscription index organized by Tunnel / Node categories.
    Tunnel: Worker, Tunnel, Reverse, Yours (custom-created configs).
    Node: all node inbound configs."""
    host = SETTINGS.get("domain") or get_host()

    # Gather categories
    tunnel_items = []
    node_items = []

    async with INBOUNDS_LOCK:
        for iid, ib in INBOUNDS.items():
            proto = (ib.get("protocol") or "").lower()
            name = ib.get("name", iid)
            if proto == "worker":
                tunnel_items.append({"id": iid, "name": name, "category": "Worker", "type": "tunnel"})
            elif proto == "tunnel":
                tunnel_items.append({"id": iid, "name": name, "category": "Tunnel", "type": "tunnel"})
            elif proto == "reverse":
                tunnel_items.append({"id": iid, "name": name, "category": "Reverse", "type": "reverse"})
            elif proto == "reality":
                tunnel_items.append({"id": iid, "name": name, "category": "Reality", "type": "tunnel"})
            elif proto == "vless":
                sec = (ib.get("security") or "").lower()
                tunnel_items.append({"id": iid, "name": name, "category": "Worker", "type": "tunnel"})
            elif proto == "node":
                node_items.append({"id": iid, "name": name, "category": "Node", "type": "node"})
            elif proto == "telegram":
                tunnel_items.append({"id": iid, "name": name, "category": "Telegram Proxy", "type": "tunnel"})

    return {
        "tunnel": {
            "items": tunnel_items,
            "yours": {"description": "Configurationهای ساخته‌شده توسط Create Your Own Config"},
        },
        "node": {
            "items": node_items,
        },
    }


# ── BOT Management ───────────────────────────────────────────────────────────

@app.get("/api/bot")
async def get_bot_config(_=Depends(require_auth)):
    """Return bot config (token masked)."""
    async with BOT_CONFIG_LOCK:
        cfg = dict(BOT_CONFIG)
    # Mask the bot token: show only last 4 chars
    if cfg.get("bot_token"):
        t = cfg["bot_token"]
        cfg["bot_token_masked"] = "****" + t[-4:] if len(t) > 4 else "****"
    else:
        cfg["bot_token_masked"] = ""
    return cfg


@app.post("/api/bot/setup")
async def bot_setup(request: Request, _=Depends(require_auth)):
    """Set up bot token + channel ID + optional promotion channel."""
    body = await request.json()
    token = (body.get("bot_token") or "").strip()
    channel_id = (body.get("channel_id") or "").strip()
    # The UI masks the existing bot token. Editing channel/promotion settings
    # must not replace a valid token with the mask.
    async with BOT_CONFIG_LOCK:
        existing_token = str(BOT_CONFIG.get("bot_token") or "")
    if not token and existing_token:
        token = existing_token
    if not token:
        raise HTTPException(status_code=400, detail="Bot Token is required")
    if not channel_id:
        raise HTTPException(status_code=400, detail="Channel ID is required")
    # Validate token
    from spider_features import validate_bot_token, check_bot_channel_access, tg_api_call
    try:
        bot_info = await validate_bot_token(token)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Check channel access
    try:
        await check_bot_channel_access(token, channel_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    target_channel_link = ""
    try:
        chat = await tg_api_call(token, "getChat", chat_id=channel_id)
        username = str((chat.get("username") if isinstance(chat, dict) else "") or "").strip().lstrip("@")
        if username:
            target_channel_link = f"https://t.me/{username}"
    except Exception:
        target_channel_link = channel_id if str(channel_id).startswith(("http://", "https://", "@")) else ""
    async with BOT_CONFIG_LOCK:
        BOT_CONFIG["bot_token"] = token
        BOT_CONFIG["channel_id"] = channel_id
        BOT_CONFIG["promotion_channel"] = str(body.get("promotion_channel") or BOT_CONFIG.get("promotion_channel") or "").strip()
        BOT_CONFIG["fixed_channel"] = str(body.get("fixed_channel") or BOT_CONFIG.get("fixed_channel") or "").strip()
        BOT_CONFIG["target_channel_link"] = target_channel_link or BOT_CONFIG.get("target_channel_link", "")
        BOT_CONFIG["last_error"] = ""
    asyncio.create_task(save_state())
    log_activity("bot", "ربات کانال تنظیم شد", "ok")
    return {
        "ok": True,
        "bot_name": bot_info.get("username", ""),
        "bot_id": bot_info.get("id", ""),
    }


@app.post("/api/bot/schedule")
async def bot_schedule(request: Request, _=Depends(require_auth)):
    """Set bot schedule (interval)."""
    body = await request.json()
    value = int(body.get("interval_value") or 10)
    unit = str(body.get("interval_unit") or "seconds").lower()
    if unit not in ("seconds", "minutes", "hours"):
        unit = "seconds"
    if value < 1:
        value = 1
    if value > 3600 and unit == "seconds":
        value = 3600
    async with BOT_CONFIG_LOCK:
        BOT_CONFIG["interval_value"] = value
        BOT_CONFIG["interval_unit"] = unit
    asyncio.create_task(save_state())
    return {"ok": True, "interval_value": value, "interval_unit": unit}


@app.post("/api/bot/start")
async def bot_start(_=Depends(require_auth)):
    """Start the bot loop."""
    global BOT_TASK
    async with BOT_CONFIG_LOCK:
        if BOT_CONFIG.get("running"):
            return {"ok": True, "message": "Bot is already running"}
        if not BOT_CONFIG.get("bot_token") or not BOT_CONFIG.get("channel_id"):
            raise HTTPException(status_code=400, detail="ابتدا ربات را تنظیم کنید")
        BOT_CONFIG["running"] = True
    BOT_TASK = asyncio.create_task(_bot_loop())
    log_activity("bot", "ربات کانال شروع شد", "ok")
    return {"ok": True}


@app.post("/api/bot/stop")
async def bot_stop(_=Depends(require_auth)):
    """Stop the bot loop."""
    global BOT_TASK
    async with BOT_CONFIG_LOCK:
        BOT_CONFIG["running"] = False
    if BOT_TASK and not BOT_TASK.done():
        BOT_TASK.cancel()
        BOT_TASK = None
    log_activity("bot", "ربات کانال متوقف شد", "warn")
    return {"ok": True}


async def _bot_loop():
    """Main bot loop: create user → send QR to channel → wait → delete → repeat."""
    import qrcode
    import io as _io
    from spider_features import tg_api_call

    last_user_id = None

    while True:
        try:
            async with BOT_CONFIG_LOCK:
                token = BOT_CONFIG.get("bot_token", "")
                channel_id = BOT_CONFIG.get("channel_id", "")
                value = BOT_CONFIG.get("interval_value", 10)
                unit = BOT_CONFIG.get("interval_unit", "seconds")
                running = BOT_CONFIG.get("running", False)
                promo = str(BOT_CONFIG.get("promotion_channel") or "").strip()

            host = _safe_host(SETTINGS.get("domain"), get_host())
            if not running or not token or not channel_id:
                break

            # Calculate sleep interval in seconds
            if unit == "minutes":
                sleep_sec = value * 60
            elif unit == "hours":
                sleep_sec = value * 3600
            else:
                sleep_sec = value

            # 1) Delete previous user if exists
            if last_user_id:
                try:
                    async with USERS_LOCK:
                        u_del = USERS.get(last_user_id)
                        if u_del:
                            cuuid = u_del.get("config_uuid")
                            username_del = u_del.get("username", last_user_id)
                            USERS.pop(last_user_id, None)
                            if cuuid:
                                LINKS.pop(cuuid, None)
                                PATH_INDEX.pop(cuuid, None)
                    if u_del and u_del.get("inbound_ids"):
                        for _iid in u_del["inbound_ids"]:
                            _ib = INBOUNDS.get(_iid, {})
                            if (_ib.get("protocol") or "").lower() == "telegram":
                                asyncio.create_task(_restart_telegram_proxy(_iid))
                    # Delete from all nodes
                    if u_del:
                        await _delete_user_from_nodes(u_del.get("config_uuid", last_user_id))
                    asyncio.create_task(save_state())
                    log_activity("bot", f"کاربر قبلی (bot) حذف شد", "info")
                except Exception as e:
                    logger.warning("Bot: failed to delete previous user: %s", e)

            # 2) Pick a random non-bot, non-telegram inbound for the user
            async with INBOUNDS_LOCK:
                available = [
                    (iid, ib) for iid, ib in INBOUNDS.items()
                    if (ib.get("protocol") or "").lower() not in ("telegram", "wireguard", "node", "worker", "tunnel", "reverse")
                ]
            if not available:
                async with BOT_CONFIG_LOCK:
                    BOT_CONFIG["last_error"] = "هیچ اینباندی در دسترس نیست"
                    BOT_CONFIG["running"] = False
                break

            # Pick up to 2 random inbounds
            picks = random.sample(available, min(2, len(available)))
            inbound_ids = [iid for iid, _ in picks]

            # 3) Create user with unlimited settings
            uid = generate_short_id()
            cuuid = generate_uuid()
            uname = f"bot-{secrets.token_hex(3)}"
            now = datetime.now()
            async with USERS_LOCK:
                USERS[uid] = {
                    "username": uname,
                    "password_hash": hash_password(secrets.token_urlsafe(8)),
                    "protocol": "vless",
                    "traffic_limit_bytes": 0,
                    "traffic_used_bytes": 0,
                    "expire_at": None,
                    "concurrent_connections": 0,
                    "created_at": now.isoformat(),
                    "status": "active",
                    "server": "Bot",
                    "config_uuid": cuuid,
                    "subscription_uuid": secrets.token_urlsafe(16),
                    "sni": "",
                    "proxy_ip": "",
                    "proxy_country": "",
                    "proxy_ips": [],
                    "proxy_countries": [],
                    "proxy_ip_enabled": False,
                    "custom_ip_type": "",
                    "custom_ip_inbounds": {"cf": [], "railway": []},
                    "sni_spoof_v2box": False,
                    "inbound_id": inbound_ids[0],
                    "inbound_ids": inbound_ids,
                    "path": f"/ws/{cuuid}",
                    "transport_type": "ws",
                    "telegram_secret": "",
                }
            _path = f"ws/{cuuid}"
            async with LINKS_LOCK:
                LINKS[cuuid] = {
                    "label": uname,
                    "limit_bytes": 0,
                    "used_bytes": 0,
                    "created_at": now.isoformat(),
                    "active": True,
                    "expires_at": None,
                    "note": f"Bot user {uname}",
                    "is_default": False,
                    "sub_id": None,
                    "protocol": "vless-ws",
                    "transport_type": "ws",
                    "xhttp_settings": {},
                    "path": _path,
                    "user_id": uid,
                }
                PATH_INDEX[cuuid] = cuuid
                PATH_INDEX[_path] = cuuid

            # Create on all nodes
            asyncio.create_task(_create_user_on_nodes(cuuid, {"username": uname}))

            # Refresh Xray
            asyncio.create_task(_xray_apply())
            asyncio.create_task(save_state())

            # 4) Generate QR code of the subscription link
            sub_url = f"https://{host}/subs/{cuuid}"
            qr_img_url = ""
            try:
                if QR_AVAILABLE:
                    qr = qrcode.QRCode(version=1, box_size=10, border=2)
                    qr.add_data(sub_url)
                    qr.make(fit=True)
                    img = qr.make_image(fill_color="black", back_color="white")
                    buf = _io.BytesIO()
                    img.save(buf, format="PNG")
                    buf.seek(0)
                    # Save to a temp file for Telegram upload
                    qr_path = DATA_DIR / "bot_qr.png"
                    with open(qr_path, "wb") as f:
                        f.write(buf.read())
                    qr_img_url = str(qr_path)
            except Exception as e:
                logger.warning("QR generation failed: %s", e)

            # 5) Send to Telegram channel
            try:
                async with BOT_CONFIG_LOCK:
                    target_link = str(BOT_CONFIG.get("target_channel_link") or "").strip()
                    fixed_channel = str(BOT_CONFIG.get("fixed_channel") or "").strip()
                caption_lines = [f"🔗 <b>Subscription</b>\n<code>{sub_url}</code>"]
                if target_link:
                    shown = target_link if target_link.startswith("http") else f"https://t.me/{target_link.lstrip('@')}"
                    caption_lines.append(f"\n<a href=\"{shown}\">Target Channel</a>")
                elif str(channel_id).startswith("@"):
                    shown = f"https://t.me/{str(channel_id).lstrip('@')}"
                    caption_lines.append(f"\n<a href=\"{shown}\">Target Channel</a>")
                if fixed_channel:
                    fixed = fixed_channel
                    if fixed.startswith("@"):
                        fixed = f"https://t.me/{fixed.lstrip('@')}"
                    elif not fixed.startswith(("http://", "https://")):
                        fixed = f"https://t.me/{fixed.lstrip('@')}"
                    caption_lines.append(f"<a href=\"{fixed}\">Fixed Channel</a>")
                if promo:
                    caption_lines.append("")
                    caption_lines.append(f"Promotion: <code>{promo}</code>")
                caption_lines.append("")
                caption_lines.append('<a href="https://t.me/amirsp1ider">Spider Panel by Amir</a>')
                caption = "\n".join(caption_lines)

                if qr_img_url and os.path.exists(qr_img_url):
                    # Send photo with caption
                    with open(qr_img_url, "rb") as photo:
                        files = {"photo": ("qr.png", photo, "image/png")}
                        data = {
                            "chat_id": channel_id,
                            "caption": caption,
                            "parse_mode": "HTML",
                        }
                        async with httpx.AsyncClient(timeout=15) as client:
                            r = await client.post(
                                f"https://api.telegram.org/bot{token}/sendPhoto",
                                data=data,
                                files=files,
                            )
                            res = r.json()
                            if not res.get("ok"):
                                raise RuntimeError(res.get("description", "Telegram sendPhoto failed"))
                else:
                    raise RuntimeError("QR code is unavailable; refusing to send a text-only bot post")

                async with BOT_CONFIG_LOCK:
                    BOT_CONFIG["last_run"] = datetime.now().isoformat()
                    BOT_CONFIG["last_error"] = ""
                asyncio.create_task(save_state())
                log_activity("bot", f"کاربر {uname} به کانال ارسال شد", "ok")
            except Exception as e:
                async with BOT_CONFIG_LOCK:
                    BOT_CONFIG["last_error"] = str(e)[:200]
                asyncio.create_task(save_state())
                logger.warning("Telegram bot send failed: %s", e)

            last_user_id = uid

            # 6) Wait for the interval
            await asyncio.sleep(sleep_sec)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Bot loop error: %s", e)
            async with BOT_CONFIG_LOCK:
                BOT_CONFIG["last_error"] = str(e)[:200]
                BOT_CONFIG["running"] = False
            asyncio.create_task(save_state())
            break

    async with BOT_CONFIG_LOCK:
        BOT_CONFIG["running"] = False
    asyncio.create_task(save_state())


# ── Hook into delete_user to also delete from nodes ──────────────────────────
# Patch: the original delete_user endpoint is at line ~3708; we add node sync.
# We do this by wrapping the save_state call with a node-delete task.

# NOTE: We add a helper that main.py's delete_user can call (imported above).


# ══════════════════════════════════════════════════════════════════════════════
# CUSTOM CONFIG CREATION ("Create Your Own Config") — scans IPs, builds TLS configs
# Only operates on TLS inbounds (never Reality, XHTTP Reality, Telegram, WireGuard).
# Returns session-only configs (not persisted to DB unless the caller saves them).
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/custom-config/scan")
async def custom_config_scan(request: Request, _=Depends(require_auth)):
    """Scan for healthy IPs and generate custom TLS configs for the given user.

    Body: { user_id: str }
    Returns: { configs: [base64_config_lines], yours: [config_strings] }

    The "yours" configs use scanned IPs as `server` (address) while keeping
    `host`/`sni` on the real panel domain. They are session-only and will be
    lost on page refresh (stored client-side only).
    """
    import socket as _sk

    body = await request.json()
    user_id = (body.get("user_id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required")

    async with USERS_LOCK:
        user = USERS.get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")

    # Determine which providers to scan (Railway / Cloudflare)
    host = SETTINGS.get("domain") or get_host()
    is_railway = ".railway.app" in host or ".rlwy.net" in host
    providers = []
    if is_railway:
        providers.append("railway")
    else:
        providers.append("cf")
    # If user has both cf and railway scanned IPs, scan both
    if user.get("custom_ip_type") in ("cf", "railway"):
        providers = [user["custom_ip_type"]]

    healthy_ips = []
    for prov in providers:
        candidates = _read_scanned_ips(prov)
        for entry in candidates:
            if len(healthy_ips) >= 2:
                break
            # Parse entry: "ip:port" or just "ip"
            hip = entry.strip()
            if ":" in hip:
                hip, _, _ = hip.rpartition(":")
            if not hip or hip in healthy_ips:
                continue
            try:
                s = _sk.socket(_sk.AF_INET, _sk.SOCK_STREAM)
                s.settimeout(3)
                s.connect((hip, 443))
                s.close()
                healthy_ips.append(hip)
            except Exception:
                continue
        if len(healthy_ips) >= 2:
            break

    # Build custom configs for each healthy IP (TLS inbounds only)
    import base64
    configs = []
    panel_domain = _safe_host(SETTINGS.get("domain"), get_host())
    username = user.get("username", user_id)
    config_uuid = user.get("config_uuid", user_id)

    async with INBOUNDS_LOCK:
        tls_inbounds = {
            iid: ib for iid, ib in INBOUNDS.items()
            if (ib.get("security") or "").lower() == "tls"
            and (ib.get("protocol") or "").lower() not in ("reality", "telegram", "wireguard", "worker", "tunnel")
        }

    for hip in healthy_ips:
        for iid, ib in tls_inbounds.items():
            net = (ib.get("network") or "ws").lower()
            transport = "ws" if net == "ws" else "xhttp"
            sni = panel_domain
            addr_port = "443"
            if transport == "xhttp":
                xpath = f"/xhttp-siz10/stream-up/{config_uuid}"
                params = (f"encryption=none&security=tls&type=xhttp"
                          f"&host={quote(panel_domain)}&path={quote(xpath, safe='')}&sni={quote(panel_domain)}"
                          f"&fp=chrome&alpn=h2,http/1.1")
            else:
                ws_path = f"/ws/{config_uuid}"
                params = (f"encryption=none&security=tls&type=ws"
                          f"&host={quote(panel_domain)}&path={quote(ws_path, safe='')}&sni={quote(panel_domain)}"
                          f"&fp=chrome&alpn=http/1.1")
            cfg = f"vless://{config_uuid}@{hip}:{addr_port}?{params}#{quote(f'Spider-{username} Custom-{hip}')}"
            configs.append(cfg)

    return {
        "ok": True,
        "yours": configs,
        "healthy_ips": healthy_ips,
        "providers_scanned": providers,
    }


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=CONFIG["port"], log_level="info", workers=1)
