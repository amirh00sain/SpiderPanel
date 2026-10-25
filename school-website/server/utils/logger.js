'use strict';

/**
 * لاگر ساده سمت سرور. هیچ‌گاه رمز عبور، کد یک‌بارمصرف یا کلید API ثبت نمی‌شود.
 */

const LEVELS = { error: 0, warn: 1, info: 2, debug: 3 };
const currentLevel = LEVELS[String(process.env.LOG_LEVEL || 'info').toLowerCase()] ?? LEVELS.info;

function stamp() {
  return new Date().toISOString();
}

function write(level, message, meta) {
  if (LEVELS[level] > currentLevel) return;
  const line = `${stamp()} [${level.toUpperCase()}] ${message}`;
  const args = meta === undefined ? [line] : [line, meta];
  if (level === 'error') console.error(...args);
  else if (level === 'warn') console.warn(...args);
  else console.log(...args);
}

module.exports = {
  error: (message, meta) => write('error', message, meta),
  warn: (message, meta) => write('warn', message, meta),
  info: (message, meta) => write('info', message, meta),
  debug: (message, meta) => write('debug', message, meta),
};
