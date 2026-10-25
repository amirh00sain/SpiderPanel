'use strict';

/**
 * ابزار کمکی تست‌ها: ساخت یک نمونه برنامه با دیتابیس موقت و کلاینت HTTP.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');

function createTestEnvironment(label) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), `school-test-${label}-`));
  process.env.NODE_ENV = 'test';
  process.env.DB_PATH = path.join(dir, 'test.db');
  process.env.STORAGE_DIR = dir;
  process.env.UPLOAD_DIR = path.join(dir, 'uploads');
  process.env.BACKUP_DIR = path.join(dir, 'backups');
  process.env.SESSION_SECRET = 'test-secret-key-for-automated-tests-1234567890';
  process.env.ADMIN_SETUP_TOKEN = 'test-setup-token';
  process.env.SMS_PROVIDER = 'log';
  process.env.SMS_API_KEY = '';
  process.env.COOKIE_SECURE = '0';
  process.env.LOG_LEVEL = 'error';
  process.env.RATE_LIMIT_API_PER_MINUTE = '10000';
  process.env.OTP_RESEND_SECONDS = '0';
  process.env.OTP_MAX_PER_MOBILE_HOUR = '3';
  process.env.PREREG_MAX_PER_IP_HOUR = '1000';
  return dir;
}

/** کلاینت ساده با نگهداری کوکی‌ها و توکن CSRF */
function createClient(baseUrl) {
  const cookies = new Map();

  function cookieHeader() {
    return [...cookies.entries()].map(([key, value]) => `${key}=${value}`).join('; ');
  }

  function storeCookies(response) {
    const raw = response.headers.getSetCookie ? response.headers.getSetCookie() : [];
    for (const line of raw) {
      const [pair] = line.split(';');
      const index = pair.indexOf('=');
      if (index === -1) continue;
      cookies.set(pair.slice(0, index).trim(), pair.slice(index + 1).trim());
    }
  }

  async function call(method, url, body) {
    const headers = { accept: 'application/json' };
    if (body !== undefined) headers['content-type'] = 'application/json';
    if (method !== 'GET') {
      // ابتدا توکن CSRF گرفته می‌شود تا کوکی آن ذخیره شود، سپس درخواست اصلی ارسال می‌گردد.
      const { csrfToken } = await getCsrf();
      headers['x-csrf-token'] = csrfToken;
    }
    if (cookies.size > 0) headers.cookie = cookieHeader();
    const response = await fetch(`${baseUrl}${url}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      redirect: 'manual',
    });
    storeCookies(response);
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = { raw: text };
    }
    return { status: response.status, data, headers: response.headers };
  }

  async function getCsrf() {
    const response = await fetch(`${baseUrl}/api/auth/session`, {
      headers: { accept: 'application/json', ...(cookies.size > 0 ? { cookie: cookieHeader() } : {}) },
    });
    storeCookies(response);
    const data = await response.json().catch(() => ({}));
    if (!data.csrfToken) {
      const fallback = await fetch(`${baseUrl}/api/csrf`, {
        headers: { accept: 'application/json', ...(cookies.size > 0 ? { cookie: cookieHeader() } : {}) },
      });
      storeCookies(fallback);
      const fallbackData = await fallback.json().catch(() => ({}));
      return { csrfToken: fallbackData.csrfToken || '' };
    }
    return { csrfToken: data.csrfToken };
  }

  return {
    get: (url) => call('GET', url),
    post: (url, body) => call('POST', url, body),
    put: (url, body) => call('PUT', url, body),
    patch: (url, body) => call('PATCH', url, body),
    del: (url) => call('DELETE', url),
    getCsrf,
  };
}

module.exports = { createTestEnvironment, createClient };
