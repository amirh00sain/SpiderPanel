'use strict';

/**
 * محافظت در برابر CSRF با الگوی «توکن دوگانه»:
 * توکن در کوکی HttpOnly و هم در هدر X-CSRF-Token ارسال می‌شود؛ برای نشست‌های
 * وارد‌شده باید با توکن ذخیره‌شده در ردیف نشست هم برابر باشد.
 */

const crypto = require('crypto');

const config = require('../config');
const { sendError } = require('../utils/http');

const SAFE_METHODS = ['GET', 'HEAD', 'OPTIONS'];

function csrfCookieOptions() {
  return {
    httpOnly: true,
    secure: config.cookie.secure,
    sameSite: config.cookie.sameSite,
    path: '/',
  };
}

/** اطمینان از وجود توکن CSRF برای همه بازدیدکنندگان (حتی مهمان) */
function ensureCsrfToken(req, res, next) {
  if (!req.cookies) req.cookies = {};
  let token = req.cookies[config.cookie.csrfName];
  if (!token || typeof token !== 'string' || token.length < 16) {
    token = crypto.randomBytes(24).toString('base64url');
    res.cookie(config.cookie.csrfName, token, csrfCookieOptions());
    req.cookies[config.cookie.csrfName] = token;
  }
  // برای نشست‌های وارد‌شده، توکن معتبر همان توکن ذخیره‌شده در نشست است.
  req.csrfToken = req.auth ? req.auth.csrfToken : token;
  if (req.auth && req.auth.csrfToken !== token) {
    res.cookie(config.cookie.csrfName, req.auth.csrfToken, csrfCookieOptions());
  }
  return next();
}

function verifyCsrfToken(req, res, next) {
  if (SAFE_METHODS.includes(req.method)) return next();

  const cookieToken = (req.cookies && req.cookies[config.cookie.csrfName]) || '';
  const headerToken = String(req.get('x-csrf-token') || (req.body && req.body._csrf) || '');

  if (!cookieToken || !headerToken || cookieToken !== headerToken) {
    return sendError(res, 403, 'درخواست نامعتبر است. صفحه را تازه کنید و دوباره تلاش کنید.');
  }
  if (req.auth && req.auth.csrfToken !== headerToken) {
    return sendError(res, 403, 'نشست شما منقضی شده است. دوباره وارد شوید.');
  }
  return next();
}

module.exports = { ensureCsrfToken, verifyCsrfToken };
