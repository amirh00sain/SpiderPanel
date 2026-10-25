#!/usr/bin/env node
'use strict';

/**
 * پشتیبان‌گیری سازگار از دیتابیس SQLite.
 * نمونه: node scripts/backup.js
 * فایل پشتیبان در پوشه storage/backups ساخته می‌شود.
 */

const fs = require('fs');
const path = require('path');

const Database = require('better-sqlite3');

const config = require('../server/config');

async function main() {
  if (!fs.existsSync(config.dbPath)) {
    console.error(`فایل دیتابیس یافت نشد: ${config.dbPath}`);
    process.exit(1);
  }
  fs.mkdirSync(config.backupDir, { recursive: true });

  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const target = path.join(config.backupDir, `school-${stamp}.db`);

  const db = new Database(config.dbPath, { readonly: true });
  await db.backup(target);
  db.close();

  const size = fs.statSync(target).size;
  console.log(`پشتیبان ساخته شد: ${target} (${(size / 1024).toFixed(1)} کیلوبایت)`);
  console.log('برای بازیابی: node scripts/restore.js --file <مسیر فایل پشتیبان>');
}

main().catch((error) => {
  console.error(`پشتیبان‌گیری ناموفق بود: ${error.message}`);
  process.exit(1);
});
