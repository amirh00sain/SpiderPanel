'use strict';

const multer = require('multer');

const config = require('../config');
const logger = require('../utils/logger');
const { sendError } = require('../utils/http');

function notFoundHandler(req, res) {
  if (req.path.startsWith('/api/')) {
    return sendError(res, 404, 'این نشانی در سرور وجود ندارد.');
  }
  return res.status(404).sendFile('404.html', { root: config.publicDir }, (error) => {
    if (error) res.status(404).type('text/plain; charset=utf-8').send('صفحه یافت نشد.');
  });
}

// eslint-disable-next-line no-unused-vars
function errorHandler(error, req, res, next) {
  if (error instanceof multer.MulterError) {
    const message =
      error.code === 'LIMIT_FILE_SIZE' ? 'حجم فایل بیش از حد مجاز است.' : 'فایل ارسالی قابل پذیرش نیست.';
    return sendError(res, 400, message);
  }

  const status = Number(error.status) || 500;
  if (status >= 500) {
    logger.error(`خطای سرور در ${req.method} ${req.path}: ${error.message}`, {
      stack: config.isProduction ? undefined : error.stack,
    });
  }

  const message =
    status >= 500
      ? 'خطای غیرمنتظره در سرور رخ داد. لطفاً دوباره تلاش کنید.'
      : error.message || 'درخواست نامعتبر است.';

  if (req.path.startsWith('/api/')) {
    return sendError(res, status, message, error.details);
  }
  return res.status(status).type('text/plain; charset=utf-8').send(message);
}

module.exports = { notFoundHandler, errorHandler };
