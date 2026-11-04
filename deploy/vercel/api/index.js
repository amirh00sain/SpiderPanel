// SpiderPanel Vercel WebSocket relay.
// Public client: wss://<vercel-domain>/ws/vl/<uuid>
// Upstream:      wss://<railway-domain>/ws/vl/<uuid>
// Vercel Functions WebSocket support is used here; this is not an HTTP body proxy.
import { createServer } from 'node:http';
import { WebSocket, WebSocketServer } from 'ws';

const TARGET_BASE = String(process.env.TARGET_DOMAIN || '').trim().replace(/\/+$/, '');
const RELAY_PATH = normalizePath(process.env.RELAY_PATH || '/ws/vl');
const PUBLIC_RELAY_PATH = normalizePath(process.env.PUBLIC_RELAY_PATH || '/ws/vl');
const MAX_ACTIVE_WEBSOCKETS = 128;
const UPSTREAM_HANDSHAKE_TIMEOUT_MS = 15000;
const MAX_WS_PAYLOAD = 16 * 1024 * 1024;
let activeWebSockets = 0;

const server = createServer(handleHttpRequest);
const wss = new WebSocketServer({
  noServer: true,
  perMessageDeflate: false,
  maxPayload: MAX_WS_PAYLOAD,
});

server.on('upgrade', (req, socket, head) => {
  socket.on('error', () => {});
  const url = parseRequestUrl(req);
  if (!url) {
    rejectUpgrade(socket, 400, 'Bad Request');
    return;
  }

  const uuid = extractUuid(url);
  if (!uuid || !isUuid(uuid)) {
    rejectUpgrade(socket, 404, 'WebSocket route not found');
    return;
  }
  if (!TARGET_BASE || !RELAY_PATH || !PUBLIC_RELAY_PATH) {
    rejectUpgrade(socket, 500, 'Misconfigured WebSocket relay');
    return;
  }
  if (activeWebSockets >= MAX_ACTIVE_WEBSOCKETS) {
    rejectUpgrade(socket, 503, 'Relay is busy');
    return;
  }

  const upstreamUrl = makeUpstreamUrl(uuid);
  const clientIp = getClientIp(req);
  const forwardedHeaders = {
    'x-forwarded-for': clientIp,
    'x-real-ip': clientIp,
    'x-forwarded-proto': 'https',
    'x-forwarded-host': String(req.headers.host || ''),
    'user-agent': String(req.headers['user-agent'] || 'SpiderPanel-WebSocket-Relay'),
  };
  if (req.headers.origin) forwardedHeaders.origin = String(req.headers.origin);

  let upstream;
  let client = null;
  let connectionCounted = false;
  let clientEnded = false;
  let upstreamEnded = false;
  let handshakeFinished = false;

  const handshakeTimer = setTimeout(() => {
    if (handshakeFinished) return;
    handshakeFinished = true;
    try { upstream?.terminate(); } catch (_) {}
    rejectUpgrade(socket, 504, 'Upstream WebSocket handshake timed out');
  }, UPSTREAM_HANDSHAKE_TIMEOUT_MS);
  handshakeTimer.unref?.();

  try {
    const target = new URL(upstreamUrl);
    target.protocol = target.protocol === 'https:' ? 'wss:' : target.protocol === 'http:' ? 'ws:' : target.protocol;
    upstream = new WebSocket(target.toString(), {
      headers: forwardedHeaders,
      handshakeTimeout: UPSTREAM_HANDSHAKE_TIMEOUT_MS,
      perMessageDeflate: false,
      maxPayload: MAX_WS_PAYLOAD,
      followRedirects: false,
    });
  } catch (err) {
    clearTimeout(handshakeTimer);
    handshakeFinished = true;
    console.error('[SpiderPanel WS relay] invalid upstream URL', { name: err?.name, message: err?.message });
    rejectUpgrade(socket, 500, 'Invalid upstream URL');
    return;
  }

  upstream.once('open', () => {
    if (handshakeFinished || socket.destroyed) {
      try { upstream.close(); } catch (_) {}
      return;
    }
    handshakeFinished = true;
    clearTimeout(handshakeTimer);
    try {
      wss.handleUpgrade(req, socket, head, (acceptedClient) => {
        client = acceptedClient;
        activeWebSockets += 1;
        connectionCounted = true;
        console.info('[SpiderPanel WS relay] connected', { uuid: uuid.slice(0, 8), active: activeWebSockets });

        client.on('message', (data, isBinary) => {
          if (upstream.readyState !== WebSocket.OPEN) return;
          if (upstream.bufferedAmount > 4 * 1024 * 1024) {
            closePair(1013, 'upstream backpressure');
            return;
          }
          upstream.send(data, { binary: isBinary }, (err) => {
            if (err) {
              console.error('[SpiderPanel WS relay] client-to-upstream send failed', { message: err.message });
              closePair(1011, 'relay send failed');
            }
          });
        });

        upstream.on('message', (data, isBinary) => {
          if (client.readyState !== WebSocket.OPEN) return;
          if (client.bufferedAmount > 4 * 1024 * 1024) {
            closePair(1013, 'client backpressure');
            return;
          }
          client.send(data, { binary: isBinary }, (err) => {
            if (err) {
              console.error('[SpiderPanel WS relay] upstream-to-client send failed', { message: err.message });
              closePair(1011, 'relay send failed');
            }
          });
        });

        client.on('close', (code, reason) => {
          clientEnded = true;
          releaseSlot();
          if (upstream.readyState === WebSocket.OPEN) closeUpstream(code, reason);
          else if (upstream.readyState === WebSocket.CONNECTING) upstream.terminate();
        });
        client.on('error', (err) => {
          console.warn('[SpiderPanel WS relay] client socket error', { message: err?.message });
          if (upstream.readyState === WebSocket.OPEN) upstream.terminate();
        });
        upstream.on('close', (code, reason) => {
          upstreamEnded = true;
          releaseSlot();
          if (client.readyState === WebSocket.OPEN) closeClient(code, reason);
        });
        upstream.on('error', (err) => {
          console.warn('[SpiderPanel WS relay] upstream socket error', {
            uuid: uuid.slice(0, 8), name: err?.name, message: err?.message,
          });
          if (client.readyState === WebSocket.OPEN) client.close(1011, 'upstream relay error');
        });
      });
    } catch (err) {
      console.error('[SpiderPanel WS relay] client upgrade failed', { message: err?.message });
      try { upstream.terminate(); } catch (_) {}
      if (!socket.destroyed) rejectUpgrade(socket, 502, 'WebSocket upgrade failed');
    }
  });

  upstream.once('unexpected-response', (_request, response) => {
    clearTimeout(handshakeTimer);
    if (handshakeFinished) return;
    handshakeFinished = true;
    console.warn('[SpiderPanel WS relay] upstream rejected upgrade', {
      uuid: uuid.slice(0, 8), status: response.statusCode,
    });
    response.resume();
    rejectUpgrade(socket, 502, `Upstream WebSocket rejected (${response.statusCode || 'unknown'})`);
  });

  upstream.on('error', (err) => {
    clearTimeout(handshakeTimer);
    if (!handshakeFinished) {
      handshakeFinished = true;
      console.error('[SpiderPanel WS relay] upstream handshake failed', {
        uuid: uuid.slice(0, 8), name: err?.name, message: err?.message,
      });
      rejectUpgrade(socket, 502, 'Upstream WebSocket handshake failed');
    }
  });

  function releaseSlot() {
    if (connectionCounted) {
      connectionCounted = false;
      activeWebSockets = Math.max(0, activeWebSockets - 1);
    }
  }
  function closePair(code, reason) {
    if (client && client.readyState === WebSocket.OPEN) closeClient(code, Buffer.from(reason || ''));
    if (upstream && upstream.readyState === WebSocket.OPEN) closeUpstream(code, Buffer.from(reason || ''));
  }
  function closeClient(code, reason) {
    if (!client || client.readyState !== WebSocket.OPEN) return;
    const safeReason = closeReason(reason);
    if (isValidCloseCode(code)) client.close(code, safeReason);
    else client.close();
  }
  function closeUpstream(code, reason) {
    if (!upstream || upstream.readyState !== WebSocket.OPEN) return;
    const safeReason = closeReason(reason);
    if (isValidCloseCode(code)) upstream.close(code, safeReason);
    else upstream.close();
  }

  // The flags are retained in this lexical scope for clearer close diagnostics.
  void clientEnded;
  void upstreamEnded;
});

function handleHttpRequest(req, res) {
  const url = parseRequestUrl(req);
  const path = url ? normalizePath(url.pathname) : '';
  const isHealthRequest = path === '/_spider-health' ||
    ((path === '/api/index' || path === '/api' || path === '/') && url?.searchParams.get('health') === '1');

  if (isHealthRequest) {
    if (!TARGET_BASE || !RELAY_PATH || RELAY_PATH === '/') {
      sendHealthResponse(res, 503, false, 'Misconfigured WebSocket relay');
      return;
    }
    // Do not call the relay healthy just because this HTTP function runs. Probe
    // the dedicated WebSocket endpoint on Railway so a missing WS route or
    // upstream handshake failure is caught before the panel saves the domain.
    probeUpstreamWebSocket()
      .then(() => sendHealthResponse(res, 200, true, 'SpiderPanel WebSocket relay is online'))
      .catch((err) => {
        console.warn('[SpiderPanel WS relay] health probe failed', { name: err?.name, message: err?.message });
        sendHealthResponse(res, 502, false, 'Upstream WebSocket health check failed');
      });
    return;
  }

  if (path === PUBLIC_RELAY_PATH || path.startsWith(`${PUBLIC_RELAY_PATH}/`)) {
    res.statusCode = 426;
    res.setHeader('upgrade', 'websocket');
    res.setHeader('connection', 'Upgrade');
    res.end('WebSocket upgrade required');
    return;
  }
  res.statusCode = 404;
  res.setHeader('content-type', 'text/plain; charset=utf-8');
  res.end('Not Found');
}

function sendHealthResponse(res, status, healthy, body) {
  if (res.writableEnded || res.destroyed) return;
  res.statusCode = status;
  res.setHeader('content-type', 'text/plain; charset=utf-8');
  res.setHeader('cache-control', 'no-store, no-cache, must-revalidate');
  res.setHeader('x-spiderpanel-relay', healthy ? 'healthy' : 'unhealthy');
  res.end(body);
}

function probeUpstreamWebSocket() {
  return new Promise((resolve, reject) => {
    let settled = false;
    let socket;
    const timeout = setTimeout(() => finish(new Error('upstream WS health probe timed out')), 7000);
    timeout.unref?.();
    const finish = (err) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      try {
        if (socket && socket.readyState === WebSocket.OPEN) socket.close(1000, 'health probe complete');
        else if (socket && socket.readyState === WebSocket.CONNECTING) socket.terminate();
      } catch (_) {}
      if (err) reject(err); else resolve();
    };
    try {
      socket = new WebSocket(makeUpstreamUrl('__health'), {
        headers: { 'user-agent': 'SpiderPanel-WebSocket-Health-Probe', 'x-forwarded-proto': 'https' },
        handshakeTimeout: 5000,
        perMessageDeflate: false,
        followRedirects: false,
      });
      socket.once('message', (data, isBinary) => {
        const text = isBinary ? '' : Buffer.isBuffer(data) ? data.toString('utf8') : String(data);
        if (text === 'SpiderPanel WS health ok') finish();
        else finish(new Error('unexpected WS health response'));
      });
      socket.once('close', () => {
        if (!settled) finish(new Error('upstream closed before WS health response'));
      });
      socket.once('error', (err) => finish(err));
    } catch (err) {
      finish(err);
    }
  });
}

function parseRequestUrl(req) {
  try { return new URL(req.url || '/', `https://${req.headers.host || 'localhost'}`); }
  catch (_) { return null; }
}

function extractUuid(url) {
  const path = normalizePath(url.pathname);
  let raw = '';
  if (path.startsWith(`${PUBLIC_RELAY_PATH}/`)) {
    raw = path.slice(PUBLIC_RELAY_PATH.length + 1).split('/')[0] || '';
  } else if ((path === '/api/index' || path === '/api') && url.searchParams.get('ws') === '1') {
    raw = url.searchParams.get('uuid') || '';
  }
  try { return decodeURIComponent(raw).toLowerCase(); }
  catch (_) { return ''; }
}

function makeUpstreamUrl(uuid) {
  const target = new URL(TARGET_BASE);
  target.protocol = target.protocol === 'https:' ? 'wss:' : target.protocol === 'http:' ? 'ws:' : target.protocol;
  target.pathname = `${RELAY_PATH}/${encodeURIComponent(uuid)}`.replace(/\/{2,}/g, '/');
  target.search = '';
  target.hash = '';
  return target.toString();
}

function getClientIp(req) {
  const real = String(req.headers['x-real-ip'] || '').split(',')[0].trim();
  if (real) return real;
  return String(req.headers['x-forwarded-for'] || '').split(',')[0].trim() || 'unknown';
}

function rejectUpgrade(socket, status, message) {
  if (!socket || socket.destroyed) return;
  const body = String(message || 'Bad Gateway');
  try {
    socket.write(`HTTP/1.1 ${status} ${statusText(status)}\r\n` +
      'Connection: close\r\n' +
      'Content-Type: text/plain; charset=utf-8\r\n' +
      `Content-Length: ${Buffer.byteLength(body)}\r\n\r\n${body}`);
  } catch (_) {}
  socket.destroy();
}

function statusText(status) {
  return ({ 400: 'Bad Request', 404: 'Not Found', 426: 'Upgrade Required', 500: 'Internal Server Error', 502: 'Bad Gateway', 503: 'Service Unavailable', 504: 'Gateway Timeout' })[status] || 'Error';
}

function normalizePath(value) {
  let path = String(value || '').trim();
  if (!path) return '';
  if (!path.startsWith('/')) path = `/${path}`;
  path = path.replace(/\/{2,}/g, '/');
  if (path.length > 1 && path.endsWith('/')) path = path.slice(0, -1);
  return path;
}

function isUuid(value) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(String(value || ''));
}

function isValidCloseCode(code) {
  return Number.isInteger(code) && code >= 1000 && code <= 4999 && ![1004, 1005, 1006, 1015].includes(code);
}

function closeReason(reason) {
  const text = Buffer.isBuffer(reason) ? reason.toString('utf8') : String(reason || '');
  return Buffer.from(text).subarray(0, 123).toString('utf8');
}

// Vercel's Node.js WebSocket runtime supports exporting an HTTP server object.
export default server;
