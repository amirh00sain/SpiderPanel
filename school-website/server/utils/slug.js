'use strict';

const crypto = require('crypto');

/**
 * ساخت slug خوانا برای اخبار (حروف فارسی حفظ می‌شوند).
 */
function slugifyFa(text) {
  const base = String(text || '')
    .trim()
    .replace(/[\u0640]/g, '')
    .replace(/[\s\u200c]+/g, '-')
    .replace(/[^\p{L}\p{N}\-]/gu, '')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 80);
  return base || `item-${crypto.randomBytes(3).toString('hex')}`;
}

/** slug یکتا در یک جدول مشخص (نام جدول فقط از فهرست داخلی می‌آید) */
function uniqueSlug(db, table, title, ignoreId = null) {
  const base = slugifyFa(title);
  const statement = db.prepare(`SELECT id FROM ${table} WHERE slug = ?`);
  let candidate = base;
  let counter = 1;
  for (;;) {
    const row = statement.get(candidate);
    if (!row || (ignoreId && row.id === ignoreId)) return candidate;
    counter += 1;
    candidate = `${base}-${counter}`;
  }
}

module.exports = { slugifyFa, uniqueSlug };
