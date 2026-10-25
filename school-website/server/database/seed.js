'use strict';

/**
 * داده‌های اولیه ساختار سایت.
 * فقط اطلاعات تأییدشده و ساختاری درج می‌شود:
 * نام مدرسه، پایه‌های دهم تا دوازدهم، رشته‌های تحصیلی اعلام‌شده و پرسش‌های
 * فرایند پیش‌ثبت‌نام. هیچ نام واقعی، شماره تماس یا خبر ساختگی اضافه نمی‌شود.
 */

const { getDb } = require('./db');
const { SETTING_DEFINITIONS, getSchoolInfo } = require('../services/settings');
const logger = require('../utils/logger');

const DEFAULT_GRADES = [
  { title: 'پایه دهم', grade_number: 10, sort_order: 1 },
  { title: 'پایه یازدهم', grade_number: 11, sort_order: 2 },
  { title: 'پایه دوازدهم', grade_number: 12, sort_order: 3 },
];

const DEFAULT_STUDY_FIELDS = [
  { title: 'علوم تجربی', color: '#28a745', sort_order: 1 },
  { title: 'ریاضی و فیزیک', color: '#245e91', sort_order: 2 },
  { title: 'ادبیات و علوم انسانی', color: '#6f42c1', sort_order: 3 },
];

const DEFAULT_FAQS = [
  {
    question: 'چگونه پیش‌ثبت‌نام انجام می‌شود؟',
    answer:
      'فرم پیش‌ثبت‌نام را در صفحه «پیش‌ثبت‌نام» تکمیل و ارسال کنید. پس از ثبت، یک کد پیگیری به شما نمایش داده می‌شود؛ این کد را نزد خود نگه دارید.',
    sort_order: 1,
  },
  {
    question: 'کد پیگیری چه کاربردی دارد؟',
    answer:
      'با وارد کردن کد پیگیری و شماره موبایل در صفحه «پیگیری پیش‌ثبت‌نام» می‌توانید وضعیت درخواست خود را ببینید.',
    sort_order: 2,
  },
  {
    question: 'آیا ثبت پیش‌ثبت‌نام به معنای پذیرش قطعی است؟',
    answer:
      'خیر. پیش‌ثبت‌نام یک درخواست اولیه است و نتیجه نهایی پس از بررسی توسط مدرسه و در زمان اعلام‌شده به خانواده‌ها اطلاع داده می‌شود.',
    sort_order: 3,
  },
];

function seedDefaults() {
  const db = getDb();

  // تنظیمات عمومی: فقط کلیدهای تعریف‌شده و فقط اگر وجود نداشته باشند
  const insertSetting = db.prepare('INSERT OR IGNORE INTO site_settings (key, value) VALUES (?, ?)');
  const settingsSeed = db.transaction(() => {
    for (const [key, definition] of Object.entries(SETTING_DEFINITIONS)) {
      const value = definition.type === 'boolean' ? (definition.default ? '1' : '0') : String(definition.default ?? '');
      insertSetting.run(key, value);
    }
  });
  settingsSeed();

  // ردیف اطلاعات مدرسه
  getSchoolInfo();

  const gradeCount = db.prepare('SELECT COUNT(*) AS total FROM grades').get().total;
  if (gradeCount === 0) {
    const insert = db.prepare(
      'INSERT INTO grades (title, grade_number, description, sort_order) VALUES (?, ?, ?, ?)'
    );
    db.transaction(() => {
      for (const grade of DEFAULT_GRADES) {
        insert.run(grade.title, grade.grade_number, '', grade.sort_order);
      }
    })();
    logger.info('پایه‌های تحصیلی پیش‌فرض ثبت شد.');
  }

  const fieldCount = db.prepare('SELECT COUNT(*) AS total FROM study_fields').get().total;
  if (fieldCount === 0) {
    const insert = db.prepare(
      'INSERT INTO study_fields (title, description, color, sort_order) VALUES (?, ?, ?, ?)'
    );
    db.transaction(() => {
      for (const field of DEFAULT_STUDY_FIELDS) {
        insert.run(field.title, '', field.color, field.sort_order);
      }
    })();
    logger.info('رشته‌های تحصیلی پیش‌فرض ثبت شد.');
  }

  const faqCount = db.prepare('SELECT COUNT(*) AS total FROM faqs').get().total;
  if (faqCount === 0) {
    const insert = db.prepare('INSERT INTO faqs (question, answer, sort_order) VALUES (?, ?, ?)');
    db.transaction(() => {
      for (const faq of DEFAULT_FAQS) {
        insert.run(faq.question, faq.answer, faq.sort_order);
      }
    })();
    logger.info('پرسش‌های متداول پیش‌فرض ثبت شد.');
  }

  db.prepare('INSERT OR IGNORE INTO sms_settings (id) VALUES (1)').run();
}

module.exports = { seedDefaults, DEFAULT_GRADES, DEFAULT_STUDY_FIELDS, DEFAULT_FAQS };
