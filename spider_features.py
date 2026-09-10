# spider_features.py — New panel features: server info, API key, node management,
# NODE inbound sync, custom-config scanner, and Telegram channel bot.
#
# Kept as a helper module so main.py stays focused on routing. main.py imports
# these lazily (inside handlers) to avoid circular import issues — `main` is
# aliased in sys.modules, so `from main import ...` resolves to the running app.
import asyncio
import logging
import re
import httpx
import secrets
import json
from datetime import datetime, timedelta

logger = logging.getLogger("Spider-Features")

# Country-code → flag emoji (subset covering common server locations).
_FLAG_MAP = {}
def _build_flag_map():
    # Common ISO 3166-1 alpha-2 → emoji (regional indicator symbols).
    codes = "AD AE AF AG AI AL AM AO AR AT AU AZ BA BB BD BE BF BG BH BI BJ BN BO BR BS BT BW BY BZ CA CD CF CG CH CI CL CM CN CO CR CU CV CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FM FO FR GA GB GD GE GF GH GL GM GN GP GQ GR GT GU GW GY HK HN HR HT HU ID IE IL IN IQ IR IS IT JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MG MH MK ML MM MN MO MQ MR MT MU MV MW MX MY MZ NA NE NG NI NL NO NP NR NZ OM PA PE PF PG PH PK PL PT PW PY QA RO RS RU RW SA SB SC SD SE SG SI SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UK US UY UZ VA VC VE VN VU WF WS YE YT ZA ZM ZW".split()
    for c in codes:
        _FLAG_MAP[c] = "".join(chr(0x1F1E6 + (ord(ch) - ord("A"))) for ch in c)

_build_flag_map()

def code_to_flag(code: str) -> str:
    if not code:
        return "🏳️"
    code = code.strip().upper()
    return _FLAG_MAP.get(code, "🏳️")

def flag_to_code(flag: str) -> str:
    # Reverse lookup: emoji → alpha-2
    for c, f in _FLAG_MAP.items():
        if f == flag:
            return c
    return ""

async def detect_server_info(client: httpx.AsyncClient) -> dict:
    """Detect public IP + country via a public service. Returns dict with
    public_ip, country, country_code, country_flag, detected_at."""
    info = {"public_ip": "", "country": "", "country_code": "", "country_flag": "", "detected_at": ""}
    for url in ("https://ipinfo.io/json", "https://api.ipify.org?format=json", "https://ipapi.co/json/"):
        try:
            r = await client.get(url, timeout=8)
            if r.status_code != 200:
                continue
            d = r.json()
            ip = (d.get("ip") or d.get("query") or "").strip()
            cc = (d.get("country") or d.get("countryCode") or "").strip()
            cname = (d.get("country_name") or d.get("region") or d.get("city") or cc).strip()
            if ip:
                info["public_ip"] = ip
                info["country_code"] = cc
                info["country"] = cname or cc
                info["country_flag"] = code_to_flag(cc)
                info["detected_at"] = datetime.now().isoformat()
                return info
        except Exception as e:
            logger.warning("server info detect failed at %s: %s", url, e)
            continue
    return info

# ── Panel API Key helpers ────────────────────────────────────────────────────
def generate_panel_api_key() -> str:
    """Generate a secure random panel API key (never logged)."""
    return "spdr_" + secrets.token_urlsafe(32)


# ── Node region naming helper ────────────────────────────────────────────────
def node_region_prefix(server_info: dict) -> str:
    """spider-{flag}-{username} style naming uses the panel's country flag."""
    flag = server_info.get("country_flag") or "🌐"
    return f"spider-{flag}"


# ── Custom-config scanner ─────────────────────────────────────────────────────
def _is_tls_inbound(ib: dict) -> bool:
    if not ib:
        return False
    sec = (ib.get("security") or "").lower()
    proto = (ib.get("protocol") or "").lower()
    net = (ib.get("network") or "").lower()
    # Only TLS configs (vless-ws / xhttp on tls). Never Reality / XHTTP-Reality /
    # Telegram Proxy / WireGuard.
    if sec == "reality" or proto == "reality":
        return False
    if proto in ("telegram", "wireguard", "worker", "tunnel"):
        return False
    if sec != "tls":
        return False
    return True


import os
import sys
# Allow `from main import ...` to resolve to the running module.
sys.modules.setdefault("main", sys.modules.get("__main__", sys.modules[__name__]))


async def scan_healthy_ips(domain: str, ctype: str, max_count: int = 2) -> list:
    """Scan for healthy IPs (Railway or Cloudflare) to use as `server` in a new
    config. Returns at most `max_count` healthy IPs.

    ctype: 'railway' or 'cloudflare'. We reuse the project's saved scanned-IP
    data plus live reachability check. Kept server-side and resilient: a failure
    returns an empty list (never raises)."""
    import main as _M
    healthy = []
    try:
        # Use already-scanned IPs (from /api/scanner endpoints) as candidates.
        if ctype == "railway":
            candidates = _read_scanned_railway()
            candidates += _M.RAILWAY_REGIONS if hasattr(_M, "RAILWAY_REGIONS") else []
        else:
            candidates = _read_scanned_cf()
    except Exception as e:
        logger.warning("scan_healthy_ips candidate read failed: %s", e)
        candidates = []

    # Deduplicate by ip/host
    seen = set()
    uniq = []
    for c in candidates:
        host = (c.get("ip") or c.get("host") or "").strip()
        if not host or host in seen:
            continue
        seen.add(host)
        uniq.append(c)
    candidates = uniq

    for c in candidates:
        if len(healthy) >= max_count:
            break
        host = (c.get("ip") or c.get("host") or "").strip()
        if not host:
            continue
        # Reachability check (TCP connect on 443) with short timeout.
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect((host, 443))
            s.close()
            healthy.append(host)
        except Exception:
            continue
    return healthy


def _read_scanned_railway() -> list:
    try:
        import main as _M
        return _M._read_scanned_ips("railway")
    except Exception:
        return []


def _read_scanned_cf() -> list:
    try:
        import main as _M
        return _M._read_scanned_ips("cf")
    except Exception:
        return []


# ── Telegram Bot ──────────────────────────────────────────────────────────────
TG_API = "https://api.telegram.org/bot{token}/{method}"

async def tg_api_call(token: str, method: str, **params) -> dict:
    """Call a Telegram Bot API method. token is never logged."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, json=params)
        return r.json()


async def validate_bot_token(token: str) -> dict:
    """Validate the bot token and return bot info. Raises ValueError on failure."""
    if not token or not re.match(r"^\d+:[A-Za-z0-9_-]{20,}$", token):
        raise ValueError("فرمت توکن ربات معتبر نیست")
    res = await tg_api_call(token, "getMe")
    if not res.get("ok"):
        raise ValueError(f"توکن ربات نامعتبر: {res.get('description', 'unknown')}")
    return res["result"]


async def check_bot_channel_access(token: str, channel_id: str) -> bool:
    """Check the bot is an admin of the channel (so it can post)."""
    res = await tg_api_call(token, "getChatMember", chat_id=channel_id, user_id=(await tg_api_call(token, "getMe"))["result"]["id"])
    if not res.get("ok"):
        raise ValueError(f"دسترسی به کانال ناموفق: {res.get('description', 'unknown')}")
    status = res["result"].get("status")
    if status not in ("administrator", "creator"):
        raise ValueError("ربات باید مدیر (Admin) کانال باشد")
    return True


async def set_bot_webhook(token: str, webhook_url: str) -> bool:
    res = await tg_api_call(token, "setWebhook", url=webhook_url)
    return bool(res.get("ok"))
