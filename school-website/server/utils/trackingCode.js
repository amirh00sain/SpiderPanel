'use strict';

const crypto = require('crypto');

// الفبای بدون نویسه‌های مشابه‌ساز (0/O و 1/I)
const ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';

/** تولید کد پیگیری غیرقابل حدس برای درخواست پیش‌ثبت‌نام */
function generateTrackingCode() {
  const bytes = crypto.randomBytes(10);
  let body = '';
  for (const byte of bytes) {
    body += ALPHABET[byte % ALPHABET.length];
  }
  return `SW-${body.slice(0, 5)}-${body.slice(5, 10)}`;
}

function normalizeTrackingCode(value) {
  return String(value || '')
    .trim()
    .toUpperCase()
    .replace(/[^A-Z0-9]/g, '');
}

function trackingCodesMatch(candidate, stored) {
  const a = normalizeTrackingCode(candidate);
  const b = normalizeTrackingCode(stored);
  if (a.length === 0 || a.length !== b.length) return false;
  return crypto.timingSafeEqual(Buffer.from(a), Buffer.from(b));
}

module.exports = { generateTrackingCode, normalizeTrackingCode, trackingCodesMatch };
