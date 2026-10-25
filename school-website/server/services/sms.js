'use strict';

/**
 * سرویس پیامک ماژولار.
 * هر ارائه‌دهنده در PROVIDERS تعریف شده است؛ افزودن ارائه‌دهنده جدید فقط با
 * اضافه کردن یک شیء در همین فایل انجام می‌شود.
 *
 * نکته امنیتی: کلید API فقط سمت سرور نگهداری می‌شود، در پاسخ‌های API به صورت
 * ماسک‌شده برمی‌گردد و هرگز در HTML/JS مرورگر قرار نمی‌گیرد.
 */

const config = require('../config');
const logger = require('../utils/logger');
const settingsService = require('./settings');

const REQUEST_TIMEOUT_MS = 15000;

// پیام‌های ارسال‌شده در حالت توسعه (فقط برای تست خودکار؛ از هیچ API برگردانده نمی‌شود)
const outbox = [];

async function postJson(url, body, headers = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json', ...headers },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    const text = await response.text();
    let json = null;
    try {
      json = JSON.parse(text);
    } catch {
      json = null;
    }
    return { status: response.status, ok: response.ok, text, json };
  } finally {
    clearTimeout(timer);
  }
}

async function getJson(url, headers = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(url, { method: 'GET', headers, signal: controller.signal });
    const text = await response.text();
    let json = null;
    try {
      json = JSON.parse(text);
    } catch {
      json = null;
    }
    return { status: response.status, ok: response.ok, text, json };
  } finally {
    clearTimeout(timer);
  }
}

const PROVIDERS = {
  kavenegar: {
    label: 'کاوه‌نگار',
    requires: ['apiKey'],
    fields: ['apiKey', 'sender'],
    async send(sms, { to, message }) {
      const url = `https://api.kavenegar.com/v1/${encodeURIComponent(sms.apiKey)}/sms/send.json`;
      const form = new URLSearchParams({ receptor: to, message });
      if (sms.sender) form.set('sender', sms.sender);
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'content-type': 'application/x-www-form-urlencoded' },
        body: form,
      });
      const json = await response.json().catch(() => null);
      const status = json && json.return ? Number(json.return.status) : response.status;
      if (status === 200) return { ok: true };
      return { ok: false, error: (json && json.return && json.return.message) || `کد پاسخ ${response.status}` };
    },
    async test(sms) {
      const url = `https://api.kavenegar.com/v1/${encodeURIComponent(sms.apiKey)}/account/info.json`;
      const { json, status } = await getJson(url);
      const returnStatus = json && json.return ? Number(json.return.status) : status;
      if (returnStatus === 200) {
        return { ok: true, message: `اتصال به کاوه‌نگار برقرار شد. ${json.return.message || ''}`.trim() };
      }
      return { ok: false, message: (json && json.return && json.return.message) || `کد پاسخ ${status}` };
    },
  },

  melipayamak: {
    label: 'ملی‌پیامک',
    requires: ['username', 'apiKey'],
    fields: ['username', 'apiKey', 'sender'],
    async send(sms, { to, message }) {
      const { json, status } = await postJson('https://rest.payamak-panel.com/api/SendSMS/SendSMS', {
        username: sms.username,
        password: sms.apiKey,
        to,
        from: sms.sender,
        text: message,
        isflash: false,
      });
      const retStatus = json ? Number(json.RetStatus) : 0;
      if (retStatus === 1) return { ok: true };
      return { ok: false, error: (json && json.StrRetStatus) || `کد پاسخ ${status}` };
    },
    async test(sms) {
      const { json, status } = await postJson('https://rest.payamak-panel.com/api/SendSMS/GetCredit', {
        username: sms.username,
        password: sms.apiKey,
      });
      const retStatus = json ? Number(json.RetStatus) : 0;
      if (retStatus === 1) {
        return { ok: true, message: `اتصال برقرار شد. اعتبار حساب: ${json.Value}` };
      }
      return { ok: false, message: (json && json.StrRetStatus) || `کد پاسخ ${status}` };
    },
  },

  ippanel: {
    label: 'آی‌پی‌پنل',
    requires: ['apiKey'],
    fields: ['apiKey', 'sender'],
    async send(sms, { to, message }) {
      const { json, status } = await postJson(
        'https://edge.ippanel.com/v1/api/send',
        {
          sending_type: 'webservice',
          from_number: sms.sender,
          recipients: [to],
          message,
        },
        { Authorization: sms.apiKey }
      );
      if (json && (json.status === 'ok' || json.code === 200 || json.data)) return { ok: true };
      return { ok: false, error: (json && (json.message || json.error)) || `کد پاسخ ${status}` };
    },
    async test(sms) {
      const { json, status } = await getJson('https://edge.ippanel.com/v1/api/credit', {
        Authorization: sms.apiKey,
      });
      if (json && json.data && typeof json.data.credit !== 'undefined') {
        return { ok: true, message: `اتصال برقرار شد. اعتبار حساب: ${json.data.credit}` };
      }
      return { ok: false, message: (json && (json.message || json.error)) || `کد پاسخ ${status}` };
    },
  },

  log: {
    label: 'حالت توسعه (بدون ارسال واقعی)',
    requires: [],
    fields: [],
    async send(sms, { to, message }) {
      outbox.push({ to, message, at: new Date().toISOString() });
      if (outbox.length > 50) outbox.shift();
      // فقط برای اجرای محلی: کد در لاگ سرور دیده می‌شود و پیامکی ارسال نمی‌شود.
      logger.warn(`[SMS:dev] پیام شبیه‌سازی‌شده برای ${to}: ${message}`);
      return { ok: true, simulated: true };
    },
    async test() {
      return { ok: true, message: 'ارائه‌دهنده در حالت توسعه است و پیامک واقعی ارسال نمی‌شود.' };
    },
  },
};

function listProviders() {
  return Object.entries(PROVIDERS).map(([key, provider]) => ({
    key,
    label: provider.label,
    requiredFields: provider.requires,
  }));
}

function resolveConfig() {
  const sms = settingsService.getSmsConfig();
  const provider = PROVIDERS[sms.provider] || null;
  const missingFields = provider
    ? provider.requires.filter((field) => !String(sms[field] || '').trim())
    : ['provider'];
  return { sms, provider, missingFields };
}

function status() {
  const { sms, provider, missingFields } = resolveConfig();
  if (!sms.provider) {
    return {
      configured: false,
      enabled: false,
      reason: 'not_configured',
      message: 'ارائه‌دهنده پیامک انتخاب نشده است؛ ورود با پیامک تا زمان تنظیم اعتبارنامه کار نمی‌کند.',
    };
  }
  if (!provider) {
    return {
      configured: false,
      enabled: false,
      reason: 'unknown_provider',
      message: `ارائه‌دهنده «${sms.provider}» پشتیبانی نمی‌شود.`,
    };
  }
  if (missingFields.length > 0) {
    return {
      configured: false,
      enabled: false,
      reason: 'missing_credentials',
      message: `اطلاعات ناقص برای ${provider.label}: ${missingFields.join('، ')}`,
    };
  }
  if (!sms.isEnabled) {
    return {
      configured: true,
      enabled: false,
      reason: 'disabled',
      message: 'ارسال پیامک در تنظیمات غیرفعال است.',
    };
  }
  return { configured: true, enabled: true, reason: 'ok', message: `${provider.label} آماده ارسال است.` };
}

async function sendMessage({ to, message }) {
  const { sms, provider, missingFields } = resolveConfig();
  const current = status();
  if (!current.configured || !current.enabled) {
    return { ok: false, reason: current.reason, error: current.message };
  }
  if (missingFields.length > 0) {
    return { ok: false, reason: 'missing_credentials', error: current.message };
  }

  try {
    const result = await provider.send(sms, { to, message });
    if (result.ok) return { ok: true, simulated: Boolean(result.simulated), provider: sms.provider };
    return { ok: false, reason: 'provider_error', error: result.error || 'ارسال پیامک ناموفق بود.' };
  } catch (error) {
    logger.warn(`ارسال پیامک ناموفق: ${error.message}`);
    return { ok: false, reason: 'network_error', error: 'ارتباط با سرویس پیامک برقرار نشد.' };
  }
}

async function testConnection() {
  const { sms, provider, missingFields } = resolveConfig();
  if (!sms.provider) {
    return { ok: false, message: 'ابتدا ارائه‌دهنده پیامک را انتخاب و ذخیره کنید.' };
  }
  if (!provider) {
    return { ok: false, message: `ارائه‌دهنده «${sms.provider}» پشتیبانی نمی‌شود.` };
  }
  if (missingFields.length > 0) {
    return { ok: false, message: `اطلاعات ناقص: ${missingFields.join('، ')}` };
  }
  try {
    const result = await provider.test(sms);
    return { ok: Boolean(result.ok), message: result.message || '' };
  } catch (error) {
    return { ok: false, message: `آزمایش اتصال ناموفق بود: ${error.message}` };
  }
}

async function sendOtp({ to, code }) {
  const { sms } = resolveConfig();
  const template = sms.otpTemplate || 'کد ورود شما به سایت مدرسه: {code}';
  const message = template.replace('{code}', String(code));
  const result = await sendMessage({ to, message });
  return result;
}

module.exports = {
  PROVIDERS,
  listProviders,
  status,
  resolveConfig,
  sendMessage,
  sendOtp,
  testConnection,
  outbox,
};
