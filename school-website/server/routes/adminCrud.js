'use strict';

/**
 * CRUD عمومی پنل مدیریت.
 * نام جدول و ستون‌ها فقط از فهرست سرور (database/entities.js) خوانده می‌شود و
 * همه مقادیر با پارامتر به SQLite پاس داده می‌شوند.
 */

const express = require('express');
const bcrypt = require('bcryptjs');

const { getDb } = require('../database/db');
const { getEntity, getMeta } = require('../database/entities');
const { asyncHandler, clientIp, sendError, sendOk } = require('../utils/http');
const { validateEntityPayload } = require('../utils/validate');
const { fromTehranInput, toTehranInput } = require('../utils/dates');
const { uniqueSlug } = require('../utils/slug');
const audit = require('../services/audit');

const OWNER_ONLY_ENTITIES = ['admins'];
const PUBLISH_ENTITIES = ['news', 'announcements'];

const router = express.Router();

function requireEntityAccess(req, res, next) {
  const key = String(req.params.entity || '');
  const entity = getEntity(key);
  if (!entity) return sendError(res, 404, 'این بخش در پنل مدیریت وجود ندارد.');
  if (OWNER_ONLY_ENTITIES.includes(key) && req.auth.admin.role !== 'owner') {
    return sendError(res, 403, 'مدیریت حساب مدیران فقط برای نقش «مدیر ارشد» مجاز است.');
  }
  req.entity = entity;
  req.entityKey = key;
  return next();
}

function serializeRow(entity, row) {
  const output = { ...row };
  for (const field of entity.fields) {
    if (field.type === 'datetime') {
      output[`${field.name}_input`] = toTehranInput(row[field.name]);
    }
    if (field.type === 'password') delete output[field.name];
  }
  for (const key of entity.protectedFields || []) delete output[key];
  return output;
}

function fieldByName(entity, name) {
  return entity.fields.find((field) => field.name === name) || null;
}

function buildValues(entity, body, { partial }) {
  const { values, errors } = validateEntityPayload(entity, body, { partial });
  const prepared = {};
  for (const [name, value] of Object.entries(values)) {
    const field = fieldByName(entity, name);
    if (!field) continue;
    if (field.type === 'datetime') {
      const iso = value ? fromTehranInput(value) : null;
      if (value && !iso) {
        errors[name] = `${field.label} معتبر نیست.`;
        continue;
      }
      prepared[name] = iso;
    } else if (field.type === 'password') {
      // رمز عبور فقط به صورت هش ذخیره می‌شود؛ مقدار خالی یعنی «تغییر نده».
      if (!String(value || '').trim()) continue;
      prepared[field.mapsTo || name] = bcrypt.hashSync(String(value), 12);
    } else {
      prepared[name] = value;
    }
  }
  return { values: prepared, errors };
}

function applyPublishRules(entityKey, values, existing) {
  if (!PUBLISH_ENTITIES.includes(entityKey)) return;
  const willPublish =
    values.is_published !== undefined
      ? values.is_published === 1
      : existing
        ? existing.is_published === 1
        : false;
  const hasPublishedAt = values.published_at || (existing && existing.published_at);
  if (willPublish && !hasPublishedAt) values.published_at = new Date().toISOString();
}

function guardAdminChanges(req, res, { id, changes }) {
  if (req.entityKey !== 'admins') return null;
  const db = getDb();
  const target = db.prepare('SELECT * FROM admins WHERE id = ?').get(id);
  if (!target) return sendError(res, 404, 'این مدیر یافت نشد.');

  if (changes && changes.is_active === 0 && target.id === req.auth.id) {
    return sendError(res, 400, 'نمی‌توانید حساب خودتان را غیرفعال کنید.');
  }
  if (changes && changes.role && target.role === 'owner' && changes.role !== 'owner') {
    const owners = db.prepare("SELECT COUNT(*) AS total FROM admins WHERE role = 'owner' AND is_active = 1").get().total;
    if (owners <= 1) return sendError(res, 400, 'حداقل یک مدیر ارشد فعال باید باقی بماند.');
  }
  return null;
}

function listHandler(req, res) {
  const db = getDb();
  const entity = req.entity;
  const query = String(req.query.q || '').trim();
  const params = [];
  let where = '';
  if (query && entity.searchFields.length > 0) {
    where = ` WHERE ${entity.searchFields.map((field) => `${field} LIKE ?`).join(' OR ')}`;
    for (const _field of entity.searchFields) params.push(`%${query}%`);
  }

  const total = db.prepare(`SELECT COUNT(*) AS total FROM ${entity.table}${where}`).get(...params).total;
  const perPage = Math.min(Math.max(Number.parseInt(req.query.perPage, 10) || 100, 1), 200);
  const page = Math.max(Number.parseInt(req.query.page, 10) || 1, 1);
  const rows = db
    .prepare(
      `SELECT * FROM ${entity.table}${where} ORDER BY ${entity.orderBy} LIMIT ? OFFSET ?`
    )
    .all(...params, perPage, (page - 1) * perPage);

  sendOk(res, { items: rows.map((row) => serializeRow(entity, row)), total, page, perPage });
}

router.get('/meta/:entity', requireEntityAccess, (req, res) => {
  const meta = getMeta(req.entityKey);
  sendOk(res, {
    meta: {
      ...meta,
      canCreate: req.entityKey !== 'admins' || req.auth.admin.role === 'owner',
      canDelete: req.entityKey !== 'admins' || req.auth.admin.role === 'owner',
    },
  });
});

router.get('/:entity', requireEntityAccess, listHandler);

router.post(
  '/:entity',
  requireEntityAccess,
  asyncHandler(async (req, res) => {
    const db = getDb();
    const entity = req.entity;
    const { values, errors } = buildValues(entity, req.body, { partial: false });

    if (entity.slugFrom) {
      const title = values[entity.slugFrom];
      if (!title) {
        errors[entity.slugFrom] = 'عنوان الزامی است.';
      } else {
        values.slug = uniqueSlug(db, entity.table, title);
      }
    }
    if (Object.keys(errors).length > 0) {
      return sendError(res, 400, 'اطلاعات وارد‌شده معتبر نیست.', errors);
    }
    if (req.entityKey === 'admins') {
      if (!String((req.body || {}).password || '').trim()) {
        return sendError(res, 400, 'برای حساب جدید رمز عبور الزامی است.', { password: 'رمز عبور الزامی است.' });
      }
    }

    applyPublishRules(req.entityKey, values, null);

    const columns = Object.keys(values);
    if (columns.length === 0) return sendError(res, 400, 'داده‌ای برای ذخیره ارسال نشده است.');

    try {
      const info = db
        .prepare(
          `INSERT INTO ${entity.table} (${columns.join(', ')}) VALUES (${columns.map(() => '?').join(', ')})`
        )
        .run(...columns.map((column) => values[column]));

      const row = db.prepare(`SELECT * FROM ${entity.table} WHERE id = ?`).get(info.lastInsertRowid);
      audit.log({
        actorType: 'admin',
        actorId: req.auth.admin.id,
        actorLabel: req.auth.admin.username,
        action: `${req.entityKey}.create`,
        entity: entity.table,
        entityId: String(info.lastInsertRowid),
        ip: clientIp(req),
      });
      return sendOk(res, { item: serializeRow(entity, row), message: 'با موفقیت ذخیره شد.' }, 201);
    } catch (error) {
      if (String(error.code || '').startsWith('SQLITE_CONSTRAINT')) {
        return sendError(res, 409, 'این مقدار تکراری است یا داده الزامی ناقص است.', {
          detail: 'مقدار یکتا تکراری است.',
        });
      }
      throw error;
    }
  })
);

function updateHandler(partial) {
  return asyncHandler(async (req, res) => {
    const db = getDb();
    const entity = req.entity;
    const id = Number.parseInt(req.params.id, 10);
    if (!Number.isFinite(id)) return sendError(res, 400, 'شناسه نامعتبر است.');

    const existing = db.prepare(`SELECT * FROM ${entity.table} WHERE id = ?`).get(id);
    if (!existing) return sendError(res, 404, 'این رکورد یافت نشد.');

    const { values, errors } = buildValues(entity, req.body, { partial });

    if (entity.slugFrom && values[entity.slugFrom] && values[entity.slugFrom] !== existing[entity.slugFrom]) {
      values.slug = uniqueSlug(db, entity.table, values[entity.slugFrom], id);
    }
    if (Object.keys(errors).length > 0) {
      return sendError(res, 400, 'اطلاعات وارد‌شده معتبر نیست.', errors);
    }
    if (Object.keys(values).length === 0) {
      return sendError(res, 400, 'داده‌ای برای ذخیره ارسال نشده است.');
    }

    const guard = guardAdminChanges(req, res, { id, changes: values });
    if (guard) return guard;

    applyPublishRules(req.entityKey, values, existing);

    const columns = Object.keys(values);
    try {
      db.prepare(
        `UPDATE ${entity.table} SET ${columns.map((column) => `${column} = ?`).join(', ')},
         updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?`
      ).run(...columns.map((column) => values[column]), id);
    } catch (error) {
      if (String(error.code || '').startsWith('SQLITE_CONSTRAINT')) {
        return sendError(res, 409, 'این مقدار با رکورد دیگری تکراری است.');
      }
      throw error;
    }

    if (req.entityKey === 'admins' && (values.password_hash || values.is_active === 0)) {
      // با تغییر رمز یا غیرفعال شدن حساب، نشست‌های قبلی باطل می‌شوند.
      const { destroySessionsFor } = require('../middleware/session');
      destroySessionsFor('admin', id);
    }

    const row = db.prepare(`SELECT * FROM ${entity.table} WHERE id = ?`).get(id);
    audit.log({
      actorType: 'admin',
      actorId: req.auth.admin.id,
      actorLabel: req.auth.admin.username,
      action: `${req.entityKey}.update`,
      entity: entity.table,
      entityId: String(id),
      details: Object.keys(values).filter((key) => key !== 'password_hash'),
      ip: clientIp(req),
    });
    return sendOk(res, { item: serializeRow(entity, row), message: 'تغییرات ذخیره شد.' });
  });
}

router.put('/:entity/:id', requireEntityAccess, updateHandler(false));
router.patch('/:entity/:id', requireEntityAccess, updateHandler(true));

router.delete(
  '/:entity/:id',
  requireEntityAccess,
  asyncHandler(async (req, res) => {
    const db = getDb();
    const entity = req.entity;
    const id = Number.parseInt(req.params.id, 10);
    if (!Number.isFinite(id)) return sendError(res, 400, 'شناسه نامعتبر است.');

    const existing = db.prepare(`SELECT * FROM ${entity.table} WHERE id = ?`).get(id);
    if (!existing) return sendError(res, 404, 'این رکورد یافت نشد.');

    if (req.entityKey === 'admins') {
      if (existing.id === req.auth.id) {
        return sendError(res, 400, 'نمی‌توانید حساب خودتان را حذف کنید.');
      }
      if (existing.role === 'owner') {
        const owners = db
          .prepare("SELECT COUNT(*) AS total FROM admins WHERE role = 'owner' AND is_active = 1")
          .get().total;
        if (owners <= 1) return sendError(res, 400, 'حداقل یک مدیر ارشد فعال باید باقی بماند.');
      }
    }

    db.prepare(`DELETE FROM ${entity.table} WHERE id = ?`).run(id);
    audit.log({
      actorType: 'admin',
      actorId: req.auth.admin.id,
      actorLabel: req.auth.admin.username,
      action: `${req.entityKey}.delete`,
      entity: entity.table,
      entityId: String(id),
      ip: clientIp(req),
    });
    return sendOk(res, { message: 'رکورد حذف شد.' });
  })
);

module.exports = router;
