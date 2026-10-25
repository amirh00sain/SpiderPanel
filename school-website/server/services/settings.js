'use strict';

/**
 * تنظیمات سایت و اطلاعات مدرسه.
 * کلیدهای مجاز در همین فایل تعریف شده‌اند تا امکان ذخیره کلید ناشناخته وجود نداشته باشد.
 */

const { getDb } = require('../database/db');
const config = require('../config');
const { cleanText } = require('../utils/validate');

const SETTING_DEFINITIONS = {
  site_title: { label: 'عنوان سایت', type: 'text', default: 'دبیرستان دوره دوم امام امیرالمؤمنین (ع)' },
  site_description: { label: 'توضیح کوتاه سایت', type: 'textarea', default: '' },
  footer_note: { label: 'متن پایین فوتر', type: 'text', default: '' },
  contact_notice: {
    label: 'یادداشت بخش تماس',
    type: 'textarea',
    default: 'اطلاعات تماس رسمی مدرسه پس از تأیید در همین بخش منتشر می‌شود.',
  },
  pre_registration_open: { label: 'فرم پیش‌ثبت‌نام فعال باشد', type: 'boolean', default: 1 },
  pre_registration_notice: { label: 'توضیح بالای فرم پیش‌ثبت‌نام', type: 'textarea', default: '' },
  show_grades: { label: 'نمایش بخش پایه‌های تحصیلی', type: 'boolean', default: 1 },
  show_fields: { label: 'نمایش بخش رشته‌های تحصیلی', type: 'boolean', default: 1 },
  show_staff: { label: 'نمایش بخش کادر مدرسه', type: 'boolean', default: 1 },
  show_achievements: { label: 'نمایش بخش افتخارات', type: 'boolean', default: 1 },
  show_news: { label: 'نمایش بخش اخبار', type: 'boolean', default: 1 },
  show_gallery: { label: 'نمایش بخش گالری', type: 'boolean', default: 1 },
  show_faqs: { label: 'نمایش بخش پرسش‌های متداول', type: 'boolean', default: 1 },
  show_contact: { label: 'نمایش بخش تماس با ما', type: 'boolean', default: 1 },
};

const BOOLEAN_SETTINGS = Object.keys(SETTING_DEFINITIONS).filter(
  (key) => SETTING_DEFINITIONS[key].type === 'boolean'
);

// فیلدهای اطلاعات مدرسه که مدیر می‌تواند تکمیل کند.
const SCHOOL_INFO_FIELDS = [
  'name',
  'slogan',
  'about_short',
  'about_long',
  'address',
  'phone',
  'mobile',
  'email',
  'fax',
  'office_hours',
  'map_url',
  'logo_path',
  'hero_title',
  'hero_subtitle',
  'hero_image_path',
  'hero_cta_label',
  'hero_cta_url',
  'instagram_url',
  'telegram_url',
  'whatsapp_url',
  'aparat_url',
];

// فیلدهایی که اگر خالی باشند در داشبورد به عنوان اطلاعات ناقص گزارش می‌شوند.
const REQUIRED_PUBLIC_FIELDS = [
  ['name', 'نام مدرسه'],
  ['about_short', 'معرفی کوتاه'],
  ['about_long', 'معرفی کامل'],
  ['address', 'نشانی'],
  ['phone', 'شماره تلفن'],
  ['email', 'ایمیل'],
];

function getSettings() {
  const db = getDb();
  const rows = db.prepare('SELECT key, value FROM site_settings').all();
  const stored = new Map(rows.map((row) => [row.key, row.value]));
  const settings = {};
  for (const [key, definition] of Object.entries(SETTING_DEFINITIONS)) {
    const raw = stored.has(key) ? stored.get(key) : definition.default;
    settings[key] = definition.type === 'boolean' ? (String(raw) === '1' ? 1 : 0) : String(raw ?? '');
  }
  return settings;
}

function updateSettings(patch) {
  const db = getDb();
  const statement = db.prepare(
    `INSERT INTO site_settings (key, value, updated_at) VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
     ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`
  );
  const apply = db.transaction(() => {
    for (const [key, value] of Object.entries(patch || {})) {
      const definition = SETTING_DEFINITIONS[key];
      if (!definition) continue;
      let normalized;
      if (definition.type === 'boolean') normalized = value ? '1' : '0';
      else normalized = cleanText(value, { multiline: definition.type === 'textarea', maxLength: 2000 });
      statement.run(key, normalized);
    }
  });
  apply();
  return getSettings();
}

function getSchoolInfoRow() {
  const db = getDb();
  let row = db.prepare('SELECT * FROM school_info WHERE id = 1').get();
  if (!row) {
    db.prepare('INSERT INTO school_info (id, name, slogan) VALUES (1, ?, ?)').run(
      SETTING_DEFINITIONS.site_title.default,
      'علم، ایمان، آینده روشن'
    );
    row = db.prepare('SELECT * FROM school_info WHERE id = 1').get();
  }
  return row;
}

function updateSchoolInfo(patch) {
  const db = getDb();
  getSchoolInfoRow();
  const assignments = [];
  const values = [];
  for (const field of SCHOOL_INFO_FIELDS) {
    if (!Object.prototype.hasOwnProperty.call(patch || {}, field)) continue;
    const multiline = ['about_short', 'about_long', 'address', 'office_hours'].includes(field);
    const value = cleanText(patch[field], { multiline, maxLength: multiline ? 8000 : 600 });
    assignments.push(`${field} = ?`);
    values.push(value);
  }
  if (assignments.length === 0) return getSchoolInfoRow();

  const sql = `UPDATE school_info SET ${assignments.join(', ')}, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = 1`;
  db.prepare(sql).run(...values);
  return getSchoolInfoRow();
}

function missingPublicFields(info) {
  const row = info || getSchoolInfoRow();
  return REQUIRED_PUBLIC_FIELDS.filter(([field]) => !String(row[field] || '').trim()).map(
    ([, label]) => label
  );
}

/** اطلاعات عمومی سایت: بدون هیچ مقدار حساس یا داخلی */
function getPublicSettings() {
  const settings = getSettings();
  const info = getSchoolInfoRow();
  const booleanKeys = new Set(BOOLEAN_SETTINGS);
  const publicSettings = {};
  for (const [key, value] of Object.entries(settings)) {
    publicSettings[key] = booleanKeys.has(key) ? Boolean(value) : value;
  }
  publicSettings.sections = {
    grades: Boolean(settings.show_grades),
    fields: Boolean(settings.show_fields),
    staff: Boolean(settings.show_staff),
    achievements: Boolean(settings.show_achievements),
    news: Boolean(settings.show_news),
    gallery: Boolean(settings.show_gallery),
    faqs: Boolean(settings.show_faqs),
    contact: Boolean(settings.show_contact),
  };
  publicSettings.preRegistrationOpen = Boolean(settings.pre_registration_open);

  const school = {};
  for (const field of SCHOOL_INFO_FIELDS) {
    school[field] = info[field] || '';
  }
  return { settings: publicSettings, school };
}

/** پیکربندی پیامک: متغیرهای محیطی بر تنظیمات دیتابیس اولویت دارند */
function getSmsConfig() {
  const db = getDb();
  const row =
    db.prepare('SELECT * FROM sms_settings WHERE id = 1').get() ||
    db.prepare('INSERT INTO sms_settings (id) VALUES (1) RETURNING *').get();

  const provider = config.sms.provider || row.provider || '';
  const apiKey = config.sms.apiKey || row.api_key || '';
  const username = config.sms.username || row.username || '';
  const sender = config.sms.sender || row.sender_number || '';
  const otpTemplate = config.sms.otpTemplate || row.otp_template || 'کد ورود شما به سایت مدرسه: {code}';
  const enabledByEnv = Boolean(config.sms.provider);

  return {
    provider,
    apiKey,
    username,
    sender,
    otpTemplate,
    isEnabled: enabledByEnv ? true : Boolean(row.is_enabled),
    lastTestedAt: row.last_tested_at || '',
    lastTestResult: row.last_test_result || '',
    source: enabledByEnv ? 'env' : 'database',
  };
}

function saveSmsConfig(patch) {
  const db = getDb();
  getSmsConfig();
  const assignments = [];
  const values = [];

  if (Object.prototype.hasOwnProperty.call(patch, 'provider')) {
    assignments.push('provider = ?');
    values.push(cleanText(patch.provider, { maxLength: 30 }));
  }
  if (Object.prototype.hasOwnProperty.call(patch, 'api_key') && String(patch.api_key || '').trim() !== '') {
    assignments.push('api_key = ?');
    values.push(cleanText(patch.api_key, { maxLength: 300 }));
  }
  if (Object.prototype.hasOwnProperty.call(patch, 'username')) {
    assignments.push('username = ?');
    values.push(cleanText(patch.username, { maxLength: 120 }));
  }
  if (Object.prototype.hasOwnProperty.call(patch, 'sender_number')) {
    assignments.push('sender_number = ?');
    values.push(cleanText(patch.sender_number, { maxLength: 40 }));
  }
  if (Object.prototype.hasOwnProperty.call(patch, 'otp_template')) {
    assignments.push('otp_template = ?');
    values.push(cleanText(patch.otp_template, { multiline: true, maxLength: 300 }));
  }
  if (Object.prototype.hasOwnProperty.call(patch, 'is_enabled')) {
    assignments.push('is_enabled = ?');
    values.push(patch.is_enabled ? 1 : 0);
  }

  if (assignments.length > 0) {
    db.prepare(
      `UPDATE sms_settings SET ${assignments.join(', ')}, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = 1`
    ).run(...values);
  }
  return getSmsConfig();
}

function recordSmsTest(result) {
  getDb()
    .prepare(
      `UPDATE sms_settings SET last_tested_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), last_test_result = ? WHERE id = 1`
    )
    .run(String(result || '').slice(0, 300));
}

/** کلید API هرگز کامل به مرورگر فرستاده نمی‌شود. */
function maskSecret(value) {
  const text = String(value || '');
  if (!text) return '';
  if (text.length <= 8) return '********';
  return `${text.slice(0, 4)}********${text.slice(-4)}`;
}

function getSmsConfigForAdmin() {
  const sms = getSmsConfig();
  return {
    provider: sms.provider,
    hasApiKey: Boolean(sms.apiKey),
    apiKeyMasked: maskSecret(sms.apiKey),
    username: sms.username,
    sender_number: sms.sender,
    otp_template: sms.otpTemplate,
    is_enabled: sms.isEnabled ? 1 : 0,
    source: sms.source,
    last_tested_at: sms.lastTestedAt,
    last_test_result: sms.lastTestResult,
  };
}

module.exports = {
  SETTING_DEFINITIONS,
  SCHOOL_INFO_FIELDS,
  getSettings,
  updateSettings,
  getSchoolInfo: getSchoolInfoRow,
  updateSchoolInfo,
  missingPublicFields,
  getPublicSettings,
  getSmsConfig,
  getSmsConfigForAdmin,
  saveSmsConfig,
  recordSmsTest,
  maskSecret,
};
