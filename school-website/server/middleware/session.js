'use strict';

/**
 * مدیریت نشست‌ها.
 * - شناسه نشست تصادفی ۳۲ بایتی است و فقط به صورت هش در دیتابیس ذخیره می‌شود.
 * - کوکی نشست HttpOnly است و در محیط HTTPS پرچم Secure می‌گیرد.
 * - توکن CSRF همراه هر نشست ذخیره می‌شود.
 */

const crypto = require('crypto');

const config = require('../config');
const { getDb } = require('../database/db');
const { sendError } = require('../utils/http');

function hashToken(token) {
  return crypto.createHash('sha256').update(String(token)).digest('hex');
}

function sessionCookieOptions() {
  return {
    httpOnly: true,
    secure: config.cookie.secure,
    sameSite: config.cookie.sameSite,
    path: '/',
    maxAge: config.sessionTtlMs,
  };
}

function createSession({ principalType, principalId, ip, userAgent }) {
  const db = getDb();
  const token = crypto.randomBytes(32).toString('base64url');
  const csrfToken = crypto.randomBytes(24).toString('base64url');
  const expiresAt = new Date(Date.now() + config.sessionTtlMs).toISOString();
  db.prepare(
    `INSERT INTO sessions (token_hash, principal_type, principal_id, csrf_token, ip, user_agent, last_seen_at, expires_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(
    hashToken(token),
    principalType,
    principalId,
    csrfToken,
    String(ip || '').slice(0, 60),
    String(userAgent || '').slice(0, 300),
    new Date().toISOString(),
    expiresAt
  );
  return { token, csrfToken, expiresAt };
}

function destroySession(token) {
  if (!token) return;
  getDb().prepare('DELETE FROM sessions WHERE token_hash = ?').run(hashToken(token));
}

function destroySessionsFor(principalType, principalId) {
  getDb()
    .prepare('DELETE FROM sessions WHERE principal_type = ? AND principal_id = ?')
    .run(principalType, principalId);
}

function purgeExpiredSessions() {
  getDb().prepare('DELETE FROM sessions WHERE expires_at < ?').run(new Date().toISOString());
}

/** خواندن نشست از کوکی روی همه درخواست‌ها */
function attachSession(req, res, next) {
  req.auth = null;
  const token = req.cookies ? req.cookies[config.cookie.sessionName] : null;
  if (!token) return next();

  try {
    const db = getDb();
    const row = db.prepare('SELECT * FROM sessions WHERE token_hash = ?').get(hashToken(token));
    if (!row) return next();

    if (new Date(row.expires_at).getTime() <= Date.now()) {
      db.prepare('DELETE FROM sessions WHERE id = ?').run(row.id);
      return next();
    }

    if (row.principal_type === 'admin') {
      const admin = db
        .prepare('SELECT id, username, full_name, role, is_active, must_change_password FROM admins WHERE id = ?')
        .get(row.principal_id);
      if (!admin || !admin.is_active) {
        db.prepare('DELETE FROM sessions WHERE id = ?').run(row.id);
        return next();
      }
      req.auth = {
        type: 'admin',
        id: admin.id,
        admin,
        csrfToken: row.csrf_token,
        sessionId: row.id,
        token,
      };
    } else {
      const user = db
        .prepare('SELECT id, mobile, full_name, is_blocked FROM users WHERE id = ?')
        .get(row.principal_id);
      if (!user || user.is_blocked) {
        db.prepare('DELETE FROM sessions WHERE id = ?').run(row.id);
        return next();
      }
      req.auth = {
        type: 'user',
        id: user.id,
        user,
        csrfToken: row.csrf_token,
        sessionId: row.id,
        token,
      };
    }

    // به‌روزرسانی زمان آخرین فعالیت (حداکثر هر ۵ دقیقه یک بار)
    const lastSeen = row.last_seen_at ? new Date(row.last_seen_at).getTime() : 0;
    if (Date.now() - lastSeen > 5 * 60 * 1000) {
      db.prepare('UPDATE sessions SET last_seen_at = ? WHERE id = ?').run(new Date().toISOString(), row.id);
    }
    return next();
  } catch (error) {
    return next(error);
  }
}

function setSessionCookie(res, token) {
  res.cookie(config.cookie.sessionName, token, sessionCookieOptions());
}

function clearSessionCookie(res) {
  res.clearCookie(config.cookie.sessionName, { ...sessionCookieOptions(), maxAge: undefined });
}

function requireAdmin(req, res, next) {
  if (!req.auth || req.auth.type !== 'admin') {
    return sendError(res, 401, 'دسترسی به این بخش فقط برای مدیران مجاز است.');
  }
  return next();
}

function requireUser(req, res, next) {
  if (!req.auth || req.auth.type !== 'user') {
    return sendError(res, 401, 'برای این بخش باید با شماره موبایل وارد شوید.');
  }
  return next();
}

function requireAuth(req, res, next) {
  if (!req.auth) return sendError(res, 401, 'ابتدا وارد حساب خود شوید.');
  return next();
}

module.exports = {
  hashToken,
  sessionCookieOptions,
  createSession,
  destroySession,
  destroySessionsFor,
  purgeExpiredSessions,
  attachSession,
  setSessionCookie,
  clearSessionCookie,
  requireAdmin,
  requireUser,
  requireAuth,
};
