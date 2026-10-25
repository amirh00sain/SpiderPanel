'use strict';

/**
 * پیکربندی مرکزی برنامه.
 * همه مقادیر حساس (کلید نشست، کلید پیامک، رمز دیتابیس) فقط از متغیرهای محیطی
 * یا جدول تنظیمات سمت سرور خوانده می‌شوند و هرگز به مرورگر فرستاده نمی‌شوند.
 */

const path = require('path');
const crypto = require('crypto');

const dotenv = require('dotenv');

const projectRoot = path.resolve(__dirname, '..');

// فایل .env در ریشه پروژه (اجرای محلی و هاست cPanel)
dotenv.config({ path: path.join(projectRoot, '.env') });

function toBool(value, fallback) {
  if (value === undefined || value === null || value === '') return fallback;
  return ['1', 'true', 'yes', 'on'].includes(String(value).trim().toLowerCase());
}

function toInt(value, fallback) {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

const env = process.env.NODE_ENV || 'development';
const isProduction = env === 'production';
// در حالت پیش‌نمایش سندباکس، مقدار دقیقاً "1" است. هیچ رفتار دیگری تغییر نمی‌کند.
const isPreviewMode = process.env.BASE44_PREVIEW_MODE === '1';

const publicDir = path.join(projectRoot, 'public');
const storageDir = path.resolve(process.env.STORAGE_DIR || path.join(projectRoot, 'storage'));
// تصاویر سایت (گالری، کارکنان، هدر) ذاتاً عمومی هستند و از /uploads سرو می‌شوند.
const uploadDir = path.resolve(process.env.UPLOAD_DIR || path.join(publicDir, 'uploads'));
// دیتابیس و پشتیبان‌ها بیرون از پوشه عمومی نگهداری می‌شوند.
const dbPath = path.resolve(process.env.DB_PATH || path.join(storageDir, 'school.db'));
const backupDir = path.resolve(process.env.BACKUP_DIR || path.join(storageDir, 'backups'));

let sessionSecret = String(process.env.SESSION_SECRET || '').trim();
let sessionSecretGenerated = false;
if (sessionSecret.length < 16) {
  if (isProduction) {
    throw new Error(
      'SESSION_SECRET تعریف نشده یا کوتاه است. برای اجرای عملیاتی یک مقدار تصادفی و بلند در .env تنظیم کنید.'
    );
  }
  sessionSecret = crypto.randomBytes(32).toString('hex');
  sessionSecretGenerated = true;
}

const config = {
  projectRoot,
  publicDir,
  storageDir,
  uploadDir,
  dbPath,
  backupDir,

  env,
  isProduction,
  isPreviewMode,
  isTest: env === 'test',

  host: process.env.HOST || '0.0.0.0',
  port: toInt(process.env.PORT, 3000),
  trustProxy: toBool(process.env.TRUST_PROXY, true),
  sessionSecret,
  sessionSecretGenerated,

  cookie: {
    sessionName: process.env.SESSION_COOKIE_NAME || 'sw_session',
    csrfName: process.env.CSRF_COOKIE_NAME || 'sw_csrf',
    secure: toBool(process.env.COOKIE_SECURE, isProduction),
    sameSite: process.env.COOKIE_SAMESITE || 'lax',
  },
  sessionTtlMs: toInt(process.env.SESSION_TTL_HOURS, 12) * 60 * 60 * 1000,

  login: {
    maxAttempts: toInt(process.env.LOGIN_MAX_ATTEMPTS, 5),
    windowMs: toInt(process.env.LOGIN_WINDOW_MINUTES, 15) * 60 * 1000,
    lockMs: toInt(process.env.LOGIN_LOCK_MINUTES, 15) * 60 * 1000,
  },

  otp: {
    length: toInt(process.env.OTP_LENGTH, 6),
    ttlSeconds: toInt(process.env.OTP_TTL_SECONDS, 120),
    maxAttempts: toInt(process.env.OTP_MAX_ATTEMPTS, 5),
    resendSeconds: toInt(process.env.OTP_RESEND_SECONDS, 60),
    maxPerMobilePerHour: toInt(process.env.OTP_MAX_PER_MOBILE_HOUR, 5),
    maxPerIpHour: toInt(process.env.OTP_MAX_PER_IP_HOUR, 20),
  },

  preRegistration: {
    maxPerIpHour: toInt(process.env.PREREG_MAX_PER_IP_HOUR, 5),
    duplicateWindowHours: toInt(process.env.PREREG_DUPLICATE_WINDOW_HOURS, 24),
  },

  uploads: {
    maxBytes: toInt(process.env.MAX_UPLOAD_MB, 3) * 1024 * 1024,
    allowedExtensions: ['jpg', 'jpeg', 'png', 'webp'],
  },

  sms: {
    // اگر در متغیرهای محیطی تنظیم نشده باشد، از جدول sms_settings خوانده می‌شود.
    provider: (process.env.SMS_PROVIDER || '').trim(),
    apiKey: (process.env.SMS_API_KEY || '').trim(),
    username: (process.env.SMS_USERNAME || '').trim(),
    sender: (process.env.SMS_SENDER || '').trim(),
    otpTemplate: (process.env.SMS_OTP_TEMPLATE || '').trim(),
  },

  setup: {
    // مسیر نصب اولیه فقط با این توکن کار می‌کند و پس از ساخت اولین مدیر بسته می‌شود.
    token: (process.env.ADMIN_SETUP_TOKEN || '').trim(),
    disabled: toBool(process.env.SETUP_DISABLED, false),
  },

  rateLimits: {
    apiPerMinute: toInt(process.env.RATE_LIMIT_API_PER_MINUTE, 300),
  },
};

module.exports = config;
