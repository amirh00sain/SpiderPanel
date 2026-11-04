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


// ── Adaptive downstream routing ─────────────────────────────────────────────
// Route metrics are intentionally kept in the Worker isolate. Cloudflare
// Workers are distributed and KV is eventually consistent, so writing a
// hot per-proxy metric on every connection would add latency and can hit KV
// write limits. Passive connection measurements + occasional active checks
// give fast local adaptation without putting KV on the data path.
const ROUTE_RACE = 2;
const ROUTE_CONNECT_TIMEOUT_MS = 3200;
const ROUTE_HEALTH_TIMEOUT_MS = 2200;
const ROUTE_COOLDOWN_MIN_MS = 5000;
const ROUTE_COOLDOWN_MAX_MS = 60000;
const ROUTE_HEALTH_TTL_MS = 30000;
const ROUTE_MAX_HEALTH_CHECKS = 3;
const ROUTE_GEO_HISTORY_MAX = 24;
const STICKY_ROUTE_TTL_MS = 3 * 60 * 1000;
const STICKY_ROUTE_MAX = 2048;
const STICKY_ROUTE_FAILURE_COOLDOWN_MS = 15000;
const ROUTE_TEST_HOST = "example.com";
const ROUTE_TEST_PORT = 443;
const routeMetrics = new Map();
const routeGeoMetrics = new Map();
// Best-effort per-isolate sticky cache. It is deliberately short-lived and
// bounded: Cloudflare may evict/recreate isolates, so this is an optimization
// rather than correctness state. The adaptive score remains the fallback.
const stickyRoutes = new Map();
let lastHealthCheckAt = 0;

function proxyKey(proxy) {
  return [proxy.protocol || "http", proxy.hostname || "", proxy.port || 80,
    proxy.username || ""].join("|");
}

function normalizeCountry(value) {
  const v = String(value || "").trim().toUpperCase();
  return /^[A-Z]{2}$/.test(v) ? v : "";
}

// Continent is only used as a coarse fallback when an exact country route is
// unavailable. The exact user country and learned colo-specific latency always
// outrank this hint.
const CONTINENT_CODES = {
  AF: new Set(["DZ","AO","BJ","BW","BF","BI","CV","CM","CF","TD","KM","CG","CD","CI","DJ","EG","GQ","ER","SZ","ET","GA","GM","GH","GN","GW","KE","LS","LR","LY","MG","MW","ML","MR","MU","MA","MZ","NA","NE","NG","RW","ST","SN","SC","SL","SO","ZA","SS","SD","TZ","TG","TN","UG","ZM","ZW"]),
  AS: new Set(["AF","AM","AZ","BH","BD","BT","BN","KH","CN","CY","GE","IN","ID","IR","IQ","IL","JP","JO","KZ","KW","KG","LA","LB","MY","MV","MN","MM","NP","OM","PK","PS","PH","QA","SA","SG","LK","SY","TJ","TH","TL","TR","TM","AE","UZ","VN","YE"]),
  EU: new Set(["AL","AD","AT","BY","BE","BA","BG","HR","CY","CZ","DK","EE","FI","FR","DE","GR","HU","IS","IE","IT","XK","LV","LI","LT","LU","MT","MD","MC","ME","NL","MK","NO","PL","PT","RO","RU","SM","RS","SK","SI","ES","SE","CH","UA","GB","VA"]),
  NA: new Set(["AG","BS","BB","BZ","CA","CR","CU","DM","DO","SV","GD","GT","HT","HN","JM","MX","NI","PA","KN","LC","VC","TT","US"]),
  SA: new Set(["AR","BO","BR","CL","CO","EC","GY","PY","PE","SR","UY","VE"]),
  OC: new Set(["AU","FJ","KI","MH","FM","NR","NZ","PW","PG","WS","SB","TO","TV","VU"]),
};

function continentForCountry(code) {
  const cc = normalizeCountry(code);
  if (!cc) return "";
  for (const [continent, set] of Object.entries(CONTINENT_CODES)) {
    if (set.has(cc)) return continent;
  }
  return "";
}

function requestGeo(request) {
  const cf = (request && request.cf) || {};
  const country = normalizeCountry(cf.country || request?.headers?.get("CF-IPCountry"));
  const continent = String(cf.continent || continentForCountry(country) || "").toUpperCase();
  const colo = String(cf.colo || "").trim().toUpperCase();
  const city = String(cf.city || "").trim();
  const tcpRtt = Number(cf.clientTcpRtt || 0);
  const quicRtt = Number(cf.clientQuicRtt || cf.quicRtt || 0);
  const lat = Number(cf.latitude);
  const lon = Number(cf.longitude);
  return {
    country, continent, colo, city,
    client_rtt_ms: Number.isFinite(tcpRtt) && tcpRtt > 0 ? tcpRtt : 0,
    latitude: Number.isFinite(lat) ? lat : 0,
    longitude: Number.isFinite(lon) ? lon : 0,
    has_coordinates: Number.isFinite(lat) && Number.isFinite(lon),
    quic_rtt_ms: quicRtt,
  };
}

function geoKey(geo) {
  return `${normalizeCountry(geo?.country) || "??"}|${String(geo?.colo || "??").toUpperCase()}`;
}

function proxyCountry(proxy) {
  return normalizeCountry(proxy.country_code || proxy.country || "");
}

function proxyContinent(proxy) {
  return String(proxy.continent || continentForCountry(proxyCountry(proxy)) || "").toUpperCase();
}

function routeGeoMetric(proxy, geo, create = false) {
  if (!geo || (!geo.country && !geo.colo)) return null;
  const key = `${proxyKey(proxy)}@@${geoKey(geo)}`;
  let m = routeGeoMetrics.get(key);
  if (!m && create) {
    m = { ewmaMs: 0, jitterMs: 0, successes: 0, failures: 0, lastOkAt: 0 };
    if (routeGeoMetrics.size >= ROUTE_GEO_HISTORY_MAX * Math.max(4, routeMetrics.size || 1)) {
      const oldest = routeGeoMetrics.keys().next().value;
      if (oldest) routeGeoMetrics.delete(oldest);
    }
    routeGeoMetrics.set(key, m);
  }
  return m || null;
}

function haversineKm(lat1, lon1, lat2, lon2) {
  if (![lat1, lon1, lat2, lon2].every(Number.isFinite)) return 0;
  if (Math.abs(lat1) > 90 || Math.abs(lat2) > 90 || Math.abs(lon1) > 180 || Math.abs(lon2) > 180) return 0;
  const rad = Math.PI / 180;
  const dLat = (lat2 - lat1) * rad;
  const dLon = (lon2 - lon1) * rad;
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) ** 2;
  return 6371 * 2 * Math.asin(Math.min(1, Math.sqrt(a)));
}

function asProxy(value, fallbackPort = 80) {
  if (!value) return null;
  if (typeof value === "object") {
    const hostname = String(value.hostname || value.host || value.proxy || "").trim();
    const port = Number(value.port || fallbackPort);
    if (!hostname || !Number.isInteger(port) || port < 1 || port > 65535) return null;
    return {
      protocol: String(value.protocol || "http").toLowerCase(),
      hostname,
      port,
      username: value.username ? String(value.username) : "",
      password: value.password ? String(value.password) : "",
      country: value.country ? String(value.country) : "",
      country_code: normalizeCountry(value.country_code || ""),
      continent: value.continent ? String(value.continent).toUpperCase() : "",
      colo: value.colo ? String(value.colo).toUpperCase() : "",
      region: value.region ? String(value.region) : "",
      latitude: Number.isFinite(Number(value.latitude)) ? Number(value.latitude) : 0,
      longitude: Number.isFinite(Number(value.longitude)) ? Number(value.longitude) : 0,
    };
  }
  let raw = String(value).trim();
  if (!raw) return null;
  let protocol = "http";
  const m = raw.match(/^(https?|socks5|socks4):\/\//i);
  if (m) {
    protocol = m[1].toLowerCase();
    raw = raw.slice(m[0].length);
  }
  raw = raw.split("#", 1)[0];
  let username = "", password = "";
  const at = raw.lastIndexOf("@");
  if (at >= 0) {
    const auth = raw.slice(0, at);
    raw = raw.slice(at + 1);
    const i = auth.indexOf(":");
    if (i >= 0) { username = decodeURIComponent(auth.slice(0, i)); password = decodeURIComponent(auth.slice(i + 1)); }
  }
  let hostname = raw, port = fallbackPort;
  if (raw.startsWith("[")) {
    const end = raw.indexOf("]");
    if (end < 0) return null;
    hostname = raw.slice(1, end);
    if (raw[end + 1] === ":") port = Number(raw.slice(end + 2) || fallbackPort);
  } else {
    const idx = raw.lastIndexOf(":");
    if (idx > -1 && /^\d+$/.test(raw.slice(idx + 1))) {
      hostname = raw.slice(0, idx);
      port = Number(raw.slice(idx + 1));
    }
  }
  if (!hostname || !Number.isInteger(port) || port < 1 || port > 65535) return null;
  return { protocol, hostname, port, username, password, country: "", country_code: "", continent: "", colo: "", region: "", latitude: 0, longitude: 0 };
}

function flattenProxyMap(settings) {
  const out = [];
  const seen = new Set();
  const add = (entry, meta = {}) => {
    const base = asProxy(entry, Number(meta.port || 443));
    if (!base) return;
    const p = {
      ...base,
      country: base.country || String(meta.country || ""),
      country_code: base.country_code || normalizeCountry(meta.country_code || ""),
      continent: base.continent || String(meta.continent || "").toUpperCase(),
      colo: base.colo || String(meta.colo || "").toUpperCase(),
      region: base.region || String(meta.region || ""),
      latitude: base.latitude || Number(meta.latitude || 0),
      longitude: base.longitude || Number(meta.longitude || 0),
    };
    const key = proxyKey(p);
    if (!seen.has(key)) { seen.add(key); out.push(p); }
  };
  const map = settings && typeof settings.proxies === "object" ? settings.proxies : {};
  for (const [code, item] of Object.entries(map)) {
    if (!item || typeof item !== "object") continue;
    const common = { country: item.country || code, country_code: item.country_code || code, continent: item.continent || "", colo: item.colo || "", region: item.region || "", latitude: item.latitude || 0, longitude: item.longitude || 0, port: item.port || 443 };
    if (item.proxy) add(item.proxy, common);
    if (Array.isArray(item.proxies)) for (const x of item.proxies) add(x, common);
  }
  if (Array.isArray(settings && settings.proxies)) {
    for (const item of settings.proxies) add(item, item || {});
  }
  return out;
}

function metricFor(proxy) {
  const key = proxyKey(proxy);
  let m = routeMetrics.get(key);
  if (!m) {
    m = { ewmaMs: 0, jitterMs: 0, failures: 0, successes: 0, cooldownUntil: 0, lastOkAt: 0, lastFailAt: 0, lastCheckAt: 0 };
    routeMetrics.set(key, m);
  }
  return m;
}

function recordRoute(proxy, elapsedMs, ok, geo = null) {
  const m = metricFor(proxy);
  const gm = routeGeoMetric(proxy, geo, true);
  if (ok) {
    const prev = m.ewmaMs;
    const alpha = prev > 0 ? 0.25 : 1;
    m.ewmaMs = prev > 0 ? prev * (1 - alpha) + elapsedMs * alpha : elapsedMs;
    const delta = prev > 0 ? Math.abs(elapsedMs - prev) : 0;
    m.jitterMs = m.jitterMs > 0 ? m.jitterMs * 0.8 + delta * 0.2 : delta;
    m.failures = Math.max(0, m.failures - 1);
    m.successes += 1;
    m.lastOkAt = Date.now();
    m.cooldownUntil = 0;
    if (gm) {
      const gp = gm.ewmaMs;
      gm.ewmaMs = gp > 0 ? gp * 0.75 + elapsedMs * 0.25 : elapsedMs;
      gm.jitterMs = gm.jitterMs > 0 ? gm.jitterMs * 0.8 + Math.abs(elapsedMs - (gp || elapsedMs)) * 0.2 : 0;
      gm.successes += 1;
      gm.lastOkAt = Date.now();
      gm.failures = Math.max(0, gm.failures - 1);
    }
  } else {
    m.failures += 1;
    m.lastFailAt = Date.now();
    const backoff = Math.min(ROUTE_COOLDOWN_MAX_MS, ROUTE_COOLDOWN_MIN_MS * (2 ** Math.min(m.failures - 1, 4)));
    m.cooldownUntil = Date.now() + backoff;
    if (gm) gm.failures += 1;
  }
}

function routeScore(proxy, geo = null) {
  const m = metricFor(proxy);
  const now = Date.now();
  if (m.cooldownUntil > now) return Number.POSITIVE_INFINITY;

  const country = normalizeCountry(geo?.country);
  const continent = String(geo?.continent || "").toUpperCase();
  const pCountry = proxyCountry(proxy);
  const pContinent = proxyContinent(proxy);
  const pColo = String(proxy.colo || "").toUpperCase();
  const userColo = String(geo?.colo || "").toUpperCase();

  // Exact country is a stronger prior than continent; an explicitly tagged
  // matching colo is stronger still. A learned metric for this exact CF edge
  // gets the highest weight because it includes real path latency.
  let affinity = 0;
  if (country && pCountry && country === pCountry) affinity -= 100;
  else if (continent && pContinent && continent === pContinent) affinity -= 25;
  if (userColo && pColo && userColo === pColo) affinity -= 300;

  const gm = routeGeoMetric(proxy, geo);
  const latency = gm?.ewmaMs || m.ewmaMs || 900;
  const jitter = gm?.jitterMs || m.jitterMs || 0;
  const failures = (gm?.failures || 0) * 220 + Math.min(1200, m.failures * 180);

  // Optional proxy coordinates let operators provide a precise geographic
  // hint. Never use it without valid client coordinates.
  let distancePenalty = 0;
  if (geo?.has_coordinates && proxy.latitude && proxy.longitude) {
    const km = haversineKm(geo.latitude, geo.longitude, proxy.latitude, proxy.longitude);
    distancePenalty = Math.min(450, km / 8);
  }

  return latency + Math.min(500, jitter * 1.5) + failures + distancePenalty + affinity;
}

function orderRoutes(proxies, geo = null) {
  const unique = [];
  const seen = new Set();
  for (const proxy of proxies || []) {
    if (!proxy) continue;
    const key = proxyKey(proxy);
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(proxy);
  }
  const available = unique.filter(p => metricFor(p).cooldownUntil <= Date.now());
  const pool = available.length ? available : unique;
  return pool.sort((a, b) => routeScore(a, geo) - routeScore(b, geo));
}

function stickyKey(user, geo) {
  const uuid = normalizeUuid(user && user.uuid);
  if (!uuid) return "";
  const country = normalizeCountry(geo?.country) || "??";
  const colo = String(geo?.colo || "??").toUpperCase();
  return `${uuid}|${country}|${colo}`;
}

function pruneStickyRoutes(now = Date.now()) {
  for (const [key, value] of stickyRoutes.entries()) {
    if (!value || Number(value.expiresAt || 0) <= now) stickyRoutes.delete(key);
  }
  while (stickyRoutes.size > STICKY_ROUTE_MAX) {
    const first = stickyRoutes.keys().next().value;
    if (!first) break;
    stickyRoutes.delete(first);
  }
}

function getStickyRoute(proxies, user, geo) {
  pruneStickyRoutes();
  const key = stickyKey(user, geo);
  if (!key) return null;
  const sticky = stickyRoutes.get(key);
  if (!sticky || sticky.expiresAt <= Date.now()) {
    stickyRoutes.delete(key);
    return null;
  }
  const proxy = (proxies || []).find(p => proxyKey(p) === sticky.proxyKey);
  if (!proxy || metricFor(proxy).cooldownUntil > Date.now()) {
    stickyRoutes.delete(key);
    return null;
  }
  return { key, proxy, expiresAt: sticky.expiresAt, hits: Number(sticky.hits || 0) };
}

function setStickyRoute(proxy, user, geo) {
  if (!proxy) return;
  const key = stickyKey(user, geo);
  if (!key) return;
  pruneStickyRoutes();
  const previous = stickyRoutes.get(key);
  stickyRoutes.set(key, {
    proxyKey: proxyKey(proxy),
    expiresAt: Date.now() + STICKY_ROUTE_TTL_MS,
    hits: Number(previous?.hits || 0) + 1,
  });
}

function invalidateStickyRoute(user, geo, proxy, cooldownMs = STICKY_ROUTE_FAILURE_COOLDOWN_MS) {
  const key = stickyKey(user, geo);
  if (!key) return;
  const current = stickyRoutes.get(key);
  if (!current) return;
  if (!proxy || current.proxyKey === proxyKey(proxy)) {
    stickyRoutes.delete(key);
    // Keep a tiny memory of the failed route to avoid immediate re-stickiness
    // when a concurrent request races with the invalidation.
    if (proxy) stickyRoutes.set(`${key}|cooldown`, { proxyKey: proxyKey(proxy), expiresAt: Date.now() + cooldownMs, hits: 0 });
  }
}

function stickyCooldownProxy(user, geo) {
  const key = stickyKey(user, geo);
  const item = stickyRoutes.get(`${key}|cooldown`);
  if (!item) return null;
  if (item.expiresAt <= Date.now()) {
    stickyRoutes.delete(`${key}|cooldown`);
    return null;
  }
  return item.proxyKey || null;
}

async function withTimeout(promise, ms) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("timeout")), ms); }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function writeTextAndReadHeaders(writer, reader, requestText) {
  await writer.write(new TextEncoder().encode(requestText));
  let data = new Uint8Array(0);
  while (data.length < 16 * 1024) {
    const r = await reader.read();
    if (r.done) break;
    if (r.value && r.value.length) data = concatBytes(data, bytesFrom(r.value));
    const marker = new TextDecoder().decode(data).indexOf("\r\n\r\n");
    if (marker >= 0) return { data, headerEnd: marker + 4 };
  }
  throw new Error("proxy headers too large/incomplete");
}

async function readExact(reader, count, timeoutMs = 1800) {
  let data = new Uint8Array(0);
  while (data.length < count) {
    const r = await withTimeout(reader.read(), timeoutMs);
    if (r.done) throw new Error("socket closed");
    if (r.value && r.value.length) data = concatBytes(data, bytesFrom(r.value));
  }
  return data;
}

async function proxyConnect(proxy, targetHost, targetPort) {
  let socket = null, reader = null, writer = null;
  const secure = proxy.protocol === "https";
  try {
    socket = connect({ hostname: proxy.hostname, port: proxy.port }, secure ? { secureTransport: "on" } : undefined);
    reader = socket.readable.getReader();
    writer = socket.writable.getWriter();

    if (proxy.protocol === "socks5" || proxy.protocol === "socks4") {
      if (proxy.protocol !== "socks5") throw new Error("socks4 unsupported on Worker");
      const methods = proxy.username ? new Uint8Array([5, 2, 0, 2]) : new Uint8Array([5, 1, 0]);
      await writer.write(methods);
      const first = await readExact(reader, 2, 1500);
      if (first[0] !== 5) throw new Error("bad socks5 greeting");
      if (first[1] === 2) {
        if (!proxy.username) throw new Error("socks5 auth required");
        const ub = new TextEncoder().encode(proxy.username), pb = new TextEncoder().encode(proxy.password || "");
        await writer.write(concatBytes(new Uint8Array([1, ub.length]), concatBytes(ub, concatBytes(new Uint8Array([pb.length]), pb))));
        const ar = await readExact(reader, 2, 1500);
        if (ar[1] !== 0) throw new Error("socks5 auth failed");
      } else if (first[1] !== 0) throw new Error("socks5 auth method rejected");

      let atyp = 3, addrBytes;
      if (/^\d{1,3}(?:\.\d{1,3}){3}$/.test(targetHost)) { atyp = 1; addrBytes = Uint8Array.from(targetHost.split(".").map(Number)); }
      else if (targetHost.includes(":")) { throw new Error("ipv6 target requires domain/proxy dns support"); }
      else { const eb = new TextEncoder().encode(targetHost); addrBytes = concatBytes(new Uint8Array([eb.length]), eb); }
      await writer.write(concatBytes(new Uint8Array([5,1,0,atyp]), concatBytes(addrBytes, new Uint8Array([targetPort >> 8, targetPort & 255]))));
      const rr = await readExact(reader, 4, 1800);
      if (rr[1] !== 0 || rr[0] !== 5) throw new Error("socks5 connect failed");
      const ratyp = rr[3];
      if (ratyp === 1) await readExact(reader, 6, 1000);
      else if (ratyp === 3) { const ln = (await readExact(reader, 1, 1000))[0]; await readExact(reader, ln + 2, 1000); }
      else if (ratyp === 4) await readExact(reader, 18, 1000);
      else throw new Error("socks5 invalid bind address");
      return { socket, reader, writer };
    }

    const hostHeader = targetHost.includes(":") ? `[${targetHost}]` : targetHost;
    const auth = proxy.username ? `Proxy-Authorization: Basic ${btoa(`${proxy.username}:${proxy.password || ""}`)}\r\n` : "";
    const req = `CONNECT ${hostHeader}:${targetPort} HTTP/1.1\r\nHost: ${hostHeader}:${targetPort}\r\n${auth}User-Agent: Spider-Worker\r\nConnection: keep-alive\r\n\r\n`;
    const { data, headerEnd } = await withTimeout(writeTextAndReadHeaders(writer, reader, req), 2000);
    const head = new TextDecoder().decode(data.slice(0, headerEnd));
    if (!/^HTTP\/\d\.\d\s+200\b/m.test(head)) throw new Error(`proxy CONNECT failed: ${head.split("\r\n", 1)[0] || "unknown"}`);
    return { socket, reader, writer };
  } catch (e) {
    try { writer && await writer.close(); } catch (_) {}
    try { socket && socket.close(); } catch (_) {}
    throw e;
  }
}

async function openAdaptiveSocket(env, user, address, port, geo = null) {
  const settings = await getSettings(env);
  let proxies = flattenProxyMap(settings);
  const explicit = Array.isArray(user && user.proxy_ips) ? user.proxy_ips : [];
  if (explicit.length) {
    const selected = [];
    for (const raw of explicit) { const p = asProxy(raw, 443); if (p) selected.push(p); }
    if (selected.length) proxies = selected;
  }

  // Short-lived sticky path: use the route learned for this user+colo without
  // a new race. If it fails, invalidate it and immediately fall back to the
  // normal adaptive shortlist/race.
  const sticky = getStickyRoute(proxies, user, geo);
  if (sticky && !stickyCooldownProxy(user, geo)) {
    try {
      const t0 = Date.now();
      const conn = await withTimeout(proxyConnect(sticky.proxy, address, port), ROUTE_CONNECT_TIMEOUT_MS);
      const elapsed = Date.now() - t0;
      recordRoute(sticky.proxy, elapsed, true, geo);
      setStickyRoute(sticky.proxy, user, geo);
      return { conn, proxy: sticky.proxy, elapsed, stickyHit: true };
    } catch (_) {
      recordRoute(sticky.proxy, ROUTE_CONNECT_TIMEOUT_MS, false, geo);
      invalidateStickyRoute(user, geo, sticky.proxy);
    }
  }

  const cooldownProxyKey = stickyCooldownProxy(user, geo);
  const ordered = orderRoutes(proxies, geo).filter(p => !cooldownProxyKey || proxyKey(p) !== cooldownProxyKey);
  const attempts = ordered.length ? ordered : [null];
  let winnerChosen = false;

  async function attempt(proxy, delayMs) {
    if (delayMs) await new Promise(r => setTimeout(r, delayMs));
    const t0 = Date.now();
    try {
      const conn = proxy
        ? await withTimeout(proxyConnect(proxy, address, port), ROUTE_CONNECT_TIMEOUT_MS)
        : await withTimeout(openSocket(address, port), ROUTE_CONNECT_TIMEOUT_MS);
      if (winnerChosen) {
        try { await conn.writer.close(); } catch (_) {}
        try { conn.socket.close(); } catch (_) {}
        throw new Error("route lost race");
      }
      const elapsed = Date.now() - t0;
      if (proxy) recordRoute(proxy, elapsed, true, geo);
      return { conn, proxy, elapsed };
    } catch (e) {
      if (proxy && String(e?.message || e) !== "route lost race") {
        recordRoute(proxy, Date.now() - t0, false, geo);
      }
      throw e;
    }
  }

  for (let i = 0; i < attempts.length; i += ROUTE_RACE) {
    const batch = attempts.slice(i, i + ROUTE_RACE);
    const tasks = batch.map((p, idx) => attempt(p, idx * 60));
    try {
      const wrapped = tasks.map((p, idx) => p.then(v => {
        if (winnerChosen) {
          try { v.conn.writer.close(); } catch (_) {}
          try { v.conn.socket.close(); } catch (_) {}
          return { ok: false, idx, error: new Error("route lost race") };
        }
        winnerChosen = true;
        return { ok: true, v, idx };
      }).catch(error => ({ ok: false, error, idx })));
      const pending = new Set(wrapped);
      while (pending.size) {
        const winner = await Promise.race(pending);
        const same = wrapped[winner.idx];
        pending.delete(same);
        if (winner.ok) {
          if (winner.v.proxy) setStickyRoute(winner.v.proxy, user, geo);
          return { ...winner.v, stickyHit: false };
        }
      }
    } finally {
      // All materialized losing connections are closed by the winner branch.
    }
  }

  // Direct path is the final fallback when all managed proxy routes fail.
  const direct = await attempt(null, 0);
  return { ...direct, stickyHit: false };
}

async function getSettings(env) {
  if (!(await kvReady(env))) return {};
  try {
    const raw = await env.SPIDER_KV.get("settings");
    const data = raw ? JSON.parse(raw) : {};
    return data && typeof data === "object" ? data : {};
  } catch (_) { return {}; }
}

async function activeHealthCheck(env, geo = null) {
  if (Date.now() - lastHealthCheckAt < ROUTE_HEALTH_TTL_MS) return;
  lastHealthCheckAt = Date.now();
  const settings = await getSettings(env);
  const routes = orderRoutes(flattenProxyMap(settings), geo).slice(0, ROUTE_MAX_HEALTH_CHECKS);
  await Promise.all(routes.map(async (proxy) => {
    const t0 = Date.now();
    metricFor(proxy).lastCheckAt = Date.now();
    try {
      const conn = await withTimeout(proxyConnect(proxy, ROUTE_TEST_HOST, ROUTE_TEST_PORT), ROUTE_HEALTH_TIMEOUT_MS);
      recordRoute(proxy, Date.now() - t0, true, geo);
      try { await conn.writer.close(); } catch (_) {}
      try { conn.socket.close(); } catch (_) {}
    } catch (_) {
      recordRoute(proxy, Date.now() - t0, false, geo);
    }
  }));
}

function routingSummary(geo = null, settings = null) {
  const routes = [];
  for (const [key, m] of routeMetrics.entries()) {
    routes.push({ key, latency_ms: Math.round(m.ewmaMs || 0), jitter_ms: Math.round(m.jitterMs || 0), failures: m.failures, successes: m.successes, cooldown_until: m.cooldownUntil });
  }
  routes.sort((a, b) => (a.latency_ms || 99999) - (b.latency_ms || 99999));
  const healthy = routes.filter(x => !x.cooldown_until || x.cooldown_until <= Date.now()).length;
  let preferred = [];
  if (settings && geo) {
    preferred = orderRoutes(flattenProxyMap(settings), geo).slice(0, ROUTE_RACE).map(p => ({
      key: proxyKey(p),
      country: p.country || "",
      country_code: proxyCountry(p),
      continent: proxyContinent(p),
      colo: p.colo || "",
      score: Math.round(routeScore(p, geo)),
      latency_ms: Math.round(routeGeoMetric(p, geo)?.ewmaMs || metricFor(p).ewmaMs || 0),
      jitter_ms: Math.round(routeGeoMetric(p, geo)?.jitterMs || metricFor(p).jitterMs || 0),
    }));
  }
  return { healthy, tracked: routes.length, sticky_cached: stickyRoutes.size, routes: routes.slice(0, 12), preferred, edge: geo ? { country: geo.country, continent: geo.continent, colo: geo.colo, client_rtt_ms: geo.client_rtt_ms, quic_rtt_ms: geo.quic_rtt_ms } : {} };
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
  const geo = requestGeo(request);
  const meter = { pending: 0, lastFlush: Date.now() };
  let user = null;
  let conn = null;
  let heartbeat = null;
  let closed = false;
  let headerBuffer = new Uint8Array(0);
  let selectedRoute = null;

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

      try {
        const result = await openAdaptiveSocket(env, user, parsed.address, parsed.port, geo);
        conn = result?.conn || null;
        selectedRoute = result?.proxy || null;
      } catch (_) {
        conn = null;
        selectedRoute = null;
      }
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
          if (selectedRoute) {
            invalidateStickyRoute(user, geo, selectedRoute);
            recordRoute(selectedRoute, 0, false, geo);
          }
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
      if (selectedRoute) {
        invalidateStickyRoute(user, geo, selectedRoute);
        recordRoute(selectedRoute, 0, false, geo);
      }
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
        routing: routingSummary(),
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
          proxy_ips: Array.isArray(item.proxy_ips) ? item.proxy_ips.slice(0, 6).map(String) : [],
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
      const incomingSettings = body.settings && typeof body.settings === "object" ? { ...body.settings } : {};
      if (body.proxies && typeof body.proxies === "object") incomingSettings.proxies = body.proxies;
      if (!incomingSettings.routing) incomingSettings.routing = { race: ROUTE_RACE, connect_timeout_ms: ROUTE_CONNECT_TIMEOUT_MS, geo_affinity: true, health_check_interval_ms: ROUTE_HEALTH_TTL_MS };
      await env.SPIDER_KV.put("settings", JSON.stringify(incomingSettings));
      await env.SPIDER_KV.put("heartbeat", JSON.stringify({ at: Date.now(), users: written }));
      return json({ ok: true, users: written, traffic, online });
    }

    if (path === "/panel/status" && request.method === "GET") {
      if (!authorized(request)) return json({ error: "Forbidden" }, 403);
      if (!(await kvReady(env))) return json({ error: "SPIDER_KV binding missing" }, 503);
      const geo = requestGeo(request);
      const settings = await getSettings(env);
      await activeHealthCheck(env, geo);
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
      return json({ ok: true, users, traffic, online, routing: routingSummary(geo, settings) });
    }

    if (path === "/panel/health-check" && request.method === "POST") {
      if (!authorized(request)) return json({ error: "Forbidden" }, 403);
      if (!(await kvReady(env))) return json({ error: "SPIDER_KV binding missing" }, 503);
      lastHealthCheckAt = 0;
      const geo = requestGeo(request);
      const settings = await getSettings(env);
      await activeHealthCheck(env, geo);
      return json({ ok: true, routing: routingSummary(geo, settings) });
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
          proxy_ips: Array.isArray(body.proxy_ips) ? body.proxy_ips.slice(0, 6).map(String) : [],
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
