'use strict';

/**
 * API عمومی سایت: محتوای قابل نمایش، خبر، فرم پیش‌ثبت‌نام و پیگیری درخواست.
 * هیچ داده شخصی دانش‌آموزان بدون کد پیگیری معتبر و شماره موبایل همان درخواست بازگردانده نمی‌شود.
 */

const express = require('express');

const config = require('../config');
const { getDb } = require('../database/db');
const { asyncHandler, clientIp, hashIp, sendError, sendOk } = require('../utils/http');
const { rateLimit } = require('../middleware/rateLimit');
const settingsService = require('../services/settings');
const audit = require('../services/audit');
const smsService = require('../services/sms');
const {
  cleanText,
  normalizeMobile,
  isValidMobile,
} = require('../utils/validate');
const { generateTrackingCode, trackingCodesMatch } = require('../utils/trackingCode');

const router = express.Router();

const submitLimiter = rateLimit({
  name: 'prereg-submit',
  windowMs: 60 * 60 * 1000,
  max: config.preRegistration.maxPerIpHour,
  message: 'تعداد ثبت درخواست از این اتصال بیش از حد مجاز است. لطفاً بعداً تلاش کنید.',
});

const trackLimiter = rateLimit({
  name: 'prereg-track',
  windowMs: 10 * 60 * 1000,
  max: 10,
  message: 'تعداد تلاش برای پیگیری زیاد بوده است. کمی بعد دوباره تلاش کنید.',
});

function publishedNews(limit) {
  return getDb()
    .prepare(
      `SELECT id, title, slug, summary, cover_image, published_at, views
       FROM news WHERE is_published = 1
       ORDER BY COALESCE(published_at, created_at) DESC, id DESC LIMIT ?`
    )
    .all(limit);
}

router.get('/health', (req, res) => {
  sendOk(res, { status: 'up', time: new Date().toISOString() });
});

router.get('/csrf', (req, res) => {
  sendOk(res, { csrfToken: req.csrfToken });
});

/** همه محتوای لازم برای صفحه‌های عمومی در یک درخواست */
router.get('/site', (req, res) => {
  const db = getDb();
  const { settings, school } = settingsService.getPublicSettings();

  const grades = db
    .prepare('SELECT id, title, grade_number, description FROM grades WHERE is_active = 1 ORDER BY sort_order ASC, grade_number ASC')
    .all();
  const studyFields = db
    .prepare('SELECT id, title, description, icon, color FROM study_fields WHERE is_active = 1 ORDER BY sort_order ASC, id ASC')
    .all();
  const staff = db
    .prepare(
      `SELECT id, full_name, role_title, bio, photo_path FROM staff WHERE is_active = 1
       ORDER BY sort_order ASC, id ASC LIMIT 60`
    )
    .all();
  const achievements = db
    .prepare('SELECT id, title, description, year, image_path FROM achievements WHERE is_active = 1 ORDER BY sort_order ASC, id DESC')
    .all();
  const gallery = db
    .prepare('SELECT id, title, image_path, category FROM gallery WHERE is_active = 1 ORDER BY sort_order ASC, id DESC LIMIT 60')
    .all();
  const faqs = db
    .prepare('SELECT id, question, answer FROM faqs WHERE is_active = 1 ORDER BY sort_order ASC, id ASC')
    .all();
  const news = publishedNews(6);
  const announcements = db
    .prepare(
      `SELECT id, title, content, is_pinned, published_at, expires_at FROM announcements
       WHERE is_published = 1 AND (expires_at IS NULL OR expires_at = '' OR expires_at > ?)
       ORDER BY is_pinned DESC, COALESCE(published_at, created_at) DESC LIMIT 10`
    )
    .all(new Date().toISOString());

  sendOk(res, {
    settings,
    school,
    grades,
    studyFields,
    staff,
    achievements,
    gallery,
    faqs,
    news,
    announcements,
    counts: {
      news: db.prepare('SELECT COUNT(*) AS total FROM news WHERE is_published = 1').get().total,
      gallery: gallery.length,
      staff: staff.length,
    },
    sms: { enabled: smsService.status().enabled },
  });
});

router.get('/news', (req, res) => {
  const db = getDb();
  const perPage = Math.min(Math.max(Number.parseInt(req.query.perPage, 10) || 9, 1), 30);
  const page = Math.max(Number.parseInt(req.query.page, 10) || 1, 1);
  const total = db.prepare('SELECT COUNT(*) AS total FROM news WHERE is_published = 1').get().total;
  const items = db
    .prepare(
      `SELECT id, title, slug, summary, cover_image, published_at FROM news
       WHERE is_published = 1 ORDER BY COALESCE(published_at, created_at) DESC, id DESC
       LIMIT ? OFFSET ?`
    )
    .all(perPage, (page - 1) * perPage);
  sendOk(res, { items, total, page, perPage, pages: Math.max(Math.ceil(total / perPage), 1) });
});

router.get('/news/:slug', (req, res) => {
  const db = getDb();
  const item = db
    .prepare('SELECT * FROM news WHERE slug = ? AND is_published = 1')
    .get(String(req.params.slug || ''));
  if (!item) return sendError(res, 404, 'این خبر یافت نشد یا منتشر نشده است.');
  db.prepare('UPDATE news SET views = views + 1 WHERE id = ?').run(item.id);
  const related = publishedNews(4).filter((row) => row.id !== item.id).slice(0, 3);
  return sendOk(res, {
    item: {
      id: item.id,
      title: item.title,
      slug: item.slug,
      summary: item.summary,
      content: item.content,
      cover_image: item.cover_image,
      published_at: item.published_at,
      views: item.views + 1,
    },
    related,
  });
});

/** ثبت درخواست پیش‌ثبت‌نام */
router.post(
  '/pre-registrations',
  submitLimiter,
  asyncHandler(async (req, res) => {
    const db = getDb();
    const body = req.body || {};
    const errors = {};

    const studentName = cleanText(body.student_name, { maxLength: 120 });
    if (studentName.length < 3) errors.student_name = 'نام و نام خانوادگی دانش‌آموز را کامل وارد کنید.';

    const mobile = normalizeMobile(body.mobile);
    if (!isValidMobile(mobile)) errors.mobile = 'شماره موبایل معتبر نیست. نمونه صحیح: ۰۹۱۲۳۴۵۶۷۸۹';

    const parentName = cleanText(body.parent_name, { maxLength: 120 });
    if (parentName.length < 3) errors.parent_name = 'نام و نام خانوادگی ولی را کامل وارد کنید.';

    const gradeTitle = cleanText(body.grade_title, { maxLength: 120 });
    const gradeRow = gradeTitle
      ? db.prepare('SELECT title FROM grades WHERE is_active = 1 AND title = ?').get(gradeTitle)
      : null;
    if (!gradeRow) errors.grade_title = 'پایه تحصیلی را از فهرست انتخاب کنید.';

    const fieldTitle = cleanText(body.field_title, { maxLength: 120 });
    const fieldRow = fieldTitle
      ? db.prepare('SELECT title FROM study_fields WHERE is_active = 1 AND title = ?').get(fieldTitle)
      : null;
    if (!fieldRow) errors.field_title = 'رشته تحصیلی را از فهرست انتخاب کنید.';

    const note = cleanText(body.note, { multiline: true, maxLength: 1500 });
    const agree = body.agree === true || body.agree === 'true' || body.agree === 1 || body.agree === '1';
    if (!agree) errors.agree = 'تأیید صحت اطلاعات الزامی است.';

    if (!settingsService.getSettings().pre_registration_open) {
      return sendError(res, 403, 'فرم پیش‌ثبت‌نام در حال حاضر بسته است.');
    }

    if (Object.keys(errors).length > 0) {
      return sendError(res, 400, 'لطفاً خطاهای فرم را برطرف کنید.', errors);
    }

    const duplicateCutoff = new Date(
      Date.now() - config.preRegistration.duplicateWindowHours * 60 * 60 * 1000
    ).toISOString();
    const duplicate = db
      .prepare(
        `SELECT id FROM pre_registrations
         WHERE mobile = ? AND grade_title = ? AND created_at > ? LIMIT 1`
      )
      .get(mobile, gradeRow.title, duplicateCutoff);
    if (duplicate) {
      return sendError(
        res,
        409,
        'برای این شماره و این پایه در روز گذشته درخواستی ثبت شده است. برای پیگیری از کد پیگیری خود استفاده کنید.'
      );
    }

    const ip = clientIp(req);
    const trackingCode = generateTrackingCode();
    const info = db
      .prepare(
        `INSERT INTO pre_registrations
           (student_name, mobile, grade_title, field_title, parent_name, note, tracking_code, source_ip, user_agent)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
      )
      .run(
        studentName,
        mobile,
        gradeRow.title,
        fieldRow.title,
        parentName,
        note,
        trackingCode,
        hashIp(ip),
        String(req.get('user-agent') || '').slice(0, 300)
      );

    audit.log({
      actorType: 'public',
      action: 'preregistration.create',
      entity: 'pre_registrations',
      entityId: String(info.lastInsertRowid),
      details: { grade: gradeRow.title, field: fieldRow.title },
      ip,
    });

    return sendOk(
      res,
      {
        message: 'درخواست پیش‌ثبت‌نام شما ثبت شد. کد پیگیری را نزد خود نگه دارید.',
        tracking_code: trackingCode,
        created_at: new Date().toISOString(),
      },
      201
    );
  })
);

/** پیگیری وضعیت درخواست با کد پیگیری و شماره موبایل */
router.post('/pre-registrations/track', trackLimiter, (req, res) => {
  const db = getDb();
  const code = String((req.body || {}).tracking_code || '');
  const mobile = normalizeMobile((req.body || {}).mobile || '');
  if (!code.trim() || !isValidMobile(mobile)) {
    return sendError(res, 400, 'کد پیگیری و شماره موبایل را کامل وارد کنید.');
  }

  const candidates = db
    .prepare(
      `SELECT id, student_name, grade_title, field_title, status, tracking_code, admin_note, created_at, updated_at, reviewed_at
       FROM pre_registrations WHERE mobile = ? ORDER BY id DESC LIMIT 20`
    )
    .all(mobile);

  const match = candidates.find((row) => trackingCodesMatch(code, row.tracking_code));
  if (!match) {
    const ip = clientIp(req);
    audit.log({
      actorType: 'public',
      action: 'preregistration.track_failed',
      entity: 'pre_registrations',
      ip,
    });
    return sendError(res, 404, 'درخواستی با این کد پیگیری و شماره موبایل یافت نشد.');
  }

  return sendOk(res, {
    request: {
      student_name: match.student_name,
      grade_title: match.grade_title,
      field_title: match.field_title,
      status: match.status,
      tracking_code: match.tracking_code,
      created_at: match.created_at,
      updated_at: match.updated_at,
      reviewed_at: match.reviewed_at,
      public_note: match.admin_note,
    },
  });
});

module.exports = router;
