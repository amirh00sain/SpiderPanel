# state.py — Shared mutable state (breaks circular import between main.py and relay_vless.py)
import asyncio
import threading
import collections

# ── VLESS Relay State ──
RELAY_BUF = 256 * 1024   # 256 KB buffer
connections: dict = {}
sub_clients: dict = {}
TIMEOUT = 30

# ── Core State ──
stats = {
    "total_bytes": 0,
    "total_requests": 0,
    "total_errors": 0,
    "start_time": 0,
}

hourly_traffic = collections.defaultdict(int)
error_logs = collections.deque(maxlen=100)
LINKS: dict = {}
LINKS_LOCK = asyncio.Lock()

# ── Other shared locks/state ──
USERS_LOCK = asyncio.Lock()
SUBS_LOCK = asyncio.Lock()
SETTINGS_LOCK = asyncio.Lock()
SESSIONS_LOCK = asyncio.Lock()
GROUPS_LOCK = asyncio.Lock()
INBOUNDS: dict = {}  # inbound_id -> {name, protocol, port, network, security, domain, sni, external_port, fingerprint, reality_settings, xhttp_settings, created_at}
INBOUNDS_LOCK = asyncio.Lock()

# ── Path to UUID index (maps random paths like /abc123 to user/link UUIDs) ──
PATH_INDEX: dict = {}  # stripped_path -> {"uuid": str, "type": "user"|"link"}
PATH_INDEX_LOCK = asyncio.Lock()

IP_LOCK = threading.Lock()
