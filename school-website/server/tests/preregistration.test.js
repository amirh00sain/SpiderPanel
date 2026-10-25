'use strict';

const test = require('node:test');
const assert = require('node:assert');

const { createTestEnvironment, createClient } = require('./helpers');

const tempDir = createTestEnvironment('prereg');
const { createApp } = require('../app');

const server = createApp().listen(0);
const baseUrl = `http://127.0.0.1:${server.address().port}`;
const client = createClient(baseUrl);

test.after(() => server.close());
test.after(() => {
  require('fs').rmSync(tempDir, { recursive: true, force: true });
});

let trackingCode = '';
const mobile = '09121112233';

test('پیش‌ثبت‌نام نامعتبر رد می‌شود', async () => {
  const invalid = await client.post('/api/pre-registrations', {
    student_name: 'ا',
    mobile: '12345',
    grade_title: 'پایه دهم',
    field_title: 'رشته ناموجود',
    parent_name: 'ب',
    agree: false,
  });
  assert.equal(invalid.status, 400);
  const details = invalid.data.details;
  assert.ok(details.student_name);
  assert.ok(details.mobile);
  assert.ok(details.field_title);
  assert.ok(details.agree);
});

test('ثبت پیش‌ثبت‌نام معتبر و دریافت کد پیگیری', async () => {
  const created = await client.post('/api/pre-registrations', {
    student_name: 'دانش‌آموز آزمون',
    mobile,
    grade_title: 'پایه دهم',
    field_title: 'علوم تجربی',
    parent_name: 'ولی آزمون',
    note: 'یادداشت آزمون',
    agree: true,
  });
  assert.equal(created.status, 201);
  assert.match(created.data.tracking_code, /^SW-[A-Z0-9]{5}-[A-Z0-9]{5}$/);
  trackingCode = created.data.tracking_code;
});

test('ثبت تکراری در بازه کوتاه رد می‌شود', async () => {
  const duplicate = await client.post('/api/pre-registrations', {
    student_name: 'دانش‌آموز آزمون',
    mobile,
    grade_title: 'پایه دهم',
    field_title: 'علوم تجربی',
    parent_name: 'ولی آزمون',
    agree: true,
  });
  assert.equal(duplicate.status, 409);
});

test('پیگیری درخواست نیازمند کد پیگیری و موبایل درست است', async () => {
  const wrongCode = await client.post('/api/pre-registrations/track', {
    tracking_code: 'SW-AAAAA-BBBBB',
    mobile,
  });
  assert.equal(wrongCode.status, 404);

  const wrongMobile = await client.post('/api/pre-registrations/track', {
    tracking_code: trackingCode,
    mobile: '09120000000',
  });
  assert.equal(wrongMobile.status, 404);

  const ok = await client.post('/api/pre-registrations/track', { tracking_code: trackingCode, mobile });
  assert.equal(ok.status, 200);
  assert.equal(ok.data.request.status, 'new');
  assert.equal(ok.data.request.student_name, 'دانش‌آموز آزمون');
});

test('اطلاعات شخصی دانش‌آموزان در API عمومی منتشر نمی‌شود', async () => {
  const site = await client.get('/api/site');
  const serialized = JSON.stringify(site.data);
  assert.ok(!serialized.includes('دانش‌آموز آزمون'), 'نام دانش‌آموز نباید در API عمومی باشد');
  assert.ok(!serialized.includes(mobile), 'شماره موبایل نباید در API عمومی باشد');
});

test('مدیر وضعیت درخواست را تغییر می‌دهد و برای خانواده قابل مشاهده است', async () => {
  const setup = await client.post('/api/setup/admin', {
    setup_token: 'test-setup-token',
    username: 'manager',
    password: 'ManagerPass123',
  });
  assert.equal(setup.status, 201);
  const login = await client.post('/api/admin/login', { username: 'manager', password: 'ManagerPass123' });
  assert.equal(login.status, 200);

  const list = await client.get('/api/admin/pre-registrations?status=new');
  assert.equal(list.status, 200);
  assert.ok(list.data.items.length >= 1);
  const target = list.data.items[0];

  const updated = await client.patch(`/api/admin/pre-registrations/${target.id}`, {
    status: 'reviewing',
    admin_note: 'مدارک ناقص است، لطفاً تماس بگیرید.',
  });
  assert.equal(updated.status, 200);

  const invalidStatus = await client.patch(`/api/admin/pre-registrations/${target.id}`, {
    status: 'unknown-status',
  });
  assert.equal(invalidStatus.status, 400);

  const track = await client.post('/api/pre-registrations/track', { tracking_code: trackingCode, mobile });
  assert.equal(track.data.request.status, 'reviewing');
  assert.equal(track.data.request.public_note, 'مدارک ناقص است، لطفاً تماس بگیرید.');

  const archived = await client.patch(`/api/admin/pre-registrations/${target.id}`, { status: 'archived' });
  assert.equal(archived.data.item.status, 'archived');
});

test('جست‌وجو و شمارش درخواست‌ها در پنل مدیریت', async () => {
  const search = await client.get(`/api/admin/pre-registrations?q=${encodeURIComponent('09121112233')}`);
  assert.equal(search.status, 200);
  assert.ok(search.data.total >= 1);
  assert.ok(search.data.counts.archived >= 1);
});
