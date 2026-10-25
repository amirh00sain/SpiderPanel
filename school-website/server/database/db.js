'use strict';

/**
 * اتصال دیتابیس SQLite.
 * - مسیر فایل دیتابیس از config خوانده می‌شود و بیرون از پوشه عمومی است.
 * - ساختار جداول به صورت خودکار با migration‌های server/database/migrations ساخته می‌شود.
 * - همه کوئری‌ها در لایه‌های بالاتر با prepared statement و پارامتر اجرا می‌شوند.
 */

const fs = require('fs');
const path = require('path');

const Database = require('better-sqlite3');

const config = require('../config');
const logger = require('../utils/logger');

let db = null;

function ensureDirectories() {
  for (const dir of [
    config.storageDir,
    path.dirname(config.dbPath),
    config.backupDir,
    config.uploadDir,
  ]) {
    fs.mkdirSync(dir, { recursive: true });
  }
}

function migrationsDir() {
  return path.join(__dirname, 'migrations');
}

function runMigrations(database) {
  database.exec(
    "CREATE TABLE IF NOT EXISTS migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))"
  );
  const applied = new Set(database.prepare('SELECT id FROM migrations').all().map((row) => row.id));
  const files = fs
    .readdirSync(migrationsDir())
    .filter((file) => file.endsWith('.sql'))
    .sort();

  for (const file of files) {
    if (applied.has(file)) continue;
    const sql = fs.readFileSync(path.join(migrationsDir(), file), 'utf8');
    const apply = database.transaction(() => {
      database.exec(sql);
      database.prepare('INSERT INTO migrations (id) VALUES (?)').run(file);
    });
    apply();
    logger.info(`migration اعمال شد: ${file}`);
  }
}

function initDb() {
  if (db) return db;
  ensureDirectories();
  db = new Database(config.dbPath);
  db.pragma('journal_mode = WAL');
  db.pragma('foreign_keys = ON');
  db.pragma('busy_timeout = 5000');
  db.pragma('synchronous = NORMAL');
  runMigrations(db);
  return db;
}

function getDb() {
  return db || initDb();
}

function closeDb() {
  if (db) {
    db.close();
    db = null;
  }
}

/** اجرای یک بلوک در یک تراکنش اتمیک */
function transaction(fn) {
  return getDb().transaction(fn);
}

module.exports = { getDb, initDb, runMigrations, closeDb, transaction };
