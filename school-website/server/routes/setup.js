'use strict';

/**
 * فرایند نصب اولیه: ساخت امن اولین حساب مدیر.
 * - فقط تا زمانی کار می‌کند که هیچ مدیری وجود نداشته باشد.
 * - نیازمند توکن نصب (ADMIN_SETUP_TOKEN) است تا کسی نتواند از راه دور مدیر بسازد.
 * - پس از ساخت اولین مدیر به صورت خودکار غیرفعال می‌شود.
 */

const crypto = require('crypto');

const express = require('express');
const bcrypt = require('bcryptjs');

const config = require('../config');
const { getDb } = require('../database/db');
const { asyncHandler, clientIp, sendError, sendOk } = require('../utils/http');
const { rateLimit } = require('../middleware/rateLimit');
const { isValidUsername, strongEnoughPassword, cleanText } = require('../utils/validate');
const audit = require('../services/audit');

const router = express.Router();

const setupLimiter = rateLimit({
  name: 'setup',
  windowMs: 15 * 60 * 1000,
  max: 10,
  message: 'تلاش‌های نصب بیش از حد مجاز است. کمی بعد تلاش کنید.',
});

function setupState() {
  const db = getDb();
  const adminsTotal = db.prepare('SELECT COUNT(*) AS total FROM admins').get().total;
  const alreadyDone = adminsTotal > 0;
  return {
    adminsTotal,
    alreadyDone,
    open: !alreadyDone && !config.setup.disabled,
    tokenRequired: Boolean(config.setup.token),
    disabled: config.setup.disabled,
  };
}

function tokenMatches(provided) {
  const expected = String(config.setup.token || '');
  const candidate = String(provided || '');
  if (!expected) return false;
  const left = Buffer.from(candidate);
  const right = Buffer.from(expected);
  if (left.length !== right.length) return false;
  return crypto.timingSafeEqual(left, right);
}

router.get('/status', (req, res) => {
  const state = setupState();
  sendOk(res, {
    needsSetup: state.open,
    alreadyDone: state.alreadyDone,
    tokenRequired: state.tokenRequired,
    disabled: state.disabled,
  });
});

router.post(
  '/admin',
  setupLimiter,
  asyncHandler(async (req, res) => {
    const db = getDb();
    const state = setupState();
    const ip = clientIp(req);

    if (state.alreadyDone) {
      return sendError(res, 403, 'مرحله نصب قبلاً انجام شده است. برای تغییر مدیران از پنل مدیریت استفاده کنید.');
    }
    if (config.setup.disabled) {
      return sendError(res, 403, 'مرحله نصب غیرفعال شده است (SETUP_DISABLED).');
    }
    if (!config.setup.token) {
      return sendError(
        res,
        503,
        'توکن نصب تعیین نشده است. متغیر ADMIN_SETUP_TOKEN را در .env تنظیم و سرور را دوباره اجرا کنید.'
      );
    }
    if (!tokenMatches((req.body || {}).setup_token)) {
      audit.log({ actorType: 'public', action: 'setup.token_failed', ip });
      return sendError(res, 403, 'توکن نصب نادرست است.');
    }

    const body = req.body || {};
    const username = String(body.username || '').trim();
    const password = String(body.password || '');
    const fullName = cleanText(body.full_name, { maxLength: 120 });
    const errors = {};

    if (!isValidUsername(username)) {
      errors.username = 'نام کاربری باید ۳ تا ۶۰ نویسه لاتین، رقم، نقطه، خط تیره یا زیرخط باشد.';
    }
    if (!strongEnoughPassword(password)) {
      errors.password = 'رمز عبور باید حداقل ۱۰ نویسه و شامل حرف و رقم باشد.';
    }
    if (Object.keys(errors).length > 0) {
      return sendError(res, 400, 'اطلاعات وارد‌شده معتبر نیست.', errors);
    }

    const exists = db.prepare('SELECT id FROM admins WHERE username = ?').get(username);
    if (exists) return sendError(res, 409, 'این نام کاربری قبلاً استفاده شده است.', { username: 'تکراری است.' });

    const info = db
      .prepare(
        "INSERT INTO admins (username, password_hash, full_name, role, is_active) VALUES (?, ?, ?, 'owner', 1)"
      )
      .run(username, bcrypt.hashSync(password, 12), fullName);

    audit.log({
      actorType: 'public',
      action: 'setup.admin_created',
      entity: 'admins',
      entityId: String(info.lastInsertRowid),
      details: { username },
      ip,
    });

    return sendOk(
      res,
      {
        message: 'حساب مدیر ارشد ساخته شد. اکنون از صفحه ورود پنل مدیریت وارد شوید.',
        admin: { id: info.lastInsertRowid, username },
      },
      201
    );
  })
);

module.exports = router;
