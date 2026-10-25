'use strict';

const test = require('node:test');
const assert = require('node:assert');

const { createTestEnvironment, createClient } = require('./helpers');

const tempDir = createTestEnvironment('admin');
const { createApp } = require('../app');

const server = createApp().listen(0);
const baseUrl = `http://127.0.0.1:${server.address().port}`;
const client = createClient(baseUrl);

test.after(() => server.close());
test.after(() => {
  require('fs').rmSync(tempDir, { recursive: true, force: true });
});

let createdGradeId = null;

test('نصب مدیر اولیه و ورود با رمز عبور', async () => {
  const created = await client.post('/api/setup/admin', {
    setup_token: 'test-setup-token',
    username: 'manager',
    full_name: 'مدیر مدرسه',
    password: 'ManagerPass123',
  });
  assert.equal(created.status, 201);

  const wrong = await client.post('/api/admin/login', { username: 'manager', password: 'bad-password' });
  assert.equal(wrong.status, 401);

  const login = await client.post('/api/admin/login', { username: 'manager', password: 'ManagerPass123' });
  assert.equal(login.status, 200);
  assert.equal(login.data.admin.role, 'owner');

  const session = await client.get('/api/auth/session');
  assert.equal(session.data.principal, 'admin');
});

test('ساخت محتوا در پنل، در سایت عمومی نمایش داده می‌شود', async () => {
  const create = await client.post('/api/admin/grades', {
    title: 'پایه دهم — آزمون',
    grade_number: 10,
    description: 'توضیح آزمون',
    is_active: true,
    sort_order: 9,
  });
  assert.equal(create.status, 201);
  createdGradeId = create.data.item.id;

  const site = await client.get('/api/site');
  const found = site.data.grades.find((grade) => grade.id === createdGradeId);
  assert.ok(found, 'پایه ساخته‌شده باید در API عمومی دیده شود');
  assert.equal(found.title, 'پایه دهم — آزمون');
});

test('ویرایش و غیرفعال‌سازی محتوا از پنل اعمال می‌شود', async () => {
  const update = await client.put(`/api/admin/grades/${createdGradeId}`, {
    title: 'پایه دهم — ویرایش‌شده',
    grade_number: 10,
    description: 'توضیح ویرایش‌شده',
    is_active: false,
    sort_order: 9,
  });
  assert.equal(update.status, 200);

  const site = await client.get('/api/site');
  const stillVisible = site.data.grades.find((grade) => grade.id === createdGradeId);
  assert.equal(stillVisible, undefined, 'محتوای غیرفعال نباید در سایت نمایش داده شود');

  const reActivate = await client.patch(`/api/admin/grades/${createdGradeId}`, { is_active: true });
  assert.equal(reActivate.status, 200);
  const siteAgain = await client.get('/api/site');
  assert.ok(siteAgain.data.grades.find((grade) => grade.id === createdGradeId));
});

test('اعتبارسنجی سمت سرور برای محتوای نامعتبر', async () => {
  const invalid = await client.post('/api/admin/grades', { title: '', grade_number: 99 });
  assert.equal(invalid.status, 400);
  assert.ok(invalid.data.details.title);
});

test('تنظیمات سایت ذخیره و در سایت عمومی منعکس می‌شود', async () => {
  const update = await client.put('/api/admin/settings', {
    site_title: 'عنوان آزمون',
    pre_registration_open: false,
    show_gallery: false,
  });
  assert.equal(update.status, 200);

  const site = await client.get('/api/site');
  assert.equal(site.data.settings.site_title, 'عنوان آزمون');
  assert.equal(site.data.settings.preRegistrationOpen, false);
  assert.equal(site.data.settings.sections.gallery, false);

  // بازگردانی مقدار پیش‌فرض برای سایر تست‌ها
  await client.put('/api/admin/settings', { pre_registration_open: true, show_gallery: true });
});

test('اطلاعات تماس ناقص به عنوان محتوای تأییدنشده گزارش می‌شود', async () => {
  const info = await client.get('/api/admin/school-info');
  assert.equal(info.status, 200);
  assert.ok(info.data.missing.includes('نشانی'));

  const updated = await client.put('/api/admin/school-info', {
    name: 'دبیرستان دوره دوم امام امیرالمؤمنین (ع)',
    address: 'نشانی آزمون',
    phone: '02100000000',
    email: 'info@example.com',
  });
  assert.equal(updated.status, 200);
  assert.ok(!updated.data.missing.includes('نشانی'));

  const invalid = await client.put('/api/admin/school-info', { email: 'not-an-email' });
  assert.equal(invalid.status, 400);
  assert.ok(invalid.data.details.email);
});

test('افزودن مدیر جدید و تغییر رمز عبور', async () => {
  const created = await client.post('/api/admin/admins', {
    username: 'editor1',
    full_name: 'ویرایشگر',
    password: 'EditorPass123',
    role: 'editor',
    is_active: true,
  });
  assert.equal(created.status, 201);
  assert.equal(created.data.item.password_hash, undefined, 'هش رمز نباید بازگردانده شود');

  const weak = await client.post('/api/admin/admins', {
    username: 'editor2',
    password: 'short',
    role: 'editor',
  });
  assert.equal(weak.status, 400);

  const changePassword = await client.post('/api/admin/password', {
    current_password: 'ManagerPass123',
    new_password: 'ManagerPass456',
  });
  assert.equal(changePassword.status, 200);

  const wrongCurrent = await client.post('/api/admin/password', {
    current_password: 'nope',
    new_password: 'ManagerPass789',
  });
  assert.equal(wrongCurrent.status, 400);
});

test('خروجی CSV فقط با دسترسی مدیر در دسترس است', async () => {
  const response = await client.get('/api/admin/pre-registrations/export.csv');
  assert.equal(response.status, 200);
  assert.match(response.data.raw, /کد پیگیری/);
});

test('حذف محتوا از پنل مدیریت', async () => {
  const removed = await client.del(`/api/admin/grades/${createdGradeId}`);
  assert.equal(removed.status, 200);
  const site = await client.get('/api/site');
  assert.equal(site.data.grades.find((grade) => grade.id === createdGradeId), undefined);
});
