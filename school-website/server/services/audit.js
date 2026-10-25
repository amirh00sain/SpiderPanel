'use strict';

/**
 * ثبت رویدادهای امنیتی و مدیریتی.
 * در جزئیات رویدادها هیچ رمز عبور، کد یک‌بارمصرف یا کلید API ذخیره نمی‌شود.
 */

const { getDb } = require('../database/db');
const logger = require('../utils/logger');

const SENSITIVE_KEYS = ['password', 'api_key', 'code', 'token', 'session', 'secret'];

function sanitizeDetails(details) {
  if (details === undefined || details === null) return '';
  let payload = details;
  if (typeof details !== 'string') {
    try {
      payload = JSON.stringify(details);
    } catch {
      payload = '';
    }
  }
  let text = String(payload);
  for (const key of SENSITIVE_KEYS) {
    text = text.replace(new RegExp(`("?${key}"?\\s*[:=]\\s*)("[^"]*"|'[^']*'|[^,}\\s]+)`, 'gi'), `$1"[حذف شد]"`);
  }
  return text.slice(0, 1000);
}

function log(entry) {
  const {
    actorType = 'system',
    actorId = null,
    actorLabel = '',
    action,
    entity = '',
    entityId = '',
    details = '',
    ip = '',
  } = entry || {};

  if (!action) return;
  try {
    getDb()
      .prepare(
        `INSERT INTO audit_logs (actor_type, actor_id, actor_label, action, entity, entity_id, details, ip)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
      )
      .run(
        actorType,
        actorId,
        String(actorLabel || '').slice(0, 120),
        String(action).slice(0, 80),
        String(entity || '').slice(0, 60),
        String(entityId || '').slice(0, 60),
        sanitizeDetails(details),
        String(ip || '').slice(0, 60)
      );
  } catch (error) {
    logger.warn(`ثبت لاگ امنیتی ناموفق بود: ${error.message}`);
  }
}

function recent(limit = 100) {
  const safeLimit = Math.min(Math.max(Number(limit) || 50, 1), 500);
  return getDb()
    .prepare(
      `SELECT id, actor_type, actor_id, actor_label, action, entity, entity_id, details, ip, created_at
       FROM audit_logs ORDER BY id DESC LIMIT ?`
    )
    .all(safeLimit);
}

module.exports = { log, recent, sanitizeDetails };
