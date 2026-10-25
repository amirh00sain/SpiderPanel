'use strict';

/**
 * کار با تاریخ‌ها.
 * - ذخیره‌سازی در دیتابیس همیشه به صورت ISO-8601 در UTC است.
 * - ورودی و نمایش تاریخ‌ها در پنل مدیریت بر اساس ساعت ایران (UTC+03:30) است.
 */

const TEHRAN_OFFSET_MINUTES = 210; // ساعت رسمی ایران از سال ۱۴۰۱ بدون تغییر ساعت تابستانی
const TEHRAN_OFFSET = '+03:30';

function nowIso() {
  return new Date().toISOString().replace(/(\.\d{3})Z$/, '$1Z');
}

function isoPlusSeconds(seconds) {
  return new Date(Date.now() + seconds * 1000).toISOString();
}

function isoPlusHours(hours) {
  return new Date(Date.now() + hours * 3600 * 1000).toISOString();
}

/** تبدیل ورودی datetime-local پنل مدیریت (ساعت ایران) به ISO در UTC */
function fromTehranInput(value) {
  const text = String(value || '').trim();
  if (!text) return null;
  // اگر مقدار از قبل شامل منطقه زمانی باشد، همان را تبدیل می‌کنیم.
  if (/(Z|[+-]\d{2}:\d{2})$/.test(text)) {
    const parsed = new Date(text);
    return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
  }
  const normalized = text.length === 16 ? `${text}:00` : text;
  const parsed = new Date(`${normalized}${TEHRAN_OFFSET}`);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

/** تبدیل ISO ذخیره‌شده در UTC به مقدار datetime-local بر اساس ساعت ایران */
function toTehranInput(isoValue) {
  if (!isoValue) return '';
  const parsed = new Date(isoValue);
  if (Number.isNaN(parsed.getTime())) return '';
  const tehran = new Date(parsed.getTime() + TEHRAN_OFFSET_MINUTES * 60 * 1000);
  return tehran.toISOString().slice(0, 16);
}

function isValidIso(value) {
  if (!value) return false;
  const parsed = new Date(value);
  return !Number.isNaN(parsed.getTime());
}

module.exports = {
  TEHRAN_OFFSET,
  nowIso,
  isoPlusSeconds,
  isoPlusHours,
  fromTehranInput,
  toTehranInput,
  isValidIso,
};
