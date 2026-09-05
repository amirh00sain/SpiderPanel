# relay_vless.py
# بخش VLESS Relay — جدا شده از main.py
# از _get_main() برای دسترسی به main.LINKS استفاده می‌کند (جلوگیری از circular import)

import asyncio
import secrets
import logging
from datetime import datetime, timezone, timedelta

from fastapi import WebSocket, WebSocketDisconnect

# ── Shared state (used directly; main.py populates these) ──
from shared import (
    stats,
    hourly_traffic,
    connections,
    error_logs,
    RELAY_BUF,
)

logger = logging.getLogger("Spider-Gateway")
IRAN_TZ = timezone(timedelta(hours=3, minutes=30))

# ── Lazy access to main module (avoids circular import) ──
_main = None

def _get_main():
    global _main
    if _main is None:
        import main as _main
    return _main

# ══════════════════════════════════════════════════════════════════════════════
# VLESS Relay — بهینه‌شده برای حداکثر throughput
# ══════════════════════════════════════════════════════════════════════════════

RELAY_BUF_LOCAL = 256 * 1024

def _ws_client_ip(ws: WebSocket) -> str:
    fwd = ws.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    real_ip = ws.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return ws.client.host if ws.client else "نامشخص"

async def parse_vless_header(chunk: bytes):
    if len(chunk) < 24:
        raise ValueError("chunk too small")
    pos = 1
    pos += 16
    addon_len = chunk[pos]; pos += 1 + addon_len
    command = chunk[pos]; pos += 1
    port = int.from_bytes(chunk[pos:pos+2], "big"); pos += 2
    addr_type = chunk[pos]; pos += 1
    if addr_type == 1:
        address = ".".join(str(b) for b in chunk[pos:pos+4]); pos += 4
    elif addr_type == 2:
        dlen = chunk[pos]; pos += 1
        address = chunk[pos:pos+dlen].decode("utf-8", errors="ignore"); pos += dlen
    elif addr_type == 3:
        ab = chunk[pos:pos+16]; pos += 16
        address = ":".join(f"{ab[i]:02x}{ab[i+1]:02x}" for i in range(0, 16, 2))
    else:
        raise ValueError(f"unknown addr type: {addr_type}")
    return command, address, port, chunk[pos:]

async def check_and_use(uid: str, n: int) -> bool:
    m = _get_main()
    async with m.LINKS_LOCK:
        link = m.LINKS.get(uid)
        if link is None:
            return False
        if not m.is_link_allowed(link):
            return False
        link["used_bytes"] += n
        stats["total_bytes"] += n
        hourly_traffic[m.now_ir().strftime("%H:00")] += n

    # Sync traffic back to user (so subscription page shows real usage)
    user_id = link.get("user_id")
    if user_id:
        async with m.USERS_LOCK:
            u = m.USERS.get(user_id)
            if u:
                u["traffic_used_bytes"] = u.get("traffic_used_bytes", 0) + n

    return True

async def relay_ws_to_tcp(ws: WebSocket, writer: asyncio.StreamWriter, conn_id: str, uid: str):
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes") or (msg.get("text") or "").encode()
            if not data:
                continue
            if not await check_and_use(uid, len(data)):
                await ws.close(code=1008, reason="quota/disabled/unknown")
                break
            stats["total_requests"] += 1
            connections[conn_id]["bytes"] += len(data)
            writer.write(data)
            if writer.transport.get_write_buffer_size() > RELAY_BUF_LOCAL:
                await writer.drain()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        try:
            writer.write_eof()
        except Exception:
            pass

async def relay_tcp_to_ws(ws: WebSocket, reader: asyncio.StreamReader, conn_id: str, uid: str):
    first = True
    try:
        while True:
            data = await reader.read(RELAY_BUF_LOCAL)
            if not data:
                break
            if not await check_and_use(uid, len(data)):
                await ws.close(code=1008, reason="quota/disabled/unknown")
                break
            connections[conn_id]["bytes"] += len(data)
            payload = (b"\x00\x00" + data) if first else data
            first = False
            await ws.send_bytes(payload)
    except Exception:
        pass

async def websocket_tunnel(ws: WebSocket, uuid: str, proxy_override: str = None):
    if proxy_override:
        try:
            from urllib.parse import unquote
            proxy_override = unquote(proxy_override)
        except Exception:
            pass
    await ws.accept()
    m = _get_main()

    async with m.LINKS_LOCK:
        link = m.LINKS.get(uuid)

    if not m.is_link_allowed(link):
        logger.warning(f"WS rejected uuid={uuid[:8]}… (link={'not found' if link is None else 'disabled/expired'})")
        await ws.close(code=1008, reason="not authorized")
        return

    ip = _ws_client_ip(ws)
    conn_id = secrets.token_urlsafe(6)
    connections[conn_id] = {
        "uuid": uuid,
        "ip": ip,
        "transport": "vless-ws",
        "connected_at": datetime.now().isoformat(),
        "bytes": 0,
    }
    logger.info(f"WS [{conn_id}] uuid={uuid[:8]}… ip={ip} total={len(connections)}")
    m.log_activity("connection", f"اتصال جدید از {ip} (کانفیگ {link.get('label','?')})", "info")

    # Enforce per-user IP limit using the real connection IP
    if not await m.enforce_ip_limit_for_link(uuid, ip):
        logger.warning(f"WS rejected uuid={uuid[:8]}… ip={ip}: IP limit reached")
        await ws.close(code=1008, reason="ip limit reached")
        return
    writer = None

    try:
        first_msg = await asyncio.wait_for(ws.receive(), timeout=15.0)
        if first_msg["type"] == "websocket.disconnect":
            return
        first_chunk = first_msg.get("bytes") or (first_msg.get("text") or "").encode()
        if not first_chunk:
            return

        command, address, port, payload = await parse_vless_header(first_chunk)

        if not await check_and_use(uuid, len(first_chunk)):
            await ws.close(code=1008, reason="quota/disabled")
            return

        stats["total_requests"] += 1
        connections[conn_id]["bytes"] += len(first_chunk)
        logger.info(f"[{conn_id}] → {address}:{port}")

        # Route the outbound connection through the user's selected proxy IP(s),
        # so egress shows the proxy IP instead of the Railway host.
        reader, writer = await m.proxy_connect(uuid, address, port, proxy_override=proxy_override)
        sock = writer.transport.get_extra_info('socket')
        if sock:
            import socket
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

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

        asyncio.create_task(m.save_state())

    except WebSocketDisconnect:
        pass
    except asyncio.TimeoutError:
        stats["total_errors"] += 1
        error_logs.append({"error": "connection timeout", "time": datetime.now().isoformat()})
    except Exception as exc:
        stats["total_errors"] += 1
        error_logs.append({"error": str(exc), "time": datetime.now().isoformat()})
        logger.error(f"WS error [{conn_id}]: {exc}")
    finally:
        if writer:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        connections.pop(conn_id, None)
        # Release the IP so USER_IP_MAP reflects live concurrent connections
        try:
            asyncio.create_task(m.release_ip_for_link(uuid, ip))
        except Exception:
            pass
        logger.info(f"WS closed [{conn_id}] total={len(connections)}")
