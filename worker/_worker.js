// SpiderPanel — managed Cloudflare Pages Worker
// VLESS over WebSocket/TLS, one canonical route per user: /ws/{uuid}
// The panel injects __PANEL_TOKEN__, __PANEL_DOMAIN__ and __WORKER_DOMAIN__
// during deployment. SPIDER_KV is a Pages KV binding configured by the panel.

import { connect } from "cloudflare:sockets";

const PANEL_TOKEN = __PANEL_TOKEN__;
const PANEL_DOMAIN = __PANEL_DOMAIN__;
const WORKER_DOMAIN = __WORKER_DOMAIN__;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const MAX_HEADER_BYTES = 64 * 1024;
const USAGE_FLUSH_BYTES = 256 * 1024;
const USAGE_FLUSH_MS = 1000;
const IP_TTL_SECONDS = 900;
const IP_HEARTBEAT_MS = 5 * 60 * 1000;

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      "access-control-allow-origin": "*",
    },
  });
}

function authorized(request) {
  return (request.headers.get("Authorization") || "") === `Bearer ${PANEL_TOKEN}`;
}

function normalizeUuid(value) {
  const u = String(value || "").trim().toLowerCase();
  return UUID_RE.test(u) ? u : "";
}

function clientIp(request) {
  const cf = request.headers.get("CF-Connecting-IP");
  if (cf) return cf.trim();
  const fwd = request.headers.get("x-forwarded-for");
  if (fwd) return fwd.split(",")[0].trim();
  return "unknown";
}

function bytesFrom(value) {
  if (value instanceof ArrayBuffer) return new Uint8Array(value);
  if (ArrayBuffer.isView(value)) return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  return null;
}

function formatUuid(bytes) {
  if (!bytes || bytes.length !== 16) return "";
  let hex = "";
  for (const b of bytes) hex += b.toString(16).padStart(2, "0");
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`.toLowerCase();
}

function parseVlessHeader(data) {
  if (!(data instanceof Uint8Array) || data.length < 24) return { needMore: true };
  let pos = 0;

  const version = data[pos++];
  if (version !== 0 && version !== 1) return { error: "unsupported vless version" };

  if (pos + 16 > data.length) return { needMore: true };
  const userId = formatUuid(data.subarray(pos, pos + 16));
  pos += 16;

  if (pos >= data.length) return { needMore: true };
  const addonLen = data[pos++];
  if (addonLen > 0) {
    if (pos + addonLen > data.length) return { needMore: true };
    pos += addonLen;
  }

  if (pos >= data.length) return { needMore: true };
  const command = data[pos++];
  if (command !== 1) return { error: "only VLESS TCP is supported by this Worker" };

  if (pos + 2 > data.length) return { needMore: true };
  const port = (data[pos] << 8) | data[pos + 1];
  pos += 2;
  if (port < 1 || port > 65535) return { error: "invalid target port" };

  if (pos >= data.length) return { needMore: true };
  const addressType = data[pos++];
  let address = "";

  if (addressType === 1) {
    if (pos + 4 > data.length) return { needMore: true };
    address = `${data[pos]}.${data[pos+1]}.${data[pos+2]}.${data[pos+3]}`;
    pos += 4;
  } else if (addressType === 2) {
    if (pos >= data.length) return { needMore: true };
    const len = data[pos++];
    if (len < 1 || pos + len > data.length) return { needMore: true };
    address = new TextDecoder().decode(data.subarray(pos, pos + len));
    pos += len;
    if (!address) return { error: "empty target domain" };
  } else if (addressType === 3) {
    if (pos + 16 > data.length) return { needMore: true };
    const view = new DataView(data.buffer, data.byteOffset + pos, 16);
    const groups = [];
    for (let i = 0; i < 8; i++) groups.push(view.getUint16(i * 2).toString(16));
    address = groups.join(":");
    pos += 16;
  } else {
    return { error: `unsupported address type ${addressType}` };
  }

  return {
    version,
    userId,
    command,
    address,
    port,
    payload: data.subarray(pos),
  };
}

function responseHeader(version) {
  return new Uint8Array([version & 0xff, 0]);
}

// ── KV user state ───────────────────────────────────────────────────────────
async function kvReady(env) {
  return !!(env && env.SPIDER_KV && typeof env.SPIDER_KV.get === "function");
}

async function getUser(env, rawUuid) {
  const uuid = normalizeUuid(rawUuid);
  if (!uuid || !(await kvReady(env))) return null;
  try {
    const raw = await env.SPIDER_KV.get(`user:${uuid}`);
    if (!raw) return null;
    const user = JSON.parse(raw);
    if (!user || normalizeUuid(user.uuid) !== uuid) return null;
    const now = Date.now() / 1000;
    if (user.expire && now >= Number(user.expire)) return null;
    if (Number(user.limit_bytes) > 0 && Number(user.used_bytes || 0) >= Number(user.limit_bytes)) return null;
    return user;
  } catch (_) {
    return null;
  }
}

async function setUser(env, uuid, user) {
  await env.SPIDER_KV.put(`user:${uuid}`, JSON.stringify(user));
}

// KV does not provide an atomic increment for these simple records, so usage
// is flushed in chunks to keep write pressure low. Final connection flush
// closes the accounting gap for normal disconnects.
async function addUsage(env, uuid, amount, meter) {
  if (!amount || amount < 1 || !meter) return true;
  meter.pending += amount;
  const now = Date.now();
  if (meter.pending < USAGE_FLUSH_BYTES && (now - meter.lastFlush) < USAGE_FLUSH_MS) return true;

  const pending = meter.pending;
  meter.pending = 0;
  meter.lastFlush = now;
  const user = await getUser(env, uuid);
  if (!user) return false;
  user.used_bytes = Number(user.used_bytes || 0) + pending;
  await setUser(env, uuid, user);
  return !(Number(user.limit_bytes) > 0 && user.used_bytes >= Number(user.limit_bytes));
}

async function flushUsage(env, uuid, meter) {
  if (!meter || !meter.pending || !uuid) return;
  const pending = meter.pending;
  meter.pending = 0;
  const user = await getUser(env, uuid);
  if (!user) return;
  user.used_bytes = Number(user.used_bytes || 0) + pending;
  await setUser(env, uuid, user);
}

// ── Concurrent IP guard ─────────────────────────────────────────────────────
async function getIpRecord(env, uuid) {
  try {
    const raw = await env.SPIDER_KV.get(`ips:${uuid}`);
    return raw ? JSON.parse(raw) : { ips: [] };
  } catch (_) {
    return { ips: [] };
  }
}

async function saveIpRecord(env, uuid, record) {
  try { await env.SPIDER_KV.put(`ips:${uuid}`, JSON.stringify(record)); } catch (_) {}
}

async function touchIp(env, uuid, ip, maxIps) {
  if (!ip || ip === "unknown" || ip === "127.0.0.1" || !maxIps || maxIps < 1) return true;
  const now = Date.now() / 1000;
  const record = await getIpRecord(env, uuid);
  const live = Array.isArray(record.ips) ? record.ips.filter(x => x && Number(x.exp) > now) : [];
  const current = live.find(x => x.ip === ip);
  if (current) {
    current.exp = now + IP_TTL_SECONDS;
  } else {
    if (live.length >= maxIps) {
      return false;
    }
    live.push({ ip, exp: now + IP_TTL_SECONDS });
  }
  await saveIpRecord(env, uuid, { ips: live });
  return true;
}

async function removeIp(env, uuid, ip) {
  if (!uuid || !ip || ip === "unknown") return;
  const record = await getIpRecord(env, uuid);
  const now = Date.now() / 1000;
  const live = (record.ips || []).filter(x => x && x.ip !== ip && Number(x.exp) > now);
  await saveIpRecord(env, uuid, { ips: live });
}

// ── TCP connection ──────────────────────────────────────────────────────────
async function openSocket(hostname, port) {
  const host = String(hostname || "").trim();
  const p = Number(port);
  if (!host || !Number.isInteger(p) || p < 1 || p > 65535) return null;
  try {
    const socket = connect({ hostname: host, port: p });
    const reader = socket.readable.getReader();
    const writer = socket.writable.getWriter();
    return { socket, reader, writer };
  } catch (_) {
    return null;
  }
}

async function closeSocket(conn) {
  if (!conn) return;
  try { await conn.writer.close(); } catch (_) {}
  try { conn.socket.close(); } catch (_) {}
}

function concatBytes(a, b) {
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

async function pumpTcpToWs(conn, server, version, meter, env, uuid) {
  let sentHeader = false;
  try {
    while (true) {
      const { done, value } = await conn.reader.read();
      if (done) break;
      if (!value || !value.length) continue;
      if (!await addUsage(env, uuid, value.length, meter)) {
        try { server.close(1008, "quota reached"); } catch (_) {}
        break;
      }
      let frame = value;
      if (!sentHeader) {
        frame = concatBytes(responseHeader(version), value);
        sentHeader = true;
      }
      try { server.send(frame); } catch (_) { break; }
    }
  } catch (_) {
    // Connection teardown is handled by the caller.
  }
}

async function handleVlessWs(request, env, uuidFromPath) {
  const pathUuid = normalizeUuid(uuidFromPath);
  if (!pathUuid) return json({ error: "bad uuid" }, 400);
  if (!(await kvReady(env))) return json({ error: "SPIDER_KV binding missing" }, 503);

  const pair = new WebSocketPair();
  const [client, server] = Object.values(pair);
  server.accept();
  server.binaryType = "arraybuffer";

  const ip = clientIp(request);
  const meter = { pending: 0, lastFlush: Date.now() };
  let user = null;
  let conn = null;
  let heartbeat = null;
  let closed = false;
  let headerBuffer = new Uint8Array(0);

  const cleanup = async () => {
    if (closed) return;
    closed = true;
    if (heartbeat) clearInterval(heartbeat);
    await flushUsage(env, pathUuid, meter);
    await removeIp(env, pathUuid, ip);
    await closeSocket(conn);
    conn = null;
  };

  const handleMessage = async (ev) => {
    if (closed) return;
    const incoming = bytesFrom(ev.data);
    if (!incoming || !incoming.length) return;

    // First message contains the VLESS request header. Buffer it so a client
    // that fragments the first WebSocket message still works.
    if (!user) {
      headerBuffer = concatBytes(headerBuffer, incoming);
      if (headerBuffer.length > MAX_HEADER_BYTES) {
        try { server.close(1002, "vless header too large"); } catch (_) {}
        await cleanup();
        return;
      }

      const parsed = parseVlessHeader(headerBuffer);
      if (parsed.needMore) return;
      if (parsed.error) {
        try { server.close(1002, parsed.error); } catch (_) {}
        await cleanup();
        return;
      }

      if (parsed.userId !== pathUuid) {
        try { server.close(1008, "uuid mismatch"); } catch (_) {}
        await cleanup();
        return;
      }

      user = await getUser(env, pathUuid);
      if (!user) {
        try { server.close(1008, "unauthorized"); } catch (_) {}
        await cleanup();
        return;
      }

      const maxIps = Number(user.concurrent_connections || 0);
      if (!(await touchIp(env, pathUuid, ip, maxIps))) {
        try { server.close(1008, "ip limit reached"); } catch (_) {}
        await cleanup();
        return;
      }

      heartbeat = setInterval(() => {
        touchIp(env, pathUuid, ip, maxIps).catch(() => {});
      }, IP_HEARTBEAT_MS);

      conn = await openSocket(parsed.address, parsed.port);
      if (!conn) {
        try { server.close(1011, "outbound connect failed"); } catch (_) {}
        await cleanup();
        return;
      }

      if (parsed.payload && parsed.payload.length) {
        try {
          await conn.writer.write(parsed.payload);
          if (!await addUsage(env, pathUuid, parsed.payload.length, meter)) {
            try { server.close(1008, "quota reached"); } catch (_) {}
            await cleanup();
            return;
          }
        } catch (_) {
          try { server.close(1011, "upstream write failed"); } catch (_) {}
          await cleanup();
          return;
        }
      }

      headerBuffer = null;
      pumpTcpToWs(conn, server, parsed.version, meter, env, pathUuid).finally(() => cleanup());
      return;
    }

    if (!conn) return;
    try {
      await conn.writer.write(incoming);
      if (!await addUsage(env, pathUuid, incoming.length, meter)) {
        try { server.close(1008, "quota reached"); } catch (_) {}
        await cleanup();
      }
    } catch (_) {
      try { server.close(1011, "upstream write failed"); } catch (_) {}
      await cleanup();
    }
  };

  // Serialize messages: WebSocket event handlers are not automatically a
  // single-file writer queue, and concurrent writer.write() calls can race.
  let queue = Promise.resolve();
  server.addEventListener("message", (ev) => {
    queue = queue.then(() => handleMessage(ev)).catch(() => cleanup());
  });
  server.addEventListener("close", () => { cleanup(); });
  server.addEventListener("error", () => { cleanup(); });

  return new Response(null, { status: 101, webSocket: client });
}

// ── Main handler ────────────────────────────────────────────────────────────
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;

    // Useful diagnostics for the panel and for manual verification.
    if (path === "/health" || path === "/") {
      const kv = await kvReady(env);
      return json({
        ok: kv,
        service: "SpiderPanel VLESS Worker",
        panel_domain: PANEL_DOMAIN,
        worker_domain: WORKER_DOMAIN,
        kv_bound: kv,
        route: "/ws/{uuid}",
        transport: "vless-ws-tcp",
      }, kv ? 200 : 503);
    }

    // Panel → Worker control plane.
    if (path === "/panel/config" && request.method === "POST") {
      if (!authorized(request)) return json({ error: "Forbidden" }, 403);
      if (!(await kvReady(env))) return json({ error: "SPIDER_KV binding missing" }, 503);

      let body;
      try { body = await request.json(); } catch (_) { return json({ error: "bad json" }, 400); }
      const users = Array.isArray(body.users) ? body.users : [];
      const existing = await env.SPIDER_KV.list({ prefix: "user:" });
      const keep = new Set();
      let written = 0;
      let traffic = 0;
      let online = 0;
      const now = Date.now() / 1000;

      for (const item of users) {
        const uuid = normalizeUuid(item.uuid);
        if (!uuid || item.disabled) continue;
        const record = {
          uuid,
          remark: String(item.remark || "user"),
          limit_bytes: Math.max(0, Number(item.limit_bytes) || 0),
          expire: Math.max(0, Number(item.expire) || 0),
          used_bytes: Math.max(0, Number(item.used_bytes) || 0),
          proxy_ip: String(item.proxy_ip || ""),
          concurrent_connections: Math.max(0, Number(item.concurrent_connections) || 0),
          created: Date.now(),
        };
        keep.add(`user:${uuid}`);
        await setUser(env, uuid, record);
        written++;
        traffic += record.used_bytes;
        const expired = record.expire && now >= record.expire;
        const quota = record.limit_bytes > 0 && record.used_bytes >= record.limit_bytes;
        if (!expired && !quota) online++;
      }

      for (const key of existing.keys || []) {
        if (!keep.has(key.name)) await env.SPIDER_KV.delete(key.name);
      }
      if (body.settings && typeof body.settings === "object") {
        await env.SPIDER_KV.put("settings", JSON.stringify(body.settings));
      }
      await env.SPIDER_KV.put("heartbeat", JSON.stringify({ at: Date.now(), users: written }));
      return json({ ok: true, users: written, traffic, online });
    }

    if (path === "/panel/status" && request.method === "GET") {
      if (!authorized(request)) return json({ error: "Forbidden" }, 403);
      if (!(await kvReady(env))) return json({ error: "SPIDER_KV binding missing" }, 503);
      let users = 0, traffic = 0, online = 0;
      const now = Date.now() / 1000;
      const list = await env.SPIDER_KV.list({ prefix: "user:" });
      for (const key of list.keys || []) {
        try {
          const user = JSON.parse(await env.SPIDER_KV.get(key.name));
          if (!user) continue;
          users++;
          traffic += Number(user.used_bytes || 0);
          const expired = user.expire && now >= Number(user.expire);
          const quota = Number(user.limit_bytes || 0) > 0 && Number(user.used_bytes || 0) >= Number(user.limit_bytes);
          if (!expired && !quota) online++;
        } catch (_) {}
      }
      return json({ ok: true, users, traffic, online });
    }

    // Internal worker admin API.
    if (path.startsWith("/api/")) {
      if (!authorized(request)) return json({ error: "Forbidden" }, 403);
      if (!(await kvReady(env))) return json({ error: "SPIDER_KV binding missing" }, 503);

      if (path === "/api/users" && request.method === "GET") {
        const out = [];
        const list = await env.SPIDER_KV.list({ prefix: "user:" });
        for (const key of list.keys || []) {
          const raw = await env.SPIDER_KV.get(key.name);
          if (raw) out.push(JSON.parse(raw));
        }
        return json({ ok: true, users: out });
      }

      if (path === "/api/users" && request.method === "POST") {
        let body;
        try { body = await request.json(); } catch (_) { return json({ error: "bad json" }, 400); }
        const uuid = normalizeUuid(body.uuid);
        if (!uuid) return json({ error: "bad uuid" }, 400);
        const user = {
          uuid,
          remark: String(body.remark || "user"),
          limit_bytes: Math.max(0, Number(body.limit_bytes) || 0),
          expire: Math.max(0, Number(body.expire) || 0),
          used_bytes: Math.max(0, Number(body.used_bytes) || 0),
          proxy_ip: String(body.proxy_ip || ""),
          concurrent_connections: Math.max(0, Number(body.concurrent_connections) || 0),
          created: Date.now(),
        };
        await setUser(env, uuid, user);
        return json({ ok: true, user });
      }

      if (path.startsWith("/api/user/")) {
        const uuid = normalizeUuid(path.split("/").pop());
        if (!uuid) return json({ error: "bad uuid" }, 400);
        if (request.method === "DELETE") {
          await env.SPIDER_KV.delete(`user:${uuid}`);
          return json({ ok: true });
        }
        const user = await getUser(env, uuid);
        if (!user) return json({ error: "not found" }, 404);
        return json({ ok: true, user });
      }

      return json({ error: "Not Found" }, 404);
    }

    // Canonical VLESS path used by every generated Worker config.
    const match = path.match(/^\/ws\/([^/]+)\/?$/i);
    if (match) {
      const uuid = normalizeUuid(match[1]);
      if (!uuid) return json({ error: "bad uuid path" }, 400);
      if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
        return json({ error: "websocket upgrade required", path: `/ws/${uuid}` }, 400);
      }
      return handleVlessWs(request, env, uuid);
    }

    // Pages Advanced Mode requires falling back to ASSETS for everything the
    // Worker does not own. This keeps the project compatible with Pages.
    if (env && env.ASSETS && typeof env.ASSETS.fetch === "function") {
      return env.ASSETS.fetch(request);
    }

    return json({ error: "Not Found" }, 404);
  },
};
