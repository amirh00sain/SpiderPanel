'use strict';

/**
 * ابزارهای مشترک درخواست/پاسخ و آی‌پی.
 */

const crypto = require('crypto');

const config = require('../config');

class HttpError extends Error {
  constructor(status, message, details) {
    super(message);
    this.status = status;
    this.details = details;
  }
}

function asyncHandler(handler) {
  return function wrapped(req, res, next) {
    Promise.resolve(handler(req, res, next)).catch(next);
  };
}

function clientIp(req) {
  const forwarded = req.headers['x-forwarded-for'];
  if (typeof forwarded === 'string' && forwarded.length > 0) {
    return forwarded.split(',')[0].trim();
  }
  return (req.ip || req.socket?.remoteAddress || '').replace(/^::ffff:/, '');
}

/** برای گزارش‌ها فقط هش آی‌پی نگهداری می‌شود تا داده شخصی ذخیره نشود. */
function hashIp(ip) {
  if (!ip) return '';
  return crypto.createHmac('sha256', config.sessionSecret).update(String(ip)).digest('hex').slice(0, 32);
}

function sendError(res, status, message, details) {
  const payload = { ok: false, error: message };
  if (details) payload.details = details;
  return res.status(status).json(payload);
}

function sendOk(res, data, status = 200) {
  return res.status(status).json({ ok: true, ...(data || {}) });
}

module.exports = { HttpError, asyncHandler, clientIp, hashIp, sendError, sendOk };
