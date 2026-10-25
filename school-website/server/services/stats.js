'use strict';

/** آمار داشبورد پنل مدیریت (همه از دیتابیس خوانده می‌شود). */

const { getDb } = require('../database/db');
const settingsService = require('./settings');
const smsService = require('./sms');

function count(db, sql, params = []) {
  const row = db.prepare(sql).get(...params);
  return row ? Number(Object.values(row)[0]) : 0;
}

function lastDays(days) {
  const result = [];
  for (let index = days - 1; index >= 0; index -= 1) {
    const date = new Date(Date.now() - index * 24 * 60 * 60 * 1000);
    result.push(date.toISOString().slice(0, 10));
  }
  return result;
}

function dashboard() {
  const db = getDb();

  const preRegByStatus = db
    .prepare('SELECT status, COUNT(*) AS total FROM pre_registrations GROUP BY status')
    .all();
  const statusCounts = { new: 0, reviewing: 0, approved: 0, rejected: 0, archived: 0 };
  for (const row of preRegByStatus) statusCounts[row.status] = Number(row.total);
  const totalPreReg = Object.values(statusCounts).reduce((sum, value) => sum + value, 0);

  const days = lastDays(7);
  const preRegSeries = Object.fromEntries(days.map((day) => [day, 0]));
  for (const row of db
    .prepare('SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS total FROM pre_registrations GROUP BY day')
    .all()) {
    if (row.day in preRegSeries) preRegSeries[row.day] = Number(row.total);
  }
  const viewSeries = Object.fromEntries(days.map((day) => [day, 0]));
  for (const row of db
    .prepare('SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS total FROM page_views GROUP BY day')
    .all()) {
    if (row.day in viewSeries) viewSeries[row.day] = Number(row.total);
  }

  const recentPreRegistrations = db
    .prepare(
      `SELECT id, student_name, grade_title, field_title, status, tracking_code, created_at
       FROM pre_registrations ORDER BY id DESC LIMIT 6`
    )
    .all();

  const schoolInfo = settingsService.getSchoolInfo();

  return {
    stats: {
      pageViewsTotal: count(db, 'SELECT COUNT(*) FROM page_views'),
      pageViewsToday: count(db, "SELECT COUNT(*) FROM page_views WHERE substr(created_at, 1, 10) = ?", [
        new Date().toISOString().slice(0, 10),
      ]),
      preRegistrationsTotal: totalPreReg,
      preRegistrationsNew: statusCounts.new,
      preRegistrationsReviewing: statusCounts.reviewing,
      preRegistrationsApproved: statusCounts.approved,
      preRegistrationsRejected: statusCounts.rejected,
      newsTotal: count(db, 'SELECT COUNT(*) FROM news'),
      newsPublished: count(db, 'SELECT COUNT(*) FROM news WHERE is_published = 1'),
      announcementsPublished: count(db, 'SELECT COUNT(*) FROM announcements WHERE is_published = 1'),
      galleryTotal: count(db, 'SELECT COUNT(*) FROM gallery WHERE is_active = 1'),
      staffTotal: count(db, 'SELECT COUNT(*) FROM staff WHERE is_active = 1'),
      achievementsTotal: count(db, 'SELECT COUNT(*) FROM achievements WHERE is_active = 1'),
      faqsTotal: count(db, 'SELECT COUNT(*) FROM faqs WHERE is_active = 1'),
      adminsTotal: count(db, 'SELECT COUNT(*) FROM admins WHERE is_active = 1'),
    },
    charts: {
      days,
      preRegistrations: days.map((day) => preRegSeries[day]),
      pageViews: days.map((day) => viewSeries[day]),
    },
    preRegistrationStatus: statusCounts,
    recentPreRegistrations,
    content: {
      missingFields: settingsService.missingPublicFields(schoolInfo),
      schoolName: schoolInfo.name || '',
      heroReady: Boolean(schoolInfo.hero_image_path),
      logoReady: Boolean(schoolInfo.logo_path),
    },
    sms: smsService.status(),
  };
}

module.exports = { dashboard };
