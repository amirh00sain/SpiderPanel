'use strict';

const test = require('node:test');
const assert = require('node:assert');

const { createTestEnvironment, createClient } = require('./helpers');

const tempDir = createTestEnvironment('security');
const { createApp } = require('../app');

const server = createApp().listen(0);
const baseUrl = `http://127.0.0.1:${server.address().port}`;
const client = createClient(baseUrl);

test.after(() => server.close());
test.after(() => {
  // پاک‌سازی فایل‌های موقت تست
  require('fs').rmSync(tempDir, { recursive: true, force: true });
});

test('صفحه اصلی و API عمومی پاسخ می‌دهند', async () => {
  const site = await client.get('/api/site');
  assert.equal(site.status, 200);
  assert.equal(site.data.ok, true);
  assert.ok(Array.isArray(site.data.grades));
  assert.ok(site.data.grades.length >= 3, 'پایه‌های پیش‌فرض باید موجود باشند');
  assert.ok(site.data.school.name.includes('امام امیرالمؤمنین'));
});

test('هدرهای امنیتی اعمال می‌شوند', async () => {
  const response = await fetch(`${baseUrl}/api/health`);
  assert.equal(response.headers.get('x-content-type-options'), 'nosniff');
  assert.ok(response.headers.get('content-security-policy'));
  assert.equal(response.headers.get('x-powered-by'), null);
});

test('درخواست بدون توکن CSRF رد می‌شود', async () => {
  const response = await fetch(`${baseUrl}/api/pre-registrations`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ student_name: 'آزمون' }),
  });
  assert.equal(response.status, 403);
});

test('دسترسی بدون ورود به APIهای مدیریتی مسدود است', async () => {
  const dashboard = await client.get('/api/admin/dashboard');
  assert.equal(dashboard.status, 401);
  const crud = await client.get('/api/admin/grades');
  assert.equal(crud.status, 401);
  const exportCsv = await client.get('/api/admin/pre-registrations/export.csv');
  assert.equal(exportCsv.status, 401);
});

test('تلاش ورود با ورودی مشکوک باعث خطای سرور نمی‌شود', async () => {
  const attempt = await client.post('/api/admin/login', {
    username: "admin' OR '1'='1",
    password: "' OR 1=1 --",
  });
  assert.equal(attempt.status, 401);
  assert.equal(attempt.data.ok, false);
});

test('محدودیت تلاش ورود اعمال می‌شود', async () => {
  let lastStatus = 0;
  for (let index = 0; index < 7; index += 1) {
    const attempt = await client.post('/api/admin/login', {
      username: 'someone',
      password: `wrong-password-${index}`,
    });
    lastStatus = attempt.status;
  }
  assert.equal(lastStatus, 429, 'پس از چند تلاش ناموفق باید محدودیت اعمال شود');
});

test('مسیر نصب پس از ساخت مدیر بسته می‌شود', async () => {
  const status = await client.get('/api/setup/status');
  assert.equal(status.status, 200);
  const created = await client.post('/api/setup/admin', {
    setup_token: 'test-setup-token',
    username: 'owner',
    full_name: 'مدیر ارشد',
    password: 'StrongPass1234',
  });
  assert.equal(created.status, 201);

  const again = await client.post('/api/setup/admin', {
    setup_token: 'test-setup-token',
    username: 'owner2',
    password: 'StrongPass1234',
  });
  assert.equal(again.status, 403);

  const statusAfter = await client.get('/api/setup/status');
  assert.equal(statusAfter.data.needsSetup, false);
  assert.equal(statusAfter.data.alreadyDone, true);

  const wrongToken = await client.post('/api/setup/admin', {
    setup_token: 'wrong-token-value',
    username: 'owner3',
    password: 'StrongPass1234',
  });
  assert.equal(wrongToken.status, 403);
});
