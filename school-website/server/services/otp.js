'use strict';

/**
 * ورود با شماره موبایل و کد یک‌بارمصرف.
 * - کد هرگز به صورت خام ذخیره نمی‌شود (فقط HMAC-SHA256 با کلید سرور).
 * - اعتبار زمانی، تعداد تلاش و فاصله ارسال مجدد کنترل می‌شود.
 * - محدودیت درخواست بر اساس شماره موبایل و آی‌پی اعمال می‌گردد.
 */

const crypto = require('crypto');

const config = require('../config');
const { getDb } = require('../database/db');
const { HttpError } = require('../utils/http');
const { normalizeMobile, isValidMobile } = require('../utils/validate');
const smsService = require('./sms');

function hashCode(mobile, code) {
  return crypto.createHmac('sha256', config.sessionSecret).update(`${mobile}:${code}`).digest('hex');
}

function generateCode() {
  const max = 10 ** config.otp.length;
  const value = crypto.randomInt(0, max);
  return String(value).padStart(config.otp.length, '0');
}

function safeEqualHex(a, b) {
  const left = Buffer.from(String(a), 'utf8');
  const right = Buffer.from(String(b), 'utf8');
  if (left.length !== right.length) return false;
  return crypto.timingSafeEqual(left, right);
}

async function requestOtp({ mobile: rawMobile, ip = '', purpose = 'login' }) {
  const mobile = normalizeMobile(rawMobile);
  if (!isValidMobile(mobile)) throw new HttpError(400, 'شماره موبایل معتبر نیست. نمونه صحیح: ۰۹۱۲۳۴۵۶۷۸۹');

  const db = getDb();
  const hourAgo = new Date(Date.now() - 60 * 60 * 1000).toISOString();

  const perMobile = db
    .prepare('SELECT COUNT(*) AS total FROM otp_requests WHERE mobile = ? AND created_at > ?')
    .get(mobile, hourAgo).total;
  if (perMobile >= config.otp.maxPerMobilePerHour) {
    throw new HttpError(429, 'تعداد درخواست کد برای این شماره بیش از حد مجاز است. کمی بعد تلاش کنید.');
  }

  const perIp = db
    .prepare('SELECT COUNT(*) AS total FROM otp_requests WHERE ip = ? AND created_at > ?')
    .get(String(ip || ''), hourAgo).total;
  if (perIp >= config.otp.maxPerIpHour) {
    throw new HttpError(429, 'تعداد درخواست کد از این اتصال بیش از حد مجاز است. کمی بعد تلاش کنید.');
  }

  const last = db
    .prepare('SELECT created_at FROM otp_requests WHERE mobile = ? ORDER BY id DESC LIMIT 1')
    .get(mobile);
  if (last) {
    const elapsed = (Date.now() - new Date(last.created_at).getTime()) / 1000;
    if (elapsed < config.otp.resendSeconds) {
      const wait = Math.ceil(config.otp.resendSeconds - elapsed);
      throw new HttpError(429, `برای دریافت کد جدید ${wait} ثانیه صبر کنید.`);
    }
  }

  const status = smsService.status();
  if (!status.configured || !status.enabled) {
    // بدون سرویس پیامک فعال، کدی ساخته نمی‌شود تا هیچ ادعای نادرستی درباره ارسال نداشته باشیم.
    throw new HttpError(503, `ارسال پیامک در این سایت فعال نیست. ${status.message}`);
  }

  const code = generateCode();
  const expiresAt = new Date(Date.now() + config.otp.ttlSeconds * 1000).toISOString();
  const info = db
    .prepare(
      `INSERT INTO otp_requests (mobile, purpose, code_hash, max_attempts, ip, expires_at)
       VALUES (?, ?, ?, ?, ?, ?)`
    )
    .run(mobile, purpose, hashCode(mobile, code), config.otp.maxAttempts, String(ip || '').slice(0, 60), expiresAt);

  const sendResult = await smsService.sendOtp({ to: mobile, code });
  if (!sendResult.ok) {
    db.prepare('DELETE FROM otp_requests WHERE id = ?').run(info.lastInsertRowid);
    throw new HttpError(502, `ارسال پیامک انجام نشد. ${sendResult.error || ''}`.trim());
  }

  return {
    mobile,
    expiresInSeconds: config.otp.ttlSeconds,
    simulated: Boolean(sendResult.simulated),
    message: sendResult.simulated
      ? 'سرویس پیامک در حالت توسعه است؛ کد در لاگ سرور ثبت شد و پیامک واقعی ارسال نشد.'
      : 'کد ورود پیامک شد.',
  };
}

function verifyOtp({ mobile: rawMobile, code, purpose = 'login' }) {
  const mobile = normalizeMobile(rawMobile);
  if (!isValidMobile(mobile)) throw new HttpError(400, 'شماره موبایل معتبر نیست.');
  const cleanCode = String(code || '').replace(/[۰-۹]/g, (d) => String('۰۱۲۳۴۵۶۷۸۹'.indexOf(d))).trim();
  if (!/^\d{4,8}$/.test(cleanCode)) throw new HttpError(400, 'کد وارد شده معتبر نیست.');

  const db = getDb();
  const row = db
    .prepare(
      `SELECT * FROM otp_requests
       WHERE mobile = ? AND purpose = ? AND consumed_at IS NULL
       ORDER BY id DESC LIMIT 1`
    )
    .get(mobile, purpose);

  if (!row) throw new HttpError(400, 'کد فعالی برای این شماره وجود ندارد. دوباره درخواست کد کنید.');

  if (new Date(row.expires_at).getTime() <= Date.now()) {
    db.prepare('UPDATE otp_requests SET consumed_at = ? WHERE id = ?').run(new Date().toISOString(), row.id);
    throw new HttpError(400, 'کد منقضی شده است. کد جدید درخواست کنید.');
  }

  if (row.attempts >= row.max_attempts) {
    db.prepare('UPDATE otp_requests SET consumed_at = ? WHERE id = ?').run(new Date().toISOString(), row.id);
    throw new HttpError(429, 'تعداد تلاش‌های مجاز این کد تمام شده است. کد جدید درخواست کنید.');
  }

  db.prepare('UPDATE otp_requests SET attempts = attempts + 1 WHERE id = ?').run(row.id);

  if (!safeEqualHex(hashCode(mobile, cleanCode), row.code_hash)) {
    const remaining = Math.max(row.max_attempts - row.attempts - 1, 0);
    throw new HttpError(400, `کد وارد شده صحیح نیست. تلاش‌های باقی‌مانده: ${remaining}`);
  }

  db.prepare('UPDATE otp_requests SET consumed_at = ? WHERE id = ?').run(
    new Date().toISOString(),
    row.id
  );

  return { mobile };
}

function purgeOldRequests() {
  const cutoff = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
  getDb().prepare('DELETE FROM otp_requests WHERE created_at < ?').run(cutoff);
}

module.exports = { requestOtp, verifyOtp, purgeOldRequests, generateCode, hashCode };
