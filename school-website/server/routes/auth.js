'use strict';

/**
 * احراز هویت:
 * - ورود مدیر با نام کاربری و رمز عبور (bcrypt + محدودیت تلاش).
 * - ورود کاربران با شماره موبایل و کد پیامکی.
 */

const express = require('express');
const bcrypt = require('bcryptjs');

const config = require('../config');
const { getDb } = require('../database/db');
const { asyncHandler, clientIp, sendError, sendOk } = require('../utils/http');
const { rateLimit } = require('../middleware/rateLimit');
const {
  createSession,
  destroySession,
  destroySessionsFor,
  setSessionCookie,
  clearSessionCookie,
  requireUser,
} = require('../middleware/session');
const audit = require('../services/audit');
const otpService = require('../services/otp');
const smsService = require('../services/sms');

const router = express.Router();

const loginLimiter = rateLimit({
  name: 'admin-login',
  windowMs: config.login.windowMs,
  max: config.login.maxAttempts,
  blockMs: config.login.lockMs,
  message: 'تلاش‌های ناموفق ورود بیش از حد مجاز بود. لطفاً چند دقیقه بعد تلاش کنید.',
});

const otpRequestLimiter = rateLimit({
  name: 'otp-request',
  windowMs: 10 * 60 * 1000,
  max: 10,
  message: 'تعداد درخواست کد بیش از حد مجاز است. کمی بعد تلاش کنید.',
});

const otpVerifyLimiter = rateLimit({
  name: 'otp-verify',
  windowMs: 10 * 60 * 1000,
  max: 20,
  message: 'تعداد بررسی کد بیش از حد مجاز است. کمی بعد تلاش کنید.',
});

/** ورود مدیر */
router.post(
  '/admin/login',
  loginLimiter,
  asyncHandler(async (req, res) => {
    const db = getDb();
    const body = req.body || {};
    const username = String(body.username || '').trim();
    const password = String(body.password || '');
    const ip = clientIp(req);

    if (!username || !password) {
      return sendError(res, 400, 'نام کاربری و رمز عبور را وارد کنید.');
    }

    const admin = db.prepare('SELECT * FROM admins WHERE username = ?').get(username);
    const hash = admin ? admin.password_hash : '$2a$12$invalidinvalidinvalidinvalidinvalidinvali';
    const passwordMatches = bcrypt.compareSync(password, hash);

    if (!admin || !passwordMatches || !admin.is_active) {
      audit.log({
        actorType: 'admin',
        actorId: admin ? admin.id : null,
        actorLabel: username,
        action: 'admin.login_failed',
        ip,
      });
      return sendError(res, 401, 'نام کاربری یا رمز عبور نادرست است.');
    }

    db.prepare('UPDATE admins SET last_login_at = ? WHERE id = ?').run(new Date().toISOString(), admin.id);
    const session = createSession({
      principalType: 'admin',
      principalId: admin.id,
      ip,
      userAgent: req.get('user-agent'),
    });
    setSessionCookie(res, session.token);
    res.cookie(config.cookie.csrfName, session.csrfToken, {
      httpOnly: true,
      secure: config.cookie.secure,
      sameSite: config.cookie.sameSite,
      path: '/',
    });

    audit.log({
      actorType: 'admin',
      actorId: admin.id,
      actorLabel: admin.username,
      action: 'admin.login',
      ip,
    });

    return sendOk(res, {
      admin: {
        id: admin.id,
        username: admin.username,
        full_name: admin.full_name,
        role: admin.role,
        must_change_password: admin.must_change_password,
      },
      csrfToken: session.csrfToken,
    });
  })
);

/** خروج */
router.post('/auth/logout', (req, res) => {
  if (req.auth) {
    audit.log({
      actorType: req.auth.type,
      actorId: req.auth.id,
      actorLabel: req.auth.type === 'admin' ? req.auth.admin.username : req.auth.user.mobile,
      action: 'auth.logout',
      ip: clientIp(req),
    });
    destroySession(req.auth.token);
  }
  clearSessionCookie(res);
  return sendOk(res, { message: 'از حساب خود خارج شدید.' });
});

/** وضعیت نشست جاری */
router.get('/auth/session', (req, res) => {
  if (!req.auth) {
    return sendOk(res, { principal: null, csrfToken: req.csrfToken });
  }
  if (req.auth.type === 'admin') {
    return sendOk(res, {
      principal: 'admin',
      csrfToken: req.csrfToken,
      admin: {
        id: req.auth.admin.id,
        username: req.auth.admin.username,
        full_name: req.auth.admin.full_name,
        role: req.auth.admin.role,
        must_change_password: req.auth.admin.must_change_password,
      },
    });
  }
  return sendOk(res, {
    principal: 'user',
    csrfToken: req.csrfToken,
    user: { id: req.auth.user.id, mobile: req.auth.user.mobile, full_name: req.auth.user.full_name },
  });
});

/** درخواست کد یک‌بارمصرف */
router.post(
  '/auth/otp/request',
  otpRequestLimiter,
  asyncHandler(async (req, res) => {
    const ip = clientIp(req);
    const result = await otpService.requestOtp({ mobile: (req.body || {}).mobile, ip, purpose: 'login' });
    audit.log({
      actorType: 'public',
      action: 'otp.request',
      entity: 'otp_requests',
      details: { simulated: result.simulated },
      ip,
    });
    return sendOk(res, result);
  })
);

/** بررسی کد و ایجاد نشست کاربر */
router.post(
  '/auth/otp/verify',
  otpVerifyLimiter,
  asyncHandler(async (req, res) => {
    const ip = clientIp(req);
    const body = req.body || {};
    const { mobile } = otpService.verifyOtp({ mobile: body.mobile, code: body.code, purpose: 'login' });

    const db = getDb();
    let user = db.prepare('SELECT * FROM users WHERE mobile = ?').get(mobile);
    const now = new Date().toISOString();
    if (!user) {
      db.prepare(
        'INSERT INTO users (mobile, mobile_verified_at, last_login_at) VALUES (?, ?, ?)'
      ).run(mobile, now, now);
      user = db.prepare('SELECT * FROM users WHERE mobile = ?').get(mobile);
    } else {
      db.prepare('UPDATE users SET mobile_verified_at = ?, last_login_at = ? WHERE id = ?').run(
        now,
        now,
        user.id
      );
    }

    if (user.is_blocked) {
      return sendError(res, 403, 'دسترسی این شماره موبایل مسدود شده است.');
    }

    const session = createSession({
      principalType: 'user',
      principalId: user.id,
      ip,
      userAgent: req.get('user-agent'),
    });
    setSessionCookie(res, session.token);
    res.cookie(config.cookie.csrfName, session.csrfToken, {
      httpOnly: true,
      secure: config.cookie.secure,
      sameSite: config.cookie.sameSite,
      path: '/',
    });

    audit.log({
      actorType: 'user',
      actorId: user.id,
      actorLabel: mobile,
      action: 'user.login',
      ip,
    });

    return sendOk(res, {
      user: { id: user.id, mobile: user.mobile, full_name: user.full_name },
      csrfToken: session.csrfToken,
      message: 'ورود با موفقیت انجام شد.',
    });
  })
);

/** درخواست‌های پیش‌ثبت‌نام کاربر وارد‌شده */
router.get('/my/requests', requireUser, (req, res) => {
  const db = getDb();
  const items = db
    .prepare(
      `SELECT id, student_name, grade_title, field_title, status, tracking_code, created_at, updated_at, reviewed_at
       FROM pre_registrations WHERE mobile = ? ORDER BY id DESC`
    )
    .all(req.auth.user.mobile);
  return sendOk(res, { items, mobile: req.auth.user.mobile });
});

/** پایان دادن به همه نشست‌های خود (خروج از همه دستگاه‌ها) */
router.post('/auth/logout-all', (req, res) => {
  if (!req.auth) return sendError(res, 401, 'ابتدا وارد حساب خود شوید.');
  destroySessionsFor(req.auth.type, req.auth.id);
  clearSessionCookie(res);
  audit.log({
    actorType: req.auth.type,
    actorId: req.auth.id,
    action: 'auth.logout_all',
    ip: clientIp(req),
  });
  return sendOk(res, { message: 'همه نشست‌های شما بسته شد.' });
});

/** وضعیت سرویس پیامک برای صفحه ورود */
router.get('/auth/sms-status', (req, res) => {
  const status = smsService.status();
  return sendOk(res, {
    enabled: status.enabled,
    configured: status.configured,
    message: status.message,
  });
});

module.exports = router;
