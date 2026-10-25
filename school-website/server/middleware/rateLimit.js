'use strict';

/**
 * محدودیت نرخ درخواست در حافظه سرور.
 * برای هر مسیر حساس (ورود مدیر، ورود با پیامک، فرم پیش‌ثبت‌نام) یک محدودکننده
 * جداگانه ساخته می‌شود؛ کلید می‌تواند آی‌پی یا مقدار دیگری مانند شماره موبایل باشد.
 */

const { clientIp, sendError } = require('../utils/http');

const stores = [];

// پاک‌سازی دوره‌ای رکوردهای منقضی
const cleanupTimer = setInterval(() => {
  const now = Date.now();
  for (const store of stores) {
    for (const [key, entry] of store.entries()) {
      if (!entry.blockedUntil && now - entry.start > 60 * 60 * 1000) store.delete(key);
      else if (entry.blockedUntil && entry.blockedUntil < now && now - entry.start > 60 * 60 * 1000) store.delete(key);
    }
  }
}, 10 * 60 * 1000);
if (cleanupTimer.unref) cleanupTimer.unref();

function rateLimit(options) {
  const {
    name,
    windowMs = 60 * 1000,
    max = 60,
    blockMs = 0,
    message = 'تعداد درخواست‌های شما بیش از حد مجاز است. لطفاً کمی بعد تلاش کنید.',
    keyGenerator = (req) => clientIp(req),
  } = options;

  const store = new Map();
  stores.push(store);

  return function limiter(req, res, next) {
    const key = `${name}:${keyGenerator(req) || 'unknown'}`;
    const now = Date.now();
    let entry = store.get(key);

    if (!entry || now - entry.start >= windowMs) {
      entry = { start: now, count: 0, blockedUntil: 0 };
      store.set(key, entry);
    }

    if (entry.blockedUntil && entry.blockedUntil > now) {
      res.set('Retry-After', String(Math.ceil((entry.blockedUntil - now) / 1000)));
      return sendError(res, 429, message);
    }

    entry.count += 1;
    if (entry.count > max) {
      if (blockMs > 0) entry.blockedUntil = now + blockMs;
      res.set('Retry-After', String(Math.ceil((blockMs || windowMs) / 1000)));
      return sendError(res, 429, message);
    }
    return next();
  };
}

module.exports = { rateLimit };
