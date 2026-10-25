'use strict';

const test = require('node:test');
const assert = require('node:assert');

const { createTestEnvironment, createClient } = require('./helpers');

const tempDir = createTestEnvironment('otp');
const { createApp } = require('../app');
const sms = require('../services/sms');

const server = createApp().listen(0);
const baseUrl = `http://127.0.0.1:${server.address().port}`;
const client = createClient(baseUrl);

test.after(() => server.close());
test.after(() => {
  require('fs').rmSync(tempDir, { recursive: true, force: true });
});

const mobile = '09355556677';

function lastOtpCode() {
  const message = sms.outbox[sms.outbox.length - 1];
  assert.ok(message, 'پیامک شبیه‌سازی‌شده باید در outbox موجود باشد');
  const match = String(message.message).match(/(\d{6})/);
  assert.ok(match, 'کد در متن پیامک یافت نشد');
  return match[1];
}

test('درخواست کد برای شماره نامعتبر رد می‌شود', async () => {
  const response = await client.post('/api/auth/otp/request', { mobile: '123' });
  assert.equal(response.status, 400);
});

test('درخواست کد معتبر در حالت توسعه', async () => {
  const response = await client.post('/api/auth/otp/request', { mobile });
  assert.equal(response.status, 200);
  assert.equal(response.data.ok, true);
  assert.ok(response.data.simulated, 'در حالت توسعه، ارسال شبیه‌سازی می‌شود');
});

test('کد نادرست پذیرفته نمی‌شود و تعداد تلاش محدود است', async () => {
  const response = await client.post('/api/auth/otp/verify', { mobile, code: '000000' });
  assert.equal(response.status, 400);
  assert.match(response.data.error, /صحیح نیست|تلاش/);
});

test('کد صحیح ورود کاربر را ممکن می‌کند', async () => {
  const code = lastOtpCode();
  const response = await client.post('/api/auth/otp/verify', { mobile, code });
  assert.equal(response.status, 200);
  assert.equal(response.data.user.mobile, mobile);

  const session = await client.get('/api/auth/session');
  assert.equal(session.data.principal, 'user');
  assert.equal(session.data.user.mobile, mobile);
});

test('کد استفاده‌شده دوباره کار نمی‌کند', async () => {
  const code = lastOtpCode();
  const response = await client.post('/api/auth/otp/verify', { mobile, code });
  assert.equal(response.status, 400);
});

test('محدودیت تعداد درخواست کد در ساعت اعمال می‌شود', async () => {
  // سقف در محیط تست ۳ درخواست در ساعت برای هر شماره است (یک درخواست پیش‌تر ثبت شده است).
  const first = await client.post('/api/auth/otp/request', { mobile });
  const second = await client.post('/api/auth/otp/request', { mobile });
  const third = await client.post('/api/auth/otp/request', { mobile });
  assert.equal(first.status, 200);
  assert.equal(second.status, 200);
  assert.equal(third.status, 429);
});

test('کاربر فقط درخواست‌های خودش را می‌بیند', async () => {
  const response = await client.get('/api/my/requests');
  assert.equal(response.status, 200);
  assert.ok(Array.isArray(response.data.items));
  assert.ok(response.data.items.every((item) => item.tracking_code));

  const anonymousClient = createClient(baseUrl);
  const denied = await anonymousClient.get('/api/my/requests');
  assert.equal(denied.status, 401, 'کاربر مهمان نباید به درخواست‌ها دسترسی داشته باشد');
});

test('کاربر عادی به APIهای مدیریتی دسترسی ندارد', async () => {
  const denied = await client.get('/api/admin/dashboard');
  assert.equal(denied.status, 401);
  const deniedCrud = await client.post('/api/admin/grades', { title: 'تلاش غیرمجاز', is_active: true });
  assert.equal(deniedCrud.status, 401);
});
