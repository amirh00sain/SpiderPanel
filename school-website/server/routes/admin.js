'use strict';

/**
 * API مدیریتی: داشبورد، تنظیمات، اطلاعات مدرسه، پیامک، پیش‌ثبت‌نام‌ها،
 * کاربران، بارگذاری تصویر و گزارش‌های امنیتی.
 */

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const express = require('express');
const bcrypt = require('bcryptjs');
const multer = require('multer');

const config = require('../config');
const { getDb } = require('../database/db');
const { asyncHandler, clientIp, sendError, sendOk } = require('../utils/http');
const { cleanText, isValidEmail, isValidUrl } = require('../utils/validate');
const settingsService = require('../services/settings');
const smsService = require('../services/sms');
const statsService = require('../services/stats');
const audit = require('../services/audit');
const { destroySessionsFor } = require('../middleware/session');

const router = express.Router();

const ROLE_ADMIN_ONLY = ['owner', 'admin'];
const PREREG_STATUSES = ['new', 'reviewing', 'approved', 'rejected', 'archived'];

function requireRole(...roles) {
  return (req, res, next) => {
    const role = req.auth && req.auth.admin ? req.auth.admin.role : null;
    if (!role || !roles.includes(role)) {
      return sendError(res, 403, 'نقش کاربری شما اجازه این عملیات را ندارد.');
    }
    return next();
  };
}

function actor(req) {
  return {
    actorType: 'admin',
    actorId: req.auth.admin.id,
    actorLabel: req.auth.admin.username,
    ip: clientIp(req),
  };
}

router.get('/dashboard', (req, res) => {
  sendOk(res, statsService.dashboard());
});

/** تنظیمات عمومی سایت */
router.get('/settings', requireRole(...ROLE_ADMIN_ONLY), (req, res) => {
  sendOk(res, {
    settings: settingsService.getSettings(),
    definitions: Object.entries(settingsService.SETTING_DEFINITIONS).map(([key, definition]) => ({
      key,
      ...definition,
    })),
  });
});

router.put(
  '/settings',
  requireRole(...ROLE_ADMIN_ONLY),
  asyncHandler(async (req, res) => {
    const settings = settingsService.updateSettings(req.body || {});
    audit.log({ ...actor(req), action: 'settings.update', entity: 'site_settings' });
    sendOk(res, { settings, message: 'تنظیمات ذخیره شد.' });
  })
);

/** اطلاعات مدرسه */
router.get('/school-info', requireRole(...ROLE_ADMIN_ONLY), (req, res) => {
  const info = settingsService.getSchoolInfo();
  sendOk(res, {
    info,
    fields: settingsService.SCHOOL_INFO_FIELDS,
    missing: settingsService.missingPublicFields(info),
  });
});

router.put(
  '/school-info',
  requireRole(...ROLE_ADMIN_ONLY),
  asyncHandler(async (req, res) => {
    const body = req.body || {};
    const errors = {};

    if (body.email && !isValidEmail(body.email)) errors.email = 'ایمیل معتبر نیست.';
    for (const field of ['map_url', 'instagram_url', 'telegram_url', 'whatsapp_url', 'aparat_url']) {
      if (body[field] && !isValidUrl(body[field])) {
        errors[field] = 'آدرس باید با http:// یا https:// شروع شود.';
      }
    }
    if (body.hero_cta_url && !isValidUrl(body.hero_cta_url)) {
      errors.hero_cta_url = 'آدرس دکمه باید معتبر باشد.';
    }
    if (body.phone && !/^[\d+()\-\s]{4,40}$/.test(String(body.phone))) {
      errors.phone = 'شماره تلفن معتبر نیست.';
    }
    if (Object.keys(errors).length > 0) {
      return sendError(res, 400, 'لطفاً خطاهای فرم را برطرف کنید.', errors);
    }

    const info = settingsService.updateSchoolInfo(body);
    audit.log({ ...actor(req), action: 'school_info.update', entity: 'school_info', entityId: '1' });
    return sendOk(res, {
      info,
      missing: settingsService.missingPublicFields(info),
      message: 'اطلاعات مدرسه ذخیره شد.',
    });
  })
);

/** تنظیمات پیامک */
router.get('/settings/sms', requireRole(...ROLE_ADMIN_ONLY), (req, res) => {
  sendOk(res, {
    sms: settingsService.getSmsConfigForAdmin(),
    status: smsService.status(),
    providers: smsService.listProviders(),
    availableProviders: Object.keys(smsService.PROVIDERS),
  });
});

router.put(
  '/settings/sms',
  requireRole(...ROLE_ADMIN_ONLY),
  asyncHandler(async (req, res) => {
    const body = req.body || {};
    if (body.provider && !smsService.PROVIDERS[String(body.provider)]) {
      return sendError(res, 400, 'ارائه‌دهنده پیامک پشتیبانی نمی‌شود.', {
        provider: 'ارائه‌دهنده انتخاب‌شده معتبر نیست.',
      });
    }
    settingsService.saveSmsConfig(body);
    audit.log({
      ...actor(req),
      action: 'sms_settings.update',
      entity: 'sms_settings',
      details: { provider: body.provider, is_enabled: body.is_enabled },
    });
    return sendOk(res, {
      sms: settingsService.getSmsConfigForAdmin(),
      status: smsService.status(),
      message: 'تنظیمات پیامک ذخیره شد.',
    });
  })
);

router.post(
  '/settings/sms/test',
  requireRole(...ROLE_ADMIN_ONLY),
  asyncHandler(async (req, res) => {
    const result = await smsService.testConnection();
    settingsService.recordSmsTest(result.message);
    audit.log({
      ...actor(req),
      action: 'sms_settings.test',
      entity: 'sms_settings',
      details: { ok: result.ok },
    });
    return sendOk(res, {
      result,
      sms: settingsService.getSmsConfigForAdmin(),
      status: smsService.status(),
    });
  })
);

/** بارگذاری تصویر (فقط مدیر، فقط تصویر، نام فایل تولیدشده توسط سرور) */
const upload = multer({
  storage: multer.memoryStorage(),
  limits: { fileSize: config.uploads.maxBytes, files: 1 },
});

const IMAGE_SIGNATURES = [
  { ext: 'jpg', test: (buffer) => buffer[0] === 0xff && buffer[1] === 0xd8 && buffer[2] === 0xff },
  {
    ext: 'png',
    test: (buffer) =>
      buffer[0] === 0x89 && buffer[1] === 0x50 && buffer[2] === 0x4e && buffer[3] === 0x47,
  },
  {
    ext: 'webp',
    test: (buffer) =>
      buffer.length > 12 &&
      buffer.subarray(0, 4).toString('ascii') === 'RIFF' &&
      buffer.subarray(8, 12).toString('ascii') === 'WEBP',
  },
];

router.post(
  '/uploads',
  requireRole(...ROLE_ADMIN_ONLY),
  upload.single('file'),
  asyncHandler(async (req, res) => {
    if (!req.file || !req.file.buffer) return sendError(res, 400, 'فایلی ارسال نشده است.');

    const signature = IMAGE_SIGNATURES.find((item) => item.test(req.file.buffer));
    if (!signature) {
      return sendError(res, 400, 'فقط تصویر با فرمت JPG، PNG یا WEBP پذیرفته می‌شود.');
    }

    const fileName = `img-${Date.now().toString(36)}-${crypto.randomBytes(5).toString('hex')}.${signature.ext}`;
    fs.mkdirSync(config.uploadDir, { recursive: true });
    const target = path.join(config.uploadDir, fileName);
    fs.writeFileSync(target, req.file.buffer, { mode: 0o644 });

    audit.log({
      ...actor(req),
      action: 'upload.create',
      entity: 'uploads',
      entityId: fileName,
      details: { size: req.file.size },
    });

    return sendOk(res, {
      path: `/uploads/${fileName}`,
      size: req.file.size,
      message: 'تصویر بارگذاری شد.',
    });
  })
);

/** پیش‌ثبت‌نام‌ها */
router.get('/pre-registrations', requireRole(...ROLE_ADMIN_ONLY), (req, res) => {
  const db = getDb();
  const status = PREREG_STATUSES.includes(String(req.query.status)) ? String(req.query.status) : '';
  const query = String(req.query.q || '').trim();
  const params = [];
  const conditions = [];

  if (status) {
    conditions.push('status = ?');
    params.push(status);
  }
  if (query) {
    conditions.push('(student_name LIKE ? OR mobile LIKE ? OR parent_name LIKE ? OR tracking_code LIKE ?)');
    for (let index = 0; index < 4; index += 1) params.push(`%${query}%`);
  }
  const where = conditions.length > 0 ? ` WHERE ${conditions.join(' AND ')}` : '';

  const perPage = Math.min(Math.max(Number.parseInt(req.query.perPage, 10) || 20, 1), 100);
  const page = Math.max(Number.parseInt(req.query.page, 10) || 1, 1);
  const total = db.prepare(`SELECT COUNT(*) AS total FROM pre_registrations${where}`).get(...params).total;
  const items = db
    .prepare(
      `SELECT id, student_name, mobile, grade_title, field_title, parent_name, status, tracking_code,
              created_at, updated_at, reviewed_at
       FROM pre_registrations${where} ORDER BY id DESC LIMIT ? OFFSET ?`
    )
    .all(...params, perPage, (page - 1) * perPage);

  const counts = { new: 0, reviewing: 0, approved: 0, rejected: 0, archived: 0 };
  for (const row of db
    .prepare('SELECT status, COUNT(*) AS total FROM pre_registrations GROUP BY status')
    .all()) {
    counts[row.status] = Number(row.total);
  }

  sendOk(res, { items, total, page, perPage, pages: Math.max(Math.ceil(total / perPage), 1), counts });
});

router.get('/pre-registrations/export.csv', requireRole(...ROLE_ADMIN_ONLY), (req, res) => {
  const db = getDb();
  const rows = db
    .prepare(
      `SELECT tracking_code, student_name, mobile, grade_title, field_title, parent_name, note,
              status, admin_note, created_at, reviewed_at
       FROM pre_registrations ORDER BY id DESC`
    )
    .all();

  const headers = [
    'کد پیگیری',
    'نام دانش‌آموز',
    'موبایل',
    'پایه',
    'رشته',
    'نام ولی',
    'توضیحات',
    'وضعیت',
    'یادداشت مدیر',
    'زمان ثبت',
    'زمان بررسی',
  ];
  const escapeCell = (value) => `"${String(value === undefined || value === null ? '' : value).replace(/"/g, '""')}"`;
  const lines = [headers.map(escapeCell).join(',')];
  for (const row of rows) {
    lines.push(
      [
        row.tracking_code,
        row.student_name,
        row.mobile,
        row.grade_title,
        row.field_title,
        row.parent_name,
        row.note,
        row.status,
        row.admin_note,
        row.created_at,
        row.reviewed_at,
      ]
        .map(escapeCell)
        .join(',')
    );
  }

  audit.log({ ...actor(req), action: 'preregistration.export', entity: 'pre_registrations' });

  res.setHeader('content-type', 'text/csv; charset=utf-8');
  res.setHeader(
    'content-disposition',
    `attachment; filename="pre-registrations-${new Date().toISOString().slice(0, 10)}.csv"`
  );
  return res.send(`\uFEFF${lines.join('\r\n')}`);
});

router.get('/pre-registrations/:id', requireRole(...ROLE_ADMIN_ONLY), (req, res) => {
  const db = getDb();
  const id = Number.parseInt(req.params.id, 10);
  const item = db.prepare('SELECT * FROM pre_registrations WHERE id = ?').get(id);
  if (!item) return sendError(res, 404, 'این درخواست یافت نشد.');
  return sendOk(res, { item });
});

router.patch(
  '/pre-registrations/:id',
  requireRole(...ROLE_ADMIN_ONLY),
  asyncHandler(async (req, res) => {
    const db = getDb();
    const id = Number.parseInt(req.params.id, 10);
    const existing = db.prepare('SELECT * FROM pre_registrations WHERE id = ?').get(id);
    if (!existing) return sendError(res, 404, 'این درخواست یافت نشد.');

    const body = req.body || {};
    const errors = {};
    let status = existing.status;
    if (body.status !== undefined) {
      if (!PREREG_STATUSES.includes(String(body.status))) {
        errors.status = 'وضعیت انتخابی معتبر نیست.';
      } else {
        status = String(body.status);
      }
    }
    const adminNote =
      body.admin_note !== undefined
        ? cleanText(body.admin_note, { multiline: true, maxLength: 2000 })
        : existing.admin_note;

    if (Object.keys(errors).length > 0) {
      return sendError(res, 400, 'اطلاعات ارسالی معتبر نیست.', errors);
    }

    const reviewedAt = status !== 'new' ? new Date().toISOString() : existing.reviewed_at;
    db.prepare(
      `UPDATE pre_registrations
       SET status = ?, admin_note = ?, reviewed_at = ?, reviewed_by = ?,
           archived_at = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
       WHERE id = ?`
    ).run(
      status,
      adminNote,
      reviewedAt,
      status !== 'new' ? req.auth.admin.id : existing.reviewed_by,
      status === 'archived' ? new Date().toISOString() : existing.archived_at,
      id
    );

    audit.log({
      ...actor(req),
      action: 'preregistration.update',
      entity: 'pre_registrations',
      entityId: String(id),
      details: { status },
    });

    const item = db.prepare('SELECT * FROM pre_registrations WHERE id = ?').get(id);
    return sendOk(res, { item, message: 'وضعیت درخواست ذخیره شد.' });
  })
);

router.delete(
  '/pre-registrations/:id',
  requireRole('owner'),
  asyncHandler(async (req, res) => {
    const db = getDb();
    const id = Number.parseInt(req.params.id, 10);
    const existing = db.prepare('SELECT id FROM pre_registrations WHERE id = ?').get(id);
    if (!existing) return sendError(res, 404, 'این درخواست یافت نشد.');
    db.prepare('DELETE FROM pre_registrations WHERE id = ?').run(id);
    audit.log({
      ...actor(req),
      action: 'preregistration.delete',
      entity: 'pre_registrations',
      entityId: String(id),
    });
    return sendOk(res, { message: 'درخواست حذف شد.' });
  })
);

/** کاربران (ورود با موبایل) */
router.get('/users', requireRole(...ROLE_ADMIN_ONLY), (req, res) => {
  const db = getDb();
  const query = String(req.query.q || '').trim();
  const params = [];
  let where = '';
  if (query) {
    where = ' WHERE mobile LIKE ? OR full_name LIKE ?';
    params.push(`%${query}%`, `%${query}%`);
  }
  const items = db
    .prepare(
      `SELECT u.id, u.mobile, u.full_name, u.mobile_verified_at, u.last_login_at, u.is_blocked, u.created_at,
              (SELECT COUNT(*) FROM pre_registrations p WHERE p.mobile = u.mobile) AS requests_total
       FROM users u${where} ORDER BY u.id DESC LIMIT 200`
    )
    .all(...params);
  sendOk(res, { items });
});

router.patch(
  '/users/:id',
  requireRole(...ROLE_ADMIN_ONLY),
  asyncHandler(async (req, res) => {
    const db = getDb();
    const id = Number.parseInt(req.params.id, 10);
    const existing = db.prepare('SELECT id FROM users WHERE id = ?').get(id);
    if (!existing) return sendError(res, 404, 'این کاربر یافت نشد.');
    const blocked = (req.body || {}).is_blocked ? 1 : 0;
    db.prepare("UPDATE users SET is_blocked = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?").run(
      blocked,
      id
    );
    if (blocked) destroySessionsFor('user', id);
    audit.log({
      ...actor(req),
      action: 'user.update',
      entity: 'users',
      entityId: String(id),
      details: { is_blocked: blocked },
    });
    return sendOk(res, { message: blocked ? 'دسترسی کاربر مسدود شد.' : 'دسترسی کاربر باز شد.' });
  })
);

/** تغییر رمز عبور حساب جاری */
router.post(
  '/password',
  asyncHandler(async (req, res) => {
    const db = getDb();
    const { current_password: currentPassword, new_password: newPassword } = req.body || {};
    const admin = db.prepare('SELECT * FROM admins WHERE id = ?').get(req.auth.admin.id);
    if (!bcrypt.compareSync(String(currentPassword || ''), admin.password_hash)) {
      audit.log({ ...actor(req), action: 'admin.password_change_failed' });
      return sendError(res, 400, 'رمز عبور فعلی نادرست است.', { current_password: 'رمز عبور فعلی نادرست است.' });
    }
    const { strongEnoughPassword } = require('../utils/validate');
    if (!strongEnoughPassword(newPassword)) {
      return sendError(res, 400, 'رمز عبور جدید باید حداقل ۱۰ نویسه و شامل حرف و رقم باشد.', {
        new_password: 'رمز عبور جدید ضعیف است.',
      });
    }

    db.prepare(
      "UPDATE admins SET password_hash = ?, must_change_password = 0, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?"
    ).run(bcrypt.hashSync(String(newPassword), 12), admin.id);

    // سایر نشست‌ها باطل می‌شوند و فقط نشست جاری باقی می‌ماند.
    db.prepare('DELETE FROM sessions WHERE principal_type = ? AND principal_id = ? AND id != ?').run(
      'admin',
      admin.id,
      req.auth.sessionId
    );

    audit.log({ ...actor(req), action: 'admin.password_change', entity: 'admins', entityId: String(admin.id) });
    return sendOk(res, { message: 'رمز عبور تغییر کرد. سایر دستگاه‌ها از حساب خارج شدند.' });
  })
);

/** گزارش رویدادهای امنیتی */
router.get('/audit-logs', requireRole(...ROLE_ADMIN_ONLY), (req, res) => {
  sendOk(res, { items: audit.recent(req.query.limit) });
});

module.exports = router;
