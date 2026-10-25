'use strict';

/**
 * اعتبارسنجی و پاک‌سازی ورودی‌ها در سمت سرور (مستقل از اعتبارسنجی مرورگر).
 */

const MOBILE_RE = /^09\d{9}$/;
const USERNAME_RE = /^[A-Za-z0-9._-]{3,60}$/;
const EMAIL_RE = /^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$/;

function isPlainObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

/** حذف نویسه‌های کنترلی و فاصله‌های اضافی */
function cleanText(value, { multiline = false, maxLength = 20000 } = {}) {
  if (value === undefined || value === null) return '';
  let text = String(value);
  // حذف نویسه‌های کنترلی به جز newline در متن‌های چندخطی
  text = multiline
    ? text.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, '')
    : text.replace(/[\u0000-\u001F\u007F]/g, ' ');

  if (multiline) {
    text = text
      .split('\n')
      .map((line) => line.replace(/[ \t]+/g, ' ').trim())
      .join('\n')
      .replace(/\n{3,}/g, '\n\n');
  } else {
    text = text.replace(/\s+/g, ' ');
  }
  text = text.trim();
  if (text.length > maxLength) text = text.slice(0, maxLength);
  return text;
}

function normalizeMobile(value) {
  let digits = String(value || '').replace(/[\s\-()]/g, '');
  // تبدیل ارقام فارسی/عربی به لاتین
  digits = digits.replace(/[۰-۹]/g, (d) => String('۰۱۲۳۴۵۶۷۸۹'.indexOf(d)));
  digits = digits.replace(/[٠-٩]/g, (d) => String('٠١٢٣٤٥٦٧٨٩'.indexOf(d)));
  if (digits.startsWith('+98')) digits = `0${digits.slice(3)}`;
  else if (digits.startsWith('0098')) digits = `0${digits.slice(4)}`;
  else if (digits.startsWith('98') && digits.length === 12) digits = `0${digits.slice(2)}`;
  return digits;
}

function isValidMobile(value) {
  return MOBILE_RE.test(normalizeMobile(value));
}

function isValidEmail(value) {
  return EMAIL_RE.test(String(value || '').trim());
}

function isValidUsername(value) {
  return USERNAME_RE.test(String(value || '').trim());
}

/** فقط آدرس‌های http/https یا مسیرهای داخلی مجاز هستند (جلوگیری از javascript:) */
function isValidUrl(value) {
  const text = String(value || '').trim();
  if (!text) return false;
  if (text.startsWith('/') && !text.startsWith('//')) return true;
  try {
    const parsed = new URL(text);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:';
  } catch {
    return false;
  }
}

function strongEnoughPassword(value) {
  const text = String(value || '');
  if (text.length < 10) return false;
  if (!/[A-Za-z]/.test(text)) return false;
  if (!/[0-9]/.test(text)) return false;
  return true;
}

/**
 * اعتبارسنجی یک مقدار بر اساس تعریف فیلد.
 * خروجی: { value, error }
 */
function validateField(field, rawValue, options = {}) {
  const { partial = false, required = field.required } = options;
  const empty =
    rawValue === undefined ||
    rawValue === null ||
    (typeof rawValue === 'string' && rawValue.trim() === '');

  if (field.type === 'boolean') {
    if (empty) return { value: partial ? undefined : field.default ? 1 : 0 };
    const truthy = [1, '1', true, 'true', 'on', 'yes'];
    return { value: truthy.includes(rawValue) ? 1 : 0 };
  }

  if (empty) {
    if (required && !partial) return { error: `${field.label} الزامی است.` };
    if (partial) return { value: undefined };
    return { value: field.nullable ? null : '' };
  }

  switch (field.type) {
    case 'number': {
      const num = Number.parseInt(rawValue, 10);
      if (!Number.isFinite(num)) return { error: `${field.label} باید عدد باشد.` };
      if (field.min !== undefined && num < field.min) return { error: `${field.label} کوچک‌تر از حد مجاز است.` };
      if (field.max !== undefined && num > field.max) return { error: `${field.label} بزرگ‌تر از حد مجاز است.` };
      return { value: num };
    }
    case 'email': {
      const value = cleanText(rawValue, { maxLength: field.maxLength || 160 });
      if (!isValidEmail(value)) return { error: `${field.label} معتبر نیست.` };
      return { value };
    }
    case 'tel': {
      const value = cleanText(rawValue, { maxLength: field.maxLength || 40 });
      if (!/^[\d+()\-\s]{4,40}$/.test(value)) return { error: `${field.label} معتبر نیست.` };
      return { value };
    }
    case 'mobile': {
      const value = normalizeMobile(rawValue);
      if (!isValidMobile(value)) return { error: `${field.label} باید یک شماره موبایل معتبر ایران باشد.` };
      return { value };
    }
    case 'color': {
      const value = cleanText(rawValue, { maxLength: 20 });
      if (value && !/^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(value)) {
        return { error: `${field.label} باید یک رنگ مانند #245e91 باشد.` };
      }
      return { value };
    }
    case 'datetime': {
      const value = cleanText(rawValue, { maxLength: 40 });
      return { value };
    }
    case 'password': {
      const value = String(rawValue);
      if (!strongEnoughPassword(value)) {
        return { error: `${field.label} باید حداقل ۱۰ نویسه و شامل حرف و رقم باشد.` };
      }
      return { value };
    }
    case 'textarea': {
      const maxLength = field.maxLength || 20000;
      const value = cleanText(rawValue, { multiline: true, maxLength: maxLength + 200 });
      if (value.length > maxLength) return { error: `${field.label} بیش از حد مجاز طولانی است.` };
      if (field.required && value.length < 2) return { error: `${field.label} الزامی است.` };
      return { value };
    }
    default: {
      const maxLength = field.maxLength || 300;
      const value = cleanText(rawValue, { maxLength: maxLength + 200 });
      if (value.length > maxLength) return { error: `${field.label} بیش از حد مجاز طولانی است.` };
      if (field.pattern === 'username' && !isValidUsername(value)) {
        return { error: `${field.label} باید بین ۳ تا ۶۰ نویسه لاتین، رقم، نقطه، خط تیره یا زیرخط باشد.` };
      }
      if (field.pattern === 'url' && value && !isValidUrl(value)) {
        return { error: `${field.label} باید یک آدرس http/https معتبر باشد.` };
      }
      if (field.pattern === 'image' && value && !isValidUrl(value)) {
        return { error: `${field.label} باید مسیر تصویر معتبر باشد.` };
      }
      if (field.required && value.length < 2) return { error: `${field.label} الزامی است.` };
      return { value };
    }
  }
}

/**
 * اعتبارسنجی کل بدنه یک درخواست بر اساس تعریف موجودیت.
 * خروجی: { values, errors }
 */
function validateEntityPayload(entity, body, { partial = false } = {}) {
  const values = {};
  const errors = {};
  const payload = isPlainObject(body) ? body : {};

  for (const field of entity.fields) {
    const hasKey = Object.prototype.hasOwnProperty.call(payload, field.name);
    if (partial && !hasKey) continue;
    const result = validateField(field, payload[field.name], { partial });
    if (result.error) errors[field.name] = result.error;
    else if (result.value !== undefined) values[field.name] = result.value;
  }

  return { values, errors };
}

module.exports = {
  isPlainObject,
  cleanText,
  normalizeMobile,
  isValidMobile,
  isValidEmail,
  isValidUsername,
  isValidUrl,
  strongEnoughPassword,
  validateField,
  validateEntityPayload,
};
